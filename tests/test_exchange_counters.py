"""P50 (4B): counters after a gateway exchange never jump in the statistics."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.bosch_buderus_heating import gateway_exchange
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import ResourceSnapshot
from custom_components.bosch_buderus_heating.exchange_counters import (
    RecordedValue,
    async_last_recorded_value,
)
from custom_components.bosch_buderus_heating.gateway_exchange import (
    CounterRecord,
    ModuleFingerprints,
    module_fingerprints,
)
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata
from tests.ha_test_helpers import add_without_device
from tests.test_start_counters import (
    GLOBAL,
    coordinator_for,
    global_resource,
    recorder_runtime_annotations,  # noqa: F401 - fixture
    sensors_for,
)

TARGET = "target-1"
ENERGY = "/heatSources/emon/totalConsumption"
RECORDED = "custom_components.bosch_buderus_heating.exchange_counters"
RECORDED += ".async_last_recorded_value"


def energy(compressor: float, eheater: float, produced: float) -> Resource:
    return Resource(
        ENERGY,
        values=(
            {"compressor": compressor, "eheater": eheater, "outputProduced": produced},
        ),
        metadata=ResourceMetadata(resource_type="emonValue"),  # unitless = kWh
    )


def _environmental(sensors):
    return next(
        sensor
        for sensor in sensors.values()
        if sensor.entity_description.value_kind == "environmental_energy"
    )


class Plant:
    """A managed coordinator with one counter sensor added to HA."""

    def __init__(self, hass: HomeAssistant, resource: Resource, *, managed=True):
        self.hass, self.resource = hass, resource
        self.entry, self.coordinator = coordinator_for(hass, resource)
        if managed:
            self.coordinator.source = SimpleNamespace(
                binding=SimpleNamespace(target_id=TARGET)
            )
        self.sensor = None

    def pick(self, sensors):
        if self.resource.path == ENERGY:
            return _environmental(sensors)
        return sensors[GLOBAL, None]

    async def add(self):
        self.sensor = self.pick(sensors_for(self.coordinator))
        await add_without_device(self.hass, self.sensor)
        await self.hass.async_block_till_done(wait_background_tasks=True)
        return self.sensor

    async def remove(self):
        await self.sensor.async_remove()
        await self.hass.async_block_till_done(wait_background_tasks=True)

    async def show(self, resource: Resource):
        self.coordinator.async_set_updated_data(
            {resource.path: ResourceSnapshot(resource, True, datetime.now(UTC))}
        )
        await self.hass.async_block_till_done(wait_background_tasks=True)

    @property
    def state(self):
        return self.hass.states.get(self.sensor.entity_id)


@pytest.fixture
async def sensor_platform(hass):
    assert await async_setup_component(hass, "sensor", {})


async def _exchanged(hass, plant, *, ago=timedelta(hours=1)) -> str:
    at = (dt_util.utcnow() - ago).isoformat()
    await module_fingerprints(hass, plant.entry.entry_id).async_record_exchange(
        TARGET, at
    )
    return at


async def _restart(hass, plant):
    """Save pending data, drop the shared store and add the entity again."""
    await plant.remove()
    await hass.async_block_till_done()
    store = hass.data[gateway_exchange._FINGERPRINTS].pop(plant.entry.entry_id)
    await store._store.async_save(store._data())
    return await plant.add()


def _recorded(value, unit=None):
    return AsyncMock(return_value=RecordedValue(value, unit))


# ------------------------------------------------------- total_increasing


async def test_a_continued_counter_passes_and_never_asks_again(
    hass, sensor_platform
) -> None:
    plant = Plant(hass, global_resource(105))
    at = await _exchanged(hass, plant)
    with patch(RECORDED, _recorded(100.0)) as recorded:
        sensor = await plant.add()
        assert plant.state.state == "105"
        recorded.assert_awaited_once()
        assert recorded.await_args.args[1:] == (
            sensor.entity_id,
            dt_util.parse_datetime(at),
        )
        stored = module_fingerprints(hass, plant.entry.entry_id).counter(
            sensor.unique_id
        )
        assert stored == CounterRecord(at, True, None)
        await _restart(hass, plant)
        assert plant.state.state == "105"
        recorded.assert_awaited_once()  # settled: no second query


async def test_a_large_drop_is_a_meter_reset_for_ha(hass, sensor_platform) -> None:
    plant = Plant(hass, global_resource(10))
    await _exchanged(hass, plant)
    with patch(RECORDED, _recorded(1000.0)):
        sensor = await plant.add()
    assert plant.state.state == "10"
    assert "last_reset" not in plant.state.attributes
    stored = module_fingerprints(hass, plant.entry.entry_id).counter(sensor.unique_id)
    assert stored is not None and stored.settled and stored.last_reset is None


async def test_a_small_drop_shows_a_gap_until_the_counter_caught_up(
    hass, sensor_platform
) -> None:
    plant = Plant(hass, global_resource(95))
    await _exchanged(hass, plant)
    with patch(RECORDED, _recorded(100.0)) as recorded:
        sensor = await plant.add()
        store = module_fingerprints(hass, plant.entry.entry_id)
        assert plant.state.state == STATE_UNKNOWN
        await plant.show(global_resource(99))
        assert plant.state.state == STATE_UNKNOWN
        assert store.counter(sensor.unique_id) is None
        # A restart while the gap lasts asks again and keeps the gap.
        await _restart(hass, plant)
        assert plant.state.state == STATE_UNKNOWN
        assert recorded.await_count == 2
        await plant.show(global_resource(100))
        assert plant.state.state == "100"
        await plant.show(global_resource(97))  # settled: no correction any more
        assert plant.state.state == "97"
    store = module_fingerprints(hass, plant.entry.entry_id)
    assert store.counter(plant.sensor.unique_id).settled


async def test_no_value_while_the_recorded_value_is_read(hass, sensor_platform) -> None:
    plant = Plant(hass, global_resource(105))
    await _exchanged(hass, plant)
    release = asyncio.Event()

    async def slow(*_args):
        await release.wait()
        return RecordedValue(100.0, None)

    with patch(RECORDED, side_effect=slow):
        sensor = plant.pick(sensors_for(plant.coordinator))
        plant.sensor = sensor
        await add_without_device(hass, sensor)
        assert plant.state.state == STATE_UNKNOWN
        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert plant.state.state == "105"


# ------------------------------------------------------------------ total


async def test_a_restarted_total_counter_starts_a_cycle_at_the_exchange(
    hass, sensor_platform, hass_storage
) -> None:
    plant = Plant(hass, energy(2.0, 0.0, 7.0))  # environmental energy 5 kWh
    at = await _exchanged(hass, plant)
    with patch(RECORDED, _recorded(1000.0, "kWh")) as recorded:
        sensor = await plant.add()
        assert sensor.entity_description.state_class == SensorStateClass.TOTAL
        assert float(plant.state.state) == 5.0
        last_reset = plant.state.attributes["last_reset"]
        assert dt_util.parse_datetime(last_reset) == dt_util.parse_datetime(at)
        await hass.async_block_till_done()  # saved immediately
        key = f"{DOMAIN}.module_fingerprints.{plant.entry.entry_id}"
        assert hass_storage[key]["data"]["counters"][sensor.unique_id] == {
            "at": at,
            "settled": True,
            "last_reset": at,
        }
        await _restart(hass, plant)
        assert plant.state.attributes["last_reset"] == last_reset
        await plant.show(energy(3.0, 0.0, 9.0))
        assert float(plant.state.state) == 6.0
        assert plant.state.attributes["last_reset"] == last_reset
        recorded.assert_awaited_once()


async def test_a_slightly_lower_total_counter_is_no_new_cycle(
    hass, sensor_platform
) -> None:
    # Net energy falls a little while defrosting; that is no restart.
    plant = Plant(hass, energy(100.0, 0.0, 1050.0))  # 950 kWh, recorded 1000
    await _exchanged(hass, plant)
    with patch(RECORDED, _recorded(1000.0, "kWh")):
        await plant.add()
        assert plant.state.state == STATE_UNKNOWN
        await plant.show(energy(100.0, 0.0, 1101.0))
    assert float(plant.state.state) == 1001.0
    assert "last_reset" not in plant.state.attributes


async def test_a_recorded_value_in_another_unit_is_converted(
    hass, sensor_platform
) -> None:
    plant = Plant(hass, energy(100.0, 0.0, 1050.0))  # 950 kWh
    await _exchanged(hass, plant)
    with patch(RECORDED, _recorded(0.9, "MWh")):  # 900 kWh: continued
        await plant.add()
    assert float(plant.state.state) == 950.0
    assert "last_reset" not in plant.state.attributes


# ------------------------------------------------------------- no guard


@pytest.mark.parametrize(
    "case", ["no_exchange", "old_exchange", "legacy", "measurement"]
)
async def test_nothing_changes_without_a_recent_exchange(
    hass, sensor_platform, case
) -> None:
    plant = Plant(hass, global_resource(5), managed=case != "legacy")
    if case == "old_exchange":
        await _exchanged(hass, plant, ago=timedelta(days=31))
    elif case != "no_exchange":
        await _exchanged(hass, plant)
    with patch(RECORDED, _recorded(1000.0)) as recorded:
        if case == "measurement":
            sensor = plant.pick(sensors_for(plant.coordinator))
            sensor.entity_description = replace(
                sensor.entity_description, state_class=SensorStateClass.MEASUREMENT
            )
            plant.sensor = sensor
            await add_without_device(hass, sensor)
            await hass.async_block_till_done(wait_background_tasks=True)
        else:
            await plant.add()
    assert plant.state.state == "5"
    recorded.assert_not_awaited()
    assert (
        module_fingerprints(hass, plant.entry.entry_id).counter(plant.sensor.unique_id)
        is None
    )


async def test_without_a_recorder_values_pass(hass, sensor_platform) -> None:
    plant = Plant(hass, global_resource(5))
    await _exchanged(hass, plant)
    assert (
        await async_last_recorded_value(hass, "sensor.anything", dt_util.utcnow())
        is None
    )
    await plant.add()
    assert plant.state.state == "5"
    # Nothing known: the next start asks again.
    assert (
        module_fingerprints(hass, plant.entry.entry_id).counter(plant.sensor.unique_id)
        is None
    )


# ------------------------------------------------------------------ store


async def test_older_store_data_stays_readable_and_is_kept(hass, hass_storage) -> None:
    key = f"{DOMAIN}.module_fingerprints.entry"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {
            "targets": {"t": {"modules": ["a"], "seen": None}},
            "declined": ["b|g"],
            "exchanges": {"t": {"at": "2026-11-01T00:00:00+00:00"}},
            "counters": {
                "good": {"at": "x", "settled": True, "last_reset": None},
                "bad": {"at": "x", "settled": "yes"},
                "worse": [],
            },
        },
    }
    store = ModuleFingerprints(hass, "entry")
    await asyncio.gather(store.async_load(), store.async_load())
    assert store.get("t") == frozenset({"a"})
    assert store.declined("b", "g")
    assert store.counter("good") == CounterRecord("x", True)
    assert store.counter("bad") is None and store.counter("worse") is None
    store.set_counter("new", CounterRecord("y", True, "y"), immediate=True)
    await hass.async_block_till_done()
    data = hass_storage[key]["data"]
    assert data["targets"]["t"]["modules"] == ["a"]
    assert data["exchanges"] == {"t": {"at": "2026-11-01T00:00:00+00:00"}}
    assert set(data["counters"]) == {"good", "new"}

    del hass_storage[key]["data"]["counters"]  # data of the previous version
    older = ModuleFingerprints(hass, "entry")
    await older.async_load()
    assert older.counter("good") is None and older.exchange("t") is not None


# --------------------------------------------------------------- recorder


async def test_recorded_value_comes_from_before_the_exchange(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass,
    freezer,
) -> None:
    from tests.ha_recorder_helpers import (
        async_wait_recording_done,
        do_adhoc_statistics,
        get_start_time,
    )

    start = get_start_time(datetime.now(UTC)) + timedelta(minutes=5)
    freezer.move_to(start)
    assert await async_setup_component(hass, "sensor", {})
    plant = Plant(hass, global_resource(100))
    sensor = await plant.add()
    await async_wait_recording_done(hass)
    assert (
        await async_last_recorded_value(hass, sensor.entity_id, start) is None
    )  # nothing compiled yet
    freezer.move_to(start + timedelta(minutes=5))
    do_adhoc_statistics(hass, start=start)
    await async_wait_recording_done(hass)
    # The exchange; a later counter dips slightly (a negative delta for HA).
    exchanged = start + timedelta(minutes=6)
    await module_fingerprints(hass, plant.entry.entry_id).async_record_exchange(
        TARGET, exchanged.isoformat()
    )
    await plant.remove()
    plant.coordinator.async_set_updated_data(
        {GLOBAL: ResourceSnapshot(global_resource(95), True, datetime.now(UTC))}
    )
    freezer.move_to(start + timedelta(minutes=7))
    await plant.add()
    await async_wait_recording_done(hass)
    assert (
        await async_last_recorded_value(hass, sensor.entity_id, exchanged)
    ) == RecordedValue(100.0, None)
    assert plant.state.state == STATE_UNKNOWN
    freezer.move_to(start + timedelta(minutes=8))
    await plant.show(global_resource(101))
    await async_wait_recording_done(hass)
    assert plant.state.state == "101"
    freezer.move_to(start + timedelta(minutes=10))
    do_adhoc_statistics(hass, start=start + timedelta(minutes=5))
    await async_wait_recording_done(hass)
    from homeassistant.components.recorder.statistics import (
        get_last_short_term_statistics,
    )

    result = await recorder_mock.async_add_executor_job(
        get_last_short_term_statistics,
        hass,
        2,
        sensor.entity_id,
        False,
        {"state", "sum"},
    )
    rows = sorted(result[sensor.entity_id], key=lambda row: row["start"])
    assert [(row["state"], row["sum"]) for row in rows] == [(100, 0), (101, 1)]
    # A row from after the given time is no value from before it.
    assert await async_last_recorded_value(hass, sensor.entity_id, start) is None


async def test_an_hourly_row_ending_after_the_exchange_is_no_baseline(hass) -> None:
    """Review P50: the state of a row is its value at the row's end."""
    from datetime import datetime as dt
    from unittest.mock import MagicMock, patch

    from custom_components.bosch_buderus_heating import exchange_counters

    exchanged = dt(2026, 11, 6, 10, 30, tzinfo=UTC)
    hour = dt(2026, 11, 6, 10, 0, tzinfo=UTC).timestamp()
    rows = {
        "short": [
            {
                "start": exchanged.timestamp() + 60,
                "end": exchanged.timestamp() + 360,
                "state": 5.0,
            }
        ],
        "long": [
            {"start": hour, "end": hour + 3600, "state": 7.0},  # ends after it
            {"start": hour - 3600, "end": hour, "state": 100.0},
        ],
    }
    instance = MagicMock()
    instance.async_db_ready = _ready()
    instance.async_add_executor_job = lambda job: _run(job)
    hass.data["recorder_instance"] = instance
    with (
        patch("homeassistant.components.recorder.get_instance", return_value=instance),
        patch(
            "homeassistant.components.recorder.statistics.get_metadata",
            return_value={"sensor.x": (1, {"unit_of_measurement": None})},
        ),
        patch(
            "homeassistant.components.recorder.statistics.get_last_short_term_statistics",
            return_value={"sensor.x": rows["short"]},
        ),
        patch(
            "homeassistant.components.recorder.statistics.get_last_statistics",
            return_value={"sensor.x": rows["long"]},
        ),
    ):
        value = await exchange_counters.async_last_recorded_value(
            hass, "sensor.x", exchanged
        )
    hass.data.pop("recorder_instance")
    assert value == exchange_counters.RecordedValue(100.0, None)


def _ready():
    import asyncio

    future = asyncio.get_event_loop().create_future()
    future.set_result(True)
    return future


async def _run(job):
    return job()
