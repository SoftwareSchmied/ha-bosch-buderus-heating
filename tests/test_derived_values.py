"""Calculated values: circuit activity, defrosts, net heat, run time, overshoot.

Synthetic data only. The air heat pump is shaped like a real installation
with one underfloor circuit (local refrigerant status, 3-way valve VW1, local
circuit pump, cloud counters); the brine-to-water heat pump is shaped like a
real one reported to the project: the same controller signals (4-way valve,
drip pan heater, crankcase heater), a brine circuit and no defrost signal.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate.const import HVACAction
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating import derived_defrost
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
    SnapshotSource,
)
from custom_components.bosch_buderus_heating.derived_activity import (
    circuit_activity,
    defrost_capable,
    defrost_state,
)
from custom_components.bosch_buderus_heating.derived_defrost import (
    GAP,
    SETTLE,
    DefrostMonitor,
    track_defrost_entities,
)
from custom_components.bosch_buderus_heating.derived_sensors import (
    NetHeatCounterSensor,
    RunTimePerStartSensor,
    SupplyOvershootSensor,
    track_calculated_sensors,
)
from custom_components.bosch_buderus_heating.derived_store import (
    DerivedStore,
    derived_store,
)
from custom_components.bosch_buderus_heating.heat_generator import (
    unnamed_hardware_cloud,
)
from custom_components.bosch_buderus_heating.heat_source_kind import classify
from custom_components.bosch_buderus_heating.local_plant_view import local_reading
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    Resource,
    ResourceMetadata,
)
from tests.ha_test_helpers import MockConfigEntry, flush_store
from tests.test_local_api import read as decode

ROOT = "/heatingCircuits/hc1"
MODE = ROOT + "/operationMode"
SUWI = ROOT + "/currentSuWiMode"
PUMP = ROOT + "/pumpStatus"
REFRIGERANT = "/heatSources/hs1/refrigerant/status"
COMPRESSOR = "/heatSources/compressor/status"
PUMP_TYPE = "/heatSources/hs1/heatPumpType"
VW1 = "/signals/SRC.CUHP.HP1.DHWValve"
COMPRESSOR_ACTIVE = "/signals/SRC.CUHP.HMI.CompressorActive"
COOLING = "/signals/SRC.CUHP.Cooling.Active"
TL2 = "/heatSources/sensors/evaporatorTemp_tl2"
OUTDOOR = "/system/sensors/temperatures/outdoor_t1"
CH = "/heatSources/emon/chConsumption"
DHW = "/heatSources/emon/dhwConsumption"
TOTAL = "/heatSources/emon/totalConsumption"
BRINE_IN = "/heatSources/hs1/brineCircuit/collectorInflowTemp"
POWER = "/heatSources/compressor/powerElecActual"
T0 = datetime(2026, 11, 13, 1, 40, tzinfo=UTC)
_COORDINATORS: list[BoschBuderusDataUpdateCoordinator] = []


@pytest.fixture(autouse=True)
async def _stop_coordinators() -> AsyncGenerator[None]:
    yield
    while _COORDINATORS:
        await _COORDINATORS.pop().async_shutdown()


class FakeSelection:
    """The local access of a plant: local-only readings and paired ones."""

    enabled = False
    has_local_access = True

    def __init__(self) -> None:
        self.only: dict[str, Any] = {}
        self.paired: dict[str, ResourceSnapshot] = {}

    def _cached(self, _key: object, compute: Any) -> Any:
        return compute()

    def select(
        self, path: str, snapshot: ResourceSnapshot, *, without_cloud: bool
    ) -> ResourceSnapshot:
        return self.paired.get(path, snapshot)

    def local_only_reading(self, path: str) -> Any:
        observation = self.only.get(path)
        return local_reading(observation) if observation is not None else None

    def local_only_observations(self) -> dict[str, Any]:
        return dict(self.only)

    @contextmanager
    def update_pass(self) -> Iterator[None]:
        yield

    def check_writes(self, _now: datetime) -> None:
        return None

    def local_states(self) -> tuple[()]:
        return ()


def _kind(value: object) -> str:
    if isinstance(value, (bool, str)):
        return "stringValue"
    return "floatValue" if isinstance(value, float) else "integerValue"


class Plant:
    """One gateway with cloud and local readings, set per cycle."""

    def __init__(self, hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
        # Readings are taken "now": the clock follows the plant's cycles.
        self.freezer = freezer
        freezer.move_to(T0)
        entry = MockConfigEntry(domain=DOMAIN)
        entry.add_to_hass(hass)
        self.hass = hass
        self.coordinator = BoschBuderusDataUpdateCoordinator(
            hass, AsyncMock(), Gateway("gateway-one", device_type="MX300"), entry
        )
        self.coordinator.data = {}
        self.coordinator.resources = {}
        self.coordinator.last_update_success = True
        self.selection = FakeSelection()
        self.coordinator.local_selection = self.selection  # type: ignore[assignment]
        self.at = T0
        _COORDINATORS.append(self.coordinator)

    def cloud(self, path: str, value: object = None, **kwargs: Any) -> Plant:
        values = kwargs.pop("values", ())
        resource = Resource(
            path=path,
            value=value,  # type: ignore[arg-type]
            has_value=value is not None,
            values=values,
            metadata=ResourceMetadata(
                resource_type=kwargs.pop("kind", None)
                or ("emonValue" if values else _kind(value)),
                unit=kwargs.pop("unit", None),
            ),
        )
        self.coordinator.resources[path] = resource
        self.coordinator.data[path] = ResourceSnapshot(
            resource, True, self.at, source=SnapshotSource.BATCH
        )
        return self

    def energy(self, path: str, produced: float, compressor: float = 400.0) -> Plant:
        return self.cloud(
            path,
            values=(
                {"outputProduced": produced, "compressor": compressor, "eheater": 0.0},
            ),
        )

    def local(self, path: str, value: object, **extra: Any) -> Plant:
        raw: dict[str, Any] = {
            "id": path,
            "type": extra.pop("kind", None) or _kind(value),
            "writeable": 0,
            "value": value,
            **extra,
        }
        self.selection.only[path] = decode(raw, observed_at=self.at)
        return self

    def forget(self, path: str) -> Plant:
        self.selection.only.pop(path, None)
        return self

    def stale_cloud(self, path: str) -> Plant:
        snapshot = self.coordinator.data[path]
        self.coordinator.data[path] = replace(snapshot, available=False)
        return self

    def flag(self, path: str, on: bool) -> Plant:
        return self.local(path, "true" if on else "false")

    def later(self, minutes: float) -> Plant:
        self.at += timedelta(minutes=minutes)
        self.freezer.move_to(self.at)
        return self


def air(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, *, refrigerant: str = "heating"
) -> Plant:
    plant = Plant(hass, freezer)
    plant.cloud(MODE, "manual").cloud(SUWI, "forced").cloud(PUMP_TYPE, "air_water")
    plant.cloud(COMPRESSOR, "heating").cloud(OUTDOOR, 4.0, unit="°C")
    plant.energy(CH, 1234.16).energy(DHW, 380.29).energy(TOTAL, 1614.45)
    plant.local(REFRIGERANT, refrigerant).flag(VW1, False).local(PUMP, "on")
    plant.flag(COMPRESSOR_ACTIVE, True).local(TL2, 2.6, unitOfMeasure="C")
    return plant


def brine(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> Plant:
    """No heat pump type, no defrost flag, no TL2, no fan; a brine circuit."""
    plant = Plant(hass, freezer)
    plant.cloud(MODE, "manual").cloud(SUWI, "forced").cloud(COMPRESSOR, "heating")
    plant.cloud(BRINE_IN, 6.5, unit="°C")
    plant.local(REFRIGERANT, "heating").flag(VW1, False).local(PUMP, "on")
    plant.flag(COMPRESSOR_ACTIVE, True).flag(COOLING, False)
    for name in ("FourwayValve", "DripPanHeater", "CrankcaseHeater"):
        plant.flag(f"/signals/SRC.CUHP.HP1.{name}", False)
    return plant


# ------------------------------------------------------------------ activity


def test_activity_follows_the_heat_pump_and_the_circuit(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    coordinator = plant.coordinator
    result = circuit_activity(coordinator, ROOT)
    assert result.value == "heating"
    # The local readings come first and are named with their origin.
    assert result.derived_from[VW1] == "local"
    assert result.derived_from[PUMP] == "local"
    assert result.derived_from[MODE] == "cloud"
    plant.flag(VW1, True)  # the hot water charge
    assert circuit_activity(coordinator, ROOT).value == "idle"
    plant.flag(VW1, False).local(PUMP, "off")
    assert circuit_activity(coordinator, ROOT).value == "idle"
    plant.local(PUMP, "on").flag(COMPRESSOR_ACTIVE, False)
    assert circuit_activity(coordinator, ROOT).value == "idle"
    plant.flag(COMPRESSOR_ACTIVE, True).flag(COOLING, True)
    assert circuit_activity(coordinator, ROOT).value == "cooling"
    plant.flag(COOLING, False).local(REFRIGERANT, "defrost")
    assert circuit_activity(coordinator, ROOT).value == "defrosting"
    plant.cloud(SUWI, "off")  # summer
    assert circuit_activity(coordinator, ROOT).value == "off"
    plant.cloud(SUWI, "forced").cloud(MODE, "off")
    assert circuit_activity(coordinator, ROOT).value == "off"


def test_a_missing_input_gives_no_activity(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    coordinator = plant.coordinator
    plant.forget(PUMP)
    assert circuit_activity(coordinator, ROOT).value is None
    assert circuit_activity(coordinator, ROOT).derived_from == {}
    plant.local(PUMP, "on").forget(VW1)
    # The side comes from the refrigerant status instead.
    assert circuit_activity(coordinator, ROOT).value == "heating"
    plant.flag(VW1, False).forget(REFRIGERANT).stale_cloud(COMPRESSOR)
    assert circuit_activity(coordinator, ROOT).value is None  # defrost unknown
    plant.stale_cloud(MODE)
    assert circuit_activity(coordinator, ROOT).value is None


def test_cloud_only_activity(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = Plant(hass, freezer)
    plant.cloud(MODE, "auto").cloud(COMPRESSOR, "heating")
    plant.cloud(ROOT + "/pumpModulation", 40.0, unit="%")
    coordinator = plant.coordinator
    coordinator.local_selection = None
    assert circuit_activity(coordinator, ROOT).value == "heating"
    plant.cloud(COMPRESSOR, "dhw")
    assert circuit_activity(coordinator, ROOT).value == "idle"
    plant.cloud(COMPRESSOR, "off")
    assert circuit_activity(coordinator, ROOT).value == "idle"
    # Without a heat pump type or a reported defrost, "defrost" is evidence.
    plant.cloud(COMPRESSOR, "defrost")
    assert circuit_activity(coordinator, ROOT).value == "defrosting"
    plant.cloud(COMPRESSOR, "heating").cloud(ROOT + "/pumpModulation", 0.0)
    assert circuit_activity(coordinator, ROOT).value == "idle"


def test_brine_unit_never_defrosts(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = brine(hass, freezer)
    coordinator = plant.coordinator
    assert classify(coordinator).kind == "brine"
    assert classify(coordinator).evidence == (BRINE_IN,)
    assert not defrost_capable(coordinator)
    assert defrost_state(coordinator) is None
    assert circuit_activity(coordinator, ROOT).value == "heating"
    # Even a status reporting "defrost" is no defrost of a brine unit.
    plant.local(REFRIGERANT, "defrost").cloud(COMPRESSOR, "defrost")
    assert circuit_activity(coordinator, ROOT).value != "defrosting"


def test_heat_source_kind_needs_evidence(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = Plant(hass, freezer)
    coordinator = plant.coordinator
    for name in ("FourwayValve", "DripPanHeater", "CrankcaseHeater"):
        plant.flag(f"/signals/SRC.CUHP.HP1.{name}", True)
    plant.flag("/signals/SRC.CUHP.HP1.BrineTempHeatingLow", False)
    assert classify(coordinator).kind is None  # shared signal names
    plant.local(TL2, 1.9, unitOfMeasure="C")
    assert classify(coordinator).kind == "air"
    plant.cloud(BRINE_IN, 6.0, unit="°C")
    assert classify(coordinator).kind is None  # contradicting evidence
    plant.cloud(PUMP_TYPE, "liquid_water")  # as the gateway reports it
    assert classify(coordinator) == classify(coordinator)
    assert classify(coordinator).kind == "brine"  # the reported type decides


@pytest.mark.parametrize(
    ("reported", "kind"),
    [
        ("air_water", "air"),
        ("exhaustAir_water", "air"),
        ("liquid_water", "brine"),
        ("brine_water", "brine"),
        ("Unknown", None),
    ],
)
def test_the_reported_heat_pump_type_decides(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, reported, kind
) -> None:
    """The gateway's own values (allowed values of ``heatPumpType``) decide
    over readings of the other kind, so a liquid source never defrosts."""
    plant = Plant(hass, freezer)
    plant.local(TL2, 1.9, unitOfMeasure="C")  # air evidence
    plant.cloud(BRINE_IN, 6.0, unit="°C")  # brine evidence
    plant.cloud(PUMP_TYPE, reported)
    result = classify(plant.coordinator)
    assert result.kind == kind
    if kind is not None:
        assert result.evidence == (PUMP_TYPE,)


# ------------------------------------------------------------------ entities


def _entry() -> SimpleNamespace:
    listeners: list[Any] = []
    return SimpleNamespace(async_on_unload=listeners.append, listeners=listeners)


async def test_defrost_entities_only_for_a_defrosting_plant(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    added: list[Any] = []
    plant = brine(hass, freezer)
    for create in (
        derived_defrost.binary_sensors,
        derived_defrost.events,
        derived_defrost.sensors,
    ):
        track_defrost_entities(_entry(), plant.coordinator, added.extend, create)
    plant.coordinator.async_update_listeners()
    assert added == []
    other: list[Any] = []
    plant = air(hass, freezer)
    for create in (
        derived_defrost.binary_sensors,
        derived_defrost.events,
        derived_defrost.sensors,
    ):
        track_defrost_entities(_entry(), plant.coordinator, other.extend, create)
    keys = sorted(entity.translation_key for entity in other)
    assert keys == sorted(
        [
            "calculated_defrost_active",
            "calculated_defrost",
            "calculated_last_defrost",
            "calculated_defrost_count",
            "calculated_defrost_duration",
            "calculated_defrost_heat_withdrawn",
            "calculated_defrost_electricity",
        ]
    )
    enabled = {
        entity.translation_key
        for entity in other
        if entity.entity_registry_enabled_default
    }
    assert enabled == {
        "calculated_defrost_active",
        "calculated_defrost",
        "calculated_last_defrost",
    }
    assert {entity.unique_id for entity in other} == {
        f"gateway-one:heatSources:{entity.translation_key}" for entity in other
    }


async def test_calculated_sensors_are_added_once(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    plant.cloud("/heatSources/actualSupplyTemperature", 25.1, unit="°C")
    plant.local("/heatSources/currentSupplySetpoint", 24.0, unitOfMeasure="C")
    plant.cloud(
        "/heatSources/hs1/workingTime",
        values=({"ch": 1751376, "total": 2000000},),
    )
    plant.cloud("/heatSources/hs1/numberOfStarts", values=({"ch": 92, "total": 117},))
    added: list[Any] = []
    entry = _entry()
    track_calculated_sensors(entry, plant.coordinator, added.extend)
    plant.coordinator.async_update_listeners()
    keys = sorted(entity.translation_key for entity in added)
    assert keys.count("calculated_activity") == 1
    assert keys.count("calculated_net_heat") == 3
    assert "calculated_run_time_per_start_heating" in keys
    assert "calculated_supply_overshoot" in keys
    assert "calculated_last_defrost" in keys
    activity = next(e for e in added if e.translation_key == "calculated_activity")
    assert activity.unique_id == "gateway-one:heatingCircuits:hc1:calculated_activity"
    assert activity.entity_registry_enabled_default
    assert VW1 in activity.extra_resource_paths
    net = [e for e in added if e.translation_key == "calculated_net_heat"]
    assert not any(e.entity_registry_enabled_default for e in net)
    # A brine unit gets the same calculated sensors, without defrost ones.
    brine_added: list[Any] = []
    track_calculated_sensors(
        _entry(), brine(hass, freezer).coordinator, brine_added.extend
    )
    assert {e.translation_key for e in brine_added} == {"calculated_activity"}


async def test_no_activity_sensor_on_a_room_controller(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch: Any
) -> None:
    from custom_components.bosch_buderus_heating import derived_sensors

    monkeypatch.setattr(
        derived_sensors, "context_of", lambda _c: SimpleNamespace(rrc_plant=True)
    )
    added: list[Any] = []
    track_calculated_sensors(_entry(), air(hass, freezer).coordinator, added.extend)
    assert "calculated_activity" not in {e.translation_key for e in added}


# ------------------------------------------------------------------- defrost


def _cycle(plant: Plant, monitor: DefrostMonitor, minutes: float) -> None:
    plant.later(minutes)
    monitor.update(plant.at)


async def _monitor(plant: Plant) -> DefrostMonitor:
    monitor = DefrostMonitor(plant.coordinator, derived_store(plant.hass))
    await monitor.async_load()
    return monitor


async def test_a_defrost_is_reported_with_its_stages_and_energy(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    monitor = await _monitor(plant)
    events: list[dict[str, Any]] = []
    monitor.add_event_listener(events.append)
    monitor.update(plant.at)
    assert monitor.active is False
    # The defrost starts: the heating stage first.
    plant.later(1).local(REFRIGERANT, "defrost")
    monitor.update(plant.at)
    start = plant.at
    assert monitor.active is True and monitor.running
    # The heat counter falls; the heater counter is unchanged.
    plant.later(3).energy(CH, 1233.80, 400.04)
    monitor.update(plant.at)
    plant.later(2).flag(VW1, True)  # the hot water stage
    monitor.update(plant.at)
    plant.later(1).local(REFRIGERANT, "heating").energy(CH, 1233.60, 400.07)
    plant.energy(DHW, 380.21, 400.0)
    monitor.update(plant.at)
    end = plant.at
    assert monitor.active is False and events == []  # waiting for the counters
    plant.later(2).energy(CH, 1233.70, 400.10).energy(DHW, 380.25, 400.0)
    monitor.update(plant.at)
    assert len(events) == 1
    event = events[0]
    assert event["started_at"] == start.isoformat()
    assert event["ended_at"] == end.isoformat()
    assert event["duration_min"] == 6.0
    assert event["stages"] == ["heating", "dhw"]
    assert event["stage_minutes"] == {"heating": 5.0, "dhw": 1.0}
    assert event["heat_withdrawn_kwh"] == pytest.approx(0.64)
    assert event["electricity_kwh"] == pytest.approx(0.0)  # no total update
    assert event["outdoor_temp_start"] == 4.0
    assert event["air_inlet_temp_start"] == 2.6
    assert event["detected_by"] == REFRIGERANT
    assert event["derived_from"][REFRIGERANT] == "local"
    assert monitor.totals == {
        "count": 1.0,
        "duration_min": 6.0,
        "heat_withdrawn_kwh": pytest.approx(0.64),
        "electricity_kwh": 0.0,
    }
    assert monitor.last == event
    # Saved, and read again by a new monitor (a restart).
    await flush_store(derived_store(hass)._store)
    store = DerivedStore(hass)
    await store.async_load()
    restored = DefrostMonitor(plant.coordinator, store)
    await restored.async_load()
    assert restored.totals["count"] == 1.0
    assert restored.last == event


async def test_electricity_is_the_counter_change(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    monitor = await _monitor(plant)
    events: list[dict[str, Any]] = []
    monitor.add_event_listener(events.append)
    monitor.update(plant.at)
    plant.later(1).local(REFRIGERANT, "defrost").energy(TOTAL, 1614.45, 416.20)
    monitor.update(plant.at)
    plant.later(5).local(REFRIGERANT, "heating").energy(TOTAL, 1614.0, 416.27)
    monitor.update(plant.at)
    _cycle(plant, monitor, SETTLE.total_seconds() / 60)  # the counters never rise
    assert events[0]["electricity_kwh"] == pytest.approx(0.07)


async def test_no_defrost_without_a_known_start(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer, refrigerant="defrost")  # already defrosting at the start
    monitor = await _monitor(plant)
    events: list[dict[str, Any]] = []
    monitor.add_event_listener(events.append)
    monitor.update(plant.at)
    plant.later(1).local(REFRIGERANT, "defrost")  # still the same defrost
    monitor.update(plant.at)
    assert not monitor.running
    plant.later(3).local(REFRIGERANT, "heating")
    monitor.update(plant.at)
    _cycle(plant, monitor, 20)
    assert events == [] and monitor.totals["count"] == 0


async def test_a_long_gap_drops_the_defrost(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    monitor = await _monitor(plant)
    events: list[dict[str, Any]] = []
    monitor.add_event_listener(events.append)
    monitor.update(plant.at)
    plant.later(1).local(REFRIGERANT, "defrost")
    monitor.update(plant.at)
    # Every defrost source unreadable for longer than the gap.
    plant.forget(REFRIGERANT).stale_cloud(COMPRESSOR)
    _cycle(plant, monitor, GAP.total_seconds() / 60 + 1)
    assert not monitor.running and monitor.active is None
    plant.later(1).local(REFRIGERANT, "heating")
    monitor.update(plant.at)
    _cycle(plant, monitor, 15)
    assert events == []


async def test_a_new_defrost_finishes_the_settling_one(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    monitor = await _monitor(plant)
    events: list[dict[str, Any]] = []
    monitor.add_event_listener(events.append)
    monitor.update(plant.at)
    for _ in range(2):
        plant.later(1).local(REFRIGERANT, "defrost")
        monitor.update(plant.at)
        plant.later(4).local(REFRIGERANT, "heating")
        monitor.update(plant.at)
    assert len(events) == 1  # the second start finished the first
    _cycle(plant, monitor, SETTLE.total_seconds() / 60)
    assert len(events) == 2 and monitor.totals["count"] == 2


async def test_a_late_dip_after_the_end_still_counts(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A counter that rose again during the defrost is no sign that its dip
    is complete: only a rise after the end is."""
    plant = air(hass, freezer)
    plant.stale_cloud(DHW)  # only the heating counter
    monitor = await _monitor(plant)
    events: list[dict[str, Any]] = []
    monitor.add_event_listener(events.append)
    monitor.update(plant.at)
    plant.later(1).local(REFRIGERANT, "defrost")
    monitor.update(plant.at)
    for minutes, value in ((3, 1233.80), (2, 1233.90)):
        plant.later(minutes).energy(CH, value)
        monitor.update(plant.at)
    plant.later(1).local(REFRIGERANT, "heating")
    monitor.update(plant.at)
    _cycle(plant, monitor, 1)  # no new counter reading yet
    assert events == []
    for value in (1233.70, 1233.95):
        plant.later(2).energy(CH, value)
        monitor.update(plant.at)
    assert len(events) == 1
    assert events[0]["heat_withdrawn_kwh"] == pytest.approx(0.46)


