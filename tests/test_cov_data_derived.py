"""Calculated values: saved state, inputs that are not usable, activity of
boilers and cooling, defrosts that are only partly measurable, the net heat
counters over restarts and resets."""

from __future__ import annotations

import asyncio
import itertools
import logging
import math
from collections.abc import AsyncGenerator
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.sensor import SensorExtraStoredData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from custom_components.bosch_buderus_heating import derived_defrost
from custom_components.bosch_buderus_heating import derived_store as store_module
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.derived_activity import (
    CENTRAL_HEATING_STATUS,
    HEAT_SOURCE_ACTIVE,
    circuit_activity,
    defrost_state,
)
from custom_components.bosch_buderus_heating.derived_defrost import (
    ACTIVE_KEY,
    HEATER_POWER,
    SETTLE,
    DefrostActiveBinarySensor,
    DefrostMonitor,
    LastDefrostSensor,
    track_defrost_entities,
)
from custom_components.bosch_buderus_heating.derived_inputs import (
    Input,
    energy_part,
    number,
    runtime_hours,
    starts,
)
from custom_components.bosch_buderus_heating.derived_sensors import (
    CircuitActivitySensor,
    NetHeatCounterSensor,
    _restored,
)
from custom_components.bosch_buderus_heating.derived_store import (
    MAX_ROWS,
    DerivedStore,
    derived_store,
    stored_number,
)
from custom_components.bosch_buderus_heating.heat_source_kind import DEFROST_SIGNAL
from custom_components.bosch_buderus_heating.pointt import (
    Resource,
    ResourceMetadata,
)
from tests.ha_test_helpers import MockConfigEntry, flush_store
from tests.test_derived_values import (
    _COORDINATORS,
    CH,
    COMPRESSOR,
    COOLING,
    DHW,
    MODE,
    POWER,
    PUMP,
    REFRIGERANT,
    ROOT,
    SUWI,
    T0,
    TOTAL,
    VW1,
    Plant,
    _cycle,
    _monitor,
    air,
    brine,
)


@pytest.fixture(autouse=True)
async def _stop_coordinators() -> AsyncGenerator[None]:
    yield
    while _COORDINATORS:
        await _COORDINATORS.pop().async_shutdown()


