"""Recorder test helpers from the installed test package (see ha_test_helpers)."""

from __future__ import annotations

from importlib.util import find_spec

if find_spec("pytest_homeassistant") is not None:
    from pytest_homeassistant.components.recorder.common import (
        async_wait_recording_done,
        do_adhoc_statistics,
        get_start_time,
    )
else:  # Home Assistant before 2026.11
    from pytest_homeassistant_custom_component.components.recorder.common import (  # type: ignore[no-redef]
        async_wait_recording_done,
        do_adhoc_statistics,
        get_start_time,
    )

__all__ = ["async_wait_recording_done", "do_adhoc_statistics", "get_start_time"]