def test_energy_from_power_needs_dense_readings() -> None:
    from custom_components.bosch_buderus_heating.derived_defrost import (
        MAX_POWER_GAP,
        _power_energy,
    )

    start = T0
    end = T0 + timedelta(minutes=6)
    every_minute = [(T0 + timedelta(minutes=m), 0.6) for m in range(7)]
    assert _power_energy(every_minute, start, end) == pytest.approx(0.06)
    # A linear rise between two readings counts as its mean.
    two = start + timedelta(minutes=2)
    assert _power_energy([(start, 0.0), (two, 1.2)], start, two) == 0.02
    assert timedelta(minutes=3) > MAX_POWER_GAP
    gap = [every_minute[0], every_minute[-1]]
    assert _power_energy(gap, start, end) is None  # too sparse
    late = every_minute[3:]
    assert _power_energy(late, start, end) is None  # the start is not covered
    early = every_minute[:3]
    assert _power_energy(early, start, end) is None  # the end is not covered
    assert _power_energy([], start, end) is None


def test_a_declared_error_value_is_no_defrost_power() -> None:
    """The power during a defrost skips the device's declared error values,
    as COP live does."""
    from custom_components.bosch_buderus_heating.derived_defrost import _kilowatts
    from custom_components.bosch_buderus_heating.derived_inputs import Input

    resource = Resource(
        path=POWER,
        value=6553.5,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="floatValue", unit="kW", numeric_error_values=(6553.5,)
        ),
    )
    reading = Input(POWER, 6553.5, "cloud", T0, resource, "kW")
    assert _kilowatts(reading) is None
    assert _kilowatts(replace(reading, value=2.5)) == 2.5
    assert _kilowatts(None) is None


