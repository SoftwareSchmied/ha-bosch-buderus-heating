"""X42: a hybrid system's heat pump keeps recordings of its own (synthetic).

On a hybrid system (a heat pump with a boiler, for example on an MX300) the
gateway's energy recordings (``/recordings/heatSources/emon/...``) count the
boiler, and the heat pump's own heat and compressor are recorded under its
heat source (``/recordings/heatSources/hs2/emon/<mode>/...``). They are read,
locally or from the cloud, only for such a plant; the heat pump's period
performance factors and environmental energy come from them first and from
its counters only where they are missing. A plant with a heat pump only does
not change. Not yet confirmed on a real hybrid system.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating import performance_history
from custom_components.bosch_buderus_heating.local_read import (
    ReadProblem,
    ReadResult,
    ReadState,
)
from custom_components.bosch_buderus_heating.performance import HEAT_PUMP_COUNTERS
from custom_components.bosch_buderus_heating.performance_history import (
    ELECTRICITY,
    HEAT,
    Period,
)
from custom_components.bosch_buderus_heating.performance_sensors import (
    PARTIAL_PERIOD,
    PerformanceFactorSensor,
)
from custom_components.bosch_buderus_heating.pointt import ResourceNotFound
from custom_components.bosch_buderus_heating.recordings import (
    Period as RecordingPeriod,
)
from custom_components.bosch_buderus_heating.recordings import (
    gating,
    recordings_view,
)
from custom_components.bosch_buderus_heating.recordings import (
    manager as manager_module,
)
from custom_components.bosch_buderus_heating.recordings.measures import (
    EntitySpec,
    entity_specs,
    measures,
    statistic_name,
)
from custom_components.bosch_buderus_heating.recordings.parse import Rate
from custom_components.bosch_buderus_heating.recordings.sensor import (
    RecordingPeriodSensor,
)
from custom_components.bosch_buderus_heating.recordings.series import (
    ALL_SERIES,
    BY_KEY,
    BY_PATH,
    CLOUD_PATHS,
    HEAT_SOURCE_SERIES,
    LOCAL_PATHS,
)
from custom_components.bosch_buderus_heating.recordings.sources import (
    Outcome,
    read_local,
)
from tests.test_derived_values import TOTAL, Plant, air
from tests.test_perf_hybrid_helpers import (
    HS1_TOTAL,
    HS2_CH,
    HS2_DHW,
    HS2_TOTAL,
    boiler_gateway,
)
from tests.test_performance import View, _at, _engine, _span, local
from tests.test_recordings_core import days, hours, recording
from tests.test_recordings_manager import (
    GW,
    Clock,
    Z,
    manager,
)

HS2_KEYS = frozenset(
    item.key for item in HEAT_SOURCE_SERIES if item.heat_source == "hs2"
)
HS2_HEAT = "/recordings/heatSources/hs2/emon/total/outputProduced"
HS2_COMPRESSOR = "/recordings/heatSources/hs2/emon/total/compressor"
GATEWAY_HEAT = "/recordings/heatSources/emon/total/outputProduced"


@pytest.fixture(autouse=True)
async def _stop_engines(hass: HomeAssistant) -> AsyncGenerator[None]:
    yield
    for engine in hass.data.get(performance_history._ENGINES, {}).values():
        engine.stop()


def _compressor_only(plant: Plant, path: str, heat: float, compressor: float) -> Plant:
    return plant.cloud(
        path, values=({"outputProduced": heat}, {"compressor": compressor})
    )


def _hybrid(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, at: datetime | None = None
) -> Plant:
    """An MX300 as in #138: the gateway counts the boiler, hs2 the heat pump."""
    plant = air(hass, freezer)
    if at is not None:
        _at(plant, at)
    boiler_gateway(plant, heat=900.0, burner=1100.0)
    _compressor_only(plant, HS2_TOTAL, 2607.64, 866.94)
    _compressor_only(plant, HS2_CH, 2000.0, 600.0)
    return _compressor_only(plant, HS2_DHW, 607.64, 266.94)


