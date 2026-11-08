"""Counters from both connections never count backwards (P15)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.coordinator import (
    COUNTER_HELD_CATEGORY,
    SOURCE_STOPPED_CATEGORY,
    Freshness,
)
from custom_components.bosch_buderus_heating.counter_selection import (
    CounterGuard,
    CounterSourceStore,
    ShownCounter,
    counter_vector,
    not_lower,
)
from custom_components.bosch_buderus_heating.local_selection import LocalSelection
from custom_components.bosch_buderus_heating.observations import (
    PairResult,
    compare,
    counter_evidence,
)
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata
from tests.test_local_api import read
from tests.test_local_selection import TARGET, FakeRuntime

TOTAL = "/heatSources/emon/totalConsumption"
STARTS = "/heatSources/numberOfStarts"
NOW = datetime.now(UTC)


def cloud_energy(compressor: float, eheater: float, produced: float) -> Resource:
    return Resource(
        TOTAL,
        values=(
            {"compressor": compressor, "eheater": eheater, "outputProduced": produced},
        ),
        metadata=ResourceMetadata(resource_type="emonValue"),  # unitless = kWh
    )


def local_energy(compressor: float, eheater: float, produced: float, *, monotonic=10.0):
    raw = {
        "id": TOTAL,
        "type": "emonValue",
        "writeable": 0,
        "unit": "kWh",
        "values": [
            {"compressor": compressor},
            {"eheater": eheater},
            {"outputProduced": produced},
        ],
    }
    return read(raw, observed_at=NOW, observed_monotonic=monotonic)


def _setup(
    hass: HomeAssistant,
    cloud_resource: Resource,
    *,
    store=None,
    enabled=True,
    baseline=True,
):
    from tests.test_validity import _coordinator

    coordinator = _coordinator(hass, cloud_resource)
    runtime = FakeRuntime([], enabled=enabled)
    coordinator.local_selection = LocalSelection(runtime, TARGET)
    store = store or CounterSourceStore(hass)
    coordinator.counter_guard = CounterGuard(store, "owner", TARGET)
    if baseline:
        coordinator.async_update_listeners()  # the cloud sets the first count
    return coordinator, runtime, store


def shown(coordinator) -> Resource | None:
    coordinator.async_update_listeners()
    snapshot = coordinator.display_snapshot(TOTAL)
    if snapshot is None or not snapshot.available:
        return None
    return snapshot


def parts(snapshot) -> dict[str, float]:
    return dict(snapshot.resource.values[0])


def test_vector_includes_derived_energy_values() -> None:
    vector = counter_vector(cloud_energy(10.0, 2.0, 40.0))
    assert vector["=total_electricity"] == 12.0
    assert vector["=environmental_energy"] == 28.0
    assert not_lower({"a": 2.0}, {"a": 2.0}) and not not_lower({"a": 1.9}, {"a": 2.0})
    assert not not_lower({}, {"a": 1.0})


async def test_local_counter_is_shown_when_it_does_not_count_backwards(hass) -> None:
    coordinator, runtime, _ = _setup(hass, cloud_energy(10.0, 2.0, 40.0))
    assert parts(shown(coordinator)) == {
        "compressor": 10.0,
        "eheater": 2.0,
        "outputProduced": 40.0,
    }
    runtime.observations = [local_energy(10.0, 2.0, 40.5)]
    snapshot = shown(coordinator)
    assert (
        snapshot.source.value == "local" and parts(snapshot)["outputProduced"] == 40.5
    )
    assert coordinator.counter_guard.switches == {TOTAL: 1}


async def test_lower_local_counter_waits_until_it_reaches_the_last_count(hass) -> None:
    coordinator, runtime, _ = _setup(hass, cloud_energy(10.0, 2.0, 40.0))
    shown(coordinator)
    runtime.observations = [local_energy(10.0, 2.0, 39.9)]  # 0.1 kWh lower
    snapshot = shown(coordinator)
    assert (
        snapshot.source.value != "local" and parts(snapshot)["outputProduced"] == 40.0
    )
    assert coordinator.counter_guard.blocked == {TOTAL: 1}
    runtime.observations = [local_energy(10.0, 2.0, 40.0, monotonic=20.0)]
    assert shown(coordinator).source.value == "local"


async def test_derived_energy_must_not_count_backwards_either(hass) -> None:
    coordinator, runtime, _ = _setup(hass, cloud_energy(10.0, 2.0, 40.0))
    shown(coordinator)
    # Every part is higher, but environmental energy 40.1 - 10.5 - 2 < 28.
    runtime.observations = [local_energy(10.5, 2.0, 40.1)]
    assert shown(coordinator).source.value != "local"


async def test_fallback_to_a_lower_cloud_counter_is_held_until_it_catches_up(
    hass,
) -> None:
    coordinator, runtime, _ = _setup(hass, cloud_energy(10.0, 2.0, 40.0))
    runtime.observations = [local_energy(10.0, 2.0, 41.0)]
    assert shown(coordinator).source.value == "local"
    runtime.observations = []  # local reading expired
    assert shown(coordinator) is None
    assert coordinator.display_snapshot(TOTAL).last_error_category == (
        COUNTER_HELD_CATEGORY
    )
    coordinator.data[TOTAL] = replace(
        coordinator.data[TOTAL], resource=cloud_energy(10.0, 2.0, 41.2)
    )
    snapshot = shown(coordinator)
    assert (
        snapshot.source.value != "local" and parts(snapshot)["outputProduced"] == 41.2
    )


async def test_defrost_dip_passes_within_a_source_and_only_ever_holds_a_switch(
    hass,
) -> None:
    """P48: the produced heat is a net count and falls while defrosting.

    Within one source the dip is shown as reported. It never blocks a switch by
    itself, because the dipped numbers become the last shown ones. A switch to a
    source that is still lower (here a local reading taken during a later
    defrost) is held; if the shown source then expires, the whole energy counter
    (electricity included) is a gap, never a lower or mixed number.
    """
    coordinator, runtime, _ = _setup(hass, cloud_energy(10.0, 2.0, 40.0))
    guard = coordinator.counter_guard
    runtime.observations = [local_energy(10.0, 2.0, 40.5, monotonic=10.0)]
    assert shown(coordinator).source.value == "local"

    # Defrost while shown from the local access: shown as reported, no hold.
    runtime.observations = [local_energy(10.1, 2.0, 39.6, monotonic=20.0)]
    snapshot = shown(coordinator)
    assert snapshot.source.value == "local"
    assert parts(snapshot) == {
        "compressor": 10.1,
        "eheater": 2.0,
        "outputProduced": 39.6,
    }
    assert guard.switches == {TOTAL: 1} and guard.blocked == {}

    # The local reading expires; the cloud now reports the same dipped count.
    # The dip lowered the bar: the switch is allowed at once.
    runtime.observations = []
    coordinator.data[TOTAL] = replace(
        coordinator.data[TOTAL], resource=cloud_energy(10.1, 2.0, 39.6)
    )
    assert shown(coordinator).source.value != "local"
    assert guard.switches == {TOTAL: 2} and guard.blocked == {}

    # The cloud recovers; the local access returns during the next defrost.
    coordinator.data[TOTAL] = replace(
        coordinator.data[TOTAL], resource=cloud_energy(10.2, 2.0, 40.4)
    )
    runtime.observations = [local_energy(10.3, 2.0, 39.8, monotonic=30.0)]
    snapshot = shown(coordinator)
    assert snapshot.source.value != "local"  # held: 39.8 < 40.4 shown
    assert parts(snapshot) == {
        "compressor": 10.2,
        "eheater": 2.0,
        "outputProduced": 40.4,
    }
    assert guard.blocked == {TOTAL: 1}

    # The cloud reading expires while the switch is held: a gap for the whole
    # counter, the higher local electricity is not shown either.
    coordinator.data[TOTAL] = replace(
        coordinator.data[TOTAL], last_success=datetime.now(UTC) - timedelta(hours=2)
    )
    assert shown(coordinator) is None
    assert coordinator.display_snapshot(TOTAL).last_error_category == (
        COUNTER_HELD_CATEGORY
    )

    # Produced heat is back above the shown count, but the derived
    # environmental energy (40.5 - 10.4 - 2.0 = 28.1 < 28.2) is not: still held.
    runtime.observations = [local_energy(10.4, 2.0, 40.5, monotonic=40.0)]
    assert shown(coordinator) is None

    # Every shown number has caught up: the local access takes over.
    runtime.observations = [local_energy(10.4, 2.0, 40.7, monotonic=50.0)]
    snapshot = shown(coordinator)
    assert snapshot.source.value == "local"
    assert parts(snapshot)["outputProduced"] == 40.7
    assert guard.switches == {TOTAL: 3} and guard.blocked == {TOTAL: 1}


async def test_a_real_reset_within_one_source_stays_visible(hass) -> None:
    coordinator, _, _ = _setup(hass, cloud_energy(10.0, 2.0, 40.0))
    shown(coordinator)
    coordinator.data[TOTAL] = replace(
        coordinator.data[TOTAL], resource=cloud_energy(0.0, 0.0, 0.0)
    )
    assert parts(shown(coordinator))["outputProduced"] == 0.0


async def test_rule_survives_a_restart_through_the_store(hass) -> None:
    store = CounterSourceStore(hass)
    coordinator, runtime, _ = _setup(hass, cloud_energy(10.0, 2.0, 40.0), store=store)
    runtime.observations = [local_energy(10.0, 2.0, 41.0)]
    shown(coordinator)
    data = store._data()
    restarted = CounterSourceStore(hass)
    restarted.store.async_load = Mock(return_value=None)

    async def load():
        return data

    restarted.store.async_load = load
    await restarted.async_initialize()
    after, runtime2, _ = _setup(hass, cloud_energy(10.0, 2.0, 40.5), store=restarted)
    assert shown(after) is None  # cloud 40.5 < shown 41.0 before the restart
    runtime2.observations = [local_energy(10.0, 2.0, 41.1)]
    assert shown(after).source.value == "local"


async def test_switching_the_option_off_returns_only_when_not_backwards(hass) -> None:
    coordinator, runtime, _ = _setup(hass, cloud_energy(10.0, 2.0, 40.0))
    runtime.observations = [local_energy(10.0, 2.0, 41.0)]
    assert shown(coordinator).source.value == "local"
    runtime.snapshot.enabled = False
    assert shown(coordinator) is None
    coordinator.data[TOTAL] = replace(
        coordinator.data[TOTAL], resource=cloud_energy(10.0, 2.0, 41.0)
    )
    assert shown(coordinator) is not None


async def test_mismatched_parts_or_units_are_never_supplied(hass) -> None:
    coordinator, runtime, _ = _setup(hass, cloud_energy(10.0, 2.0, 40.0))
    raw = {
        "id": TOTAL,
        "type": "emonValue",
        "writeable": 0,
        "unit": "kWh",
        "values": [{"compressor": 11}, {"eheater": 2}],  # part missing
    }
    runtime.observations = [read(raw, observed_at=NOW, observed_monotonic=10.0)]
    assert shown(coordinator).source.value != "local"
    raw["values"].append({"outputProduced": 41})
    raw["unit"] = "Wh"  # a factor, not a spelling
    runtime.observations = [read(raw, observed_at=NOW, observed_monotonic=11.0)]
    assert shown(coordinator).source.value != "local"


async def test_stopped_source_shows_no_counter(hass) -> None:
    coordinator, runtime, _ = _setup(hass, cloud_energy(10.0, 2.0, 40.0))
    runtime.observations = [local_energy(10.0, 2.0, 41.0)]
    assert shown(coordinator).source.value == "local"
    coordinator.data[TOTAL] = replace(
        coordinator.data[TOTAL],
        available=False,
        freshness=Freshness.STALE,
        last_error_category=SOURCE_STOPPED_CATEGORY,
        ended=True,
    )
    assert shown(coordinator) is None


async def test_counter_without_a_value_is_shown_as_before(hass) -> None:
    empty = Resource(STARTS, metadata=ResourceMetadata(resource_type="floatValue"))
    from tests.test_validity import _coordinator

    coordinator = _coordinator(hass, empty)
    coordinator.local_selection = LocalSelection(FakeRuntime([]), TARGET)
    coordinator.counter_guard = CounterGuard(CounterSourceStore(hass), "o", TARGET)
    coordinator.async_update_listeners()
    snapshot = coordinator.display_snapshot(STARTS)
    assert snapshot is not None and snapshot.available


async def test_diagnostics_summarise_counters_by_measurement(hass) -> None:
    coordinator, runtime, _ = _setup(hass, cloud_energy(10.0, 2.0, 40.0))
    shown(coordinator)
    runtime.observations = [local_energy(10.0, 2.0, 39.0)]
    shown(coordinator)
    assert coordinator.counter_guard.summary() == {
        "energy_total": {"shown": {"cloud": 1}, "switches": 0, "blocked": 1}
    }


def test_counter_evidence_without_absolute_values() -> None:
    from tests.test_observations import cloud as cloud_observation
    from tests.test_observations import local as local_observation

    cloud_item = cloud_observation(
        TOTAL,
        values=[{"compressor": 100.0, "eheater": 2.0, "outputProduced": 400.0}],
        resource_type="emonValue",
        unit=None,
    )
    raw = {
        "id": TOTAL,
        "type": "emonValue",
        "writeable": 0,
        "unit": "kWh",
        "values": [{"compressor": 101}, {"eheater": 2}, {"outputProduced": 400}],
    }
    comparison = compare(cloud_item, local_observation(raw))
    assert comparison.result is PairResult.DIFFER  # comparable via the kWh contract
    evidence = counter_evidence([comparison])
    assert evidence == {
        "energy_total": {
            "result": "differ",
            "max_difference_percent": 1.0,
            "cloud_step": 1.0,
            "local_step": 1.0,
            "receive_gap_seconds": comparison.receive_gap_seconds,
        }
    }
    assert "400" not in repr(evidence) and "101" not in repr(evidence)


@pytest.mark.parametrize("enabled", [True, False])
async def test_cloud_only_counters_keep_their_behaviour(hass, enabled) -> None:
    coordinator, _, _ = _setup(hass, cloud_energy(10.0, 2.0, 40.0), enabled=enabled)
    assert parts(shown(coordinator))["outputProduced"] == 40.0
    coordinator.data[TOTAL] = replace(
        coordinator.data[TOTAL],
        last_success=datetime.now(UTC) - timedelta(hours=2),
    )
    assert shown(coordinator) is None  # P12 validity still applies


def test_resolution_is_the_finest_reported_step() -> None:
    from decimal import Decimal

    from custom_components.bosch_buderus_heating.observations import _step

    assert _step([Decimal("400"), Decimal("40.5")]) == Decimal("0.1")
    assert _step([Decimal("400"), Decimal("12")]) == Decimal("1")
    assert _step([Decimal("1.25")]) == Decimal("0.01")


async def test_stopping_a_managed_cloud_source_drops_decided_counters(hass) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from custom_components.bosch_buderus_heating.managed_pointt import (
        ManagedPointTCoordinator,
    )

    coordinator, runtime, _ = _setup(hass, cloud_energy(10.0, 2.0, 40.0))
    runtime.observations = [local_energy(10.0, 2.0, 41.0)]
    assert shown(coordinator).source.value == "local"
    coordinator._remove_local_listener = None
    coordinator._updates = SimpleNamespace(stop=AsyncMock())
    await ManagedPointTCoordinator.async_stop_source(coordinator)
    snapshot = coordinator.display_snapshot(TOTAL)
    assert snapshot is not None and not snapshot.available
    assert snapshot.last_error_category == SOURCE_STOPPED_CATEGORY
    assert coordinator.counter_guard.summary() == {}


async def test_without_history_the_cloud_sets_the_first_count(hass) -> None:
    coordinator, runtime, _ = _setup(
        hass, cloud_energy(10.0, 2.0, 40.0), baseline=False
    )
    runtime.observations = [local_energy(10.0, 2.0, 39.0)]  # e.g. after an upgrade
    snapshot = shown(coordinator)
    assert (
        snapshot.source.value != "local" and parts(snapshot)["outputProduced"] == 40.0
    )
    coordinator.data[TOTAL] = replace(
        coordinator.data[TOTAL], last_success=datetime.now(UTC) - timedelta(hours=2)
    )
    assert shown(coordinator) is None  # no cloud baseline: no local count either


async def test_a_temporarily_missing_part_keeps_binding_the_switch(hass) -> None:
    coordinator, runtime, store = _setup(hass, cloud_energy(10.0, 2.0, 40.0))
    partial = Resource(
        TOTAL,
        values=({"eheater": 2.0, "outputProduced": 40.0},),  # compressor missing
        metadata=ResourceMetadata(resource_type="emonValue"),
    )
    coordinator.data[TOTAL] = replace(coordinator.data[TOTAL], resource=partial)
    shown(coordinator)
    assert store.get(f"owner|{TARGET}|{TOTAL}").values["compressor"] == 10.0
    runtime.observations = [local_energy(9.0, 2.0, 40.0)]
    snapshot = shown(coordinator)
    assert snapshot.source.value != "local"


async def test_after_a_restart_the_last_shown_source_goes_first(hass) -> None:
    store = CounterSourceStore(hass)
    coordinator, runtime, _ = _setup(hass, cloud_energy(10.0, 2.0, 40.0), store=store)
    runtime.observations = [local_energy(10.0, 2.0, 41.0)]
    assert shown(coordinator).source.value == "local"
    # Restart: a new guard on the same store, the cloud listed first this time.
    guard = CounterGuard(store, "owner", TARGET)
    local_snapshot = coordinator.display_snapshot(TOTAL)
    # The cloud has caught up and is not lower, but the shown source goes first.
    cloud_snapshot = replace(
        coordinator.data[TOTAL], resource=cloud_energy(10.0, 2.0, 41.5)
    )
    chosen = guard.decide(TOTAL, (("cloud", cloud_snapshot), ("local", local_snapshot)))
    assert chosen is local_snapshot


async def test_a_source_switch_is_saved_at_once(hass) -> None:
    store = CounterSourceStore(hass)
    calls = []
    store.store.async_delay_save = lambda data, delay: calls.append(delay)
    coordinator, runtime, _ = _setup(hass, cloud_energy(10.0, 2.0, 40.0), store=store)
    assert calls == [60]  # first count: regular delayed save (O69)
    runtime.observations = [local_energy(10.0, 2.0, 41.0)]
    shown(coordinator)
    assert calls[-1] == 0  # the switch is written immediately


def test_diagnostics_add_counters_of_several_sources() -> None:
    from custom_components.bosch_buderus_heating.diagnostics import _add_counters

    total: dict = {}
    _add_counters(
        total, {"energy_total": {"shown": {"local": 1}, "switches": 1, "blocked": 0}}
    )
    _add_counters(
        total, {"energy_total": {"shown": {"cloud": 1}, "switches": 0, "blocked": 2}}
    )
    assert total == {
        "energy_total": {"shown": {"local": 1, "cloud": 1}, "switches": 1, "blocked": 2}
    }


def test_evidence_reports_a_difference_from_a_zero_cloud_count() -> None:
    from tests.test_observations import cloud as cloud_observation
    from tests.test_observations import local as local_observation

    cloud_item = cloud_observation(
        TOTAL,
        values=[{"compressor": 0.0, "eheater": 2.0, "outputProduced": 400.0}],
        resource_type="emonValue",
        unit=None,
    )
    raw = {
        "id": TOTAL,
        "type": "emonValue",
        "writeable": 0,
        "unit": "kWh",
        "values": [{"compressor": 1}, {"eheater": 2}, {"outputProduced": 400}],
    }
    evidence = counter_evidence([compare(cloud_item, local_observation(raw))])
    assert evidence["energy_total"]["max_difference_percent"] == 100.0


def test_vectors_of_start_runtime_and_scalar_counters() -> None:
    starts = Resource(
        STARTS,
        value=139,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue"),
    )
    assert counter_vector(starts) == {"value": 139.0}
    runtime = Resource(
        "/heatSources/workingTime/totalSystem",
        value=7200,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="s"),
    )
    assert counter_vector(runtime) == {"value": 2.0}  # hours, as the sensor shows
    text = Resource("/other/counter", value="n/a", has_value=True)
    assert counter_vector(text) is None


async def test_store_loads_only_valid_rows_and_survives_a_damaged_file(hass) -> None:
    store = CounterSourceStore(hass)

    async def load():
        return {
            "counters": {
                "owner|target|/heatSources/numberOfStarts": {
                    "source": "local",
                    "values": {"value": 139},
                },
                "owner|target|/bad": {"source": "elsewhere", "values": {"v": 1}},
                "owner|target|/nan": {"source": "cloud", "values": {"v": "x"}},
                "bad\x00key": {"source": "cloud", "values": {"v": 1}},
            }
        }

    store.store.async_load = load
    await store.async_initialize()
    assert store.get("owner|target|/heatSources/numberOfStarts").values == {
        "value": 139.0
    }
    assert store.get("owner|target|/bad") is None
    assert store.get("owner|target|/nan") is None
    damaged = CounterSourceStore(hass)

    async def broken():
        raise ValueError("damaged")

    damaged.store.async_load = broken
    await damaged.async_initialize()
    assert damaged.get("owner|target|/heatSources/numberOfStarts") is None


async def test_full_store_keeps_the_cloud_baseline_and_owner_removal(hass) -> None:
    from custom_components.bosch_buderus_heating import counter_selection
    from custom_components.bosch_buderus_heating.counter_selection import ShownCounter

    store = CounterSourceStore(hass)
    store.store.async_delay_save = lambda data, delay: None
    original = counter_selection.MAX_ROWS
    counter_selection.MAX_ROWS = 1
    try:
        store.set("a|t|/x", ShownCounter("cloud", {"v": 1.0}))
        store.set("b|t|/y", ShownCounter("local", {"v": 2.0}))  # full: not stored
        assert store.get("b|t|/y") is None
    finally:
        counter_selection.MAX_ROWS = original
    store.discard_owner("a")
    assert store.get("a|t|/x") is None
    assert store._data() == {"counters": {}}


async def test_local_scalar_start_counter_is_supplied_in_the_cloud_shape(hass) -> None:
    from tests.test_local_api import scalar

    starts = Resource(
        STARTS,
        value=139,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue"),
    )
    from tests.test_validity import _coordinator

    coordinator = _coordinator(hass, starts)
    runtime = FakeRuntime([])
    coordinator.local_selection = LocalSelection(runtime, TARGET)
    coordinator.counter_guard = CounterGuard(CounterSourceStore(hass), "o", TARGET)
    coordinator.async_update_listeners()
    raw = scalar(STARTS, value=140)
    raw.pop("unitOfMeasure")  # start counters carry no unit
    runtime.observations = [read(raw, observed_at=NOW, observed_monotonic=10.0)]
    coordinator.async_update_listeners()
    snapshot = coordinator.display_snapshot(STARTS)
    assert snapshot is not None and snapshot.available


# ------------------------------------------------ partial energy collections


def produced_only(produced: float, *, monotonic=10.0):
    """A local energy collection without the consumption parts."""
    raw = {
        "id": TOTAL,
        "type": "emonValue",
        "writeable": 0,
        "unit": "kWh",
        "values": [{"outputProduced": produced}],
    }
    return read(raw, observed_at=NOW, observed_monotonic=monotonic)


def test_a_partial_collection_has_no_derived_values() -> None:
    partial = Resource(
        TOTAL,
        values=({"outputProduced": 41.0},),
        metadata=ResourceMetadata(resource_type="emonValue"),
    )
    assert counter_vector(partial) == {"outputProduced": 41.0}
    assert (
        not_lower({"outputProduced": 41.0}, {"outputProduced": 40.0, "a": 1.0}) is False
    )
    assert not_lower(
        {"outputProduced": 41.0}, {"outputProduced": 40.0, "a": 1.0}, partial=True
    )
    assert not not_lower(
        {"outputProduced": 39.0}, {"outputProduced": 40.0, "a": 1.0}, partial=True
    )
    assert not not_lower({"b": 1.0}, {"a": 1.0}, partial=True)  # nothing shared


async def test_a_partial_local_collection_never_replaces_a_complete_cloud(
    hass,
) -> None:
    coordinator, runtime, _ = _setup(hass, cloud_energy(10.0, 2.0, 40.0))
    runtime.observations = [produced_only(41.0)]
    snapshot = shown(coordinator)
    assert snapshot.source.value != "local"
    assert parts(snapshot) == {
        "compressor": 10.0,
        "eheater": 2.0,
        "outputProduced": 40.0,
    }


# ---------------------------------------- confirmed parts (P49b review)


def energy(**parts: float) -> Resource:
    return Resource(
        TOTAL,
        values=tuple({name: value} for name, value in parts.items()),
        metadata=ResourceMetadata(resource_type="emonValue"),
    )


def snap(resource: Resource):
    from custom_components.bosch_buderus_heating.coordinator import ResourceSnapshot

    return ResourceSnapshot(resource, True, NOW)


ROW = f"owner|{TARGET}|{TOTAL}"


def cloud_baseline(hass):
    store = CounterSourceStore(hass)
    full = energy(compressor=1000.0, eheater=20.0, outputProduced=3000.0)
    store.set(ROW, ShownCounter("cloud", counter_vector(full)))
    return store, CounterGuard(store, "owner", TARGET)


def view_decide(guard, resource, source="local"):
    """As the local plant view decides: its local access is the sole source."""
    return guard.decide(TOTAL, ((source, snap(resource)),), partial=source == "local")


async def test_a_part_taken_over_from_the_cloud_never_goes_backwards(hass) -> None:
    store, guard = cloud_baseline(hass)
    produced = energy(outputProduced=3010.0)
    assert view_decide(guard, produced) is not None
    row = store.get(ROW)
    assert row.source == "local" and row.confirmed == frozenset({"outputProduced"})
    assert row.values["compressor"] == 1000.0  # not confirmed by this source
    # The same source later reports the part, below the number shown before.
    lower = energy(compressor=990.0, eheater=20.0, outputProduced=3020.0)
    assert view_decide(guard, lower) is None
    assert guard.blocked[TOTAL] == 1
    assert store.get(ROW).values["compressor"] == 1000.0


async def test_a_part_that_comes_back_higher_is_confirmed(hass) -> None:
    store, guard = cloud_baseline(hass)
    view_decide(guard, energy(outputProduced=3010.0))
    higher = energy(compressor=1001.0, eheater=20.0, outputProduced=3020.0)
    assert view_decide(guard, higher) is not None
    row = store.get(ROW)
    assert row.confirmed is None  # every part shown by this source
    assert row.values["=environmental_energy"] == 3020.0 - 1001.0 - 20.0
    # Confirmed: a later lower reading of the same source is a real reset.
    reset = energy(compressor=5.0, eheater=1.0, outputProduced=10.0)
    assert view_decide(guard, reset) is not None


async def test_derived_values_never_mix_parts_of_two_sources(hass) -> None:
    _, guard = cloud_baseline(hass)
    shown = view_decide(guard, energy(outputProduced=3010.0))
    vector = counter_vector(shown.resource)
    assert vector == {"outputProduced": 3010.0}  # nothing derived from old parts


async def test_only_the_local_plant_view_accepts_fewer_parts(hass) -> None:
    _, guard = cloud_baseline(hass)
    produced = snap(energy(outputProduced=3010.0))
    assert guard.decide(TOTAL, (("local", produced),)) is None


async def test_a_later_switch_to_the_cloud_leaves_only_a_gap(hass) -> None:
    store, guard = cloud_baseline(hass)
    view_decide(guard, energy(outputProduced=3010.0))
    behind = energy(compressor=995.0, eheater=20.0, outputProduced=3015.0)
    assert view_decide(guard, behind, "cloud") is None  # never backwards
    ahead = energy(compressor=1005.0, eheater=20.0, outputProduced=3015.0)
    assert view_decide(guard, ahead, "cloud") is not None
    row = store.get(ROW)
    assert row.source == "cloud" and row.confirmed is None


async def test_rows_keep_their_confirmed_parts(hass) -> None:
    from custom_components.bosch_buderus_heating.counter_selection import _rows

    values = {"compressor": 1.0, "outputProduced": 2.0}
    stored = {
        "counters": {
            "a|t|/x": {"source": "local", "values": values},  # older version
            "a|t|/y": {
                "source": "local",
                "values": values,
                "confirmed": ["outputProduced"],
            },
            "a|t|/z": {"source": "local", "values": values, "confirmed": "x"},
        }
    }
    rows = dict(_rows(stored))
    assert rows["a|t|/x"].confirmed is None and not rows["a|t|/x"].unconfirmed()
    assert rows["a|t|/y"].unconfirmed() == frozenset({"compressor"})
    assert rows["a|t|/z"].unconfirmed() == frozenset(values)  # damaged: none
    store = CounterSourceStore(hass)
    store.set("a|t|/y", rows["a|t|/y"])
    assert store._data()["counters"]["a|t|/y"]["confirmed"] == ["outputProduced"]
    store.set("a|t|/x", rows["a|t|/x"])
    assert "confirmed" not in store._data()["counters"]["a|t|/x"]


# ------------------------------- without the cloud always local (0.9 owner)


def stop_cloud(coordinator, *, retained=True) -> None:
    coordinator.data[TOTAL] = replace(
        coordinator.data[TOTAL],
        available=False,
        freshness=Freshness.STALE,
        last_error_category=SOURCE_STOPPED_CATEGORY,
        ended=True,
    )
    coordinator.local_retained = retained


async def test_a_switched_off_cloud_shows_local_counters_with_the_option_off(
    hass,
) -> None:
    coordinator, runtime, _ = _setup(hass, cloud_energy(10.0, 2.0, 40.0), enabled=False)
    runtime.observations = [local_energy(10.0, 2.0, 40.5)]
    # Cloud running, option off: the cloud counter, never the local one.
    assert shown(coordinator).source.value != "local"
    stop_cloud(coordinator)
    snapshot = shown(coordinator)
    assert snapshot.source.value == "local"
    assert parts(snapshot)["outputProduced"] == 40.5


async def test_without_the_cloud_a_lower_local_counter_is_still_held(hass) -> None:
    coordinator, runtime, _ = _setup(hass, cloud_energy(10.0, 2.0, 40.0), enabled=False)
    stop_cloud(coordinator)
    runtime.observations = [local_energy(10.0, 2.0, 39.0)]
    assert shown(coordinator) is None  # never backwards
    assert coordinator.counter_guard.blocked == {TOTAL: 1}
    runtime.observations = [local_energy(10.0, 2.0, 40.0, monotonic=20.0)]
    assert shown(coordinator).source.value == "local"


async def test_a_limited_cloud_offers_the_local_counter_with_the_option_off(
    hass,
) -> None:
    from time import monotonic

    coordinator, runtime, _ = _setup(hass, cloud_energy(10.0, 2.0, 40.0), enabled=False)
    runtime.observations = [local_energy(10.0, 2.0, 40.5)]
    # A pause is a durable outage: the local counter at once.
    coordinator._cloud_backoff_until = monotonic() + 60
    assert shown(coordinator).source.value == "local"
    coordinator._cloud_backoff_until = 0.0
    # Back with the option off: the cloud only when not backwards.
    assert shown(coordinator) is None


async def test_one_failed_cloud_cycle_never_switches_a_counter(
    hass, monkeypatch
) -> None:
    from custom_components.bosch_buderus_heating import coordinator as module

    clock = {"now": 5_000.0}
    monkeypatch.setattr(module, "monotonic", lambda: clock["now"])
    coordinator, runtime, _ = _setup(hass, cloud_energy(10.0, 2.0, 40.0), enabled=False)
    runtime.observations = [local_energy(10.0, 2.0, 40.5)]
    coordinator.last_update_success = False  # one blip
    assert coordinator.cloud_unavailable()  # other readings: at once
    assert not coordinator.counter_cloud_unavailable()
    assert shown(coordinator).source.value != "local"
    clock["now"] += module.COUNTER_OUTAGE_SECONDS - 1
    coordinator.last_update_success = False  # a later failed cycle keeps the start
    assert shown(coordinator).source.value != "local"
    coordinator.last_update_success = True  # back: the cloud counter stays shown
    clock["now"] += 60
    coordinator.last_update_success = False  # a new blip starts a new count
    assert not coordinator.counter_cloud_unavailable()
    assert shown(coordinator).source.value != "local"
    assert coordinator.counter_guard.blocked == {}


async def test_a_durable_unreachable_cloud_switches_counters_to_local(
    hass, monkeypatch
) -> None:
    from custom_components.bosch_buderus_heating import coordinator as module

    clock = {"now": 5_000.0}
    monkeypatch.setattr(module, "monotonic", lambda: clock["now"])
    coordinator, runtime, _ = _setup(hass, cloud_energy(10.0, 2.0, 40.0), enabled=False)
    runtime.observations = [local_energy(10.0, 2.0, 40.5)]
    coordinator.last_update_success = False
    clock["now"] += module.COUNTER_OUTAGE_SECONDS
    coordinator.last_update_success = False
    assert coordinator.counter_cloud_unavailable()
    snapshot = shown(coordinator)
    assert snapshot.source.value == "local"
    assert parts(snapshot)["outputProduced"] == 40.5
    # Sign-in required counts at once, without waiting.
    coordinator.last_update_success = True
    monkeypatch.setattr(coordinator, "_cloud_auth_required", lambda: True)
    assert coordinator.counter_cloud_unavailable()


# ------------------------------------- gone sources and waiting parts (0.9)


async def test_a_gone_source_lets_the_other_take_over_for_shared_parts(hass) -> None:
    store = CounterSourceStore(hass)
    full = energy(compressor=1000.0, eheater=20.0, outputProduced=3000.0)
    store.set(ROW, ShownCounter("local", counter_vector(full)))
    guard = CounterGuard(store, "owner", TARGET)
    fewer = snap(energy(compressor=1001.0, outputProduced=3005.0))
    # The local access still exists: every stored part must be present.
    assert guard.decide(TOTAL, (("cloud", fewer),)) is None
    shown_now = guard.decide(TOTAL, (("cloud", fewer),), gone=frozenset({"local"}))
    assert shown_now is not None
    row = store.get(ROW)
    assert row.source == "cloud" and "eheater" in row.unconfirmed()
    lower = snap(energy(compressor=999.0, outputProduced=3010.0))
    assert (
        CounterGuard(store, "owner", TARGET).decide(
            TOTAL, (("cloud", lower),), gone=frozenset({"local"})
        )
        is not None
    )  # the same source now: a real reset of a confirmed part stays visible


async def test_a_gone_source_never_allows_a_lower_shared_part(hass) -> None:
    store = CounterSourceStore(hass)
    store.set(ROW, ShownCounter("local", {"compressor": 1000.0, "eheater": 20.0}))
    guard = CounterGuard(store, "owner", TARGET)
    lower = snap(energy(compressor=999.0))
    assert guard.decide(TOTAL, (("cloud", lower),), gone=frozenset({"local"})) is None


async def test_parts_nobody_reports_are_dropped_after_the_waiting_time(
    hass, monkeypatch
) -> None:
    from custom_components.bosch_buderus_heating import counter_selection

    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(counter_selection, "_now", lambda: clock["now"])
    store, guard = cloud_baseline(hass)
    produced = energy(outputProduced=3010.0)
    assert view_decide(guard, produced) is not None
    row = store.get(ROW)
    assert row.since == 1_000_000.0 and "compressor" in row.unconfirmed()
    # Within the waiting time the parts keep binding.
    clock["now"] += counter_selection.STALE_PART_SECONDS - 1
    assert view_decide(guard, energy(outputProduced=3011.0)) is not None
    assert "compressor" in store.get(ROW).values
    # No current reading: no evidence, nothing is dropped.
    clock["now"] += 2
    assert guard.decide(TOTAL, (("local", None),), partial=True) is None
    assert "compressor" in store.get(ROW).values
    # A reported part is never dropped: it waits until it reaches its number.
    assert view_decide(guard, energy(eheater=19.0, outputProduced=3012.0)) is None
    assert store.get(ROW).values["eheater"] == 20.0
    # Parts nobody reports any more are dropped; shown numbers stay.
    assert view_decide(guard, energy(outputProduced=3013.0)) is not None
    row = store.get(ROW)
    assert set(row.values) == {"outputProduced"} and row.confirmed is None
    assert row.values["outputProduced"] == 3013.0
    # A later full cloud reading must not be lower than the shown part.
    behind = energy(compressor=1.0, eheater=1.0, outputProduced=3000.0)
    assert view_decide(guard, behind, "cloud") is None


async def test_a_row_without_a_waiting_time_starts_it(hass, monkeypatch) -> None:
    from custom_components.bosch_buderus_heating import counter_selection

    monkeypatch.setattr(counter_selection, "_now", lambda: 500.0)
    store = CounterSourceStore(hass)
    store.set(
        ROW,
        ShownCounter(
            "local",
            {"compressor": 1.0, "outputProduced": 2.0},
            frozenset({"outputProduced"}),
        ),
    )
    guard = CounterGuard(store, "owner", TARGET)
    view_decide(guard, energy(outputProduced=3.0))
    assert store.get(ROW).since == 500.0
    monkeypatch.setattr(
        counter_selection, "_now", lambda: 500.0 - 10 * 24 * 3600.0
    )  # clock set back: never stale
    view_decide(guard, energy(outputProduced=4.0))
    assert "compressor" in store.get(ROW).values


async def test_rows_keep_their_waiting_time(hass) -> None:
    from custom_components.bosch_buderus_heating.counter_selection import _rows

    values = {"compressor": 1.0, "outputProduced": 2.0}
    stored = {
        "counters": {
            "a|t|/y": {
                "source": "local",
                "values": values,
                "confirmed": ["outputProduced"],
                "since": 123.5,
            },
            "a|t|/z": {
                "source": "local",
                "values": values,
                "confirmed": ["outputProduced"],
                "since": "x",
            },
        }
    }
    rows = dict(_rows(stored))
    assert rows["a|t|/y"].since == 123.5
    assert rows["a|t|/z"].since is None  # damaged: the wait starts again
    store = CounterSourceStore(hass)
    store.set("a|t|/y", rows["a|t|/y"])
    assert store._data()["counters"]["a|t|/y"]["since"] == 123.5


async def test_shown_counters_save_within_a_minute_and_at_shutdown(
    hass, hass_storage
) -> None:
    """O69: 60 s save delay; a switch is written at once, a pending save at stop."""
    from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
    from homeassistant.util import dt as dt_util

    from custom_components.bosch_buderus_heating import counter_selection
    from tests.ha_test_helpers import async_fire_time_changed

    assert counter_selection.SAVE_DELAY_SECONDS == 60
    key = counter_selection.STORAGE_KEY
    store = CounterSourceStore(hass)
    await store.async_initialize()
    start = dt_util.utcnow()
    store.set("o|t|/a", ShownCounter("cloud", {"v": 1.0}))
    async_fire_time_changed(hass, start + timedelta(seconds=59))
    await hass.async_block_till_done()
    assert key not in hass_storage  # not after ten seconds any more
    async_fire_time_changed(hass, start + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert hass_storage[key]["data"]["counters"]["o|t|/a"]["values"] == {"v": 1.0}
    # A source switch is written at once.
    store.set("o|t|/a", ShownCounter("local", {"v": 2.0}), immediate=True)
    async_fire_time_changed(hass, start + timedelta(seconds=62))
    await hass.async_block_till_done()
    assert hass_storage[key]["data"]["counters"]["o|t|/a"]["source"] == "local"
    # A pending regular save is written when Home Assistant stops.
    store.set("o|t|/a", ShownCounter("local", {"v": 3.0}))
    await hass.async_block_till_done()
    assert hass_storage[key]["data"]["counters"]["o|t|/a"]["values"] == {"v": 2.0}
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()
    assert hass_storage[key]["data"]["counters"]["o|t|/a"]["values"] == {"v": 3.0}
