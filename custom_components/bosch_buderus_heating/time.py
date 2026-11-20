"""Time settings of RRC 2.0 room controllers (W3: disinfection time).

The platform itself adds no entity; the packages add theirs through
``rrc_platforms.track``.
"""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BoschBuderusConfigEntry, rrc_platforms
from .platform_scope import PlatformScope, setup_platform

# Writes already run one at a time per gateway under the write lock, and an
# overlapping change is refused as busy instead of queued. Home Assistant's
# per-platform limit would queue it behind the first change instead, so no
# limit is set here.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BoschBuderusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    setup_platform(entry, Platform.TIME, async_add_entities, _setup_entities)


def _setup_entities(
    entry: PlatformScope,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for coordinator in entry.coordinators:
        rrc_platforms.track(Platform.TIME, entry, coordinator, async_add_entities)
