"""Explicit one-shot commands using the shared validated write transaction."""

from collections.abc import Mapping

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import BoschBuderusConfigEntry
from .control import async_set_control, track_control_entities
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness
from .device import device_info_for_resource, grouped_entity_name
from .platform_scope import PlatformScope, setup_platform
from .pointt import Resource
from .writes import SOFTENER_REGENERATION_POLICY, assess_control


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BoschBuderusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    setup_platform(entry, Platform.BUTTON, async_add_entities, _setup_entities)


def _setup_entities(
    entry: PlatformScope,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Expose only the reviewed regeneration start command, initially disabled."""
    for coordinator in entry.coordinators:
        track_control_entities(
            entry,
            coordinator,
            async_add_entities,
            build_button_descriptions,
            BoschBuderusRegenerationButton,
        )


def build_button_descriptions(
    resources: Mapping[str, Resource],
) -> tuple[ButtonEntityDescription, ...]:
    return tuple(
        ButtonEntityDescription(
            key=resource.path,
            translation_key="softener_start_regeneration",
            icon="mdi:water-sync",
            entity_registry_enabled_default=False,
        )
        for resource in resources.values()
        if (assessment := assess_control(resource)).eligible
        and assessment.policy is SOFTENER_REGENERATION_POLICY
    )


class BoschBuderusRegenerationButton(
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator], ButtonEntity
):
    """Start once; a lost response is resolved by reading without another PUT."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        description: ButtonEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        semantic = description.key.strip("/").replace("/", ":")
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:{semantic}:start"
        self._attr_name = grouped_entity_name(
            coordinator,
            description.key,
            "Regeneration starten"
            if coordinator.hass.config.language.startswith("de")
            else "Start regeneration",
        )

    @property
    def resource_path(self) -> str:
        """The command path the button reads and writes (O51)."""
        return self.entity_description.key

    @property
    def available(self) -> bool:
        snapshot = (self.coordinator.data or {}).get(self.entity_description.key)
        return (
            super().available
            and snapshot is not None
            and snapshot.available
            and snapshot.freshness is Freshness.FRESH
            and assess_control(snapshot.resource).eligible
            and snapshot.resource.value in ("on", "off")
        )

    async def async_press(self) -> None:
        await async_set_control(
            self.coordinator,
            self.entity_description.key,
            "on",
            SOFTENER_REGENERATION_POLICY,
        )

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, self.entity_description.key)
