"""Issue #107: energy estimated from a heat source's reported current power.

Synthetic data only, shaped like an appliance that reports
``/heatSources/hs1/actualPower`` in kW but no consumption counter.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from math import inf, nan
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import restore_state
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.restore_state import RestoredExtraData
from homeassistant.util import dt as dt_util

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
    SnapshotSource,
)
from custom_components.bosch_buderus_heating.derived import (
    MAX_ESTIMATION_GAP,
    PowerSample,
    estimated_energy_kwh,
    estimation_gap_limit,
    power_kilowatts,
)
from custom_components.bosch_buderus_heating.estimated_energy import (
    BoschBuderusEstimatedEnergySensor,
    estimated_energy_paths,
    estimated_energy_unique_id,
)
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    Resource,
    ResourceMetadata,
)
from custom_components.bosch_buderus_heating.pointt.metrics import RequestMetrics
from custom_components.bosch_buderus_heating.sensor import async_setup_entry
from tests.ha_test_helpers import MockConfigEntry, mock_restore_cache

POWER = "/heatSources/hs1/actualPower"
UNIQUE_ID = "gateway-one:heatSources:hs1:actualPower:estimated_energy"
ENTITY_ID = "sensor.estimated_energy"
T0 = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
_COORDINATORS: list[BoschBuderusDataUpdateCoordinator] = []


@pytest.fixture(autouse=True)
async def _stop_coordinators() -> AsyncGenerator[None]:
    """Stop the refresh timer a coordinator starts with its first listener.
    Home Assistant 2026.5 does not cancel it on shutdown by itself, so the
    test teardown would report it as lingering."""
    yield
    while _COORDINATORS:
        await _COORDINATORS.pop().async_shutdown()


def _power(value: object = 6.0, *, path: str = POWER, unit: str = "kW") -> Resource:
    return Resource(
        path=path,
        value=value,  # type: ignore[arg-type]
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="floatValue", unit=unit, numeric_error_values=(-3276.8,)
        ),
    )


def _counter(path: str) -> Resource:
    return Resource(
        path=path,
        value=1234.0,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="kWh"),
    )


def _coordinator(
    hass: HomeAssistant, *resources: Resource
) -> BoschBuderusDataUpdateCoordinator:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-one", device_type="MX300"), entry
    )
    resources = resources or (_power(),)
    coordinator.resources = {item.path: item for item in resources}
    now = dt_util.utcnow()
    coordinator.data = {
        item.path: ResourceSnapshot(item, True, now) for item in resources
    }
    coordinator.last_update_success = True
    _COORDINATORS.append(coordinator)
    return coordinator


def _read(
    coordinator: BoschBuderusDataUpdateCoordinator,
    value: object,
    *,
    available: bool = True,
    freshness: Freshness = Freshness.FRESH,
    source: SnapshotSource = SnapshotSource.BATCH,
) -> None:
    """Store one new reading of the power, taken now."""
    coordinator.data[POWER] = ResourceSnapshot(
        _power(value),
        available,
        dt_util.utcnow(),
        source=source,
        freshness=freshness,
    )


# ------------------------------------------------------------ pure calculation


def test_known_series_is_a_left_riemann_sum() -> None:
    series = ((0, 6.0), (60, 3.0), (120, 0.0), (300, 12.0), (360, 1.0))
    samples = [
        PowerSample(T0 + timedelta(seconds=second), power, "cloud")
        for second, power in series
    ]
    limit = estimation_gap_limit(timedelta(minutes=1))
    total = sum(
        estimated_energy_kwh(previous, current, limit)
        for previous, current in zip([None, *samples], samples, strict=False)
    )
    # 6 kW for 60 s + 3 kW for 60 s + 0 kW for 180 s + 12 kW for 60 s.
    assert total == pytest.approx(0.1 + 0.05 + 0.0 + 0.2)


def test_gap_limit_is_three_intervals_and_at_most_thirty_minutes() -> None:
    assert estimation_gap_limit(timedelta(minutes=1)) == timedelta(minutes=3)
    assert estimation_gap_limit(timedelta(minutes=2)) == timedelta(minutes=6)
    assert estimation_gap_limit(timedelta(minutes=15)) == MAX_ESTIMATION_GAP
    assert estimation_gap_limit(None) == timedelta(minutes=30)
    previous = PowerSample(T0, 6.0, "cloud")
    limit = timedelta(minutes=3)
    at_limit = PowerSample(T0 + limit, 1.0, "cloud")
    beyond = PowerSample(T0 + limit + timedelta(seconds=1), 1.0, "cloud")
    assert estimated_energy_kwh(previous, at_limit, limit) == pytest.approx(0.3)
    assert estimated_energy_kwh(previous, beyond, limit) == 0.0
    # A change of origin counts (the same power), an older reading never.
    other = PowerSample(T0 + timedelta(seconds=60), 1.0, "local")
    older = PowerSample(T0 - timedelta(seconds=60), 1.0, "cloud")
    assert estimated_energy_kwh(previous, other, limit) == pytest.approx(0.1)
    assert estimated_energy_kwh(previous, older, limit) == 0.0
    later_other = PowerSample(T0 + limit + timedelta(seconds=1), 1.0, "local")
    assert estimated_energy_kwh(previous, later_other, limit) == 0.0


@pytest.mark.parametrize(
    ("value", "unit"),
    [
        (-0.1, "kW"),
        (nan, "kW"),
        (inf, "kW"),
        (-inf, "kW"),
        (True, "kW"),
        ("6.0", "kW"),
        (None, "kW"),
        (32767.0, "kW"),
        (-3276.8, "kW"),
        (6.0, "kWh"),
        (6.0, None),
    ],
)
def test_invalid_power_is_never_a_reading(value: object, unit: str | None) -> None:
    assert power_kilowatts(value, unit, (-3276.8,)) is None


def test_power_in_watts_is_converted() -> None:
    assert power_kilowatts(4200, "W") == pytest.approx(4.2)
    assert power_kilowatts(0, "kW") == 0.0


# ------------------------------------------------------------------ creation


def test_only_heat_sources_without_a_counter_get_an_estimate() -> None:
    hs2 = "/heatSources/hs2/actualPower"
    resources = {POWER: _power(), hs2: _power(path=hs2)}
    assert estimated_energy_paths(resources) == (POWER, hs2)
    # A counter of one heat source covers that source only.
    own = {**resources, "/heatSources/hs2/emon/totalConsumption": _counter("x")}
    assert estimated_energy_paths(own) == (POWER,)
    # A counter of the plant covers every heat source.
    for path in (
        "/heatSources/gasTotalConsumption",
        "/heatSources/electricityTotalConsumption",
        "/heatSources/emon/totalConsumption",
    ):
        assert estimated_energy_paths({**resources, path: _counter(path)}) == ()
    # A counter known from an earlier start but not read yet also counts.
    known = ("/heatSources/gasTotalConsumption",)
    assert estimated_energy_paths(resources, known) == ()
    # Only a power reading in kW or W.
    assert estimated_energy_paths({POWER: _power(unit="%")}) == ()
    assert estimated_energy_paths({"/heatSources/actualPower": _power()}) == ()


async def test_created_disabled_on_the_platform(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    entry = SimpleNamespace(
        entry_id="entry-one",
        async_on_unload=Mock(),
        runtime_data=SimpleNamespace(
            bacon=None,
            client=SimpleNamespace(metrics=RequestMetrics()),
            coordinators=(coordinator,),
        ),
    )
    added: list[object] = []

    await async_setup_entry(hass, entry, added.extend)

    estimates = [
        item for item in added if isinstance(item, BoschBuderusEstimatedEnergySensor)
    ]
    assert len(estimates) == 1
    sensor = estimates[0]
    assert sensor.unique_id == UNIQUE_ID
    assert sensor.entity_registry_enabled_default is False
    assert sensor.device_class is SensorDeviceClass.ENERGY
    assert sensor.state_class is SensorStateClass.TOTAL_INCREASING
    assert sensor.native_unit_of_measurement == UnitOfEnergy.KILO_WATT_HOUR
    assert sensor.translation_key == "estimated_energy"
    assert sensor.name.endswith("Energy (estimated from current power)")
    assert "gas" not in sensor.name.casefold()
    # Another cycle with the same resources adds nothing twice.
    coordinator.async_update_listeners()
    estimates = [
        item for item in added if isinstance(item, BoschBuderusEstimatedEnergySensor)
    ]
    assert len(estimates) == 1


async def test_not_created_with_a_native_counter(hass: HomeAssistant) -> None:
    coordinator = _coordinator(
        hass, _power(), _counter("/heatSources/gasTotalConsumption")
    )
    entry = SimpleNamespace(
        entry_id="entry-one",
        async_on_unload=Mock(),
        runtime_data=SimpleNamespace(
            bacon=None,
            client=SimpleNamespace(metrics=RequestMetrics()),
            coordinators=(coordinator,),
        ),
    )
    added: list[object] = []

    await async_setup_entry(hass, entry, added.extend)

    assert not any(
        isinstance(item, BoschBuderusEstimatedEnergySensor) for item in added
    )


def test_unique_id_is_stable(hass: HomeAssistant) -> None:
    assert estimated_energy_unique_id("gateway-one", POWER) == UNIQUE_ID
    hass.config.language = "de"
    sensor = BoschBuderusEstimatedEnergySensor(_coordinator(hass), POWER)
    assert sensor.unique_id == UNIQUE_ID  # independent of language and order
    assert sensor.name.endswith("Energie (geschätzt aus aktueller Leistung)")


# ---------------------------------------------------------------- integration


def test_entity_integrates_fresh_readings(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    coordinator = _coordinator(hass)
    sensor = BoschBuderusEstimatedEnergySensor(coordinator, POWER)
    _read(coordinator, 6.0)
    sensor._sample()
    assert sensor.available
    assert sensor.native_value == 0.0  # the first reading only starts
    for power in (3.0, 0.0, 12.0):
        freezer.tick(timedelta(seconds=60))
        _read(coordinator, power)
        sensor._sample()
    # 6 kW, 3 kW and 0 kW for one minute each.
    assert sensor.native_value == pytest.approx(0.15)
    # The same reading again (a kept value) adds nothing.
    freezer.tick(timedelta(seconds=30))
    sensor._sample()
    assert sensor.native_value == pytest.approx(0.15)
    freezer.tick(timedelta(seconds=30))
    _read(coordinator, 12.0)
    sensor._sample()
    assert sensor.native_value == pytest.approx(0.35)
    attributes = sensor.extra_state_attributes
    assert attributes["estimate"] is True
    assert attributes["method"] == "integration of the reported current power"
    assert attributes["sampling_interval_minutes"] == 1.0
    assert attributes["max_gap_minutes"] == 3.0


def test_no_counting_across_a_long_gap(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    coordinator = _coordinator(hass)
    sensor = BoschBuderusEstimatedEnergySensor(coordinator, POWER)
    _read(coordinator, 6.0)
    sensor._sample()
    freezer.tick(timedelta(minutes=3, seconds=1))
    _read(coordinator, 6.0)
    sensor._sample()
    assert sensor.native_value == 0.0  # gap over three intervals: not counted
    freezer.tick(timedelta(seconds=60))
    _read(coordinator, 6.0)
    sensor._sample()
    assert sensor.native_value == pytest.approx(0.1)  # restarted at the gap


@pytest.mark.parametrize(
    "break_reading",
    [
        {"value": 6.0, "available": False},
        {"value": 6.0, "freshness": Freshness.STALE},
        {"value": -3276.8},  # declared error value
        {"value": -1.0},
        {"value": nan},
        {"value": inf},
        {"value": "6.0"},
    ],
)
def test_unavailable_and_invalid_readings_are_not_bridged(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, break_reading: dict
) -> None:
    coordinator = _coordinator(hass)
    sensor = BoschBuderusEstimatedEnergySensor(coordinator, POWER)
    _read(coordinator, 6.0)
    sensor._sample()
    freezer.tick(timedelta(seconds=60))
    _read(coordinator, **break_reading)
    sensor._sample()
    assert sensor.native_value == 0.0
    freezer.tick(timedelta(seconds=60))
    _read(coordinator, 6.0)
    sensor._sample()
    # Neither the interval before nor the one after the break counts.
    assert sensor.native_value == 0.0
    assert sensor.available  # the total stays
    freezer.tick(timedelta(seconds=60))
    _read(coordinator, 6.0)
    sensor._sample()
    assert sensor.native_value == pytest.approx(0.1)


def test_expired_reading_is_not_bridged(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    coordinator = _coordinator(hass)
    sensor = BoschBuderusEstimatedEnergySensor(coordinator, POWER)
    _read(coordinator, 6.0)
    sensor._sample()
    freezer.tick(timedelta(minutes=4))  # beyond the validity limit, no new read
    sensor._sample()
    _read(coordinator, 6.0)
    sensor._sample()
    assert sensor.native_value == 0.0


def test_alternating_local_and_cloud_readings_are_integrated(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    coordinator = _coordinator(hass)
    sensor = BoschBuderusEstimatedEnergySensor(coordinator, POWER)
    _read(coordinator, 6.0)
    sensor._sample()
    sources = (SnapshotSource.LOCAL, SnapshotSource.BATCH, SnapshotSource.LOCAL)
    for count, source in enumerate(sources, start=1):
        freezer.tick(timedelta(seconds=60))
        _read(coordinator, 6.0, source=source)
        sensor._sample()
        # No interval is lost at a switch: 6 kW for one minute each.
        assert sensor.native_value == pytest.approx(0.1 * count)
    # An older reading of the other access never steps back.
    coordinator.data[POWER] = replace(
        coordinator.data[POWER],
        source=SnapshotSource.BATCH,
        last_success=dt_util.utcnow() - timedelta(seconds=30),
    )
    sensor._sample()
    freezer.tick(timedelta(seconds=60))
    _read(coordinator, 6.0, source=SnapshotSource.LOCAL)
    sensor._sample()
    assert sensor.native_value == pytest.approx(0.4)
    # A switch after a gap beyond the limit is not bridged.
    freezer.tick(timedelta(minutes=3, seconds=1))
    _read(coordinator, 6.0)
    sensor._sample()
    assert sensor.native_value == pytest.approx(0.4)


def test_total_never_decreases(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    coordinator = _coordinator(hass)
    sensor = BoschBuderusEstimatedEnergySensor(coordinator, POWER)
    values = []
    for power in (5.0, -2.0, 0.0, nan, 7.5, 0.0, 3.0, -0.5, 2.0):
        _read(coordinator, power)
        sensor._sample()
        values.append(sensor.native_value)
        freezer.tick(timedelta(seconds=60))
    # An older reading arriving late never steps back either.
    coordinator.data[POWER] = replace(
        coordinator.data[POWER], last_success=dt_util.utcnow() - timedelta(minutes=5)
    )
    sensor._sample()
    values.append(sensor.native_value)
    assert all(later >= earlier for earlier, later in pairwise(values))
    assert values[-1] == pytest.approx((7.5 + 0.0) / 60)


# -------------------------------------------------------------------- restore


def _stored(hass: HomeAssistant, native_value: object, unit: str = "kWh") -> None:
    mock_restore_cache(hass, [State(ENTITY_ID, str(native_value))])
    data = restore_state.async_get(hass)
    # Home Assistant 2026.11 keys the restored states by entity ID and by
    # registry ID; older releases have one mapping.
    states = getattr(data, "last_states_by_entity_id", None)
    if states is None:
        states = data.last_states
    states[ENTITY_ID].extra_data = RestoredExtraData(
        json.loads(
            json.dumps(
                {"native_value": native_value, "native_unit_of_measurement": unit}
            )
        )
    )


async def _add(hass: HomeAssistant, sensor: BoschBuderusEstimatedEnergySensor):
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    sensor.entity_id = ENTITY_ID
    # Enabled by the user; without a registry entry for the test.
    sensor._attr_entity_registry_enabled_default = True
    with patch.object(
        BoschBuderusEstimatedEnergySensor,
        "device_info",
        new_callable=PropertyMock,
        return_value=None,
    ):
        await component.async_add_entities([sensor])
    await hass.async_block_till_done()


async def test_restored_total_continues_and_first_reading_only_starts(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    _stored(hass, 12.5)
    coordinator = _coordinator(hass)
    sensor = BoschBuderusEstimatedEnergySensor(coordinator, POWER)
    await _add(hass, sensor)
    # The reading present at the start counts nothing yet.
    assert hass.states.get(ENTITY_ID).state == "12.5"
    freezer.tick(timedelta(seconds=60))
    _read(coordinator, 3.0)
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    # The reading at the start (6 kW) applies until this one.
    assert float(hass.states.get(ENTITY_ID).state) == pytest.approx(12.6)
    stored = sensor.extra_restore_state_data.as_dict()
    assert stored["native_unit_of_measurement"] == "kWh"
    assert stored["native_value"] == pytest.approx(12.6)


@pytest.mark.parametrize(
    ("value", "unit"), [(-5.0, "kWh"), ("nan", "kWh"), ("text", "kWh"), (5.0, "Wh")]
)
async def test_invalid_stored_total_starts_from_zero(
    hass: HomeAssistant, value: object, unit: str
) -> None:
    _stored(hass, value, unit)
    sensor = BoschBuderusEstimatedEnergySensor(_coordinator(hass), POWER)
    await _add(hass, sensor)
    assert hass.states.get(ENTITY_ID).state == "0.0"


async def test_without_a_stored_total_it_starts_at_zero(hass: HomeAssistant) -> None:
    mock_restore_cache(hass, [])
    sensor = BoschBuderusEstimatedEnergySensor(_coordinator(hass), POWER)
    await _add(hass, sensor)
    assert hass.states.get(ENTITY_ID).state == "0.0"


@pytest.mark.parametrize(
    ("saved", "restored", "start"),
    [(14.0, 12.5, "14.0"), (11.0, 12.5, "12.5"), (None, 12.5, "12.5")],
)
async def test_the_higher_of_saved_and_restored_total_is_the_start(
    hass: HomeAssistant, hass_storage: dict, saved, restored, start
) -> None:
    from custom_components.bosch_buderus_heating import estimated_energy

    if saved is not None:
        hass_storage[estimated_energy.STORAGE_KEY] = {
            "version": 1,
            "minor_version": 1,
            "key": estimated_energy.STORAGE_KEY,
            "data": {"totals": {UNIQUE_ID: saved, "other": "damaged"}},
        }
    _stored(hass, restored)
    sensor = BoschBuderusEstimatedEnergySensor(_coordinator(hass), POWER)
    await _add(hass, sensor)
    assert hass.states.get(ENTITY_ID).state == start


async def test_the_running_total_is_saved_within_a_minute(
    hass: HomeAssistant, hass_storage: dict, freezer: FrozenDateTimeFactory
) -> None:
    from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE

    from custom_components.bosch_buderus_heating import estimated_energy
    from tests.ha_test_helpers import async_fire_time_changed

    _stored(hass, 12.5)
    coordinator = _coordinator(hass)
    sensor = BoschBuderusEstimatedEnergySensor(coordinator, POWER)
    await _add(hass, sensor)
    key = estimated_energy.STORAGE_KEY
    assert key not in hass_storage  # nothing new to save yet
    published = []
    for _ in range(3):
        freezer.tick(timedelta(seconds=30))
        _read(coordinator, 6.0)
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
        published.append(float(hass.states.get(ENTITY_ID).state))
    assert published == sorted(published) and published[-1] > 12.5
    # Readings every 30 s never postpone the save beyond a minute.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert hass_storage[key]["data"]["totals"][UNIQUE_ID] == pytest.approx(
        sensor._total
    )
    freezer.tick(timedelta(seconds=30))
    _read(coordinator, 6.0)
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    # Shutdown writes the newest total without waiting.
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()
    assert hass_storage[key]["data"]["totals"][UNIQUE_ID] == pytest.approx(
        sensor._total
    )


async def test_a_lower_later_start_is_never_published_within_a_run(
    hass: HomeAssistant,
) -> None:
    _stored(hass, 12.5)
    coordinator = _coordinator(hass)
    sensor = BoschBuderusEstimatedEnergySensor(coordinator, POWER)
    await _add(hass, sensor)
    sensor._raise_total(3.0)  # for example a late, lower restore
    assert sensor.native_value == 12.5
