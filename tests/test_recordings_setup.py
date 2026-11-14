"""The plant's recordings through a real setup (cloud only).

The cloud tree of the room controller tests (it has a heat source) and a
fake ``get_recording``: entities with their unique IDs and default
activation, the options step (import on and off, delete with confirmation)
without a reload, and the diagnostics section.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.pointt import PointTClient
from custom_components.bosch_buderus_heating.recordings.manager import (
    OPTION_IMPORT,
    START_DELAY,
    managers_of,
)
from tests.helpers.options_menu import outcome
from tests.helpers.rrc_cloud import RrcCloud, load_tree
from tests.test_rrc_energy_statistics import (
    GW,
    advance,
    legacy_setup_path,  # noqa: F401 - fixture
    start,
    stop,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")
NOW = datetime(2026, 11, 14, 9, 30, tzinfo=UTC)
YESTERDAY_HEAT = f"{GW}:recordings:heatSources:emon:total:outputProduced:yesterday"
YESTERDAY_POWER = f"{GW}:recordings:heatSources:emon:total:electricity:yesterday"


def cloud_tree() -> RrcCloud:
    return RrcCloud(load_tree("rrc/ct200-shapes.json"))


async def recording(gateway_id, path, interval):
    assert gateway_id == GW
    if len(interval) == 10:
        rows = [{"y": 1.0, "c": 1}] * 24
    elif len(interval) == 7:
        rows = [{"y": 24.0, "c": 24}] * 30
    else:
        rows = [{"y": 700.0, "c": 1}] * 12
    return {"type": "yRecording", "unitOfMeasure": "kWh", "recording": rows}


async def test_entities_options_and_diagnostics(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await hass.config.async_update(time_zone="Europe/Berlin")
    freezer.move_to(NOW)
    calls = AsyncMock(side_effect=recording)
    with patch.object(PointTClient, "get_recording", calls):
        entry, stack = await start(hass, cloud_tree())
        try:
            await advance(hass, freezer, timedelta(seconds=START_DELAY + 5))
            await hass.async_block_till_done(wait_background_tasks=True)
            assert calls.await_count > 0
            registry = er.async_get(hass)
            ours = [
                item
                for item in er.async_entries_for_config_entry(registry, entry.entry_id)
                if ":recordings:" in item.unique_id
            ]
            assert ours, "the period entities exist"
            enabled = sorted(item.unique_id for item in ours if not item.disabled_by)
            assert enabled == sorted([YESTERDAY_HEAT, YESTERDAY_POWER])
            heat = registry.async_get_entity_id(
                "sensor", "bosch_buderus_heating", YESTERDAY_HEAT
            )
            state = hass.states.get(heat)
            assert state is not None
            assert "system recording" in state.attributes["friendly_name"]
            # Yesterday was read once: shown only once a second read confirms it.
            assert state.state == "unavailable"
            await advance(hass, freezer, timedelta(minutes=55))
            await hass.async_block_till_done(wait_background_tasks=True)
            state = hass.states.get(heat)
            assert float(state.state) == pytest.approx(24.0)
            assert state.attributes["method"] == "recording"
            assert state.attributes["source"] == "cloud_recording"
            assert state.attributes["period_start"] == "2026-11-13"
            assert state.attributes["period_end"] == "2026-11-13"
            assert state.attributes["unit_of_measurement"] == "kWh"
            assert state.attributes["device_class"] == "energy"
            assert "state_class" not in state.attributes
            power = registry.async_get_entity_id(
                "sensor", "bosch_buderus_heating", YESTERDAY_POWER
            )
            assert float(hass.states.get(power).state) == pytest.approx(48.0)
            assert hass.states.get(power).attributes["derived_from"] == [
                "/recordings/heatSources/emon/total/compressor",
                "/recordings/heatSources/emon/total/eheater",
            ]

            # The options step: on, without a reload; then delete.
            reload = patch.object(hass.config_entries, "async_schedule_reload")
            with reload as scheduled:
                menu = await hass.config_entries.options.async_init(entry.entry_id)
                assert "recordings" in menu["menu_options"]
                form = await hass.config_entries.options.async_configure(
                    menu["flow_id"], {"next_step_id": "recordings"}
                )
                assert form["type"] is FlowResultType.FORM
                assert form["description_placeholders"]["series"] != "0"
                result = await hass.config_entries.options.async_configure(
                    form["flow_id"], {OPTION_IMPORT: True, "delete_statistics": False}
                )
                assert await outcome(hass, result) == "recordings_import_on"
                assert entry.options[OPTION_IMPORT] is True
                result = await hass.config_entries.options.async_configure(
                    result["flow_id"], {"next_step_id": "recordings"}
                )
                result = await hass.config_entries.options.async_configure(
                    result["flow_id"], {OPTION_IMPORT: True, "delete_statistics": True}
                )
                assert result["step_id"] == "recordings_delete"
                kept = await hass.config_entries.options.async_configure(
                    result["flow_id"], {"confirm": False}
                )
                assert await outcome(hass, kept) == "recordings_delete_kept"
                assert entry.options[OPTION_IMPORT] is True
                result = await hass.config_entries.options.async_configure(
                    kept["flow_id"], {"next_step_id": "recordings"}
                )
                result = await hass.config_entries.options.async_configure(
                    result["flow_id"], {OPTION_IMPORT: True, "delete_statistics": True}
                )
                done = await hass.config_entries.options.async_configure(
                    result["flow_id"], {"confirm": True}
                )
                assert await outcome(hass, done) == "recordings_deleted"
                assert entry.options[OPTION_IMPORT] is False
                scheduled.assert_not_called()
            assert managers_of(hass, entry.entry_id)

            report = await async_get_config_entry_diagnostics(hass, entry)
            section = report["gateways"][0]["recordings"]
            assert section["series"]["total_heat"]["cloud"] == "ok"
            assert GW not in json.dumps(section)
        finally:
            await stop(hass, entry, stack)
    assert not managers_of(hass, entry.entry_id)
