"""Shared parts of the air-conditioner entities (PointT ``airConditioning``)."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .aircon import AirconCoordinator
from .const import DEFAULT_MANUFACTURER, DOMAIN
from .control import cloud_down_error
from .device import registered_device
from .pointt import (
    AuthenticationError,
    PointTError,
    RateLimited,
    WriteNotConfirmed,
    WriteValidationError,
)
from .writes import (
    ContractRejected,
    ControlUnused,
    EnumWritePolicy,
    NumberWritePolicy,
    WriteBusy,
    assess_control,
)

if TYPE_CHECKING:
    from .platform_scope import PlatformScope


def option_key(token: str) -> str:
    """The Home Assistant option of a wire token (``ultraWide`` ->
    ``ultra_wide``, ``mid`` -> ``medium``)."""
    if token == "mid":
        return "medium"
    return re.sub(r"(?<!^)([A-Z])", r"_\1", token).lower()


def options(
    coordinator: AirconCoordinator, path: str, order: Iterable[str] | None = None
) -> dict[str, str]:
    """Offered options of a current, writable control: HA option -> wire."""
    resource = coordinator.resource(path)
    if resource is None or not coordinator.referenced(path):
        return {}
    assessment = assess_control(resource)
    if not assessment.eligible:
        return {}
    offered = list(assessment.options)
    if order is not None:
        ranking = {token: index for index, token in enumerate(order)}
        offered.sort(key=lambda token: (ranking.get(token, len(ranking)), token))
    return {option_key(token): token for token in offered}


class AirconEntity(CoordinatorEntity[AirconCoordinator]):
    """One entity of the air conditioner's device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: AirconCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:{key}"

    @property
    def device_info(self) -> DeviceInfo:
        gateway = self.coordinator.gateway
        info = DeviceInfo(
            identifiers={(DOMAIN, gateway.gateway_id)},
            manufacturer=DEFAULT_MANUFACTURER,
            model=gateway.model,
            sw_version=gateway.firmware_version,
        )
        if not registered_device(self.coordinator.hass, gateway.gateway_id):
            # Named without a brand; the user can rename it.
            info["name"] = "Air conditioner"
        return info

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.current()

    async def _write(
        self,
        path: str,
        value: str | float,
        policy: EnumWritePolicy | NumberWritePolicy,
    ) -> None:
        await async_write(self.coordinator, path, value, policy)


async def async_write(
    coordinator: AirconCoordinator,
    path: str,
    value: str | float,
    policy: EnumWritePolicy | NumberWritePolicy,
) -> None:
    """Write one control and show only translated, value-free errors."""
    if (down := cloud_down_error(coordinator)) is not None:
        raise down
    try:
        await coordinator.async_write(path, value, policy)
    except ControlUnused as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="control_unused"
        ) from err
    except ContractRejected as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key=err.translation_key
        ) from err
    except WriteValidationError as err:
        if (down := cloud_down_error(coordinator)) is not None:
            raise down from err
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="write_validation_failed"
        ) from err
    except WriteNotConfirmed as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="aircon_write_not_confirmed"
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


def invalid_command() -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key="write_validation_failed"
    )


type Builder = Callable[[AirconCoordinator], Iterable[tuple[str, Callable[[], Entity]]]]


def track_aircon_entities(
    scope: PlatformScope,
    async_add_entities: AddConfigEntryEntitiesCallback,
    build: Builder,
) -> None:
    """Add each entity once, as soon as its control is offered.

    ``build`` yields (key, factory) pairs for the controls offered now; an
    entity stays when its control goes away for a while (unavailable).
    """
    for coordinator in scope.aircons:
        seen: set[str] = set()

        @callback
        def discover(
            coordinator: AirconCoordinator = coordinator, seen: set[str] = seen
        ) -> None:
            if not scope.current() or not coordinator.current():
                return
            additions = []
            for key, factory in build(coordinator):
                if key not in seen:
                    seen.add(key)
                    additions.append(factory())
            if additions:
                async_add_entities(additions)

        scope.async_on_unload(coordinator.async_add_listener(discover))
        discover()
