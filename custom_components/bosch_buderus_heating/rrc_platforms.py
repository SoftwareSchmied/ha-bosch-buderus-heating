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
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import rrc_contracts
from .coordinator import BoschBuderusDataUpdateCoordinator
from .platform_scope import DiscoveryLifecycle
from .pointt import Resource

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


async def async_load_trackers(hass: HomeAssistant) -> None:
    """Import the entity modules in the executor, before platforms start.

    Every path that sets up the entity platforms awaits this first, so
    ``track`` finds the cached trackers and never imports in the event loop.
    """
    await hass.async_add_import_executor_job(trackers)


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


def setting_without_control(resource: Resource) -> bool:
    """Whether a contract setting's reading offers no control at all.

    True only for a path with a contract write rule that no control can be
    created from, for example a setting that is not writable or has a single
    option. A control whose only issue is its current value still counts,
    so the answer does not change with every reading. Checked with and
    without the heat pump variant of the contracts. Paths without a write
    rule (shown by entities of their own) are never such a setting.
    """
    from .write_policies import RRC_CONTEXT, RRC_HEAT_PUMP_CONTEXT
    from .writes import assess_control

    ruled = False
    for context in (RRC_CONTEXT, RRC_HEAT_PUMP_CONTEXT):
        assessment = assess_control(resource, context)
        if assessment.policy is None:
            continue
        ruled = True
        if assessment.rejection_reason in (None, assessment.current_value_issue):
            return False
    return ruled


def generic_presentation[DescriptionT: EntityDescription](
    description: DescriptionT,
    platform: Platform,
    resource: Resource | None = None,
) -> DescriptionT:
    """An RRC contract's category, activation, name, key and icon for a
    generic read-only entity of one of its paths; unchanged without one.

    Only for an RRC controller (S11). A read-only mirror of a contract control
    becomes a disabled diagnostic entity, like the catalog's own mirrors. When
    the reading (``resource``) of a setting offers no control, for example
    because it is not writable or has a single option, its read-only entity
    is the only place the value is shown: it stays a diagnostic entity,
    enabled by default unless the contract decides otherwise.
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
        if resource is not None and setting_without_control(resource):
            if enabled is None:
                changes["entity_registry_enabled_default"] = True
        else:
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