# ---------------------------------------------------------------- catalogue
def test_the_heat_pumps_own_series_and_measures() -> None:
    assert len({item.key for item in ALL_SERIES}) == len(ALL_SERIES)
    assert len({item.path for item in ALL_SERIES}) == len(ALL_SERIES)
    heat = BY_KEY["hs2_total_heat"]
    assert heat.path == HS2_HEAT
    assert (heat.mode, heat.kind, heat.heat_source) == (
        "total",
        "outputProduced",
        "hs2",
    )
    assert BY_PATH[HS2_COMPRESSOR].key == "hs2_total_compressor"
    # Heat and compressor per total, heating and hot water; no heater.
    assert sorted(HS2_KEYS) == sorted(
        f"hs2_{mode}_{kind}"
        for mode in ("total", "ch", "dhw")
        for kind in ("heat", "compressor")
    )
    assert not any("eheater" in item.key for item in HEAT_SOURCE_SERIES)
    # None of them is a path of the gateway's own series.
    assert not {item.path for item in HEAT_SOURCE_SERIES} & set(LOCAL_PATHS)
    assert not {item.path for item in HEAT_SOURCE_SERIES} & set(CLOUD_PATHS)
    found = {item.key: item for item in measures(HS2_KEYS)}
    electricity = found["hs2_total_electricity"]
    assert electricity.parts == ("hs2_total_compressor",)
    assert electricity.heat_source == "hs2"
    assert electricity.counter is None
    assert found["hs2_ch_heat"].kind == "heat"
    assert found["hs2_ch_heat"].counter is None
    # No entities: the factors use them, the import names their statistics.
    assert entity_specs(found.values()) == ()
    assert (
        statistic_name("en", BY_KEY["hs2_total_heat"])
        == "Heat pump \N{EN DASH} Total \N{EN DASH} Heat produced (system recording)"
    )
    assert (
        statistic_name("de", BY_KEY["hs2_dhw_compressor"])
        == "Wärmepumpe \N{EN DASH} Warmwasser \N{EN DASH} Strom Verdichter"
        " (Anlagenaufzeichnung)"
    )


def test_the_gateways_measures_are_unchanged() -> None:
    keys = ("total_heat", "total_compressor", "total_eheater", "ch_heat")
    found = measures(keys)
    assert [item.key for item in found] == [
        "ch_heat",
        "total_compressor",
        "total_eheater",
        "total_heat",
        "total_electricity",
    ]
    assert all(item.heat_source is None for item in found)
    assert len(entity_specs(found)) == 5 * 2 * 5
    # Mixed with a heat pump's series, the gateway's stay as they are.
    mixed = measures((*keys, *HS2_KEYS))
    assert [item for item in mixed if item.heat_source is None] == list(found)


