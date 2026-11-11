"""Home Assistant test helpers from the installed test package.

From Home Assistant 2026.11 the helpers come with the official
``pytest-homeassistant`` package. The older supported releases use
``pytest-homeassistant-custom-component``, which provides the same helpers.
Tests import them from here so the suite runs on every supported release.
The recorder helpers are in ``ha_recorder_helpers`` and imported late.
"""

from __future__ import annotations

from importlib.util import find_spec
from unittest.mock import patch

from homeassistant.components.sensor import DATA_COMPONENT

if find_spec("pytest_homeassistant") is not None:
    from pytest_homeassistant.common import (
        MockConfigEntry,
        async_fire_time_changed,
        flush_store,
        mock_restore_cache,
        mock_restore_cache_with_extra_data,
    )
else:  # Home Assistant before 2026.11
    from pytest_homeassistant_custom_component.common import (  # type: ignore[no-redef]
        MockConfigEntry,
        async_fire_time_changed,
        flush_store,
        mock_restore_cache,
        mock_restore_cache_with_extra_data,
    )

__all__ = [
    "MockConfigEntry",
    "add_without_device",
    "async_fire_time_changed",
    "flush_store",
    "mock_restore_cache",
    "mock_restore_cache_with_extra_data",
]


async def add_without_device(hass, sensor) -> None:
    """Add a resource sensor to the sensor component directly. Without a
    config entry it carries no device; Home Assistant 2027.8 refuses one."""
    with patch.object(type(sensor), "device_info", new=None):
        await hass.data[DATA_COMPONENT].async_add_entities([sensor])
