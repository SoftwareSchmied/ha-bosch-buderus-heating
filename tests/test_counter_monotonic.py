"""An energy total never shows a tiny step backwards (restart, pause, source change)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from homeassistant.components.sensor import SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.bosch_buderus_heating import counter_monotonic
from custom_components.bosch_buderus_heating.coordinator import ResourceSnapshot
from custom_components.bosch_buderus_heating.counter_monotonic import (
    MAX_ROWS,
    MonotonicCounter,
    PublishedCounterStore,
    published_counter_store,
    tolerance_for,
)
from custom_components.bosch_buderus_heating.counter_selection import (
    CounterGuard,
    CounterSourceStore,
)
from custom_components.bosch_buderus_heating.local_selection import LocalSelection
from tests.ha_test_helpers import add_without_device
from tests.test_counter_selection import (
    TARGET,
    TOTAL,
    FakeRuntime,
    cloud_energy,
)
from tests.test_start_counters import coordinator_for, sensors_for


def _total(sensors):
    return next(
        s
        for s in sensors.values()
        if s.entity_description.value_kind == "total_electricity"
    )


def _heat(sensors):
    return next(
        s
        for s in sensors.values()
        if s.entity_description.value_key == "outputProduced"
    )


class Plant:
    """A coordinator with the energy sensors added to HA; real update cycles."""

    def __init__(self, hass: HomeAssistant, compressor: float, heat: float = 40.0):
        self.hass = hass
        self.entry, self.coordinator = coordinator_for(
            hass, cloud_energy(compressor, 0.0, heat)
        )
        self.sensors = sensors_for(self.coordinator)
        self.electricity = _total(self.sensors)
        self.heat = _heat(self.sensors)

    async def add(self):
        for sensor in (self.electricity, self.heat):
            await add_without_device(self.hass, sensor)
        await self.hass.async_block_till_done(wait_background_tasks=True)

    async def remove(self):
        for sensor in (self.electricity, self.heat):
            await sensor.async_remove()
        await self.hass.async_block_till_done()

    async def cycle(self, compressor: float, heat: float = 40.0):
        resource = cloud_energy(compressor, 0.0, heat)
        self.coordinator.async_set_updated_data(
            {resource.path: ResourceSnapshot(resource, True, datetime.now(UTC))}
        )
        await self.hass.async_block_till_done(wait_background_tasks=True)

    def state(self, sensor) -> float:
        return float(self.hass.states.get(sensor.entity_id).state)


@pytest.fixture
async def sensor_platform(hass):
    assert await async_setup_component(hass, "sensor", {})


async def _restart(
    hass: HomeAssistant, plant: Plant, *, save: bool = True, first: float | None = None
) -> Plant:
    """Stop the entities, optionally write the store, start everything again."""
    await plant.remove()
    store = published_counter_store(hass)
    if save:
        await store.store.async_save(store._data())
    data = store._data() if save else None
    fresh = PublishedCounterStore(hass)

    async def load():
        return data

    fresh.store.async_load = load
    hass.data[counter_monotonic._STORES] = fresh
    if first is not None:  # the first reading after the pause is already there
        resource = cloud_energy(first, 0.0, 40.0)
        plant.coordinator.data = {
            resource.path: ResourceSnapshot(resource, True, datetime.now(UTC))
        }
    plant.sensors = sensors_for(plant.coordinator)
    plant.electricity, plant.heat = _total(plant.sensors), _heat(plant.sensors)
    await plant.add()
    return plant


# --------------------------------------------------------------- the unit


def test_tolerance_applies_to_energy_totals_that_only_grow() -> None:
    assert tolerance_for(SensorStateClass.TOTAL_INCREASING, "energy", "kWh") == 0.05
    assert tolerance_for(SensorStateClass.TOTAL_INCREASING, "energy", "Wh") == 50.0
    assert tolerance_for(SensorStateClass.TOTAL, "energy", "kWh") is None  # heat
    assert tolerance_for(SensorStateClass.MEASUREMENT, "energy", "kWh") is None
    assert tolerance_for(SensorStateClass.TOTAL_INCREASING, "duration", "h") is None
    assert tolerance_for(SensorStateClass.TOTAL_INCREASING, "energy", "BTU") is None


async def test_unit_rule_holds_small_steps_and_passes_the_rest(hass) -> None:
    store = PublishedCounterStore(hass)
    counter = MonotonicCounter(store, "uid", 0.05)
    assert counter.value(372.01) == 372.01
    assert counter.value(372.00) == 372.01  # one step back: held
    assert counter.value(372.01 - 0.05) == 372.01  # the limit still holds
    assert counter.value(372.01 - 0.06) == 372.01 - 0.06  # not rounding any more
    assert counter.value(372.5) == 372.5
    assert counter.value(0.0) == 0.0  # a replaced meter stays visible
    assert counter.held == 2
    assert store.get("uid") == 0.0


async def test_store_keeps_only_valid_rows_and_is_capped(hass) -> None:
    store = PublishedCounterStore(hass)

    async def load():
        return {
            "counters": {
                "ok": 1.5,
                "bool": True,
                "text": "1",
                "nan": float("nan"),
                5: 1.0,
            }
        }

    store.store.async_load = load
    await store.async_initialize()
    assert store.get("ok") == 1.5
    assert all(store.get(k) is None for k in ("bool", "text", "nan"))
    for index in range(MAX_ROWS):
        store.set(f"id{index}", 1.0)
    store.set("beyond", 1.0)
    assert store.get("beyond") is None
    damaged = PublishedCounterStore(hass)

    async def broken():
        raise OSError("unreadable")

    damaged.store.async_load = broken
    await damaged.async_initialize()
    assert damaged.get("anything") is None


# ------------------------------------------------- real cycles in the sensor


async def test_a_step_back_after_a_restart_keeps_the_last_number(
    hass, sensor_platform
) -> None:
    """Reproduces the 372.01 -> 372.00 step after a pause and restart."""
    plant = Plant(hass, 372.01)
    await plant.add()
    assert plant.state(plant.electricity) == 372.01
    await plant.cycle(372.02)
    await plant.cycle(372.03)
    plant = await _restart(hass, plant, first=372.02)  # first reading, rounded lower
    assert plant.state(plant.electricity) == 372.03
    await plant.cycle(372.03)
    assert plant.state(plant.electricity) == 372.03
    await plant.cycle(372.04)
    assert plant.state(plant.electricity) == 372.04


async def test_without_the_guard_the_step_would_show(hass, sensor_platform) -> None:
    """Mutation check: with the rule off the same cycle shows the step back."""
    with patch.object(counter_monotonic, "TOLERANCE_KWH", 0.0):
        plant = Plant(hass, 372.01)
        await plant.add()
        await plant.cycle(372.00)
        assert plant.state(plant.electricity) == 372.00


async def test_a_paused_ha_with_a_lost_save_holds_what_was_stored(
    hass, sensor_platform
) -> None:
    plant = Plant(hass, 372.01)
    await plant.add()
    plant = await _restart(hass, plant, first=372.00)  # saved: 372.01
    assert plant.state(plant.electricity) == 372.01
    plant = await _restart(hass, plant, save=False, first=372.00)  # nothing stored
    assert plant.state(plant.electricity) == 372.00


async def test_a_meter_replacement_is_still_shown(hass, sensor_platform) -> None:
    plant = Plant(hass, 372.01)
    await plant.add()
    await plant.cycle(0.02)  # large drop: not rounding
    assert plant.state(plant.electricity) == 0.02
    plant = await _restart(hass, plant)
    await plant.cycle(0.03)
    assert plant.state(plant.electricity) == 0.03


async def test_heat_total_keeps_following_a_defrost(hass, sensor_platform) -> None:
    """The statistics class total falls in a defrost; it is never held."""
    plant = Plant(hass, 10.0, heat=1501.87)
    await plant.add()
    assert plant.heat.state_class is SensorStateClass.TOTAL
    await plant.cycle(10.0, heat=1501.86)
    assert plant.state(plant.heat) == 1501.86
    await plant.cycle(10.0, heat=1500.5)
    assert plant.state(plant.heat) == 1500.5


async def test_a_stale_counter_guard_cannot_publish_a_lower_number(
    hass, sensor_platform
) -> None:
    """The guard's numbers can be older than what was published (delayed save).

    After such a restart a cloud reading one step lower is accepted as the
    first count; the entity still holds the last published number.
    """
    plant = Plant(hass, 372.01)
    plant.coordinator.local_selection = LocalSelection(
        FakeRuntime([], enabled=True), TARGET
    )
    plant.coordinator.counter_guard = CounterGuard(
        CounterSourceStore(hass), "owner", TARGET
    )
    await plant.add()
    await plant.cycle(372.01)
    assert plant.state(plant.electricity) == 372.01
    plant.coordinator.counter_guard = CounterGuard(  # restart: numbers lost
        CounterSourceStore(hass), "owner", TARGET
    )
    await plant.cycle(372.00)
    assert plant.coordinator.counter_guard.shown[TOTAL] == "cloud"
    assert plant.state(plant.electricity) == 372.01