# ---------------------------------------------------------------- the store
async def test_a_damaged_store_file_starts_empty_and_is_logged(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def broken(self: Store[Any]) -> Any:
        raise OSError("unreadable")

    monkeypatch.setattr(Store, "async_load", broken)
    caplog.set_level(logging.WARNING)
    store = DerivedStore(hass)
    await store.async_load()
    assert store.loaded and store.get("anything") is None
    assert "Saved calculated values could not be read" in caplog.text


@pytest.mark.parametrize(
    "stored",
    [None, [], "x", {"rows": []}, {"rows": "x"}, {"other": {"a": {}}}],
)
async def test_a_store_of_the_wrong_shape_loads_no_rows(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, stored: object
) -> None:
    async def load(self: Store[Any]) -> Any:
        return stored

    monkeypatch.setattr(Store, "async_load", load)
    store = DerivedStore(hass)
    await store.async_load()
    assert store.loaded and store._rows == {}


async def test_only_rows_with_a_text_key_and_an_object_are_loaded_up_to_the_limit(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows: dict[Any, Any] = {
        f"gw:{index}": {"n": index} for index in range(MAX_ROWS + 5)
    }
    rows["bad-value"] = "text"
    rows[7] = {"n": 1}

    async def load(self: Store[Any]) -> Any:
        return {"rows": rows}

    monkeypatch.setattr(Store, "async_load", load)
    store = DerivedStore(hass)
    await store.async_load()
    assert len(store._rows) == MAX_ROWS
    assert store.get("gw:0") == {"n": 0}
    assert store.get(f"gw:{MAX_ROWS + 1}") is None


async def test_concurrent_loads_read_the_file_once_and_keep_the_first_rows(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = 0

    async def load(self: Store[Any]) -> Any:
        nonlocal reads
        reads += 1
        mine = reads
        await asyncio.sleep(0)
        return {"rows": {"gw:a": {"n": mine}}}

    monkeypatch.setattr(Store, "async_load", load)
    store = DerivedStore(hass)
    await asyncio.gather(store.async_load(), store.async_load())
    assert store.loaded and store.get("gw:a") == {"n": 1}
    await store.async_load()
    assert reads == 2, "the second concurrent call read, the third did not"


async def test_a_full_store_refuses_new_rows_but_updates_known_ones(
    hass: HomeAssistant,
) -> None:
    store = DerivedStore(hass)
    await store.async_load()
    for index in range(MAX_ROWS):
        store._rows[f"gw:{index}"] = {"n": index}
    store.set("gw:new", {"n": 1})
    assert store.get("gw:new") is None
    store.set("gw:3", {"n": 99})
    assert store.get("gw:3") == {"n": 99}
    row = store.get("gw:3")
    assert row is not None
    row["n"] = 0
    assert store.get("gw:3") == {"n": 99}, "a read gives a copy"


async def test_forgetting_gateways_drops_only_their_own_rows(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    store = derived_store(hass)
    await store.async_load()
    store.set("gw1:calculated", {"a": 1})
    store.set("gw10:calculated", {"a": 2})
    store.set("gw2:calculated", {"a": 3})
    await store.async_forget_gateways([])
    assert store.get("gw1:calculated") is not None, "no gateway named: nothing dropped"
    await store.async_forget_gateways(["gw3"])
    assert store.get("gw1:calculated") is not None
    await store.async_forget_gateways(["gw1", "gw2"])
    assert store.get("gw1:calculated") is None
    assert store.get("gw2:calculated") is None
    assert store.get("gw10:calculated") == {"a": 2}
    saved = hass_storage[store_module.STORAGE_KEY]["data"]["rows"]
    assert set(saved) == {"gw10:calculated"}


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (None, None),
        ({}, None),
        ({"v": True}, None),
        ({"v": "5"}, None),
        ({"v": math.nan}, None),
        ({"v": math.inf}, None),
        ({"v": -0.1}, None),
        ({"v": 0}, 0.0),
        ({"v": 12}, 12.0),
        ({"v": 1.5}, 1.5),
    ],
)
def test_a_stored_number_must_be_finite_and_not_negative(
    row: dict[str, Any] | None, expected: float | None
) -> None:
    assert stored_number(row, "v") == expected


# ---------------------------------------------------------------- the inputs
def _resource(path: str, value: object = None, **meta: Any) -> Resource:
    return Resource(
        path=path,
        value=value,  # type: ignore[arg-type]
        has_value=value is not None,
        metadata=ResourceMetadata(resource_type="floatValue", **meta),
    )


def _input(value: object, resource: Resource | None = None) -> Input:
    return Input("/x", value, "cloud", T0, resource)


@pytest.mark.parametrize(
    "value",
    [True, "5", None, math.nan, math.inf, 32767.0, -32768.0, 3276.7, -3276.8],
)
def test_markers_and_non_numbers_are_no_numeric_input(value: object) -> None:
    assert number(_input(value)) is None


def test_a_declared_error_value_of_the_resource_is_no_numeric_input() -> None:
    resource = _resource("/x", 6553.5, numeric_error_values=(6553.5,))
    assert number(_input(6553.5, resource)) is None
    assert number(_input(21.5, resource)) == 21.5
    assert number(None) is None


def test_counter_parts_need_a_counter_of_the_right_kind() -> None:
    plain = _input(5.0, _resource("/heatSources/hs1/something", 5.0))
    assert energy_part(plain, "outputProduced") is None
    assert energy_part(None, "outputProduced") is None
    assert runtime_hours(plain, "ch") is None
    assert runtime_hours(None, "ch") is None
    assert starts(plain, "ch") is None
    assert starts(None, "ch") is None
    assert starts(_input(1, None), "ch") is None


# ----------------------------------------------------------------- activity
def _boiler(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> Plant:
    plant = Plant(hass, freezer)
    plant.cloud(MODE, "auto").cloud(HEAT_SOURCE_ACTIVE, "on")
    plant.cloud(ROOT + "/pumpModulation", 40.0, unit="%")
    plant.coordinator.local_selection = None
    return plant


def test_a_boiler_heats_the_circuits_only_while_its_central_heating_runs(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = _boiler(hass, freezer)
    coordinator = plant.coordinator
    plant.cloud(CENTRAL_HEATING_STATUS, "on")
    assert circuit_activity(coordinator, ROOT).value == "heating"
    plant.cloud(CENTRAL_HEATING_STATUS, "off")
    assert circuit_activity(coordinator, ROOT).value == "idle"
    # A status the integration cannot read gives no guess.
    plant.cloud(CENTRAL_HEATING_STATUS, "maintenance")
    result = circuit_activity(coordinator, ROOT)
    assert result.value is None and result.derived_from == {}


def test_a_defrost_signal_that_is_not_a_flag_leaves_the_decision_to_the_next_source(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer, refrigerant="defrost")
    coordinator = plant.coordinator
    plant.local(DEFROST_SIGNAL, "unclear")
    reading = defrost_state(coordinator)
    assert reading is not None and reading.active and reading.source.path == REFRIGERANT
    plant.flag(DEFROST_SIGNAL, True)
    reading = defrost_state(coordinator)
    assert reading is not None and reading.source.path == DEFROST_SIGNAL
    assert circuit_activity(coordinator, ROOT).value == "defrosting"
    assert circuit_activity(coordinator, ROOT).derived_from[DEFROST_SIGNAL] == "local"


def test_a_status_of_the_wrong_type_is_no_defrost_reading(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    coordinator = plant.coordinator
    plant.local(REFRIGERANT, "???")
    plant.cloud(COMPRESSOR, "defrost")
    reading = defrost_state(coordinator)
    assert reading is not None and reading.active
    assert reading.source.path == COMPRESSOR, "the next source answers"
    plant.cloud(COMPRESSOR, 7.0)
    assert defrost_state(coordinator) is None


def test_the_cooling_season_or_the_refrigerant_status_makes_it_cooling(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    coordinator = plant.coordinator
    assert circuit_activity(coordinator, ROOT).value == "heating"
    plant.cloud(SUWI, "cooling")
    assert circuit_activity(coordinator, ROOT).value == "cooling"
    plant.cloud(SUWI, "forced").flag(COOLING, False)
    plant.local(REFRIGERANT, "cooling")
    result = circuit_activity(coordinator, ROOT)
    assert result.value == "cooling"
    assert result.derived_from[REFRIGERANT] == "local"
    plant.local(REFRIGERANT, "heating")
    assert circuit_activity(coordinator, ROOT).value == "heating"


async def test_the_activity_sensor_shows_the_activity_and_its_inputs(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    sensor = CircuitActivitySensor(plant.coordinator, ROOT)
    assert not sensor.available or sensor.native_value is None
    sensor._refresh()
    assert sensor.native_value == "heating" and sensor.available
    attributes = sensor.extra_state_attributes
    assert attributes is not None
    assert attributes["derived_from"][PUMP] == "local"  # type: ignore[index]
    plant.local(PUMP, "off")
    sensor._refresh()
    assert sensor.native_value == "idle"
    # The pump unreadable: no activity, no stale attributes, but available
    # while the circuit's mode is current.
    plant.forget(PUMP)
    sensor._refresh()
    assert sensor.native_value is None and sensor.extra_state_attributes is None
    assert sensor.available
    plant.stale_cloud(MODE)
    assert not sensor.available


# ----------------------------------------------------------------- defrosts
def _fall(plant: Plant, monitor: DefrostMonitor, *, with_valve: bool = True) -> None:
    """A short defrost: start, five minutes, the plant heats again."""
    monitor.update(plant.at)
    plant.later(1).local(REFRIGERANT, "defrost")
    monitor.update(plant.at)
    if with_valve:
        plant.later(2).flag(VW1, True)
        monitor.update(plant.at)
    plant.later(3).local(REFRIGERANT, "heating")
    monitor.update(plant.at)


async def test_a_defrost_that_ends_before_the_saved_totals_are_read_is_added_to_them(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    store = derived_store(hass)
    store.set(
        "gateway-one:calculated_defrost",
        {"count": 3.0, "duration_min": 30.0, "heat_withdrawn_kwh": 2.0},
    )
    monitor = DefrostMonitor(plant.coordinator, store)
    events: list[dict[str, Any]] = []
    monitor.add_event_listener(events.append)
    _fall(plant, monitor)
    _cycle(plant, monitor, SETTLE.total_seconds() / 60)
    assert len(events) == 1 and not monitor.loaded
    assert monitor.totals["count"] == 0.0, "not counted before the totals are known"
    await monitor.async_load()
    await monitor.async_load()  # a second call changes nothing
    assert monitor.totals["count"] == 4.0
    assert monitor.totals["duration_min"] == pytest.approx(
        30.0 + events[0]["duration_min"]
    )
    row = store.get("gateway-one:calculated_defrost")
    assert row is not None and row["count"] == 4.0
    assert monitor.last == events[0]


async def test_two_loads_at_once_count_a_waiting_defrost_once(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_load = DerivedStore.async_load

    async def slow_load(self: DerivedStore) -> None:
        await asyncio.sleep(0)
        await real_load(self)

    monkeypatch.setattr(DerivedStore, "async_load", slow_load)
    plant = air(hass, freezer)
    monitor = DefrostMonitor(plant.coordinator, derived_store(hass))
    _fall(plant, monitor)
    _cycle(plant, monitor, SETTLE.total_seconds() / 60)
    await asyncio.gather(monitor.async_load(), monitor.async_load())
    assert monitor.totals["count"] == 1.0


async def test_the_saved_totals_ignore_values_that_are_not_numbers(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    store = derived_store(hass)
    store.set(
        "gateway-one:calculated_defrost",
        {
            "count": "many",
            "duration_min": -5,
            "heat_withdrawn_kwh": 4.5,
            "last": {"started_at": 5},
            "evidence": True,
        },
    )
    monitor = await _monitor(plant)
    assert monitor.totals["count"] == 0.0 and monitor.totals["duration_min"] == 0.0
    assert monitor.totals["heat_withdrawn_kwh"] == 4.5
    assert monitor.last is None
    assert monitor._evidence_saved


async def test_a_defrost_without_any_counter_is_reported_with_its_duration_only(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    for path in (CH, DHW, TOTAL):
        plant.stale_cloud(path)
    monitor = await _monitor(plant)
    events: list[dict[str, Any]] = []
    monitor.add_event_listener(events.append)
    _fall(plant, monitor)
    _cycle(plant, monitor, SETTLE.total_seconds() / 60)
    event = events[0]
    assert event["duration_min"] == 5.0
    assert event["heat_withdrawn_kwh"] is None
    assert event["electricity_kwh"] is None
    assert monitor.totals["count"] == 1.0
    assert monitor.totals["heat_withdrawn_kwh"] == 0.0


async def test_without_a_valve_reading_the_stages_stay_empty(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    plant.forget(VW1)
    monitor = await _monitor(plant)
    events: list[dict[str, Any]] = []
    monitor.add_event_listener(events.append)
    _fall(plant, monitor, with_valve=False)
    _cycle(plant, monitor, SETTLE.total_seconds() / 60)
    assert events[0]["stages"] == [] and events[0]["stage_minutes"] == {}


async def test_the_total_counter_stands_in_when_the_mode_counters_are_missing(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    plant.stale_cloud(CH).stale_cloud(DHW)
    monitor = await _monitor(plant)
    events: list[dict[str, Any]] = []
    monitor.add_event_listener(events.append)
    monitor.update(plant.at)
    plant.later(1).local(REFRIGERANT, "defrost")
    monitor.update(plant.at)
    plant.later(2).energy(TOTAL, 1613.90, 400.05)
    monitor.update(plant.at)
    plant.later(2).local(REFRIGERANT, "heating").energy(TOTAL, 1613.70, 400.09)
    monitor.update(plant.at)
    _cycle(plant, monitor, SETTLE.total_seconds() / 60)
    assert events[0]["heat_withdrawn_kwh"] == pytest.approx(0.75)
    assert TOTAL in events[0]["derived_from"]
    assert CH not in events[0]["derived_from"]


async def test_electricity_comes_from_the_mode_counters_without_a_total(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    plant.stale_cloud(TOTAL)
    # The heating counter has no heater part: only the parts both sides know.
    plant.cloud(CH, values=({"outputProduced": 1234.16, "compressor": 400.0},))
    plant.energy(DHW, 380.29, 100.0)
    monitor = await _monitor(plant)
    events: list[dict[str, Any]] = []
    monitor.add_event_listener(events.append)
    monitor.update(plant.at)
    plant.later(1).local(REFRIGERANT, "defrost")
    monitor.update(plant.at)
    plant.later(2).cloud(
        CH, values=({"outputProduced": 1233.9, "compressor": 400.10},)
    ).energy(DHW, 380.2, 100.05)
    monitor.update(plant.at)
    plant.later(2).local(REFRIGERANT, "heating")
    monitor.update(plant.at)
    _cycle(plant, monitor, SETTLE.total_seconds() / 60)
    assert events[0]["electricity_kwh"] == pytest.approx(0.15)


async def test_the_heater_power_adds_to_the_compressor_power_of_a_defrost(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    monitor = await _monitor(plant)
    events: list[dict[str, Any]] = []
    monitor.add_event_listener(events.append)
    monitor.update(plant.at)

    def readings() -> None:
        plant.local(POWER, 600.0, unitOfMeasure="W")
        plant.local(HEATER_POWER, 1200.0, unitOfMeasure="W")

    plant.later(1).local(REFRIGERANT, "defrost")
    readings()
    monitor.update(plant.at)
    for _ in range(5):
        plant.later(1)
        readings()
        monitor.update(plant.at)
    plant.later(1).local(REFRIGERANT, "heating")
    readings()
    monitor.update(plant.at)
    _cycle(plant, monitor, SETTLE.total_seconds() / 60)
    # 0.6 kW + 1.2 kW for six minutes.
    assert events[0]["electricity_kwh_from_power"] == pytest.approx(0.18)
    assert HEATER_POWER in events[0]["derived_from"]
    assert monitor.diagnostics()["count"] == 1


async def test_a_defrost_source_that_turns_unreadable_ends_nothing_by_itself(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A short gap inside a defrost keeps it; only the readable end finishes."""
    plant = air(hass, freezer)
    monitor = await _monitor(plant)
    events: list[dict[str, Any]] = []
    monitor.add_event_listener(events.append)
    monitor.update(plant.at)
    plant.later(1).local(REFRIGERANT, "defrost")
    monitor.update(plant.at)
    plant.forget(REFRIGERANT).stale_cloud(COMPRESSOR)
    _cycle(plant, monitor, 3)
    assert monitor.running and monitor.active is None and events == []
    plant.later(2).local(REFRIGERANT, "heating")
    monitor.update(plant.at)
    _cycle(plant, monitor, SETTLE.total_seconds() / 60)
    assert len(events) == 1 and events[0]["duration_min"] == 5.0


def test_the_diagnostics_of_an_idle_monitor_name_no_values(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    monitor = DefrostMonitor(plant.coordinator, derived_store(hass))
    report = monitor.diagnostics()
    assert report["capable"] is True and report["active"] is None
    assert report["running"] is False and report["settling"] is False
    assert report["source"] is None and report["last_started_at"] is None


# ----------------------------------------------------------- defrost entities
def _config_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    return entry


async def test_defrost_entities_of_an_earlier_start_come_back_for_the_same_entry_only(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = brine(hass, freezer)  # shows no sign of defrosting now
    registry = er.async_get(hass)
    other = _config_entry(hass)
    registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        derived_defrost.unique_id("gateway-one", ACTIVE_KEY),
        config_entry=other,
    )
    added: list[Any] = []
    entry = SimpleNamespace(async_on_unload=lambda _unsub: None)
    track_defrost_entities(
        entry, plant.coordinator, added.extend, derived_defrost.binary_sensors
    )
    assert added == [], "an entity of another entry proves nothing"
    registry.async_get_or_create(
        "event",
        DOMAIN,
        derived_defrost.unique_id("gateway-one", derived_defrost.EVENT_KEY),
        config_entry=plant.coordinator.config_entry,
    )
    track_defrost_entities(
        entry, plant.coordinator, added.extend, derived_defrost.binary_sensors
    )
    assert [e.translation_key for e in added] == [ACTIVE_KEY]


async def test_the_defrost_entities_show_what_the_monitor_knows(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    monitor = DefrostMonitor(plant.coordinator, derived_store(hass))
    active = DefrostActiveBinarySensor(plant.coordinator, monitor)
    last = LastDefrostSensor(plant.coordinator, monitor)
    assert not active.available and active.is_on is None
    assert active.extra_state_attributes is None
    assert not last.available and last.native_value is None
    assert last.extra_state_attributes is None
    await monitor.async_load()
    monitor.update(plant.at)
    assert active.available and active.is_on is False
    assert active.extra_state_attributes == {
        "detected_by": REFRIGERANT,
        "derived_from": {REFRIGERANT: "local"},
    }
    assert last.available and last.native_value is None
    monitor.last = {"started_at": "not a time", "duration_min": 5.0}
    assert last.native_value is None
    monitor.last = {"started_at": "2026-11-13T01:40:00", "duration_min": 5.0}
    assert last.native_value is None, "a time without a zone is not shown"
    monitor.last = {"started_at": "2026-11-13T01:40:00+00:00", "duration_min": 5.0}
    assert last.native_value == T0
    assert last.extra_state_attributes == {"duration_min": 5.0}
    # Entities not added to Home Assistant yet ignore a change of the monitor.
    last._changed()


# ---------------------------------------------------------- net heat counters
async def _net(
    hass: HomeAssistant,
    plant: Plant,
    monkeypatch: pytest.MonkeyPatch | None = None,
    restored: object = None,
    unit: str = "kWh",
) -> NetHeatCounterSensor:
    sensor = NetHeatCounterSensor(plant.coordinator, CH)
    sensor.hass = hass

    async def last() -> Any:
        return None if restored is None else SensorExtraStoredData(restored, unit)  # type: ignore[arg-type]

    if monkeypatch is not None:
        monkeypatch.setattr(sensor, "async_get_last_sensor_data", last)
    await sensor.async_added_to_hass()
    return sensor


async def test_a_net_heat_counter_before_its_first_reading_shows_nothing(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    sensor = NetHeatCounterSensor(plant.coordinator, CH)
    sensor._refresh()  # not ready: the saved state is not read yet
    assert sensor.native_value is None and sensor.extra_state_attributes is None
    assert not sensor.available


async def test_a_restored_total_counts_on_from_the_next_reading(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    plant = air(hass, freezer)
    plant.stale_cloud(CH)
    sensor = await _net(hass, plant, monkeypatch, restored=2500.0)
    # Restored, but the reading is not there yet: shown, with no reference.
    assert sensor.native_value == 2500.0 and sensor.available
    assert sensor.extra_state_attributes == {}
    plant.energy(CH, 1234.0)
    sensor._refresh()
    assert sensor.native_value == 2500.0, "continues at the first reading"
    assert sensor.extra_state_attributes == {
        "highest_reading": 1234.0,
        "derived_from": {CH: "cloud"},
    }
    plant.energy(CH, 1235.5)
    sensor._refresh()
    assert sensor.native_value == 2501.5


@pytest.mark.parametrize(
    ("restored", "unit"),
    [
        (10.0, "kWh"),  # older than the saved total
        (1000.0, "kWh"),  # the same as the saved total
        (5000.0, "Wh"),  # another unit is not read as kWh
        ("lots", "kWh"),
        (-3.0, "kWh"),
        (math.nan, "kWh"),
        (True, "kWh"),
    ],
)
async def test_a_restored_state_that_is_not_newer_or_not_usable_is_ignored(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    restored: object,
    unit: str,
) -> None:
    plant = air(hass, freezer)
    plant.energy(CH, 1000.0)
    store = derived_store(hass)
    await store.async_load()
    store.set(
        "gateway-one:heatSources:emon:chConsumption:calculated_net_heat",
        {"total": 1000.0, "reference": 1000.0},
    )
    sensor = await _net(hass, plant, monkeypatch, restored=restored, unit=unit)
    assert sensor.native_value == 1000.0
    plant.energy(CH, 1001.0)
    sensor._refresh()
    assert sensor.native_value == 1001.0


def test_restored_values_are_numbers_that_are_finite_and_not_negative() -> None:
    assert _restored(Decimal("2.5")) == 2.5
    assert _restored(0) == 0.0
    for bad in (True, "1", None, -1, math.inf, math.nan, Decimal("-1")):
        assert _restored(bad) is None


async def test_the_net_heat_never_falls_over_dips_and_continues_after_a_reset(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    plant.energy(CH, 1000.0)
    sensor = await _net(hass, plant)
    seen: list[float | None] = []

    def reading(value: float) -> None:
        plant.later(5).energy(CH, value)
        sensor._refresh()
        seen.append(sensor.native_value)

    for value in (1001.0, 1000.5, 1000.2, 1001.0, 1002.0):
        reading(value)
    # Dips below the highest reading count nothing; a rise after one counts
    # from the highest reading, not from the bottom of the dip.
    assert seen == [1001.0, 1001.0, 1001.0, 1001.0, 1002.0]
    # A replaced counter (a big drop, confirmed by the next reading) starts
    # again from its new value.
    reading(3.0)
    reading(4.5)
    assert seen[-2:] == [1002.0, 1003.5]
    assert all(
        later is not None and earlier is not None and later >= earlier
        for earlier, later in itertools.pairwise(seen)
    )
    row = derived_store(hass).get(str(sensor.unique_id))
    assert row == {"total": 1003.5, "reference": 4.5}
    # After a restart the saved total and reference carry on.
    await flush_store(derived_store(hass)._store)
    again = await _net(hass, plant)
    plant.energy(CH, 6.0)
    again._refresh()
    assert again.native_value == 1005.0


async def test_a_net_heat_counter_whose_reading_goes_away_keeps_its_total(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    plant.energy(CH, 1000.0)
    sensor = await _net(hass, plant)
    plant.energy(CH, 1002.0)
    sensor._refresh()
    plant.stale_cloud(CH)
    sensor._refresh()
    assert sensor.native_value == 1002.0 and sensor.available
    assert "highest_reading" in (sensor.extra_state_attributes or {})


# ------------------------------------------------------- more edge cases
def test_listeners_of_the_monitor_can_be_removed(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    monitor = DefrostMonitor(plant.coordinator, derived_store(hass))
    changed: list[bool] = []
    events: list[dict[str, Any]] = []
    remove = monitor.add_listener(lambda: changed.append(True))
    remove_event = monitor.add_event_listener(events.append)
    monitor.update(plant.at)
    assert changed == [True]
    remove()
    remove_event()
    plant.later(1).local(REFRIGERANT, "defrost")
    monitor.update(plant.at)
    assert changed == [True] and events == []


def test_the_defrost_report_of_a_gateway_never_fails(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plant = air(hass, freezer)
    report = derived_defrost.calculated_diagnostics(plant.coordinator)
    assert report == {"defrost_capable": True, "defrost": None}
    monitor = DefrostMonitor(plant.coordinator, derived_store(hass))
    derived_defrost._MONITORS[plant.coordinator] = monitor
    report = derived_defrost.calculated_diagnostics(plant.coordinator)
    assert report["defrost"] == monitor.diagnostics()
    monkeypatch.setattr(
        derived_defrost,
        "defrost_capable",
        lambda _c: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert derived_defrost.calculated_diagnostics(plant.coordinator) == {
        "error": "unavailable"
    }
    assert derived_defrost.existing_monitor(5) is None, "no monitor for a plain value"


def test_a_plant_view_reads_its_local_values_without_a_cloud(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    from custom_components.bosch_buderus_heating.derived_inputs import (
        known_paths,
        local_only_paths,
        read,
    )
    from custom_components.bosch_buderus_heating.heat_source_kind import DEFROST_FLAG
    from custom_components.bosch_buderus_heating.local_plant_view import local_reading

    plant = air(hass, freezer)
    observation = plant.selection.only[REFRIGERANT]
    view = SimpleNamespace(
        read_only_view=True,
        data={},
        local_plant_sensors=lambda: {
            REFRIGERANT: object(),
            "/heatSources/hs1/defrostActive": object(),
        },
        local_only_reading=lambda path: (
            local_reading(observation) if path == REFRIGERANT else None
        ),
    )
    assert local_only_paths(view) == {  # type: ignore[arg-type]
        REFRIGERANT,
        "/heatSources/hs1/defrostActive",
    }
    assert known_paths(view, DEFROST_FLAG) == ("/heatSources/hs1/defrostActive",)  # type: ignore[arg-type]
    reading = read(view, REFRIGERANT)  # type: ignore[arg-type]
    assert reading is not None and reading.origin == "local"
    assert reading.value == "heating"
    assert read(view, "/heatSources/hs1/defrostActive") is None  # type: ignore[arg-type]
    assert local_only_paths(SimpleNamespace(local_selection=None)) == frozenset()  # type: ignore[arg-type]


def test_the_candidates_of_a_gateway_without_readings_are_none_and_pool_is_named(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    from custom_components.bosch_buderus_heating.derived_sensors import (
        _candidates,
        _net_name,
    )

    empty = Plant(hass, freezer)
    assert _candidates(empty.coordinator) == []
    assert _net_name("en", "pool") == "Net heat pool (calculated)"
    assert _net_name("de", "pool") == "Netto-Wärme Pool (berechnet)"
    assert _net_name("en", "anything") == "Net heat total (calculated)"
    plant = air(hass, freezer)
    plant.cloud(ROOT + "/actualSupplyTemperature", 26.0, unit="°C")
    plant.local("/signals/SC.HC1.FlowTempSetp", 24, unitOfMeasure="C")
    keys = {key for key, _ in _candidates(plant.coordinator)}
    assert f"overshoot:{ROOT}" in keys and f"activity:{ROOT}" in keys
    plant.forget("/signals/SC.HC1.FlowTempSetp")
    keys = {key for key, _ in _candidates(plant.coordinator)}
    assert f"overshoot:{ROOT}" not in keys, "no setpoint, no overshoot"
