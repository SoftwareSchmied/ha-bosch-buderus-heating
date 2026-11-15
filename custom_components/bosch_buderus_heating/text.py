"""TP-05: the names of the weekly programs A and B as text controls.

Only where the gateway reports the name writable with a known coding
(``valueConfig``), and never on an iCom gateway (its app does not write
names). Disabled by default: changing a name has not been confirmed on a
real installation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from homeassistant.components.text import TextEntity, TextEntityDescription, TextMode
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import BoschBuderusConfigEntry
from .const import DOMAIN
from .control import HeldCapabilities, cloud_down_error, track_control_entities
from .control_presentation import control_name
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness
from .device import device_info_for_resource
from .icom_profile import is_icom_gateway
from .platform_scope import PlatformScope, setup_platform
from .pointt import (
    AuthenticationError,
    PointTError,
    RateLimited,
    Resource,
    WriteNotConfirmed,
    WriteValidationError,
)
from .program_names import (
    config_issue,
    decoded_name,
    is_name_path,
    value_config,
)
from .resource_catalog import resource_name
from .write_policies import ContractRejected
from .writes import ControlContext, WriteBusy


@dataclass(frozen=True, kw_only=True)
class ProgramNameDescription(TextEntityDescription):
    resource_path: str


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BoschBuderusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    setup_platform(entry, Platform.TEXT, async_add_entities, _setup_entities)


def _setup_entities(
    entry: PlatformScope,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for coordinator in entry.coordinators:
        if is_icom_gateway(coordinator.gateway):
            # KC2: the app of an iCom gateway does not write names.
            continue
        track_control_entities(
            entry,
            coordinator,
            async_add_entities,
            descriptions,
            ProgramNameText,
        )


def descriptions(
    resources: Mapping[str, Resource], context: ControlContext | None = None
) -> tuple[ProgramNameDescription, ...]:
    """A name control per writable name with a known coding."""
    return tuple(
        ProgramNameDescription(
            key=path.strip("/").replace("/", ":"),
            resource_path=path,
            translation_key="switch_program_name",
            entity_category=EntityCategory.CONFIG,
            entity_registry_enabled_default=False,
            mode=TextMode.TEXT,
            native_min=0,
            native_max=max(1, config.max_length),
        )
        for path, resource in resources.items()
        if is_name_path(path)
        and resource.metadata.writable
        and config_issue(resource) is None
        and (config := value_config(resource)) is not None
    )


class ProgramNameText(
    HeldCapabilities,
    CoordinatorEntity[BoschBuderusDataUpdateCoordinator],
    TextEntity,
):
    """The name of one weekly program; one confirmed write per change."""

    _attr_has_entity_name = True
    entity_description: ProgramNameDescription

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        description: ProgramNameDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        path = description.resource_path
        self._attr_unique_id = (
            f"{coordinator.gateway.gateway_id}:{description.key}:control"
        )
        self._attr_name = control_name(
            coordinator,
            path,
            resource_name(path, language=coordinator.hass.config.language),
        )

    def _reading(self) -> Resource | None:
        snapshot = (self.coordinator.data or {}).get(
            self.entity_description.resource_path
        )
        if (
            snapshot is None
            or not snapshot.available
            or snapshot.freshness is not Freshness.FRESH
        ):
            return None
        return snapshot.resource

    @property
    def available(self) -> bool:
        resource = self._reading()
        return (
            super().available
            and resource is not None
            and resource.metadata.writable
            and config_issue(resource) is None
        )

    @property
    def native_value(self) -> str | None:
        return decoded_name(self._reading())

    @property
    def native_max(self) -> int:
        config = value_config(self._reading())
        return max(1, config.max_length) if config is not None else super().native_max

    async def async_set_value(self, value: str) -> None:
        resource = self._reading()
        if resource is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="program_name_unsupported"
            )
        if (down := cloud_down_error(self.coordinator)) is not None:
            raise down
        try:
            await self.coordinator.async_write_program_name(resource, value)
        except ContractRejected as err:
            config = value_config(resource)
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key=err.reason,
                translation_placeholders={
                    "max": str(config.max_length if config else 0),
                    "charset": config.charset if config else "",
                },
            ) from err
        except WriteValidationError as err:
            if (down := cloud_down_error(self.coordinator)) is not None:
                raise down from err
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="program_name_changed"
            ) from err
        except WriteNotConfirmed as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="program_name_not_confirmed"
            ) from err
        except WriteBusy as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="write_busy"
            ) from err
        except AuthenticationError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="write_authentication_failed"
            ) from err
        except RateLimited as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="write_rate_limited"
            ) from err
        except PointTError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="write_failed"
            ) from err

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(
            self.coordinator, self.entity_description.resource_path
        )
