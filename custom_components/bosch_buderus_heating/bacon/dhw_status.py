"""Read-only status indicators offered by Bacon water heaters."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from ..platform_scope import DiscoveryLifecycle
from . import dhw
from .access import BaconAccess
from .entity import BaconEntity


def track_status(
    entry: DiscoveryLifecycle,
    account: BaconAccess,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    seen: set[tuple[str, str]] = set()

    @callback
    def discover() -> None:
        additions = []
        for serial, device in account.devices.items():
            if dhw.family(device) is None:
                continue
            for key, field in dhw.STATUS_FIELDS.items():
                identity = (serial, key)
                if (
                    identity not in seen
                    and dhw.metadata(device, "sensor", field) is not None
                ):
                    seen.add(identity)
                    additions.append(BaconDhwStatus(account, serial, key))
        if additions:
            async_add_entities(additions)

    entry.async_on_unload(account.add_listener(discover))
    discover()


class BaconDhwStatus(BaconEntity, BinarySensorEntity):
    def __init__(self, account: BaconAccess, serial: str, key: str) -> None:
        super().__init__(account, serial, "dhw_" + key)
        self.key = key
        self._attr_translation_key = "bacon_dhw_" + key

    @property
    def available(self) -> bool:
        return (
            dhw.family(self.device) is not None
            and self.sensor_current
            and self.metadata_current
            and dhw.status(self.device, self.key) is not None
        )

    @property
    def is_on(self) -> bool | None:
        return dhw.status(self.device, self.key) if self.available else None
