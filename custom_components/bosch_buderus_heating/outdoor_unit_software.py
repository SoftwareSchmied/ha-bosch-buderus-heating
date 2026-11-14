"""The software version of the outdoor unit, as a diagnostic sensor (0.9.2).

The module lists the integration already reads (the cloud's
``/system/info``, the local ``/system/basicInfo``) give a version for every
module. A module is the outdoor unit only when the heat generator model
table says so (``role: outdoor_unit``, see ``heat_generator``); nothing is
guessed from a name. Of such a module only the product name, the hardware
type and the version are read; serial numbers, tokens and type numbers
never are, and a version equal to one of the module's identifiers is never
shown.

The sensor never sends a request. It keeps its last version over a restart
and while no answer is current; without any outdoor unit no entity is
created. A room controller never gets it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Final, Self

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import heat_generator
from .const import DOMAIN
from .control import track_entity_once
from .device import device_info_for_resource, is_rrc_plant
from .heat_generator import (
    MAX_HARDWARE_LENGTH,
    MAX_ROWS,
    ModuleRow,
    clean_text,
    interpret_row,
)
from .local_api.models import SystemInfo

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import BoschBuderusDataUpdateCoordinator
    from .platform_scope import DiscoveryLifecycle

CLOUD_PATH: Final = "/system/info"
LOCAL_PATH: Final = "/system/basicInfo"
OUTDOOR_UNIT: Final = "outdoor_unit"
MAX_OUTDOOR_UNITS: Final = 8
_MAX_VERSION_LENGTH: Final = 64
# A version: letters, digits and the usual separators, with a digit.
_VERSION: Final = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+\-]{0,63}")
_DIGIT: Final = re.compile(r"[0-9]")
# A text made only of digits from this length on is a number, not a name.
_NUMBER_ONLY: Final = re.compile(r"[0-9]{8,}")
# The fields of a cloud row that hold identifiers.
_CLOUD_IDENTIFIERS: Final = ("ModuleSerialNumber", "ProductSerialNumber", "No")
_STORED_KEY: Final = "outdoor_units"


@dataclass(frozen=True, slots=True, order=True)
class OutdoorUnit:
    """One outdoor unit module: the three fields that are read."""

    product_name: str
    hardware_type: str
    version: str

    def attribute(self) -> dict[str, str]:
        return {
            "product_name": self.product_name,
            "hardware_type": self.hardware_type,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class OutdoorUnits:
    """The outdoor units of one answer, in a stable order."""

    units: tuple[OutdoorUnit, ...]
    source: str
    observed_at: datetime | None = None

    @property
    def state(self) -> str:
        return self.units[0].version

    def attributes(self) -> dict[str, object]:
        first = self.units[0]
        attributes: dict[str, object] = {
            "product_name": first.product_name,
            "hardware_type": first.hardware_type,
            "source": self.source,
        }
        if len(self.units) > 1:
            attributes["outdoor_units"] = [unit.attribute() for unit in self.units]
        return attributes

    def to_data(self) -> dict[str, object]:
        return {
            "units": [
                [unit.product_name, unit.hardware_type, unit.version]
                for unit in self.units
            ],
            "source": self.source,
        }


def _identifiers(values: Iterable[object]) -> frozenset[str]:
    found: set[str] = set()
    for value in values:
        if isinstance(value, str | int) and not isinstance(value, bool):
            text = " ".join(str(value).split()).casefold()
            if text:
                found.add(text)
    return frozenset(found)


def _text(value: object, limit: int, identifiers: frozenset[str]) -> str | None:
    text = clean_text(value, limit)
    if text is None or _NUMBER_ONLY.fullmatch(text) is not None:
        return None
    return None if text.casefold() in identifiers else text


def clean_version(
    value: object, identifiers: frozenset[str] = frozenset()
) -> str | None:
    """A shown version, or None for anything else (an identifier included)."""
    text = clean_text(value, _MAX_VERSION_LENGTH)
    if (
        text is None
        or _VERSION.fullmatch(text) is None
        or _DIGIT.search(text) is None
        or text.casefold() in identifiers
    ):
        return None
    return text


def outdoor_unit(
    product_name: object,
    hardware_type: object,
    version: object,
    identifiers: frozenset[str],
) -> OutdoorUnit | None:
    """The module as an outdoor unit, if the model table makes it one."""
    name = _text(product_name, heat_generator.MAX_NAME_LENGTH, identifiers)
    hardware = _text(hardware_type, MAX_HARDWARE_LENGTH, identifiers)
    if hardware is None:
        return None  # the table matches outdoor units by their hardware type
    if interpret_row(ModuleRow(name, hardware)).role != OUTDOOR_UNIT:
        return None
    shown = clean_version(version, identifiers)
    if shown is None:
        return None
    return OutdoorUnit(name or hardware, hardware, shown)


def _collect(
    units: Iterable[OutdoorUnit | None], source: str, observed_at: datetime | None
) -> OutdoorUnits | None:
    found = sorted({unit for unit in units if unit is not None})
    if not found:
        return None
    return OutdoorUnits(tuple(found[:MAX_OUTDOOR_UNITS]), source, observed_at)


def from_cloud(
    values: Iterable[object], observed_at: datetime | None = None
) -> OutdoorUnits | None:
    """The outdoor units of a cloud ``/system/info`` answer."""
    units: list[OutdoorUnit | None] = []
    for number, item in enumerate(values):
        if number >= MAX_ROWS:
            break
        if not isinstance(item, Mapping):
            continue
        identifiers = _identifiers(item.get(key) for key in _CLOUD_IDENTIFIERS)
        units.append(
            outdoor_unit(
                item.get("ProductName"),
                item.get("ModuleHwIdentStr"),
                item.get("Ver"),
                identifiers,
            )
        )
    return _collect(units, "cloud", observed_at)


def from_local(
    modules: Iterable[object], observed_at: datetime | None = None
) -> OutdoorUnits | None:
    """The outdoor units of a decoded local ``/system/basicInfo`` answer."""
    units: list[OutdoorUnit | None] = []
    for number, module in enumerate(modules):
        if number >= MAX_ROWS:
            break
        identifiers = _identifiers(
            (
                getattr(module, "serial_number", None),
                getattr(module, "product_serial", None),
                getattr(module, "token", None),
            )
        )
        units.append(
            outdoor_unit(
                getattr(module, "product_name", None),
                getattr(module, "hardware", None),
                getattr(module, "version", None),
                identifiers,
            )
        )
    return _collect(units, "local", observed_at)


def from_data(data: object) -> OutdoorUnits | None:
    """The kept outdoor units of a restart; None for anything invalid."""
    if not isinstance(data, Mapping) or data.get("source") not in ("cloud", "local"):
        return None
    raw = data.get("units")
    if not isinstance(raw, list):
        return None
    units: list[OutdoorUnit | None] = []
    for item in raw[:MAX_OUTDOOR_UNITS]:
        if not isinstance(item, list) or len(item) != 3:
            return None
        name = _text(item[0], heat_generator.MAX_NAME_LENGTH, frozenset())
        hardware = _text(item[1], MAX_HARDWARE_LENGTH, frozenset())
        version = clean_version(item[2])
        if name is None or hardware is None or version is None:
            return None
        units.append(OutdoorUnit(name, hardware, version))
    return _collect(units, str(data["source"]), None)


def choose(
    cloud: OutdoorUnits | None, local: OutdoorUnits | None
) -> OutdoorUnits | None:
    """The newer of two answers; one alone as it is."""
    if cloud is None or local is None:
        return cloud or local
    if local.observed_at is not None and (
        cloud.observed_at is None or local.observed_at >= cloud.observed_at
    ):
        return local
    return cloud


class OutdoorUnitInputs:
    """The current answer of each source."""

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        self._coordinator = coordinator

    def cloud(self) -> OutdoorUnits | None:
        snapshot = (self._coordinator.data or {}).get(CLOUD_PATH)
        if snapshot is None or not snapshot.available:
            return None
        return from_cloud(snapshot.resource.values, snapshot.last_success)

    def local(self) -> OutdoorUnits | None:
        selection = getattr(self._coordinator, "local_selection", None)
        if selection is None:
            return None
        newest: Any = None
        for state in selection.local_states():
            for observation in getattr(state, "current_observations", ()) or ():
                if (
                    observation.request.path == LOCAL_PATH
                    and isinstance(observation.data, SystemInfo)
                    and observation.data.status == "completed"
                    and (newest is None or observation.observed_at > newest.observed_at)
                ):
                    newest = observation
        if newest is None:
            return None
        return from_local(newest.data.modules, newest.observed_at)

    def current(self) -> OutdoorUnits | None:
        return choose(self.cloud(), self.local())


def outdoor_unit_software_unique_id(gateway_id: str) -> str:
    return f"{gateway_id}:outdoor_unit_software_version"


@dataclass(frozen=True, slots=True)
class _Stored(ExtraStoredData):
    data: dict[str, object] | None

    def as_dict(self) -> dict[str, Any]:
        return {_STORED_KEY: self.data}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        data = value.get(_STORED_KEY)
        return cls(data if isinstance(data, dict) else None)


class BoschBuderusOutdoorUnitSoftwareSensor(
    CoordinatorEntity["BoschBuderusDataUpdateCoordinator"],
    RestoreEntity,
    SensorEntity,
):
    """The software version the outdoor unit reports."""

    _attr_has_entity_name = True
    _attr_translation_key = "outdoor_unit_software_version"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = outdoor_unit_software_unique_id(
            coordinator.gateway.gateway_id
        )
        self._inputs = OutdoorUnitInputs(coordinator)
        self._shown: OutdoorUnits | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, "/system")

    @property
    def available(self) -> bool:
        # The last known version stays shown without a current answer.
        return True

    @property
    def native_value(self) -> str | None:
        return self._shown.state if self._shown is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        return self._shown.attributes() if self._shown is not None else None

    @property
    def extra_restore_state_data(self) -> _Stored:
        return _Stored(self._shown.to_data() if self._shown is not None else None)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (stored := await self.async_get_last_extra_data()) is not None:
            self._shown = from_data(_Stored.from_dict(stored.as_dict()).data)
        self._refresh()

    def _refresh(self) -> bool:
        """Take the current answer; True when what is shown changed."""
        current = self._inputs.current()
        if current is None or (
            self._shown is not None
            and current.units == self._shown.units
            and current.source == self._shown.source
        ):
            return False
        self._shown = current
        return True

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._refresh():
            self.async_write_ha_state()

    async def async_update(self) -> None:
        """The module lists arrive with the regular reads; never a request."""


def track_outdoor_unit_software_sensor(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the entity once an outdoor unit with a version is reported, or at
    once when it is registered already. Without a valid model table there
    is no entity."""
    if not heat_generator.TABLE_AVAILABLE:
        return
    inputs = OutdoorUnitInputs(coordinator)
    unique_id = outdoor_unit_software_unique_id(coordinator.gateway.gateway_id)

    def ready() -> bool:
        if is_rrc_plant(coordinator):
            return False
        if inputs.current() is not None:
            return True
        return (
            er.async_get(coordinator.hass).async_get_entity_id(
                "sensor", DOMAIN, unique_id
            )
            is not None
        )

    track_entity_once(
        entry,
        coordinator,
        async_add_entities,
        ready,
        partial(BoschBuderusOutdoorUnitSoftwareSensor, coordinator),
    )
