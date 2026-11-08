"""Shared Home Assistant control helpers."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness
from .platform_scope import DiscoveryLifecycle
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
    entry: DiscoveryLifecycle,
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
        # Started with the stored inventory (P47): the control exists at once
        # and stays unavailable until a fresh reading confirms it.
        for path, resource in coordinator.stored_resources().items():
            resources.setdefault(path, resource)
        entities = []
        for description in build(resources):
            if description.key not in seen:
                seen.add(description.key)
                entities.append(create(coordinator, description))
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(coordinator.async_add_listener(discover))
    discover()


def track_entity_once(
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
    ready: Callable[[], bool],
    create: Callable[[], Entity],
) -> None:
    """Add one entity as soon as its resources exist, also when found later."""
    added = False

    @callback
    def discover() -> None:
        nonlocal added
        if added or not ready():
            return
        added = True
        async_add_entities([create()])

    discover()
    if not added:
        entry.async_on_unload(coordinator.async_add_listener(discover))


async def async_set_control(
    coordinator: BoschBuderusDataUpdateCoordinator,
    path: str,
    value: str | float,
    policy: EnumWritePolicy | NumberWritePolicy,
    *,
    expected: tuple[Resource, ...] = (),
) -> None:
    """Write a control and expose only translated, value-free HA errors."""
    try:
        if expected:
            await coordinator.async_write_control(
                path, value, policy, expected=expected
            )
        else:
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