# ------------------------------------------------------------------ gating
async def test_only_a_hybrid_heat_pump_with_counters_is_asked(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = _hybrid(hass, freezer)
    assert gating.heat_pump_source(plant.coordinator) == "hs2"
    assert gating.heat_pump_keys(plant.coordinator) == HS2_KEYS
    assert gating.heat_pump_source(None) is None


async def test_a_heat_pump_only_plant_is_not_asked(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    assert gating.heat_pump_source(plant.coordinator) is None
    # Counters under a heat source, but no burner share: nothing either.
    _compressor_only(plant, HS2_TOTAL, 2607.64, 866.94)
    assert gating.heat_pump_source(plant.coordinator) is None
    assert gating.heat_pump_keys(plant.coordinator) == frozenset()


async def test_a_burner_part_or_an_unknown_heat_source_is_not_asked(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = boiler_gateway(air(hass, freezer), heat=900.0, burner=1100.0)
    plant.cloud(
        HS2_TOTAL,
        values=({"outputProduced": 10.0}, {"compressor": 3.0}, {"burner": 5.0}),
    )
    assert gating.heat_pump_source(plant.coordinator) is None
    plant.coordinator.resources.pop(HS2_TOTAL)
    plant.coordinator.data.pop(HS2_TOTAL)
    # hs1 has no recordings of its own in the local catalogue.
    _compressor_only(plant, HS1_TOTAL, 2607.64, 866.94)
    assert gating.heat_pump_source(plant.coordinator) is None


# ------------------------------------------------------------ cloud reads
class Cloud:
    """The heat pump's series answer, the heating and hot water heat of the
    heat pump and the gateway's compressor and heater are missing."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.missing: set[str] = {
            "/recordings/heatSources/hs2/emon/ch/outputProduced",
            "/recordings/heatSources/emon/total/compressor",
            "/recordings/heatSources/emon/total/eheater",
        }

    async def get_recording(self, gateway_id: str, path: str, interval: str):
        assert gateway_id == GW
        self.calls.append((path, interval))
        if path in self.missing or (
            "/emon/" in path
            and path.endswith(("/compressor", "/eheater"))
            and "/hs2/" not in path
        ):
            raise ResourceNotFound(path, 404)
        value = 0.25 if path.endswith("/compressor") else 1.0
        if "/hs2/" not in path:
            value = 3.0  # the boiler's heat
        if len(interval) == 10:
            rows = [{"y": value, "c": 1}] * 24
        elif len(interval) == 7:
            rows = [{"y": value * 24, "c": 24}] * 30
        else:
            rows = [{"y": value * 700, "c": 1}] * 12
        return {"type": "yRecording", "unitOfMeasure": "kWh", "recording": rows}


def _as_hybrid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_module, "heat_pump_keys", lambda _c: HS2_KEYS)
    monkeypatch.setattr(manager_module, "heat_pump_source", lambda _c: "hs2")


async def test_a_hybrid_reads_the_heat_pumps_own_series_first(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as_hybrid(monkeypatch)
    clock = Clock(local(2026, 11, 22, 10, 20))
    cloud = Cloud()
    rec = await manager(hass, clock, (), cloud)
    await rec.async_run("start")
    asked = [path for path, _ in cloud.calls]
    hs2 = [path for path in asked if "/hs2/" in path]
    assert set(hs2) == {
        item.path for item in HEAT_SOURCE_SERIES if item.key in HS2_KEYS
    }
    # The heat pump's series come before the gateway's (they count the boiler).
    assert asked[: len(hs2)] == hs2
    assert {path for path in asked if "/hs2/" not in path} == set(CLOUD_PATHS)
    available = set(rec.available())
    assert {"hs2_total_heat", "hs2_total_compressor", "hs2_dhw_heat"} <= available
    assert "hs2_ch_heat" not in available
    # No entities for them; the view has their measures.
    assert not any(spec.measure.heat_source for spec in rec.specs())
    view = recordings_view(hass, GW)
    assert view is not None
    assert "hs2_total_electricity" in view.measure_keys()
    yesterday = date(2026, 11, 21)
    row = view.daily("hs2_total_electricity", yesterday, yesterday)
    assert len(row) == 1 and row[0].y == pytest.approx(24 * 0.25)
    heat = view.daily("hs2_total_heat", yesterday, yesterday)
    assert heat[0].y == pytest.approx(24.0)
    # Diagnostics: the heat source and the new series, ok or missing.
    report = rec.diagnostics()
    assert report["heat_pump_source"] == "hs2"
    series = report["series"]
    assert isinstance(series, dict)
    assert series["hs2_total_heat"]["cloud"] == "ok"
    assert series["hs2_total_compressor"]["cloud"] == "ok"
    assert series["hs2_ch_heat"]["cloud"] == "missing"
    # The back-fill of the heat pump's series comes first as well.
    release = view.acquire()
    later = rec._later_tasks(date(2026, 11, 22), clock())
    assert later and later[0][0].heat_source == "hs2"
    assert {rate for _, _, rate in later if rate is Rate.MONTH}
    release()


async def test_a_heat_pump_only_plant_reads_as_before(hass: HomeAssistant) -> None:
    clock = Clock(local(2026, 11, 22, 10, 20))
    cloud = Cloud()
    rec = await manager(hass, clock, (), cloud)
    await rec.async_run("start")
    assert {path for path, _ in cloud.calls} == set(CLOUD_PATHS)
    assert not any(key.startswith("hs") for key in rec.available())
    report = rec.diagnostics()
    assert "heat_pump_source" not in report
    assert not any(key.startswith("hs") for key in report["series"])  # type: ignore[union-attr]


# ------------------------------------------------------------- local reads
class Driver:
    """A local source: the heat pump's total series answer, all else 404."""

    def __init__(self, clock: Clock, answering: frozenset[str]) -> None:
        self.clock = clock
        self.answering = answering
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    async def read_explicit(self, request):
        self.requests.append(
            (request.path, request.definition.template, dict(request.query))
        )
        series = BY_PATH[request.path]
        if series.key not in self.answering:
            return ReadResult(request, ReadState.PATH_ERROR, 404, ReadProblem.NOT_FOUND)
        now = self.clock().astimezone(Z)
        current = now.replace(minute=0, second=0, microsecond=0)
        first = date(2026, 11, 1)
        value = 0.25 if series.kind == "compressor" else 1.0
        groups = [days(first, [(value * 24, 24)] * ((now.date() - first).days + 1))]
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=2
        )
        elapsed = int((current - start).total_seconds() // 3600) + 1
        groups.append(hours(start, [(value, 1)] * elapsed, end=current))
        return ReadResult(
            request,
            ReadState.SUCCESS,
            200,
            observation=SimpleNamespace(data=recording(*groups, unit="kWh")),
        )


async def test_the_local_access_reads_the_heat_pumps_series_first(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as_hybrid(monkeypatch)
    clock = Clock(local(2026, 11, 22, 10, 20))
    driver = Driver(clock, frozenset({"hs2_total_heat", "hs2_total_compressor"}))
    cloud = Cloud()
    rec = await manager(hass, clock, [driver], cloud)
    await rec.async_run("start")
    local_hs2 = [item for item in driver.requests if "/hs2/" in item[0]]
    # The concrete path through the template of the local data port.
    assert {path for path, _, _ in local_hs2} == {
        item.path for item in HEAT_SOURCE_SERIES if item.key in HS2_KEYS
    }
    assert {template for _, template, _ in local_hs2} == {
        item.path.replace("/hs2/", "/{heatSourceId}/")
        for item in HEAT_SOURCE_SERIES
        if item.key in HS2_KEYS
    }
    assert len(driver.requests) == len(LOCAL_PATHS) + len(HS2_KEYS)
    assert rec.local_paths[HS2_HEAT][0] == "ok"
    # The cloud only for the heat pump's series the local access lacks.
    asked = {path for path, _ in cloud.calls}
    assert HS2_HEAT not in asked and HS2_COMPRESSOR not in asked
    assert asked == {
        item.path
        for item in HEAT_SOURCE_SERIES
        if item.key in HS2_KEYS and item.mode != "total"
    }
    assert {"hs2_total_heat", "hs2_total_compressor"} <= set(rec.available())


async def test_a_heat_pump_only_plant_reads_locally_as_before(
    hass: HomeAssistant,
) -> None:
    clock = Clock(local(2026, 11, 22, 10, 20))
    driver = Driver(clock, frozenset({"total_heat"}))
    cloud = Cloud()
    rec = await manager(hass, clock, [driver], cloud)
    await rec.async_run("start")
    assert len(driver.requests) == len(LOCAL_PATHS)
    assert not any("/hs2/" in path for path, _, _ in driver.requests)
    assert cloud.calls == []


async def test_read_local_fills_the_heat_source_of_the_template() -> None:
    seen: list[Any] = []

    class One:
        async def read_explicit(self, request):
            seen.append(request)
            return None

    answer = await read_local(One(), BY_KEY["hs2_ch_compressor"], {"sampleRate": "all"})
    assert answer.outcome is Outcome.UNREACHABLE
    assert seen[0].path == "/recordings/heatSources/hs2/emon/ch/compressor"
    assert (
        seen[0].definition.template
        == "/recordings/heatSources/{heatSourceId}/emon/ch/compressor"
    )


# ------------------------------------------------------- performance factors
def _factor(plant: Plant, engine: Any, period: Period, mode: str = "total"):
    sensor = PerformanceFactorSensor(plant.coordinator, engine, period, mode)
    sensor._refresh()
    return sensor


async def test_hybrid_factors_come_from_the_heat_pumps_recordings(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    plant = _hybrid(hass, freezer, local(2026, 11, 22, 12, 0))
    yesterday = date(2026, 11, 21)
    view = View(
        {
            # The boiler's heat: never part of the heat pump's factors.
            "total_heat": _span(date(2026, 11, 1), yesterday, 100.0),
            "hs2_total_heat": _span(date(2026, 11, 1), yesterday, 20.0),
            "hs2_total_electricity": _span(date(2026, 11, 1), yesterday, 5.0),
            "hs2_ch_heat": _span(date(2026, 11, 1), yesterday, 12.0),
            "hs2_ch_electricity": _span(date(2026, 11, 1), yesterday, 3.0),
        },
        months={
            "hs2_total_heat": {date(2026, 10, 1): 600.0},
            "hs2_total_electricity": {date(2026, 10, 1): 200.0},
        },
        source="cloud",
    )
    monkeypatch.setattr(
        "custom_components.bosch_buderus_heating.recordings.recordings_view",
        lambda _hass, _gateway: view,
    )
    engine = await _engine(plant)
    await engine.async_refresh(full=True)
    assert engine._heat_pump_recording_keys()[("hp:total", HEAT)] == "hs2_total_heat"
    # The counters' statistics of a few days differ: the recordings win.
    for mode, heat, power in (("hp:total", 30.0, 10.0), ("hp:ch", 9.0, 3.0)):
        engine._counted[(mode, HEAT)] = _span(date(2026, 11, 19), yesterday, heat)
        engine._counted[(mode, ELECTRICITY)] = _span(
            date(2026, 11, 19), yesterday, power
        )
    engine._first.clear()
    month = _factor(plant, engine, Period.CURRENT_MONTH)
    assert month.native_value == pytest.approx(4.0)
    attributes = month.extra_state_attributes or {}
    assert attributes["basis"] == HEAT_PUMP_COUNTERS
    assert attributes["heat_kwh"] == pytest.approx(21 * 20.0)
    assert attributes["electricity_kwh"] == pytest.approx(21 * 5.0)
    assert attributes["heat_source"] == "cloud_recording"
    assert attributes["environmental_energy_kwh"] == pytest.approx(21 * 15.0)
    # 21 of the 22 days so far (today has no start of the day yet).
    assert attributes["coverage_percent"] == pytest.approx(95.5)
    assert "reason" not in attributes
    assert attributes["derived_from"] == {
        HS2_COMPRESSOR: "recording",
        HS2_HEAT: "recording",
    }
    assert GATEWAY_HEAT not in attributes["derived_from"]
    # Heating: the heat pump's recorded heat per mode first, too.
    heating = _factor(plant, engine, Period.CURRENT_MONTH, "ch")
    assert heating.native_value == pytest.approx(4.0)
    # The previous month from the plant's month value.
    previous = _factor(plant, engine, Period.PREVIOUS_MONTH)
    assert previous.native_value == pytest.approx(3.0)
    previous_attributes = previous.extra_state_attributes or {}
    assert previous_attributes["coverage_percent"] == pytest.approx(100.0)
    assert previous_attributes["environmental_energy_kwh"] == pytest.approx(400.0)
    year = _factor(plant, engine, Period.CURRENT_YEAR)
    assert year.native_value == pytest.approx((600.0 + 420.0) / (200.0 + 105.0), 0.01)
    assert _factor(plant, engine, Period.YESTERDAY).native_value == pytest.approx(4.0)


async def test_hybrid_fallback_to_the_counters_needs_half_of_the_period(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    plant = _hybrid(hass, freezer, local(2026, 11, 22, 12, 0))
    yesterday = date(2026, 11, 21)
    # No recording of the heat pump: only the gateway's (the boiler's).
    view = View({"total_heat": _span(date(2026, 11, 1), yesterday, 100.0)})
    monkeypatch.setattr(
        "custom_components.bosch_buderus_heating.recordings.recordings_view",
        lambda _hass, _gateway: view,
    )
    engine = await _engine(plant)
    await engine.async_refresh(full=True)
    engine._counted[("hp:total", HEAT)] = _span(date(2026, 11, 17), yesterday, 6.0)
    engine._counted[("hp:total", ELECTRICITY)] = _span(
        date(2026, 11, 17), yesterday, 2.0
    )
    engine._first.clear()
    month = _factor(plant, engine, Period.CURRENT_MONTH)
    # #138: 5 of 22 days are too few for a month.
    assert month.native_value is None
    attributes = month.extra_state_attributes or {}
    assert attributes["reason"] == PARTIAL_PERIOD
    assert attributes["coverage_percent"] == pytest.approx(22.7)
    assert attributes["heat_source"] == "counters"
    assert "environmental_energy_kwh" not in attributes
    year = _factor(plant, engine, Period.CURRENT_YEAR)
    assert year.native_value is None
    assert (year.extra_state_attributes or {})["reason"] == PARTIAL_PERIOD
    # Yesterday is one whole day: shown.
    day = _factor(plant, engine, Period.YESTERDAY)
    assert day.native_value == pytest.approx(3.0)
    assert (day.extra_state_attributes or {})["coverage_percent"] == pytest.approx(
        100.0
    )
    # Half of the days so far (11 of 22): shown.
    engine._counted[("hp:total", HEAT)] = _span(date(2026, 11, 11), yesterday, 6.0)
    engine._counted[("hp:total", ELECTRICITY)] = _span(
        date(2026, 11, 11), yesterday, 2.0
    )
    engine._first.clear()
    month._refresh()
    assert month.native_value == pytest.approx(3.0)
    attributes = month.extra_state_attributes or {}
    assert attributes["coverage_percent"] == pytest.approx(50.0)
    assert attributes["environmental_energy_kwh"] == pytest.approx(11 * 4.0)
    assert "reason" not in attributes or attributes["reason"] != PARTIAL_PERIOD


async def test_no_environmental_energy_from_a_negative_difference(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    plant = _hybrid(hass, freezer, local(2026, 11, 22, 12, 0))
    yesterday = date(2026, 11, 21)
    # Standby only: more electricity than heat (a measuring artifact).
    view = View(
        {
            "hs2_total_heat": {yesterday: 0.5},
            "hs2_total_electricity": {yesterday: 1.0},
        },
        source="cloud",
    )
    monkeypatch.setattr(
        "custom_components.bosch_buderus_heating.recordings.recordings_view",
        lambda _hass, _gateway: view,
    )
    engine = await _engine(plant)
    await engine.async_refresh(full=True)
    day = _factor(plant, engine, Period.YESTERDAY)
    assert day.native_value == pytest.approx(0.5)
    attributes = day.extra_state_attributes or {}
    assert attributes["heat_kwh"] == pytest.approx(0.5)
    assert "environmental_energy_kwh" not in attributes


async def test_a_heat_pump_only_plants_factors_are_unchanged(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    plant = air(hass, freezer)
    _at(plant, local(2026, 11, 22, 12, 0))
    yesterday = date(2026, 11, 21)
    # Few days of the counters only: shown as before, with no new attribute.
    view = View({})
    monkeypatch.setattr(
        "custom_components.bosch_buderus_heating.recordings.recordings_view",
        lambda _hass, _gateway: view,
    )
    engine = await _engine(plant)
    await engine.async_refresh(full=True)
    assert engine._heat_pump_recording_keys() == {}
    engine._counted[("total", HEAT)] = _span(date(2026, 11, 20), yesterday, 8.0)
    engine._counted[("total", ELECTRICITY)] = _span(date(2026, 11, 20), yesterday, 2.0)
    engine._first.clear()
    month = _factor(plant, engine, Period.CURRENT_MONTH)
    assert month.native_value == pytest.approx(4.0)
    attributes = month.extra_state_attributes or {}
    assert "coverage_percent" not in attributes
    assert "environmental_energy_kwh" not in attributes
    assert "basis" not in attributes
    assert attributes.get("reason") != PARTIAL_PERIOD
    # The gateway's recordings are still named for a plant with them.
    view.days = {
        "total_heat": _span(date(2026, 11, 1), yesterday, 40.0),
        "total_electricity": _span(date(2026, 11, 1), yesterday, 10.0),
    }
    await engine.async_refresh(full=True)
    month._refresh()
    assert month.native_value == pytest.approx(4.0)
    derived = (month.extra_state_attributes or {})["derived_from"]
    assert derived[GATEWAY_HEAT] == "recording"
    assert not any("/hs2/" in path for path in derived)


# ------------------------------------------------ the gateway's heat entities
def _period_sensor(plant: Plant, key: str) -> RecordingPeriodSensor:
    measure = next(item for item in measures((key,)) if item.key == key)
    spec = EntitySpec(measure, RecordingPeriod.YESTERDAY, measure.primary)
    stub = SimpleNamespace(
        coordinator=lambda: plant.coordinator,
        shown=lambda _spec: None,
        counter_entities={},
    )
    return RecordingPeriodSensor(plant.coordinator, stub, spec)  # type: ignore[arg-type]


async def test_the_gateways_heat_is_the_boilers_on_a_hybrid(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = _hybrid(hass, freezer)
    attributes = _period_sensor(plant, "total_heat").extra_state_attributes
    assert attributes["heat_generator"] == "boiler"
    assert "heat_generator" not in (
        _period_sensor(plant, "total_compressor").extra_state_attributes
    )


async def test_the_gateways_heat_has_no_generator_on_a_heat_pump_only_plant(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    attributes = _period_sensor(plant, "total_heat").extra_state_attributes
    assert attributes == {"method": "recording"}
    assert plant.coordinator.resources.get(TOTAL) is not None
