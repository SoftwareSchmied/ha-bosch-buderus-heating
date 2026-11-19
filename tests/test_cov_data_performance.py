"""Performance history: damaged saved state, a stopped engine, counters
without statistics or with unreadable ones, and the reports of a gateway
that has no engine."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating import performance_history
from custom_components.bosch_buderus_heating.derived_store import derived_store
from custom_components.bosch_buderus_heating.performance_history import (
    PerformanceHistory,
    Period,
    engine_for,
    performance_diagnostics,
)
from tests.test_derived_values import Plant, air
from tests.test_performance import _at, _engine, counters, local

KEY = "gateway-one:performance:today"


@pytest.fixture(autouse=True)
async def _stop_engines(hass: HomeAssistant) -> AsyncGenerator[None]:
    yield
    for engine in hass.data.get(performance_history._ENGINES, {}).values():
        engine.stop()


async def _plain(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> tuple[Plant, PerformanceHistory]:
    plant = air(hass, freezer)
    _at(plant, local(2026, 11, 15, 10, 0))
    return plant, await _engine(plant)


# ------------------------------------------------------- the saved state
@pytest.mark.parametrize(
    "row",
    [
        {"day": "yesterday-ish"},
        {"day": "2026-11-14", "baseline": {"total": "x"}},
        {"day": "2026-11-14", "baseline": {"total": [1.0]}},
        {"day": "2026-11-14", "baseline": 5},
        {"day": "2026-11-14", "baseline": {"total": [1, 2]}, "last": {"total": [1]}},
        {"day": None},
    ],
)
async def test_a_damaged_saved_state_leaves_today_unknown(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, row: dict[str, Any]
) -> None:
    plant = air(hass, freezer)
    _at(plant, local(2026, 11, 14, 10, 0))
    store = derived_store(hass)
    await store.async_load()
    store.set(KEY, row)
    engine = await _engine(plant)
    assert engine.loaded
    assert engine._baseline == {} and engine._baseline_day is None
    assert engine._live == {} and engine._last_at is None
    counters(plant, 1600.0, 400.0)
    engine.sync()
    assert engine.value(Period.TODAY, "total") is None


async def test_a_valid_saved_state_with_other_modes_keeps_only_known_modes(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    _at(plant, local(2026, 11, 14, 10, 0))
    store = derived_store(hass)
    await store.async_load()
    store.set(
        KEY,
        {
            "day": "2026-11-14",
            "baseline": {"total": [100.0, 20.0], "unknown_mode": [1.0, 1.0]},
            "method": 5,
            "last_at": "never",
            "last": {"total": [110.0, 22.0], "other": [1.0, 1.0]},
        },
    )
    engine = await _engine(plant)
    assert engine._baseline == {"total": (100.0, 20.0)}
    assert engine._baseline_day == date(2026, 11, 14)
    assert engine._baseline_method is None and engine._last_at is None
    assert set(engine._live) == {"total"}
    assert engine.live_origin("total") is None, "a restored reading has no origin"


async def test_nothing_is_saved_before_a_day_is_known(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant, engine = await _plain(hass, freezer)
    engine._save()
    assert derived_store(hass).get(KEY) is None
    engine.sync()  # no counter at all
    assert derived_store(hass).get(KEY) is None and engine._baseline_day is None
    counters(plant, 1600.0, 400.0)
    engine.sync()
    row = derived_store(hass).get(KEY)
    assert row is not None and row["day"] == "2026-11-15"


# ---------------------------------------------------------- the lifecycle
async def test_a_second_start_neither_reads_nor_restores_again(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant, engine = await _plain(hass, freezer)
    counters(plant, 1600.0, 400.0)
    engine.sync()
    live = dict(engine._live)
    derived_store(hass).set(KEY, {"day": "2020-01-01"})
    await engine.async_start(schedule=False)
    assert engine._live == live and engine._baseline_day == date(2026, 11, 15)


async def test_a_stopped_engine_starts_no_timers_and_no_refresh(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plant, engine = await _plain(hass, freezer)
    calls: list[bool] = []

    async def refresh(*, full: bool = False) -> None:
        calls.append(full)

    real_refresh = engine.async_refresh
    monkeypatch.setattr(engine, "async_refresh", refresh)
    now = datetime(2026, 11, 15, 10, 25, tzinfo=UTC)
    engine._due_start(now)
    engine._due_hourly(now)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert calls == [True, True], "the first hourly run still reads everything"
    engine._full_done = True
    engine._due_hourly(now)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert calls == [True, True, False]
    engine.stop()
    engine._due_start(now)
    engine._due_hourly(now)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert calls == [True, True, False]
    await engine.async_start(schedule=True)
    assert engine._timers == []
    await real_refresh()
    assert engine.report == {}, "a stopped engine reads nothing"


async def test_a_stopped_engine_ignores_recordings_and_starts_no_lookup(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant, engine = await _plain(hass, freezer)
    notified: list[bool] = []
    engine.add_listener(lambda: notified.append(True))
    engine.stop()
    engine._recordings_updated()
    assert notified == []
    # A new day after the stop: the start of the day is not looked up.
    counters(plant, 1600.0, 400.0)
    engine.sync()
    assert engine._baseline == {} and engine._baseline_method is None
    assert engine._lookup is None


async def test_a_listener_can_be_removed_twice(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    _, engine = await _plain(hass, freezer)
    calls: list[bool] = []
    remove = engine.add_listener(lambda: calls.append(True))
    engine._notify()
    remove()
    remove()
    engine._notify()
    assert calls == [True]


# ------------------------------------------------------- reports and lookup
async def test_a_coordinator_without_an_entry_has_no_engine(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    plant.coordinator.config_entry = None  # type: ignore[assignment]
    assert engine_for(plant.coordinator) is None
    assert performance_diagnostics(plant.coordinator) == {"waiting": "not_started"}


def test_a_report_that_cannot_be_built_stays_available() -> None:
    broken = SimpleNamespace(config_entry=SimpleNamespace(entry_id="e"))
    assert performance_diagnostics(broken) == {"error": "unavailable"}  # type: ignore[arg-type]


async def test_the_diagnostics_of_an_engine_name_counts_not_values(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    _at(plant, local(2026, 11, 15, 10, 0))
    engine = engine_for(plant.coordinator)
    assert engine is not None
    await hass.async_block_till_done(wait_background_tasks=True)
    report = performance_diagnostics(plant.coordinator)
    assert report["recordings_attached"] is False
    assert report["today_start"] is None and report["today_modes"] == []
    assert engine_for(plant.coordinator) is engine
    engine.stop()
    assert engine_for(plant.coordinator) is not engine


async def test_counters_next_to_a_filled_day_without_a_stored_span_are_still_checked(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    _plant, engine = await _plain(hass, freezer)
    item = ("ch", "heat")
    engine._counted[item] = {date(2026, 11, 10): 5.0, date(2026, 11, 12): 7.0}
    engine._recorded[item] = {}
    assert engine._counted_amount(item, date(2026, 11, 10)) is not None
    assert engine._counted_span[item] == (date(2026, 11, 10), date(2026, 11, 12))
    # 11 November is missing between two counted days and the recordings
    # fill it: the days next to it count by the recordings only.
    engine._recorded[item] = {date(2026, 11, 11): performance_history.Amount(6.0, "x")}
    assert engine._counted_amount(item, date(2026, 11, 10)) is None
    assert engine._counted_amount(item, date(2026, 11, 12)) is None
    assert engine._counted_amount(item, date(2026, 11, 9)) is None
