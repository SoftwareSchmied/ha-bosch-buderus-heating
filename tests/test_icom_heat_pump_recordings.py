"""R7: a heat pump behind an iCom gateway reads its own energy recordings.

An iCom gateway whose plant kind is ``heat_pump`` (from ``hs1/type``) is
asked for the heat produced, the compressor and the electric heater under
the energy monitoring instead of the boiler series; an iCom boiler, a plant
without a plant kind and every other gateway read as before. The solar
yield is read for the diagnostics only (state and unit). The consumed energy
of the energy monitoring is asked as well: a measure of its own, disabled by
default and never a part of the electricity. Energy units in any case and kW
(samples of the mean power) are taken. Synthetic answers; not yet confirmed
on a real iCom heat pump (#467).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.pointt import (
    Resource,
    ResourceMetadata,
    ResourceNotFound,
)
from custom_components.bosch_buderus_heating.recordings import gating
from custom_components.bosch_buderus_heating.recordings.manager import (
    _MANAGERS,
    RECHECK_MISSING,
    RecordingsManager,
)
from custom_components.bosch_buderus_heating.recordings.measures import (
    entity_name,
    entity_specs,
    measures,
    statistic_name,
)
from custom_components.bosch_buderus_heating.recordings.series import (
    BY_KEY,
    CLOUD_PATHS,
    ICOM_HEAT_PUMP_SERIES,
    SOLAR_YIELD,
)
from tests.test_plant_detection import load_case
from tests.test_plant_inputs import GATEWAY, _start
from tests.test_plant_inputs import _coordinator as plant_coordinator
from tests.test_recordings_core import local
from tests.test_recordings_manager import Clock

ANCHOR = "/heatSources/numberOfStarts"
MONITORING = {item.key: item.path for item in ICOM_HEAT_PUMP_SERIES}
BOILER = {"ch_actual_power", "dhw_actual_power", "total_consumed"}
CONSUMED = BY_KEY["total_consumed"].path
HEAT = "/recordings/heatSources/total/energyMonitoring/outputProduced"
NOW = local(2026, 11, 22, 10) + timedelta(minutes=20)


def recording(
    interval: str, value: float, count: float = 1, unit: str = "kWh"
) -> dict[str, Any]:
    if len(interval) == 10:
        rows = [{"y": value, "c": count}] * 24
    elif len(interval) == 7:
        rows = [{"y": value * 24, "c": 24}] * 30
    else:
        rows = [{"y": value * 700, "c": 1}] * 12
    return {"type": "yRecording", "unitOfMeasure": unit, "recording": rows}


async def plant(hass: HomeAssistant, case: str) -> Any:
    """A real coordinator with the plant model of a case and a reading the
    recordings belong to (the starts of the heat source)."""
    coordinator = plant_coordinator(hass, load_case(case))
    coordinator.resources[ANCHOR] = Resource(
        ANCHOR,
        value=10,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue"),
    )
    # Cloud only: no local source runs.
    coordinator.local_selection.running_sources = lambda: ()
    await _start(hass, coordinator)
    return coordinator


async def recordings(
    hass: HomeAssistant,
    coordinator: Any,
    clock: Clock,
    answers: dict[str, Callable[[str], Any]],
) -> tuple[RecordingsManager, list[tuple[str, str]]]:
    calls: list[tuple[str, str]] = []

    async def get_recording(gateway_id: str, path: str, interval: str) -> Any:
        assert gateway_id == GATEWAY
        calls.append((path, interval))
        if path not in answers:
            raise ResourceNotFound(path, 404)
        return answers[path](interval)

    coordinator.client.get_recording = AsyncMock(side_effect=get_recording)
    await hass.config.async_update(time_zone="Europe/Berlin")
    manager = RecordingsManager(hass, coordinator.config_entry, GATEWAY, now=clock)
    manager.attach(coordinator)
    manager._test_coordinator = coordinator  # type: ignore[attr-defined]
    hass.data.setdefault(_MANAGERS, {})[
        (coordinator.config_entry.entry_id, GATEWAY)
    ] = manager
    await manager.async_start(schedule=False)
    return manager, calls


def keep_reading(manager: RecordingsManager) -> None:
    """An enabled entity needs the heat: the hourly runs read."""
    spec = next(
        spec for spec in manager.specs() if spec.measure.key == "monitoring_total_heat"
    )
    manager.request(spec.key)


def heat_pump_answers(unit: str = "kWh") -> dict[str, Callable[[str], Any]]:
    return {
        HEAT: lambda interval: recording(interval, 3.0, unit=unit),
        MONITORING["monitoring_total_compressor"]: lambda i: recording(i, 0.75),
        MONITORING["monitoring_total_eheater"]: lambda i: recording(i, 0.25),
        SOLAR_YIELD.path: lambda i: recording(i, 0.5),
    }


# ---------------------------------------------------------------- catalogue


def test_the_series_and_measures_of_an_icom_heat_pump() -> None:
    assert set(MONITORING) == {
        "monitoring_total_heat",
        "monitoring_total_compressor",
        "monitoring_total_eheater",
    }
    assert all(
        item.cloud_only and item.minute_sums and item.mode == "total"
        for item in ICOM_HEAT_PUMP_SERIES
    )
    assert MONITORING["monitoring_total_heat"] == HEAT
    found = {item.key: item for item in measures(MONITORING)}
    assert set(found) == {*MONITORING, "monitoring_total_electricity"}
    assert found["monitoring_total_heat"].kind == "heat"
    assert found["monitoring_total_electricity"].parts == (
        "monitoring_total_compressor",
        "monitoring_total_eheater",
    )
    # No counter of the integration: no (calculated) companion.
    assert all(item.counter is None for item in found.values())
    specs = entity_specs(found.values())
    assert len(specs) == 4 * 5
    enabled = {spec.measure.key for spec in specs if spec.enabled_default}
    assert enabled == {"monitoring_total_heat", "monitoring_total_electricity"}
    names = {entity_name("en", spec) for spec in specs}
    assert "Total \N{EN DASH} Heat produced yesterday (system recording)" in names
    assert "Total \N{EN DASH} Electricity this year (system recording)" in names
    keys = {spec.key for spec in specs}
    assert "heatSources:total:energyMonitoring:outputProduced:yesterday" in keys
    assert "heatSources:total:energyMonitoring:electricity:current_year" in keys
    assert statistic_name("de", BY_KEY["monitoring_total_eheater"]) == (
        "Gesamt \N{EN DASH} Strom Heizstab (Anlagenaufzeichnung)"
    )
    # The solar yield is read for the diagnostics only.
    assert SOLAR_YIELD.diagnostic
    assert measures((SOLAR_YIELD.key,)) == ()


# ------------------------------------------------------------------- gating


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("issue_467_icom_heat_pump", {*MONITORING, SOLAR_YIELD.key, "total_consumed"}),
        # An iCom boiler and an iCom plant without a plant kind: as before.
        ("issue_104_icom_boiler", BOILER),
        ("issue_467_icom_without_type", BOILER),
        # A K40 heat pump without energy monitoring: as before.
        ("k40_own", BOILER),
    ],
)
async def test_the_series_each_plant_is_asked_for(
    hass: HomeAssistant, case: str, expected: set[str]
) -> None:
    coordinator = await plant(hass, case)
    assert gating.allowed_keys(coordinator) == expected - {"water"}
    assert gating.icom_heat_pump(coordinator) is (case == "issue_467_icom_heat_pump")
    await coordinator.async_shutdown()


async def test_without_a_reading_the_recordings_belong_to_nothing_is_asked(
    hass: HomeAssistant,
) -> None:
    coordinator = await plant(hass, "issue_467_icom_heat_pump")
    del coordinator.resources[ANCHOR]
    assert gating.icom_heat_pump(coordinator)
    assert gating.allowed_keys(coordinator) == frozenset()
    await coordinator.async_shutdown()


# --------------------------------------------------------------------- reads


async def test_an_icom_heat_pump_reads_its_energy_and_no_boiler_series(
    hass: HomeAssistant,
) -> None:
    coordinator = await plant(hass, "issue_467_icom_heat_pump")
    clock = Clock(NOW)
    manager, calls = await recordings(hass, coordinator, clock, heat_pump_answers())
    await manager.async_run("start")
    asked = {path for path, _ in calls}
    assert asked == {*CLOUD_PATHS, *MONITORING.values(), SOLAR_YIELD.path, CONSUMED}
    assert not asked & {BY_KEY[key].path for key in BOILER - {"total_consumed"}}
    assert set(manager.available()) == set(MONITORING)
    specs = {spec.measure.key for spec in manager.specs()}
    assert specs == {*MONITORING, "monitoring_total_electricity"}
    yesterday = manager.daily(
        "monitoring_total_heat",
        NOW.date() - timedelta(days=1),
        NOW.date() - timedelta(days=1),
    )
    assert yesterday and yesterday[0].y == pytest.approx(24 * 3.0)
    report = manager.diagnostics()
    assert report["icom_heat_pump"] is True
    series = report["series"]
    assert series["monitoring_total_heat"]["cloud"] == "ok"  # type: ignore[index]
    assert series[SOLAR_YIELD.key] == {"local": None, "cloud": "ok", "unit": "kWh"}  # type: ignore[index]
    assert series["total_consumed"]["cloud"] == "missing"  # type: ignore[index]

    # The solar yield answered: it is not read again.
    keep_reading(manager)
    clock.now += timedelta(hours=1)
    calls.clear()
    await manager.async_run("hourly")
    assert HEAT in {path for path, _ in calls}
    assert SOLAR_YIELD.path not in {path for path, _ in calls}
    await coordinator.async_shutdown()


async def test_an_unknown_unit_gives_no_entity_and_shows_the_unit(
    hass: HomeAssistant,
) -> None:
    coordinator = await plant(hass, "issue_467_icom_heat_pump")
    clock = Clock(NOW)
    manager, calls = await recordings(
        hass, coordinator, clock, heat_pump_answers(unit="unit")
    )
    await manager.async_run("start")
    assert "monitoring_total_heat" not in manager.available()
    assert "monitoring_total_heat" not in {spec.measure.key for spec in manager.specs()}
    series = manager.diagnostics()["series"]
    assert series["monitoring_total_heat"] == {
        "local": None,
        "cloud": "unit",
        "unit": "unit",
    }  # type: ignore[index]
    # Kept over a restart, and not asked again.
    data = manager._data()
    assert data["units"] == {HEAT: "unit", SOLAR_YIELD.path: "kWh"}
    spec = next(
        spec
        for spec in manager.specs()
        if spec.measure.key == "monitoring_total_compressor"
    )
    manager.request(spec.key)
    clock.now += timedelta(days=2)
    calls.clear()
    await manager.async_run("hourly")
    asked = {path for path, _ in calls}
    assert MONITORING["monitoring_total_compressor"] in asked
    assert HEAT not in asked
    await coordinator.async_shutdown()


async def test_an_unsafe_unit_text_is_shown_as_other(hass: HomeAssistant) -> None:
    coordinator = await plant(hass, "issue_467_icom_heat_pump")
    answers = heat_pump_answers()
    answers[HEAT] = lambda interval: recording(interval, 1.0, unit="kWh<script>")
    manager, _calls = await recordings(hass, coordinator, Clock(NOW), answers)
    await manager.async_run("start")
    assert manager.diagnostics()["series"]["monitoring_total_heat"]["unit"] == "other"  # type: ignore[index]
    manager._load({"units": {HEAT: "kWh<script>", "/not/a/recording": "kWh"}})
    assert manager.unit_texts == {}
    await coordinator.async_shutdown()


async def test_a_missing_solar_yield_is_asked_again_once_a_day(
    hass: HomeAssistant,
) -> None:
    coordinator = await plant(hass, "issue_467_icom_heat_pump")
    answers = heat_pump_answers()
    del answers[SOLAR_YIELD.path]
    clock = Clock(NOW)
    manager, calls = await recordings(hass, coordinator, clock, answers)
    await manager.async_run("start")
    assert manager.diagnostics()["series"][SOLAR_YIELD.key]["cloud"] == "missing"  # type: ignore[index]
    keep_reading(manager)
    clock.now += timedelta(hours=1)
    calls.clear()
    await manager.async_run("hourly")
    assert SOLAR_YIELD.path not in {path for path, _ in calls}
    assert HEAT in {path for path, _ in calls}
    clock.now += RECHECK_MISSING
    calls.clear()
    await manager.async_run("hourly")
    assert [path for path, _ in calls].count(SOLAR_YIELD.path) == 1
    await coordinator.async_shutdown()


async def test_minute_sums_of_the_energy_monitoring_are_read_as_hours(
    hass: HomeAssistant,
) -> None:
    coordinator = await plant(hass, "issue_467_icom_heat_pump")
    answers = heat_pump_answers()
    answers[HEAT] = lambda interval: recording(interval, 60.0, count=60)
    manager, _calls = await recordings(hass, coordinator, Clock(NOW), answers)
    await manager.async_run("start")
    day = NOW.date() - timedelta(days=1)
    assert manager.daily("monitoring_total_heat", day, day)[0].y == pytest.approx(24.0)
    await coordinator.async_shutdown()


async def test_an_icom_boiler_reads_as_before(hass: HomeAssistant) -> None:
    coordinator = await plant(hass, "issue_104_icom_boiler")
    manager, calls = await recordings(
        hass, coordinator, Clock(NOW), heat_pump_answers()
    )
    await manager.async_run("start")
    asked = {path for path, _ in calls}
    assert asked == {*CLOUD_PATHS, *(BY_KEY[key].path for key in BOILER)}
    assert "icom_heat_pump" not in manager.diagnostics()
    await coordinator.async_shutdown()


async def test_with_the_import_on_the_solar_yield_is_still_read_once(
    hass: HomeAssistant,
) -> None:
    """The import reads every series every hour; the solar yield is read for
    the diagnostics only: one day, never imported."""
    from custom_components.bosch_buderus_heating.recordings.manager import (
        OPTION_IMPORT,
    )

    coordinator = await plant(hass, "issue_467_icom_heat_pump")
    hass.config_entries.async_update_entry(
        coordinator.config_entry, options={OPTION_IMPORT: True}
    )
    clock = Clock(NOW)
    manager, calls = await recordings(hass, coordinator, clock, heat_pump_answers())
    await manager.async_run("start")
    solar = [interval for path, interval in calls if path == SOLAR_YIELD.path]
    assert solar == [(NOW.date() - timedelta(days=1)).isoformat()]
    assert SOLAR_YIELD.key not in manager.ledgers
    clock.now += timedelta(hours=1)
    calls.clear()
    await manager.async_run("hourly")
    assert SOLAR_YIELD.path not in {path for path, _ in calls}
    assert HEAT in {path for path, _ in calls}
    await coordinator.async_shutdown()


# ------------------------------------------- consumed energy, units, stored


def consumed_answers(unit: str = "kW") -> dict[str, Callable[[str], Any]]:
    """The heat pump's answers plus the consumed energy, minute samples of a
    mean of 1 kW (an hour of 60 samples summing to 60)."""
    answers = heat_pump_answers()
    answers[CONSUMED] = lambda interval: recording(interval, 60.0, 60, unit=unit)
    return answers


async def test_an_icom_heat_pump_reads_its_consumed_energy_on_its_own(
    hass: HomeAssistant,
) -> None:
    coordinator = await plant(hass, "issue_467_icom_heat_pump")
    manager, calls = await recordings(hass, coordinator, Clock(NOW), consumed_answers())
    await manager.async_run("start")
    assert CONSUMED in {path for path, _ in calls}
    assert set(manager.available()) == {*MONITORING, "total_consumed"}
    day = NOW.date() - timedelta(days=1)
    assert manager.daily("total_consumed", day, day)[0].y == pytest.approx(24.0)
    specs = {spec.measure.key: spec for spec in manager.specs()}
    consumed = [
        spec for spec in manager.specs() if spec.measure.key == "total_consumed"
    ]
    assert len(consumed) == 5
    assert not any(spec.enabled_default for spec in consumed)
    assert consumed[0].measure.counter is None
    assert "heatSources:total:energyMonitoring:consumedEnergy:yesterday" in {
        spec.key for spec in consumed
    }
    # The electricity stays compressor plus heater: nothing counted twice.
    assert specs["monitoring_total_electricity"].measure.parts == (
        "monitoring_total_compressor",
        "monitoring_total_eheater",
    )
    assert manager.diagnostics()["series"]["total_consumed"]["cloud"] == "ok"  # type: ignore[index]
    await coordinator.async_shutdown()


@pytest.mark.parametrize("unit", ["kW", "KWh", "kwh"])
async def test_the_units_the_energy_monitoring_answers_in_are_taken(
    hass: HomeAssistant, unit: str
) -> None:
    coordinator = await plant(hass, "issue_467_icom_heat_pump")
    answers = heat_pump_answers(unit=unit)
    manager, _calls = await recordings(hass, coordinator, Clock(NOW), answers)
    await manager.async_run("start")
    assert "monitoring_total_heat" in manager.available()
    series = manager.diagnostics()["series"]["monitoring_total_heat"]  # type: ignore[index]
    assert series["cloud"] == "ok" and "unit" not in series  # type: ignore[index]
    day = NOW.date() - timedelta(days=1)
    assert manager.daily("monitoring_total_heat", day, day)[0].y == pytest.approx(72.0)
    await coordinator.async_shutdown()


async def test_an_icom_boiler_does_not_take_a_unit_of_power(
    hass: HomeAssistant,
) -> None:
    """The boiler's energy is no series of mean samples: kW stays unknown."""
    coordinator = await plant(hass, "issue_104_icom_boiler")
    ch = BY_KEY["ch_actual_power"].path
    answers = {ch: lambda interval: recording(interval, 2.0, unit="kW")}
    manager, _calls = await recordings(hass, coordinator, Clock(NOW), answers)
    await manager.async_run("start")
    assert "ch_actual_power" not in manager.available()
    series = manager.diagnostics()["series"]["ch_actual_power"]  # type: ignore[index]
    assert series == {"local": None, "cloud": "unit", "unit": "kW"}
    await coordinator.async_shutdown()


