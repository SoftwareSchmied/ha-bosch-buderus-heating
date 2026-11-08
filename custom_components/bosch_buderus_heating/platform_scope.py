"""Explicit sources and listener ownership for native entity discovery."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .bacon.access import BaconAccess
from .coordinator import BoschBuderusDataUpdateCoordinator
from .pointt.metrics import RequestMetrics
from .wallbox import WallboxCoordinator

type Listener = Callable[[], None]
type Subscribe = Callable[[Listener], Listener]


class DiscoveryLifecycle(Protocol):
    """A discovery listener belongs to either the entry or one source attempt."""

    def async_on_unload(self, func: Listener) -> None: ...


@dataclass(slots=True)
class PlatformScope:
    """Pass source data explicitly without replacing the entry's runtime."""

    entry_id: str
    async_on_unload: Callable[[Listener], None]
    coordinators: tuple[BoschBuderusDataUpdateCoordinator, ...] = ()
    wallboxes: tuple[WallboxCoordinator, ...] = ()
    bacon: BaconAccess | None = None
    metrics: RequestMetrics | None = None
    metric_updates: Subscribe | None = None
    current: Callable[[], bool] = lambda: True


type EntityBuilder = Callable[[PlatformScope, AddConfigEntryEntitiesCallback], None]


def setup_platform(
    entry: ConfigEntry[Any],
    platform: Platform,
    add_entities: AddConfigEntryEntitiesCallback,
    build: EntityBuilder,
) -> None:
    """Use the same entity builders for normal setup and managed sources."""
    from .managed_entry import ManagedEntry

    runtime = entry.runtime_data
    if isinstance(runtime, ManagedEntry):
        runtime.platforms.register(platform, build)
        return
    # G3/O51: the entry's entity use, as its coordinators know it.
    use = next(
        (
            value
            for coordinator in getattr(runtime, "coordinators", ())
            if (value := getattr(coordinator, "entity_use", None)) is not None
        ),
        None,
    )

    def add(
        new_entities: Iterable[Entity],
        update_before_add: bool = False,
        *,
        config_subentry_id: str | None = None,
    ) -> None:
        # G3/O51: note each entity, also one the registry keeps disabled.
        new_entities = list(new_entities)
        if use is not None:
            for entity in new_entities:
                use.note(platform, entity)
        if update_before_add or config_subentry_id is not None:
            add_entities(
                new_entities, update_before_add, config_subentry_id=config_subentry_id
            )
        else:
            add_entities(new_entities)

    build(
        PlatformScope(
            entry_id=entry.entry_id if platform is Platform.SENSOR else "",
            async_on_unload=lambda listener: entry.async_on_unload(listener),
            coordinators=getattr(runtime, "coordinators", ()),
            wallboxes=getattr(runtime, "wallboxes", ()),
            bacon=getattr(runtime, "bacon", None),
            metrics=(
                runtime.client.metrics
                if platform is Platform.SENSOR and runtime.coordinators
                else None
            ),
        ),
        add,
    )
