"""Performance history with the recorder: counters without statistics, with
unreadable ones, and the start-of-day lookup. The recorder is set up before
Home Assistant, so this module has no module-wide fixtures."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.performance_history import (
    PerformanceHistory,
)
from tests.test_derived_values import Plant
from tests.test_performance import _at, local
from tests.test_start_counters import (
    recorder_runtime_annotations,  # noqa: F401 - fixture
)


async def _with_counters(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch
) -> tuple[Plant, PerformanceHistory, dict[str, str]]:
    await hass.config.async_update(time_zone="Europe/Berlin")
    plant = Plant(hass, freezer)
    _at(plant, local(2026, 11, 15, 10, 0))
    monkeypatch.setattr(
        "custom_components.bosch_buderus_heating.recordings.recordings_view",
        lambda _hass, _gateway: None,
    )
    engine = PerformanceHistory(hass, plant.coordinator)
    await engine.async_start(schedule=False)
    registry = er.async_get(hass)
    ids: dict[str, str] = {}
    for suffix in ("total_heat_produced", "total_electricity"):
        ids[suffix] = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"gateway-one:gateway:{suffix}",
            config_entry=plant.coordinator.config_entry,
        ).entity_id
    return plant, engine, ids


def _rows(ids: dict[str, str], *, change: object = 5.0, begin: object = None) -> Any:
    start = begin if begin is not None else local(2026, 11, 14).timestamp()

    def read(*args: Any) -> dict[str, list[dict[str, Any]]]:
        # The first day of a counter's statistics is incomplete and dropped.
        return {
            entity: [
                {"start": start - 86400, "change": 1.0, "state": 1.0},
                {"start": start, "change": change, "state": change},
            ]
            for entity in ids.values()
        }

    return read


async def test_a_gateway_without_counter_entities_reports_it(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    await hass.config.async_update(time_zone="Europe/Berlin")
    plant = Plant(hass, freezer)
    _at(plant, local(2026, 11, 15, 10, 0))
    engine = PerformanceHistory(hass, plant.coordinator)
    await engine.async_start(schedule=False)
    assert await engine._async_count(date(2026, 11, 15), full=True) == "no_counters"
    await engine._async_statistics_baseline(date(2026, 11, 15))
    assert engine._lookup == "no_counters"
    assert engine._baseline_method is None


async def test_unreadable_counter_statistics_keep_what_is_known(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from homeassistant.components.recorder import statistics

    _plant, engine, ids = await _with_counters(hass, freezer, monkeypatch)
    monkeypatch.setattr(statistics, "statistics_during_period", _rows(ids, change=5.0))
    assert await engine._async_count(date(2026, 11, 15), full=True) == "ok"
    known = dict(engine._counted[("total", "heat")])
    assert known == {date(2026, 11, 14): 5.0}

    def broken(*args: Any) -> Any:
        raise RuntimeError("database locked")

    monkeypatch.setattr(statistics, "statistics_during_period", broken)
    assert await engine._async_count(date(2026, 11, 15), full=False) == "failed"
    assert engine._counted[("total", "heat")] == known
    await engine._async_statistics_baseline(date(2026, 11, 15))
    assert engine._lookup == "failed" and engine._baseline_method is None
    # The refresh reports the failure and the engine goes on.
    await engine.async_refresh()
    assert engine.report["statistics"] == "failed"
    assert engine._lookup == "failed"


async def test_rows_without_numbers_are_skipped_and_an_empty_history_clears_the_span(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from homeassistant.components.recorder import statistics

    _plant, engine, ids = await _with_counters(hass, freezer, monkeypatch)
    heat = ids["total_heat_produced"]
    power = ids["total_electricity"]
    day = local(2026, 11, 14).timestamp()

    def rows(*args: Any) -> dict[str, list[dict[str, Any]]]:
        return {
            heat: [
                {"start": day - 86400, "change": 2.0},
                {"start": day, "change": "n/a"},
                {"start": "x", "change": 1.0},
                {"start": day, "change": None},
                {"start": day + 86400 * 0 + 1, "change": 4.0},
            ],
            power: [],
        }

    monkeypatch.setattr(statistics, "statistics_during_period", rows)
    assert await engine._async_count(date(2026, 11, 15), full=True) == "ok"
    assert engine._counted[("total", "heat")] == {date(2026, 11, 14): 4.0}
    assert engine._counted_span[("total", "heat")] == (
        date(2026, 11, 14),
        date(2026, 11, 14),
    )
    assert engine._counted[("total", "electricity")] == {}
    assert ("total", "electricity") not in engine._counted_span
    assert engine._full_done


async def test_the_first_counter_day_of_a_late_history_is_never_used(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from homeassistant.components.recorder import statistics

    _plant, engine, ids = await _with_counters(hass, freezer, monkeypatch)
    first = local(2026, 11, 12).timestamp()
    second = local(2026, 11, 13).timestamp()

    def rows(*args: Any) -> dict[str, list[dict[str, Any]]]:
        return {
            entity: [
                {"start": first, "change": 1.0},
                {"start": second, "change": 24.0},
            ]
            for entity in ids.values()
        }

    monkeypatch.setattr(statistics, "statistics_during_period", rows)
    assert await engine._async_count(date(2026, 11, 15), full=True) == "ok"
    assert engine._counted[("total", "heat")] == {date(2026, 11, 13): 24.0}
    # A later short read keeps the rule: the partial day stays out.
    assert await engine._async_count(date(2026, 11, 15), full=False) == "ok"
    assert date(2026, 11, 12) not in engine._counted[("total", "heat")]


async def test_a_start_of_day_lookup_without_usable_statistics_finds_nothing(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from homeassistant.components.recorder import statistics

    _plant, engine, ids = await _with_counters(hass, freezer, monkeypatch)
    today = date(2026, 11, 15)
    engine._baseline_day = today
    monkeypatch.setattr(
        statistics,
        "statistics_during_period",
        lambda *a: {ids["total_heat_produced"]: [{"state": "x"}]},
    )
    await engine._async_statistics_baseline(today)
    assert engine._lookup == "no_statistic" and engine._baseline_method is None
    # Another way of starting the day was found while the lookup waited.
    engine._baseline_method = "midnight"
    await engine._async_statistics_baseline(today)
    assert engine._lookup == "not_needed" and engine._baseline_method == "midnight"
    # A lookup for another day than the current one changes nothing either.
    engine._baseline_method = None
    await engine._async_statistics_baseline(today - timedelta(days=1))
    assert engine._lookup == "not_needed"