async def test_a_unit_left_out_by_an_earlier_version_is_asked_again(
    hass: HomeAssistant,
) -> None:
    """Earlier versions kept no unit text: such a recording is asked once
    more; one whose unit is taken now as well; an unknown one is not."""
    coordinator = await plant(hass, "issue_467_icom_heat_pump")
    clock = Clock(NOW)
    answers = consumed_answers()
    manager, calls = await recordings(hass, coordinator, clock, answers)
    eheater = MONITORING["monitoring_total_eheater"]
    compressor = MONITORING["monitoring_total_compressor"]
    at = (NOW - timedelta(days=3)).timestamp()
    stored = {
        "cloud": {
            CONSUMED: ["unit", at],
            HEAT: ["unit", at],
            eheater: ["unit", at],
            compressor: ["unit", at],
        },
        "units": {HEAT: "kW", eheater: "unit"},
    }
    manager._load(stored)
    assert set(manager.cloud_paths) == {eheater}
    calls.clear()
    await manager.async_run("start")
    asked = {path for path, _ in calls}
    assert {CONSUMED, HEAT, compressor} <= asked
    assert eheater not in asked
    assert "total_consumed" in manager.available()
    # Once checked, a recording without a unit text is not asked again.
    data = manager._data()
    assert data["units_rechecked"] is True
    data["cloud"][CONSUMED] = ["unit", at]
    manager._load(data)
    assert manager.cloud_paths[CONSUMED][0] == "unit"
    await coordinator.async_shutdown()


