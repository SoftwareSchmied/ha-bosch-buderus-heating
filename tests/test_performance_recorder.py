"""Performance factors from the counters' long-term statistics.

The heat per mode comes from the counters' daily change first, the total
heat and the electricity from the plant's recordings first; each falls back
to the other. The counter's first day of statistics is incomplete and never
counts. After a start during the day, today starts at the counters'
statistic in the hour before midnight. The recorder is set up before Home
Assistant, so this module has no module-wide fixtures.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.performance_history import (
    PerformanceHistory,
    Period,
)
from tests.test_derived_values import TOTAL, Plant
from tests.test_performance import View, counters
from tests.test_start_counters import (
    recorder_runtime_annotations,  # noqa: F401 - fixture
)

Z = ZoneInfo("Europe/Berlin")
CH = "/heatSources/emon/chConsumption"
FIRST = date(2026, 11, 10)
# kWh per hour of each counter.
RATES = {
    "gateway:heating_heat_produced": 1.0,
    "gateway:heating_electricity": 0.25,
    "gateway:total_heat_produced": 2.0,
    "gateway:total_electricity": 0.5,
}
START = {
    "gateway:heating_heat_produced": 1000.0,
    "gateway:heating_electricity": 300.0,
    "gateway:total_heat_produced": 1600.0,
    "gateway:total_electricity": 400.0,
}


async def _counter_statistics(hass: HomeAssistant, plant: Plant) -> dict[str, str]:
    """Hourly statistics of four counter entities from ``FIRST`` to the
    hour before midnight of 14 November."""
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMeanType,
        StatisticMetaData,
    )
    from homeassistant.components.recorder.statistics import async_import_statistics

    from tests.ha_recorder_helpers import async_wait_recording_done

    registry = er.async_get(hass)
    entity_ids = {}
    hours = int(
        (
            datetime.combine(date(2026, 11, 15), datetime.min.time(), Z)
            - datetime.combine(FIRST, datetime.min.time(), Z)
        ).total_seconds()
        // 3600
    )
    for suffix, rate in RATES.items():
        entity = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"gateway-one:{suffix}",
            config_entry=plant.coordinator.config_entry,
        )
        entity_ids[suffix] = entity.entity_id
        rows = []
        for hour in range(hours):
            start = datetime.combine(FIRST, datetime.min.time(), Z) + timedelta(
                hours=hour
            )
            total = rate * (hour + 1)
            rows.append(
                StatisticData(
                    start=start.astimezone(UTC), sum=total, state=START[suffix] + total
                )
            )
        async_import_statistics(
            hass,
            StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=None,
                source="recorder",
                statistic_id=entity.entity_id,
                unit_class="energy",
                unit_of_measurement="kWh",
            ),
            rows,
        )
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    return entity_ids


def _span(first: date, last: date, value: float) -> dict[date, float]:
    return {
        first + timedelta(days=offset): value
        for offset in range((last - first).days + 1)
    }


async def _engine(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch, view
) -> tuple[Plant, PerformanceHistory]:
    await hass.config.async_update(time_zone="Europe/Berlin")
    plant = Plant(hass, freezer)
    plant.at = datetime(2026, 11, 15, 10, 0, tzinfo=Z).astimezone(UTC)
    freezer.move_to(plant.at)
    monkeypatch.setattr(
        "custom_components.bosch_buderus_heating.recordings.recordings_view",
        lambda _hass, _gateway: view,
    )
    engine = PerformanceHistory(hass, plant.coordinator)
    await engine.async_start(schedule=False)
    return plant, engine


async def test_heat_per_mode_from_the_counters_the_rest_from_the_recordings(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch,
) -> None:
    view = View(
        {
            "ch_heat": _span(FIRST, date(2026, 11, 14), 30.0),
            "ch_electricity": _span(FIRST, date(2026, 11, 14), 6.0),
            "total_heat": _span(FIRST, date(2026, 11, 14), 40.0),
            "total_electricity": _span(FIRST, date(2026, 11, 14), 10.0),
        }
    )
    plant, engine = await _engine(hass, freezer, monkeypatch, view)
    entity_ids = await _counter_statistics(hass, plant)
    await engine.async_refresh(full=True)
    assert (
        engine.counter_entities[("ch", "heat")]
        == entity_ids["gateway:heating_heat_produced"]
    )
    heating = engine.value(Period.YESTERDAY, "ch")
    assert heating is not None
    assert heating.heat == pytest.approx(24.0)  # counters, not the 30 recorded
    assert heating.electricity == pytest.approx(6.0)
    assert heating.heat_sources == {"counters"}
    assert heating.electricity_sources == {"local_recording"}
    total = engine.value(Period.YESTERDAY, "total")
    assert total is not None and total.heat == pytest.approx(40.0)
    assert total.heat_sources == {"local_recording"}
    # The counter's first day is incomplete: the recording stands in.
    assert FIRST not in engine._counted[("ch", "heat")]
    month = engine.value(Period.CURRENT_MONTH, "ch")
    assert month is not None
    assert month.heat_sources >= {"counters", "local_recording"}
    assert month.heat == pytest.approx(30.0 + 4 * 24.0)
    assert month.electricity == pytest.approx(5 * 6.0)
    assert month.missing_days == 1  # today, without a start of the day


async def test_without_recordings_the_counters_stand_in(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch,
) -> None:
    plant, engine = await _engine(hass, freezer, monkeypatch, None)
    await _counter_statistics(hass, plant)
    await engine.async_refresh(full=True)
    total = engine.value(Period.YESTERDAY, "total")
    assert total is not None
    assert total.heat == pytest.approx(48.0) and total.electricity == pytest.approx(
        12.0
    )
    assert total.heat_sources == total.electricity_sources == {"counters"}
    assert engine.report["statistics"] == "ok"
    # An hourly run reads the last days again and keeps the older ones.
    await engine.async_refresh()
    assert engine.value(Period.YESTERDAY, "total") == total
    assert date(2026, 11, 11) in engine._counted[("total", "heat")]


async def test_a_start_during_the_day_takes_the_statistic_at_midnight(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch,
) -> None:
    plant, engine = await _engine(hass, freezer, monkeypatch, None)
    await _counter_statistics(hass, plant)
    hours = 5 * 24
    heat_midnight = START["gateway:total_heat_produced"] + 2.0 * hours
    power_midnight = START["gateway:total_electricity"] + 0.5 * hours
    # Ten hours later the counters stand higher.
    counters(plant, heat_midnight + 30.0, power_midnight + 7.5, TOTAL)
    counters(plant, 1000.0 + hours + 15.0, 300.0 + hours * 0.25 + 3.0, CH)
    engine.sync()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert engine._baseline_method == "statistics", engine._lookup
    today = engine.value(Period.TODAY, "total")
    assert today is not None
    assert today.heat == pytest.approx(30.0)
    assert today.electricity == pytest.approx(7.5)
    assert today.ratio == 4.0
    assert engine.diagnostics()["today_start"] == "statistics"
