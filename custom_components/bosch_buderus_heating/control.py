"""Shared Home Assistant control helpers."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness
from .pointt import (
    AuthenticationError,
    PointTError,
    RateLimited,
    Resource,
    WriteNotConfirmed,
    WriteValidationError,
)
from .writes import EnumWritePolicy, NumberWritePolicy


def track_control_entities[DescriptionT: EntityDescription](
    entry: ConfigEntry,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
    build: Callable[[Mapping[str, Resource]], tuple[DescriptionT, ...]],
    create: Callable[[BoschBuderusDataUpdateCoordinator, DescriptionT], Entity],
) -> None:
    """Add newly eligible resources once and retain registry/user decisions."""
    seen: set[str] = set()

    @callback
    def discover() -> None:
        resources = {
            path: item.resource
            for path, item in (coordinator.data or {}).items()
            if item.available and item.freshness is Freshness.FRESH
        }
        entities = []
        for description in build(resources):
            if description.key not in seen:
                seen.add(description.key)
                entities.append(create(coordinator, description))
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(coordinator.async_add_listener(discover))
    discover()


async def async_set_control(
    coordinator: BoschBuderusDataUpdateCoordinator,
    path: str,
    value: str | float,
    policy: EnumWritePolicy | NumberWritePolicy,
) -> None:
    """Write a control and expose only translated, value-free HA errors."""
    try:
        await coordinator.async_write_control(path, value, policy)
    except WriteValidationError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="write_validation_failed"
        ) from err
    except WriteNotConfirmed as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="write_not_confirmed"
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