async def test_months_of_an_earlier_version_are_corrected_when_loaded(
    hass: HomeAssistant,
) -> None:
    coordinator = await plant(hass, "issue_467_icom_heat_pump")
    manager, _calls = await recordings(
        hass, coordinator, Clock(NOW), consumed_answers()
    )
    manager._load(
        {
            "series": {
                "total_consumed": {
                    "months": {"2026-09-01": [43200.0, 43200, "cloud", 2, True]}
                },
                "total_heat": {
                    "months": {"2026-09-01": [43200.0, 43200, "cloud", 2, True]}
                },
            }
        }
    )
    consumed = manager.ledgers["total_consumed"]
    assert consumed.months["2026-09-01"][:2] == [pytest.approx(720.0), 720.0]
    assert manager.ledgers["total_heat"].months["2026-09-01"][:2] == [43200.0, 43200]
    events = manager.diagnostics()["series"]["total_consumed"]["events"]  # type: ignore[index]
    assert events == {"months_corrected": 1}
    await coordinator.async_shutdown()


async def test_the_hour_still_running_counts_the_minutes_elapsed(
    hass: HomeAssistant,
) -> None:
    """Today's recording is read with its read time: the hour still running
    (20 samples at 10:20) is the mean times a third of an hour."""
    coordinator = await plant(hass, "issue_467_icom_heat_pump")
    answers = heat_pump_answers()
    today = NOW.date().isoformat()

    def heat(interval: str) -> dict[str, Any]:
        if interval != today:
            return recording(interval, 3.0)
        rows = [{"y": 30.0, "c": 60}] * 10 + [{"y": 10.0, "c": 20}]
        rows += [{"y": 1.0, "c": 0}] * 13
        return {"type": "yRecording", "unitOfMeasure": "kW", "recording": rows}

    answers[HEAT] = heat
    clock = Clock(NOW)
    manager, calls = await recordings(hass, coordinator, clock, answers)
    await manager.async_run("start")
    spec = next(
        spec
        for spec in manager.specs()
        if spec.measure.key == "monitoring_total_heat"
        and spec.period.value == "current_month"
    )
    manager.request(spec.key)
    calls.clear()
    await manager.async_run("hourly")
    assert (HEAT, today) in calls
    running = int(NOW.replace(minute=0).timestamp())
    cell = manager.ledgers["monitoring_total_heat"].hours[str(running)]
    assert cell[0] == pytest.approx(0.5 / 3)
    assert cell[1] == 1.0
    await coordinator.async_shutdown()
