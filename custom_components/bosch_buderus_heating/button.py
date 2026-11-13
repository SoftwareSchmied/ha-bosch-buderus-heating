"""Explicit one-shot commands using the shared validated write transaction."""

from collections.abc import Mapping
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import BoschBuderusConfigEntry, rrc_contracts, rrc_platforms
from .control import async_set_control, track_control_entities
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness
from .device import device_info_for_resource, grouped_entity_name
from .heating_climate import track_temporary_clear_buttons
from .i18n import localize
from .platform_scope import PlatformScope, setup_platform
from .pointt import Resource
from .writes import (
    SOFTENER_REGENERATION_POLICY,
    ControlContext,
    EnumWritePolicy,
    assess_control,
    context_of,
)


@dataclass(frozen=True, kw_only=True)
class BoschBuderusButtonEntityDescription(ButtonEntityDescription):
    """One command; the policy of a contract command (S11) or the
    regeneration start."""

    write_policy: EnumWritePolicy = SOFTENER_REGENERATION_POLICY


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
    """Expose only the reviewed regeneration start command, initially
    disabled, and the clear button of a heating circuit's temporary
    setpoint."""
    for coordinator in entry.coordinators:
        track_control_entities(
            entry,
            coordinator,
            async_add_entities,
            build_button_descriptions,
            BoschBuderusRegenerationButton,
        )
        track_temporary_clear_buttons(entry, coordinator, async_add_entities)
        rrc_platforms.track(Platform.BUTTON, entry, coordinator, async_add_entities)


def build_button_descriptions(
    resources: Mapping[str, Resource],
    context: ControlContext | None = None,
) -> tuple[ButtonEntityDescription, ...]:
    """The regeneration start and, on an RRC controller, the action
    commands of its contracts."""
    descriptions: list[ButtonEntityDescription] = []
    for resource in resources.values():
        assessment = assess_control(resource, context)
        policy = assessment.policy
        if not assessment.eligible or not isinstance(policy, EnumWritePolicy):
            continue
        if policy is SOFTENER_REGENERATION_POLICY:
            descriptions.append(
                ButtonEntityDescription(
                    key=resource.path,
                    translation_key="softener_start_regeneration",
                    icon="mdi:water-sync",
                    entity_registry_enabled_default=False,
                )
            )
        elif policy.action and rrc_contracts.owns(policy):
            descriptions.append(
                BoschBuderusButtonEntityDescription(
                    key=resource.path,
                    name=rrc_contracts.name(resource.path, "de"),
                    translation_key=rrc_contracts.entity_key(
                        resource.path, Platform.BUTTON.value
                    ),
                    icon=rrc_contracts.icon(resource.path),
                    entity_registry_enabled_default=policy.enabled_by_default,
                    write_policy=policy,
                )
            )
    return tuple(descriptions)


def _write_policy(description: ButtonEntityDescription) -> EnumWritePolicy:
    if isinstance(description, BoschBuderusButtonEntityDescription):
        return description.write_policy
    return SOFTENER_REGENERATION_POLICY


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
        policy = _write_policy(description)
        self._attr_name = grouped_entity_name(
            coordinator,
            description.key,
            (
                rrc_contracts.owns(policy)
                and rrc_contracts.name(
                    description.key, coordinator.hass.config.language
                )
            )
            or localize(
                coordinator.hass.config.language,
                "Regeneration starten",
                "Start regeneration",
            ),
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
            and (
                assessment := assess_control(
                    snapshot.resource, context_of(self.coordinator)
                )
            ).eligible
            and assessment.policy is _write_policy(self.entity_description)
            and snapshot.resource.value in ("on", "off")
        )

    async def async_press(self) -> None:
        await async_set_control(
            self.coordinator,
            self.entity_description.key,
            "on",
            _write_policy(self.entity_description),
        )

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, self.entity_description.key)