async def test_an_older_power_reading_is_not_taken(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    monitor = await _monitor(plant)
    events: list[dict[str, Any]] = []
    monitor.add_event_listener(events.append)
    monitor.update(plant.at)
    plant.later(1).local(POWER, 600.0, unitOfMeasure="W")
    plant.local(REFRIGERANT, "defrost")
    monitor.update(plant.at)
    plant.later(1).local(POWER, 600.0, unitOfMeasure="W")
    monitor.update(plant.at)
    # Only the other compressor reading, taken half a minute earlier.
    now = plant.at
    plant.at = now - timedelta(seconds=30)
    plant.local(
        "/heatSources/hs1/refrigerant/compressorElecPowerActual",
        6000.0,
        unitOfMeasure="W",
    )
    plant.at = now
    plant.forget(POWER)
    _cycle(plant, monitor, 1)
    plant.later(1).local(POWER, 600.0, unitOfMeasure="W")
    plant.local(REFRIGERANT, "heating")
    monitor.update(plant.at)
    _cycle(plant, monitor, SETTLE.total_seconds() / 60)
    assert events[0]["electricity_kwh_from_power"] == pytest.approx(0.03)


async def test_cloud_compressor_status_as_the_source(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    plant.forget(REFRIGERANT)
    monitor = await _monitor(plant)
    events: list[dict[str, Any]] = []
    monitor.add_event_listener(events.append)
    monitor.update(plant.at)
    plant.later(1).cloud(COMPRESSOR, "defrost")
    monitor.update(plant.at)
    assert monitor.source is not None and monitor.source.origin == "cloud"
    plant.later(4).cloud(COMPRESSOR, "heating")
    monitor.update(plant.at)
    _cycle(plant, monitor, SETTLE.total_seconds() / 60)
    assert events[0]["detected_by"] == COMPRESSOR
    assert events[0]["stages"] == ["heating"]


async def test_monitor_follows_real_coordinator_cycles(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    added: list[Any] = []
    entry = _entry()
    track_defrost_entities(
        entry, plant.coordinator, added.extend, derived_defrost.binary_sensors
    )
    track_defrost_entities(
        entry, plant.coordinator, added.extend, derived_defrost.sensors
    )
    await hass.async_block_till_done()
    binary = added[0]
    for minutes, state in ((1, "defrost"), (5, "heating"), (11, "heating")):
        plant.later(minutes).local(REFRIGERANT, state)
        plant.coordinator.async_update_listeners()
        if state == "defrost":
            assert binary.is_on is True
    assert binary.is_on is False
    counters = {e.translation_key: e for e in added}
    assert counters["calculated_defrost_count"].native_value == 1
    assert counters["calculated_defrost_duration"].native_value == 5.0
    assert counters["calculated_last_defrost"].native_value == T0 + timedelta(minutes=1)
    # Unloading the entry stops the monitor following the coordinator.
    for listener in entry.listeners:
        listener()
    plant.later(1).local(REFRIGERANT, "defrost")
    plant.coordinator.async_update_listeners()
    assert binary.is_on is False


# ---------------------------------------------------------------- net heat


async def test_net_heat_holds_during_a_dip(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    sensor = NetHeatCounterSensor(plant.coordinator, CH)
    sensor.hass = hass
    await sensor.async_added_to_hass()
    shown = []
    for value in (1234.16, 1233.60, 1233.90, 1234.50, 50.0, 51.0, 52.5):
        plant.later(5).energy(CH, value)
        sensor._refresh()
        shown.append(sensor.native_value)
    assert shown == [1234.16, 1234.16, 1234.16, 1234.5, 1234.5, 1235.5, 1237.0]
    assert sensor.extra_state_attributes == {
        "highest_reading": 52.5,
        "derived_from": {CH: "cloud"},
    }
    # A restart continues from the saved total and its last reading.
    await flush_store(derived_store(hass)._store)
    again = NetHeatCounterSensor(plant.coordinator, CH)
    again.hass = hass
    await again.async_added_to_hass()
    plant.energy(CH, 53.0)
    again._refresh()
    assert again.native_value == 1237.5


async def test_net_heat_without_a_reading_is_unavailable(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    plant.stale_cloud(CH)
    sensor = NetHeatCounterSensor(plant.coordinator, CH)
    sensor.hass = hass
    await sensor.async_added_to_hass()
    assert not sensor.available and sensor.native_value is None


# ------------------------------------------------------- run time, overshoot


async def test_run_time_per_start(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    plant.cloud("/heatSources/hs1/workingTime", values=({"ch": 1751376},))
    plant.cloud("/heatSources/hs1/numberOfStarts", values=({"ch": 92},))
    sensor = RunTimePerStartSensor(plant.coordinator, "/heatSources/hs1")
    sensor._refresh()
    assert sensor.native_value == pytest.approx(317.3)
    assert sensor.extra_state_attributes == {
        "operating_hours": 486.49,
        "starts": 92,
        "derived_from": {
            "/heatSources/hs1/numberOfStarts": "cloud",
            "/heatSources/hs1/workingTime": "cloud",
        },
    }
    plant.cloud("/heatSources/hs1/numberOfStarts", values=({"ch": 0},))
    sensor._refresh()
    assert sensor.native_value is None and not sensor.available


async def test_overshoot_only_while_heating(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    plant.cloud("/heatSources/actualSupplyTemperature", 25.1, unit="°C")
    plant.local("/heatSources/currentSupplySetpoint", 24.0, unitOfMeasure="C")
    sensor = SupplyOvershootSensor(
        plant.coordinator,
        "/heatSources/actualSupplyTemperature",
        "/heatSources/currentSupplySetpoint",
        None,
    )
    sensor._refresh()
    assert sensor.native_value == 1.1
    assert sensor.unique_id == "gateway-one:heatSources:calculated_supply_overshoot"
    plant.flag(VW1, True)  # hot water: no overshoot of the heating supply
    sensor._refresh()
    assert sensor.native_value is None
    plant.flag(VW1, False).local(REFRIGERANT, "defrost")
    sensor._refresh()
    assert sensor.native_value is None
    circuit = SupplyOvershootSensor(
        plant.coordinator,
        ROOT + "/actualSupplyTemperature",
        "/signals/SC.HC1.FlowTempSetp",
        ROOT,
    )
    plant.local(REFRIGERANT, "heating")
    plant.cloud(ROOT + "/actualSupplyTemperature", 26.0, unit="°C")
    plant.local("/signals/SC.HC1.FlowTempSetp", 24, unitOfMeasure="C")
    circuit._refresh()
    assert circuit.native_value == 2.0
    plant.local(PUMP, "off")
    circuit._refresh()
    assert circuit.native_value is None


async def test_overshoot_of_the_heat_source_prefers_the_heat_pump_supply(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The heat source's setpoint belongs to the heat pump's supply after
    the heater (TC1); the reported supply temperature (behind a buffer on
    some plants) only stands in."""
    from custom_components.bosch_buderus_heating.derived_sensors import _candidates

    plant = air(hass, freezer)
    plant.cloud("/heatSources/actualSupplyTemperature", 25.1, unit="°C")
    plant.local("/heatSources/currentSupplySetpoint", 24.0, unitOfMeasure="C")
    plant.local("/signals/SRC.IntFlowTemp", 26.8, unitOfMeasure="C")
    create = dict(_candidates(plant.coordinator))["overshoot:/heatSources"]
    sensor = create()
    assert isinstance(sensor, SupplyOvershootSensor)
    sensor._refresh()
    assert sensor.native_value == 2.8
    attributes = sensor.extra_state_attributes or {}
    assert attributes["supply_temperature"] == 26.8
    assert "/signals/SRC.IntFlowTemp" in attributes["derived_from"]
    assert "/heatSources/actualSupplyTemperature" not in attributes["derived_from"]
    assert sensor.unique_id == "gateway-one:heatSources:calculated_supply_overshoot"
    plant.forget("/signals/SRC.IntFlowTemp")
    sensor._refresh()
    assert sensor.native_value == 1.1


def test_a_hybrid_heats_while_its_boiler_burns(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Running when the compressor or the burner runs; idle only when every
    source with a reading stands. COP live follows the compressor alone."""
    from custom_components.bosch_buderus_heating.derived_activity import (
        heat_source_running,
    )
    from custom_components.bosch_buderus_heating.performance import live_cop

    plant = air(hass, freezer)
    coordinator = plant.coordinator
    plant.flag(COMPRESSOR_ACTIVE, False).cloud("/heatSources/flameStatus", "on")
    assert heat_source_running(coordinator) is True
    assert circuit_activity(coordinator, ROOT).value == "heating"
    assert live_cop(coordinator).attributes.get("reason") == "compressor_off"
    plant.cloud("/heatSources/flameStatus", "off")
    assert heat_source_running(coordinator) is False
    assert circuit_activity(coordinator, ROOT).value == "idle"
    plant.flag(COMPRESSOR_ACTIVE, True)
    assert circuit_activity(coordinator, ROOT).value == "heating"
    # A boiler alone: the burner decides.
    boiler = Plant(hass, freezer)
    boiler.cloud(MODE, "auto").cloud("/heatSources/flameStatus", "off")
    boiler.coordinator.local_selection = None
    assert heat_source_running(boiler.coordinator) is False
    boiler.cloud("/heatSources/flameStatus", "on")
    assert heat_source_running(boiler.coordinator) is True
    assert heat_source_running(Plant(hass, freezer).coordinator) is None


async def test_net_heat_restored_newer_than_saved_counts_nothing_twice(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch
) -> None:
    """The restored state is newer than the saved total: the reading it was
    counted up to moves on by as much, so that part is not counted again."""
    from homeassistant.components.sensor import SensorExtraStoredData

    plant = air(hass, freezer)
    plant.energy(CH, 1010.0)  # the restored state was counted up to this
    sensor = NetHeatCounterSensor(plant.coordinator, CH)
    sensor.hass = hass
    store = derived_store(hass)
    await store.async_load()
    store.set(str(sensor.unique_id), {"total": 1000.0, "reference": 1000.0})

    async def restored():
        return SensorExtraStoredData(1010.0, "kWh")

    monkeypatch.setattr(sensor, "async_get_last_sensor_data", restored)
    await sensor.async_added_to_hass()
    sensor._refresh()
    assert sensor.native_value == 1010.0
    plant.energy(CH, 1011.0)
    sensor._refresh()
    assert sensor.native_value == 1011.0


# ------------------------------------------------------------- climate hooks


async def test_climate_hvac_action(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    from tests.test_heating_controls import setup

    _, coordinator, climate = setup(hass)
    assert climate.hvac_action is None  # no heat source readings
    resources = [
        Resource(
            path=COMPRESSOR,
            value="heating",
            has_value=True,
            metadata=ResourceMetadata(resource_type="stringValue"),
        ),
        Resource(
            path=climate.root + "/pumpModulation",
            value=30.0,
            has_value=True,
            metadata=ResourceMetadata(resource_type="floatValue", unit="%"),
        ),
    ]
    for resource in resources:
        coordinator.resources[resource.path] = resource
        coordinator.data[resource.path] = ResourceSnapshot(
            resource, True, datetime.now(UTC)
        )
    assert climate.hvac_action is HVACAction.HEATING
    defrost = replace(resources[0], value="defrost")
    coordinator.data[COMPRESSOR] = ResourceSnapshot(defrost, True, datetime.now(UTC))
    assert climate.hvac_action is HVACAction.DEFROSTING
    assert VW1 in climate.extra_resource_paths


def test_rrc_zone_shows_the_plant_defrost(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    from custom_components.bosch_buderus_heating import rrc_climate

    plant = air(hass, freezer, refrigerant="defrost")
    fake = SimpleNamespace(
        available=True,
        coordinator=plant.coordinator,
        _lock=lambda *, setpoint: None,
        _detail=lambda _leaf: "heat_request",
    )
    action = rrc_climate.RrcZoneClimate.hvac_action.fget(fake)  # type: ignore[attr-defined]
    assert action is HVACAction.DEFROSTING
    plant.local(REFRIGERANT, "heating")
    assert rrc_climate.RrcZoneClimate.hvac_action.fget(fake) is HVACAction.HEATING  # type: ignore[attr-defined]
    brine_plant = brine(hass, freezer)
    brine_plant.local(REFRIGERANT, "defrost")
    fake.coordinator = brine_plant.coordinator
    assert rrc_climate.RrcZoneClimate.hvac_action.fget(fake) is HVACAction.HEATING  # type: ignore[attr-defined]


# ----------------------------------------------------------- heat generator


def test_unnamed_modules_are_counted_without_serials() -> None:
    rows = (
        {"ModuleHwIdentStr": "HMI_40", "ModuleSerialNumber": "123"},
        {"ModuleHwIdentStr": "HMI_40"},
        {"ProductName": "WLW176i", "ModuleHwIdentStr": "IDU"},
        {"ModuleHwIdentStr": "987654321", "No": "987654321"},
        {},
    )
    assert unnamed_hardware_cloud(rows) == {"HMI_40": 2, "unknown": 2}


async def test_heat_generator_sensor_from_evidence_alone(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    from custom_components.bosch_buderus_heating.heat_generator_sensor import (
        heat_generator_diagnostics,
        track_heat_generator_sensor,
    )

    plant = brine(hass, freezer)
    # Only the gateway and the user interface: rows without a product name.
    plant.cloud(
        "/system/info",
        values=({"ModuleHwIdentStr": "HMI_40"}, {"ModuleHwIdentStr": "GW_K40"}),
        kind="arrayData",
    )
    added: list[Any] = []
    track_heat_generator_sensor(_entry(), plant.coordinator, added.extend)
    assert len(added) == 1
    sensor = added[0]
    sensor._refresh()
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {
        "heat_source_type": "brine",
        "known": False,
    }
    report = heat_generator_diagnostics(plant.coordinator)
    assert report is not None
    assert report["heat_source_type"] == "brine"
    assert report["heat_source_type_evidence"] == [BRINE_IN]
    assert report["unnamed_modules"] == {"cloud": {"GW_K40": 1, "HMI_40": 1}}
    # Without any evidence and without a named module: no entity.
    other: list[Any] = []
    bare = Plant(hass, freezer)
    bare.cloud("/system/info", values=({"ModuleHwIdentStr": "HMI_40"},))
    track_heat_generator_sensor(_entry(), bare.coordinator, other.extend)
    assert other == []


async def test_heat_generator_sensor_names_the_kind(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    from custom_components.bosch_buderus_heating.heat_generator_sensor import (
        BoschBuderusHeatGeneratorSensor,
    )

    plant = air(hass, freezer)
    plant.cloud(
        "/system/info",
        values=({"ProductName": "WLW176i-12 TP70", "ModuleHwIdentStr": "IDU"},),
        kind="arrayData",
    )
    sensor = BoschBuderusHeatGeneratorSensor(plant.coordinator)
    sensor._refresh()
    attributes = sensor.extra_state_attributes
    assert attributes is not None
    assert attributes["heat_source_type"] == "air"
    assert sensor.native_value == "WLW176i-12 TP70"


async def test_replay_of_a_recorded_defrost(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The readings of one defrost as a real installation recorded them, in
    seconds after the first one: the refrigerant circuit status about every
    minute, the counters every five minutes."""
    plant = air(hass, freezer)
    plant.energy(CH, 1233.92).energy(DHW, 380.29).energy(TOTAL, 1614.21, 392.62)
    monitor = await _monitor(plant)
    events: list[dict[str, Any]] = []
    monitor.add_event_listener(events.append)
    timeline: list[tuple[int, list[tuple[str, object]]]] = [
        (0, []),
        (300, [(CH, 1234.16), (TOTAL, 392.68)]),
        (429, [(POWER, 675.0)]),
        (435, [(REFRIGERANT, "defrost")]),
        (489, [(POWER, 119.0)]),
        (549, [(POWER, 675.0)]),
        (600, [(CH, 1233.91), (TOTAL, 392.72)]),
        (609, [(POWER, 675.0)]),
        (669, [(POWER, 676.0)]),
        (729, [(POWER, 903.0)]),
        (757, [(VW1, True)]),
        (789, [(POWER, 351.0)]),
        (795, [(REFRIGERANT, "heating")]),
        (817, [(VW1, False)]),
        (900, [(CH, 1233.60), (TOTAL, 392.78)]),
        (906, [(DHW, 380.21)]),
        (1200, [(CH, 1233.91), (TOTAL, 392.82)]),
        (1500, [(CH, 1234.22), (TOTAL, 392.86)]),
    ]
    elapsed = 0
    for second, changes in timeline:
        plant.later((second - elapsed) / 60)
        elapsed = second
        for path, value in changes:
            if path == REFRIGERANT:
                plant.local(path, value)
            elif path == POWER:
                plant.local(path, value, unitOfMeasure="W")
            elif path == VW1:
                plant.flag(path, bool(value))
            elif path == TOTAL:
                plant.energy(path, 1614.0, float(value))  # type: ignore[arg-type]
            else:
                plant.energy(path, float(value))  # type: ignore[arg-type]
        # The cycles in between (about every 60 s) see the same readings.
        monitor.update(plant.at)
    assert len(events) == 1
    event = events[0]
    assert event["duration_min"] == 6.0
    assert event["stages"] == ["heating", "dhw"]
    assert event["stage_minutes"] == {"heating": 5.4, "dhw": 0.6}
    # The dip of both heat counters, the last of it after the defrost.
    assert event["heat_withdrawn_kwh"] == pytest.approx(0.64)
    assert event["electricity_kwh"] == pytest.approx(0.04)
    # From the compressor power: lower than a constant 675 W would give,
    # as the power fell to 119 W for a minute.
    assert event["electricity_kwh_from_power"] == pytest.approx(0.059, abs=0.001)


async def test_the_defrost_entity_names_the_origin_the_monitor_uses(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The cloud offers the refrigerant status as well: when the shown source
    of the same reading changes, the entity is written again, so its
    ``derived_from`` always matches the diagnostics."""
    from custom_components.bosch_buderus_heating.derived_defrost import (
        DefrostActiveBinarySensor,
    )

    plant = air(hass, freezer)
    plant.forget(REFRIGERANT).cloud(REFRIGERANT, "heating")
    monitor = await _monitor(plant)
    active = DefrostActiveBinarySensor(plant.coordinator, monitor)
    writes: list[dict[str, object] | None] = []
    monitor.add_listener(lambda: writes.append(active.extra_state_attributes))
    monitor.update(plant.at)
    assert writes == [
        {"detected_by": REFRIGERANT, "derived_from": {REFRIGERANT: "cloud"}}
    ]
    # The same state, now shown from the local access (the pair rule).
    cloud = plant.coordinator.data[REFRIGERANT]
    plant.selection.paired[REFRIGERANT] = replace(cloud, source=SnapshotSource.LOCAL)
    _cycle(plant, monitor, 1)
    assert monitor.diagnostics()["source_origin"] == "local"
    assert writes[-1] == {
        "detected_by": REFRIGERANT,
        "derived_from": {REFRIGERANT: "local"},
    }
    assert len(writes) == 2
    _cycle(plant, monitor, 1)  # nothing changed: not written again
    assert len(writes) == 2
