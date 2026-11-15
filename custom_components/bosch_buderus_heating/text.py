"""Text settings: the display name of a wallbox.

The platform itself adds no other entity.
"""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BoschBuderusConfigEntry
from .platform_scope import PlatformScope, setup_platform
from .wallbox_entities import track_wallbox_entities


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
    track_wallbox_entities(Platform.TEXT, entry, async_add_entities)
