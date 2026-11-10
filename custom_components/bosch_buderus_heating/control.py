"""Shared Home Assistant control helpers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
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
from .rrc_write import RrcWritePending
from .writes import (
    ContractRejected,
    ControlContext,
    ControlLocked,
    EnumWritePolicy,
    NumberWritePolicy,
    RrcWriteNotConfirmed,
    WriteBusy,
    context_of,
)


class HeldCapabilities(Entity):
    """O30: keep the last known capabilities while no current reading exists.

    Unavailable, before the first reading after a reload or during a source
    switch, an entity reports the capabilities (options, state class, limits,
    modes) it reported last; a new entity those of its registry entry. A
    reload or a source switch then no longer removes and restores them for a
    moment, and Home Assistant's registry is not updated twice each time.
    Place the class first among the bases.
    """

    _held_capabilities: Mapping[str, Any] | None = None
    _held_from_registry = False

    def _capabilities_current(self) -> bool:
        """Whether the capabilities come from a current reading."""
        return self.available

    def _live_capabilities(self) -> Mapping[str, Any] | None:
        return super().capability_attributes

    @property
    def capability_attributes(self) -> Any:  # the platform's own type
        live = self._live_capabilities()
        if self._capabilities_current():
            self._held_capabilities = live
            self._held_from_registry = True  # a current value replaces it
            return live
        if not self._held_from_registry:
            self._held_from_registry = True
            self._held_capabilities = self._registered_capabilities()
        held = self._held_capabilities
        return held if held is not None else live

    def _registered_capabilities(self) -> Mapping[str, Any] | None:
        """Capabilities the registry entry kept from the last state write."""
        entry = self.registry_entry
        if entry is None and self.hass is not None and self.unique_id is not None:
            platform = getattr(self, "platform", None)
            domain = getattr(platform, "domain", None)
            if domain is None:
                return None
            registry = er.async_get(self.hass)
            entity_id = registry.async_get_entity_id(domain, DOMAIN, self.unique_id)
            entry = registry.async_get(entity_id) if entity_id is not None else None
        return entry.capabilities if entry is not None else None


def track_control_entities[DescriptionT: EntityDescription](
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
    build: Callable[[Mapping[str, Resource], ControlContext], tuple[DescriptionT, ...]],
    create: Callable[[BoschBuderusDataUpdateCoordinator, DescriptionT], Entity],
) -> None:
    """Add newly eligible resources once and retain registry/user decisions.

    ``build`` gets the current resources and the coordinator's write
    context (S11).
    """
    seen: set[str] = set()
    checked: list[Mapping[str, Resource]] = []
    contexts: list[ControlContext] = []

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
        context = context_of(coordinator)
        if unchanged_input(checked, resources) and contexts == [context]:
            return
        contexts[:] = [context]
        entities = []
        for description in build(resources, context):
            if description.key not in seen:
                seen.add(description.key)
                entities.append(create(coordinator, description))
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(coordinator.async_add_listener(discover))
    discover()


def unchanged_input(
    checked: list[Mapping[str, Resource]], resources: Mapping[str, Resource]
) -> bool:
    """O1: whether a discovery callback saw exactly these resources last time.

    The descriptions depend on nothing but the resources, so equal input
    gives equal descriptions. Equal objects compare by identity first; only
    replaced resources compare their content. Otherwise ``checked`` keeps
    the new input for the next call.
    """
    if checked and checked[0] == resources:
        return True
    checked[:] = [resources]
    return False


def track_rrc_entities[DescriptionT: EntityDescription](
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
    build: Callable[[Mapping[str, Resource], ControlContext], tuple[DescriptionT, ...]],
    create: Callable[[BoschBuderusDataUpdateCoordinator, DescriptionT], Entity],
) -> None:
    """``track_control_entities`` for the RRC packages (W1 to W6).

    ``build`` runs only once the coordinator recognised an RRC controller
    (S11); each description key is added once. Entities stay when the
    evidence goes away for a moment.
    """

    def rrc_only(
        resources: Mapping[str, Resource], context: ControlContext
    ) -> tuple[DescriptionT, ...]:
        return build(resources, context) if context.rrc_plant else ()

    track_control_entities(entry, coordinator, async_add_entities, rrc_only, create)


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
    except ControlLocked as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="rrc_mode_locked"
        ) from err
    except (ContractRejected, RrcWritePending) as err:
        # A contract's own refusal; its token is the translation key.
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key=err.translation_key
        ) from err
    except WriteValidationError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="write_validation_failed"
        ) from err
    except RrcWriteNotConfirmed as err:
        # S7: sent to an RRC controller, which may still apply it.
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key=err.translation_key
        ) from err
    except WriteNotConfirmed as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="write_not_confirmed"
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
