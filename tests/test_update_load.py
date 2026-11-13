"""Update passes compute shared local state once; their load is measured."""

from __future__ import annotations

import gc
import math

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_CLOSE
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating import update_load as load_module
from custom_components.bosch_buderus_heating.local_selection import LocalSelection
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata
from custom_components.bosch_buderus_heating.update_load import (
    UpdateLoad,
    install_update_load,
    update_load,
    update_load_diagnostics,
)
from tests.test_local_selection import RETURN, TARGET, FakeRuntime, local


class CountingRuntime(FakeRuntime):
    """Counts how often options and local states are worked out."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.option_reads = 0
        self.state_reads = 0
        original = self.snapshot.to_options

        def to_options():
            self.option_reads += 1
            return original()

        self.snapshot.to_options = to_options

    def local_state(self, binding_id):
        self.state_reads += 1
        return super().local_state(binding_id)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


# --------------------------------------------------------- per-pass caching


def test_selection_works_out_options_and_readings_once_per_pass() -> None:
    runtime = CountingRuntime([local(RETURN, 31.5)])
    selection = LocalSelection(runtime, TARGET)

    with selection.update_pass():
        for _ in range(50):
            assert selection.enabled
            assert selection.has_local_access
            assert RETURN in selection.local_observations()
            assert RETURN in selection.local_observations(counters=True)
        with selection.update_pass():  # nested: same pass, same values
            assert selection.enabled
    # enabled + bindings: one options read each; readings once per variant.
    assert runtime.option_reads == 2
    assert runtime.state_reads == 2

    # Outside a pass nothing is kept: every call sees the current state.
    reads = runtime.option_reads
    assert selection.enabled
    assert selection.enabled
    assert runtime.option_reads == reads + 2


def test_changes_between_passes_are_seen_by_the_next_pass() -> None:
    runtime = CountingRuntime([local(RETURN, 31.5)])
    selection = LocalSelection(runtime, TARGET)
    with selection.update_pass():
        assert selection.enabled
        assert RETURN in selection.local_observations()
    runtime.snapshot.enabled = False
    runtime.observations = []
    with selection.update_pass():
        assert not selection.enabled
        assert RETURN not in selection.local_observations()
    with pytest.raises(RuntimeError), selection.update_pass():
        raise RuntimeError
    # A failing pass still ends: later calls compute again.
    runtime.snapshot.enabled = True
    assert selection.enabled


async def test_coordinator_update_pass_reads_options_once_for_all_entities(
    hass: HomeAssistant,
) -> None:
    """Entities read availability, value and attributes; the options and local
    readings behind them are worked out once per pass, not per entity."""
    from tests.test_sensor import _sensor

    resource = Resource(
        RETURN,
        value=30.0,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="C"),
    )
    sensor = _sensor(hass, resource)
    coordinator = sensor.coordinator
    runtime = CountingRuntime([local(RETURN, 31.5)])
    coordinator.local_selection = LocalSelection(runtime, TARGET)
    seen: list[tuple[object, ...]] = []

    def entity_update() -> None:
        seen.append(
            (sensor.available, sensor.native_value, sensor.extra_state_attributes)
        )

    entities = 40
    removers = [coordinator.async_add_listener(entity_update) for _ in range(entities)]
    load = install_update_load(hass)
    passes = load.passes
    runtime.option_reads = runtime.state_reads = 0

    coordinator.async_update_listeners()

    assert len(seen) == entities
    assert all(item == (True, 31.5, {"source": "local"}) for item in seen)
    assert runtime.option_reads <= 2
    assert runtime.state_reads <= 2
    assert load.passes == passes + 1
    assert load.pass_maximum_entities >= entities

    # The next pass sees a changed option at once.
    runtime.snapshot.enabled = False
    seen.clear()
    coordinator.async_update_listeners()
    assert {item[1] for item in seen} == {30.0}
    for remove in removers:
        remove()


# --------------------------------------------------------------- measurement


def test_collections_are_timed_per_generation_and_inside_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    monkeypatch.setattr(load_module.time, "perf_counter", clock)
    load = UpdateLoad()

    load.gc_callback("start", {"generation": 0})
    clock.now += 0.002
    load.gc_callback("stop", {"generation": 0})
    with load.measure_pass(entities=7):
        clock.now += 0.01
        load.gc_callback("start", {"generation": 2})
        clock.now += 0.48
        load.gc_callback("stop", {"generation": 2})
        with load.measure_pass(entities=3):  # nested: counted once
            clock.now += 0.01
    load.gc_callback("stop", {"generation": 1})  # stop without start: ignored
    with load.measure_pass(entities=2):
        clock.now += 0.05

    report = load.diagnostics()
    assert report == {
        "measured": True,
        "garbage_collections": {
            "generation_0": 1,
            "generation_1": 0,
            "generation_2": 1,
        },
        "garbage_collection_total_ms": 482,
        "garbage_collection_maximum_ms": 480,
        "garbage_collections_over_100_ms": 1,
        "garbage_collections_over_100_ms_during_updates": 1,
        "garbage_collection_maximum_during_updates_ms": 480,
        "update_passes": 2,
        "update_pass_average_ms": 275,
        "update_pass_maximum_ms": 500,
        "update_passes_over_400_ms": 1,
        "update_pass_maximum_entities": 7,
    }
    # Only counts and whole milliseconds.
    values = [*report.values(), *report["garbage_collections"].values()]
    assert not any(isinstance(item, float) for item in values)


def test_slow_collection_outside_a_pass_is_not_counted_as_during_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    monkeypatch.setattr(load_module.time, "perf_counter", clock)
    load = UpdateLoad()
    load.gc_callback("start", {"generation": 2})
    clock.now += 0.2
    load.gc_callback("stop", {"generation": "unknown"})
    report = load.diagnostics()
    assert report["garbage_collections_over_100_ms"] == 1
    assert report["garbage_collections_over_100_ms_during_updates"] == 0
    assert report["garbage_collection_maximum_during_updates_ms"] == 0
    assert report["update_pass_average_ms"] is None
    assert sum(report["garbage_collections"].values()) == 0


async def test_measurement_is_installed_once_and_removed_on_close(
    hass: HomeAssistant,
) -> None:
    before = len(gc.callbacks)
    load = install_update_load(hass)
    assert install_update_load(hass) is load
    assert update_load(hass) is load
    assert len(gc.callbacks) == before + 1

    gc.collect()  # a real collection reaches the callback
    assert sum(load.collections) >= 1
    assert math.isfinite(load.collection_total)
    assert update_load_diagnostics(hass)["measured"] is True

    hass.bus.async_fire(EVENT_HOMEASSISTANT_CLOSE)
    await hass.async_block_till_done()
    assert update_load(hass) is None
    assert len(gc.callbacks) == before
    assert update_load_diagnostics(hass) == {"measured": False}


async def test_integration_setup_installs_the_measurement(hass: HomeAssistant) -> None:
    from custom_components.bosch_buderus_heating import async_setup

    assert await async_setup(hass, {})
    assert update_load(hass) is not None
