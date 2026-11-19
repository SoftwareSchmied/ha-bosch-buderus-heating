"""Recordings of starts, working time and temperatures for yesterday, and the
heat-source anchor a plant needs for any recording read.

The manager and its local source are the stand-ins of
``test_recordings_manager`` (answers shaped like a real installation).
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.recordings import Period
from custom_components.bosch_buderus_heating.recordings.anchor import (
    has_recording_anchor,
)
from custom_components.bosch_buderus_heating.recordings.series import BY_KEY
from tests import test_recordings_manager as base
from tests.test_recordings_core import local
from tests.test_recordings_manager import Clock, Driver, key, manager

EXTRA = {
    "ch_starts": 1.0,
    "ch_working_time": 1.0,
    "supply_temperature": 250.0,
}
UNITS = {"ch_starts": "count", "ch_working_time": "h"}


@pytest.fixture
def plant_series(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(base.PLANT, "ch_starts", EXTRA["ch_starts"])
    monkeypatch.setitem(base.PLANT, "ch_working_time", EXTRA["ch_working_time"])
    monkeypatch.setitem(base.PLANT, "supply_temperature", EXTRA["supply_temperature"])
    monkeypatch.setattr(base, "UNITS", UNITS)


# ------------------------------------------------------------------ H4
async def test_yesterday_of_starts_working_time_and_temperatures_after_hourly_runs(
    hass: HomeAssistant, plant_series: None
) -> None:
    """The entities are enabled after the start run: the next hourly run
    reads their series, and yesterday is shown then; the means are the
    plant's sums over their sample counts."""
    clock = Clock(local(2026, 11, 14, 10) + timedelta(minutes=20))
    driver = Driver(clock)
    rec = await manager(hass, clock, [driver])
    await rec.async_run("start")
    keys = {
        name: key(name, Period.YESTERDAY)
        for name in (
            "ch_starts",
            "ch_working_time",
            "outdoor_temperature",
            "supply_temperature",
        )
    }
    for item in keys.values():
        rec.request(item)
    clock.now += timedelta(hours=1)
    await rec.async_run("hourly")
    clock.now += timedelta(hours=1)
    await rec.async_run("hourly")
    specs = {spec.key: spec for spec in rec.specs()}
    shown = {name: rec.shown(specs[item]) for name, item in keys.items()}
    assert all(item is not None for item in shown.values()), shown
    assert shown["ch_starts"].value == pytest.approx(24.0)
    assert shown["ch_working_time"].value == pytest.approx(24.0)
    assert shown["outdoor_temperature"].value == pytest.approx(5.0)
    assert shown["supply_temperature"].value == pytest.approx(250.0 / 60, abs=1e-3)
    assert shown["ch_starts"].start.isoformat() == "2026-11-13"
    assert BY_KEY["ch_starts"].path in {path for path, _ in driver.requests}


# ------------------------------------------------------------------ L18
def _coordinator(resources: list[str], *, data: list[str] | None = None) -> Any:
    return SimpleNamespace(
        resources={path: object() for path in resources},
        data={path: object() for path in data or ()},
        local_selection=None,
    )


def test_a_room_controller_with_a_bare_heat_source_has_no_anchor() -> None:
    ct200 = _coordinator(
        [
            "/heatSources",
            "/heatSources/actualModulation",
            "/heatSources/refillNeeded",
            "/heatingCircuits/hc1/operationMode",
        ]
    )
    assert not has_recording_anchor(ct200)


@pytest.mark.parametrize(
    "path",
    [
        "/heatSources/emon/totalConsumption",
        "/heatSources/numberOfStarts",
        "/heatSources/workingTime/totalSystem",
        "/heatSources/hs1/refrigerant/status",
        "/heatSources/compressor/status",
    ],
)
def test_a_counter_or_heat_pump_reading_is_an_anchor(path: str) -> None:
    assert has_recording_anchor(_coordinator([path]))
    assert has_recording_anchor(_coordinator([], data=[path]))


async def test_no_request_without_an_anchor_and_reads_with_one(
    hass: HomeAssistant,
) -> None:
    clock = Clock(local(2026, 11, 14, 10) + timedelta(minutes=20))
    driver = Driver(clock)
    cloud = base.Cloud()
    rec = await manager(hass, clock, [driver], cloud)
    coordinator = rec.coordinator()
    assert coordinator is not None
    coordinator.resources = {"/heatSources/actualModulation": object()}
    await rec.async_run("start")
    assert not driver.requests and cloud.calls == []
    assert rec.waiting == "no_heat_source_anchor"
    assert not rec.runs
    # The plant shows an energy counter collection later: the run reads.
    coordinator.resources["/heatSources/emon/totalConsumption"] = object()
    clock.now += timedelta(hours=1)
    await rec.async_run("hourly")
    assert driver.requests and rec.runs


async def test_stored_answers_anchor_the_start_run_without_readings(
    hass: HomeAssistant,
) -> None:
    """Right after a start the plant may show no resources yet (cloud not
    reachable): a series that answered before anchors the run, so it does not
    wait for the hourly run."""
    clock = Clock(local(2026, 11, 14, 10) + timedelta(minutes=20))
    driver = Driver(clock)
    rec = await manager(hass, clock, [driver], base.Cloud())
    coordinator = rec.coordinator()
    assert coordinator is not None
    coordinator.resources = {}
    # A softener's answer is no heat source anchor.
    rec.cloud_paths["/swCircuits/sw1/waterConsumption"] = ["ok", 1.0]
    await rec.async_run("start")
    assert rec.waiting == "no_heat_source_anchor" and not driver.requests
    rec.local_paths["/heatSources/emon/totalConsumption"] = ["ok", 1.0]
    await rec.async_run("start")
    assert rec.waiting != "no_heat_source_anchor"
    assert driver.requests and rec.runs
