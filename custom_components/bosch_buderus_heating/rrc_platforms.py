"""Platform hooks of the RRC 2.0 feature packages (W1 to W6).

Every platform's entity setup calls ``track`` once per coordinator. It hands
the platform's ``add`` callback to the trackers each package declares in its
entity module (``TRACKERS``), so a package adds entities without touching a
platform file. A tracker must itself wait for an RRC controller, best with
``control.track_rrc_entities``. See ``docs/dev/rrc-packages.md``.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping
from dataclasses import replace
from functools import cache
from typing import Final, cast

from homeassistant.const import EntityCategory, Platform
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import rrc_contracts
from .coordinator import BoschBuderusDataUpdateCoordinator
from .platform_scope import DiscoveryLifecycle

type Tracker = Callable[
    [
        DiscoveryLifecycle,
        BoschBuderusDataUpdateCoordinator,
        AddConfigEntryEntitiesCallback,
    ],
    None,
]

# One entity module per package, in the order of the contracts.
ENTITY_MODULES: Final = (
    "rrc_zone_entities",
    "rrc_absence_entities",
    "rrc_dhw_entities",
    "rrc_circuit_entities",
    "rrc_system_entities",
    "rrc_device_setting_entities",
)
# The platforms whose setup calls ``track``.
PLATFORMS: Final = frozenset(
    {
        Platform.BINARY_SENSOR,
        Platform.BUTTON,
        Platform.CALENDAR,
        Platform.CLIMATE,
        Platform.NUMBER,
        Platform.SELECT,
        Platform.SENSOR,
        Platform.SWITCH,
        Platform.TIME,
        Platform.WATER_HEATER,
    }
)


@cache
def trackers() -> tuple[tuple[str, Mapping[Platform, Tracker]], ...]:
    """The ``TRACKERS`` of every entity module, imported on first use.

    Imported late: an entity module may import a platform module, which
    imports this one.
    """
    loaded: list[tuple[str, Mapping[Platform, Tracker]]] = []
    for name in ENTITY_MODULES:
        module = importlib.import_module(f"{__package__}.{name}")
        table = cast(Mapping[Platform, Tracker], module.TRACKERS)
        if unknown := set(table) - PLATFORMS:
            raise ValueError(f"{name} tracks platforms without a hook: {unknown}")
        loaded.append((name, table))
    return tuple(loaded)


def track(
    platform: Platform,
    entry: DiscoveryLifecycle,
    coordinator: BoschBuderusDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Start the trackers of all packages for one platform and coordinator."""
    for _module, table in trackers():
        if (tracker := table.get(platform)) is not None:
            tracker(entry, coordinator, async_add_entities)


def generic_presentation[DescriptionT: EntityDescription](
    description: DescriptionT, platform: Platform
) -> DescriptionT:
    """An RRC contract's category, activation, name, key and icon for a
    generic read-only entity of one of its paths; unchanged without one.

    Only for an RRC controller (S11). A read-only mirror of a contract control
    becomes a disabled diagnostic entity, like the catalog's own mirrors.
    """
    path = getattr(description, "resource_path", None)
    if not isinstance(path, str):
        return description
    changes: dict[str, object] = {}
    if (category := rrc_contracts.category(path)) is not None:
        changes["entity_category"] = EntityCategory(category)
    if (enabled := rrc_contracts.default_enabled(path)) is not None:
        changes["entity_registry_enabled_default"] = enabled
    if rrc_contracts.is_mirror(path):
        changes["entity_category"] = EntityCategory.DIAGNOSTIC
        changes["entity_registry_enabled_default"] = False
    if getattr(description, "value_key", None) in (None, "values"):
        if (label := rrc_contracts.name(path, "de")) is not None:
            changes["name"] = label
        if (key := rrc_contracts.entity_key(path, platform.value)) is not None:
            changes["translation_key"] = key
    if (icon := rrc_contracts.icon(path)) is not None:
        changes["icon"] = icon
    if not changes:
        return description
    return replace(description, **changes)  # type: ignore[arg-type]
