"""P49b: the local plant view as a coordinator, fed by published local states.

The coordinator is built directly; local states are published through a
small runtime and reach it the way the managed runtime delivers them (its
subscription), so every check runs through the real update cycle: rebuild,
counter rule, fault list and listener notification.
"""

from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.connection_runtime import ConnectionPhase
from custom_components.bosch_buderus_heating.const import DOMAIN, PollingProfile
from custom_components.bosch_buderus_heating.coordinator import (
    COUNTER_HELD_CATEGORY,
    SOURCE_STOPPED_CATEGORY,
    SnapshotSource,
)
from custom_components.bosch_buderus_heating.counter_selection import (
    ShownCounter,
    counter_source_store,
)
from custom_components.bosch_buderus_heating.device import device_info_for_resource
from custom_components.bosch_buderus_heating.faults import FaultTracker
from custom_components.bosch_buderus_heating.local_api.catalog import request_for_path
from custom_components.bosch_buderus_heating.local_api.models import Fault, FaultList
from custom_components.bosch_buderus_heating.local_only_sensor import (
    local_only_unique_id,
    track_local_only_sensors,
)
from custom_components.bosch_buderus_heating.local_plant_view import (
    build_local_plant_view,
    local_sensor,
)
from custom_components.bosch_buderus_heating.local_polling.inventory import (
    Freshness as LocalFreshness,
)
from custom_components.bosch_buderus_heating.local_state import LocalCondition
from custom_components.bosch_buderus_heating.managed_local_plant import (
    LOCAL_NOT_CURRENT_CATEGORY,
    LocalPlantCoordinator,
    registered_local_only_paths,
)
from custom_components.bosch_buderus_heating.pointt import Gateway
from tests.ha_test_helpers import MockConfigEntry
from tests.test_local_api import NOW, read
from tests.test_local_faults import ITEM
from tests.test_local_selection import LOCAL_BINDING, TARGET, FakeSnapshot

GATEWAY = "000123"
OWNER = "owner-a"
CURRENT = "/heatingCircuits/hc1/currentRoomSetpoint"  # paired
RETURN = "/heatSources/returnTemperature"  # paired, declared error markers
OFFERED = "/gateway/dataProcessing/status"  # unpaired, offered by the cloud
SETPOINT = "/heatSources/currentSupplySetpoint"  # reviewed local-only (P45)
ROOM = "/heatingCircuits/hc1/roomtemperature"  # documented, no P45 entry
STARTS = "/heatSources/numberOfStarts"  # paired counter
PRIVATE = "/gateway/eth/ip/ipv4"
_TIMES = itertools.count(100.0)


def reading(path, kind, value=None, **extra):
    raw = {"id": path, "type": kind, "writeable": 0}
    if value is not None:
        raw["value"] = value
    raw.update(extra)
    return read(raw, observed_monotonic=next(_TIMES))


def everyday(current=21.5, setpoint=34.5, room=21.25):
    return [
        reading(CURRENT, "floatValue", current, unitOfMeasure="C"),
        reading(SETPOINT, "floatValue", setpoint, unitOfMeasure="C"),
        reading(ROOM, "floatValue", room, unitOfMeasure="C"),
        reading(PRIVATE, "stringValue", "192.0.2.10"),
    ]


def fault_view(*items, monotonic):
    observation = SimpleNamespace(
        request=request_for_path("/notifications"),
        observed_monotonic=monotonic,
        data=FaultList(
            tuple(
                Fault(
                    display_code=f["dcd"],
                    cause_code=Decimal(str(f["ccd"])),
                    origin=f["orig"],
                    level=f["dlv"],
                    fault_class=f["fc"],
                )
                for f in items
            )
        ),
    )
    return SimpleNamespace(
        request=observation.request,
        freshness=LocalFreshness.FRESH,
        available=True,
        observation=observation,
        target_interval_seconds=60,
    )


class Runtime:
    """Publishes local states to subscribers like the managed runtime."""

    def __init__(self, *, local_values=False):
        # D2: the view shows values regardless of the ``local_values`` option.
        self.snapshot = FakeSnapshot(enabled=local_values)
        self.snapshot.owner_id = OWNER
        self.states: dict[str, object] = {}
        self.listeners: list = []

    def subscribe(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def local_state(self, binding_id):
        return self.states.get(binding_id)

    def connection_phase(self, _connection_id):
        return ConnectionPhase.DISABLED

    def publish(self, observations=(), *, last=(), faults=None, ready=True):
        if ready:
            self.states[LOCAL_BINDING] = SimpleNamespace(
                condition=LocalCondition.READY,
                current_observations=tuple(observations),
                last_observations=tuple(last),
                resources=(faults,) if faults is not None else (),
            )
        else:
            self.states.pop(LOCAL_BINDING, None)
        for listener in tuple(self.listeners):
            listener()


async def plant(
    hass: HomeAssistant, runtime: Runtime | None = None, **kwargs
) -> tuple[LocalPlantCoordinator, Runtime]:
    runtime = runtime or Runtime()
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    await counter_source_store(hass).async_initialize()
    source = SimpleNamespace(
        connection=SimpleNamespace(
            hass=hass,
            entry=entry,
            configuration=SimpleNamespace(
                polling_profile=lambda: PollingProfile.STANDARD
            ),
            startup=None,
            connection_id="cloud",
            runtime=runtime,
        ),
        binding=SimpleNamespace(target_id=TARGET),
        current=lambda: False,  # a dormant connection is never current
    )
    client = AsyncMock()
    coordinator = LocalPlantCoordinator(source, client, Gateway(GATEWAY), **kwargs)
    coordinator.client_mock = client
    # As a retained view: kept on its local readings.
    await coordinator.async_stop_source(retain=True)
    return coordinator, runtime


def shown(coordinator, path):
    snapshot = coordinator.display_snapshot(path)
    return (
        snapshot.resource.value if snapshot is not None and snapshot.available else None
    )


# ---------------------------------------------------------------- entities


async def test_the_view_keeps_cloud_ids_and_adds_local_sensors(
    hass: HomeAssistant,
) -> None:
    coordinator, runtime = await plant(hass, cloud_paths={OFFERED})
    updates = Mock()
    coordinator.async_add_listener(updates)
    runtime.publish(
        [
            *everyday(),
            reading(
                OFFERED,
                "stringValue",
                "completed",
                allowedValues=["running", "completed"],
            ),
        ]
    )
    assert set(coordinator.resources) == {CURRENT, OFFERED}
    assert set(coordinator.local_plant_sensors()) == {SETPOINT, ROOM}
    assert PRIVATE not in coordinator.resources
    assert PRIVATE not in coordinator.local_plant_sensors()
    # Values with the local option off (D2), read-only, from the local access.
    snapshot = coordinator.display_snapshot(CURRENT)
    assert snapshot.available and snapshot.resource.value == 21.5
    assert snapshot.source is SnapshotSource.LOCAL
    assert not snapshot.resource.metadata.writable
    assert shown(coordinator, OFFERED) == "completed"
    assert coordinator.local_only_reading(ROOM).value == Decimal("21.25")
    assert coordinator.last_update_success
    assert updates.call_count == 1
    # The same readings again change nothing.
    state = runtime.states[LOCAL_BINDING]
    runtime.publish(state.current_observations)
    assert updates.call_count == 1
    runtime.publish(everyday(current=22.0))
    assert shown(coordinator, CURRENT) == 22.0
    assert updates.call_count == 2
    # Never a cloud request, never a refresh of its own.
    await coordinator.async_refresh()
    await coordinator.async_request_refresh()
    assert coordinator.update_interval is None
    assert not coordinator.client_mock.mock_calls
    assert coordinator.control_status()[0] == "cloud_stopped"
    await coordinator.async_release_retention()


async def test_the_view_only_grows_and_missing_readings_become_unavailable(
    hass: HomeAssistant,
) -> None:
    coordinator, runtime = await plant(hass)
    runtime.publish(everyday())
    sensors = set(coordinator.local_plant_sensors())
    runtime.publish(everyday()[:1])  # setpoint and room not current any more
    assert set(coordinator.local_plant_sensors()) == sensors
    assert coordinator.local_only_reading(ROOM) is None
    assert shown(coordinator, CURRENT) == 21.5
    # The shape known before (last observations) keeps the entity set.
    runtime.publish([], last=everyday())
    assert set(coordinator.resources) == {CURRENT}
    snapshot = coordinator.display_snapshot(CURRENT)
    assert not snapshot.available and snapshot.ended
    assert snapshot.last_error_category == LOCAL_NOT_CURRENT_CATEGORY
    # The local access offline: everything unavailable, nothing removed.
    runtime.publish(ready=False)
    assert set(coordinator.resources) == {CURRENT}
    assert set(coordinator.local_plant_sensors()) == sensors
    assert not coordinator.last_update_success
    assert shown(coordinator, CURRENT) is None
    runtime.publish(everyday(current=23.0))
    assert shown(coordinator, CURRENT) == 23.0
    await coordinator.async_release_retention()


async def test_a_reading_in_another_unit_is_unavailable(hass: HomeAssistant) -> None:
    coordinator, runtime = await plant(hass)
    runtime.publish(everyday())
    runtime.publish([reading(CURRENT, "floatValue", 70.0, unitOfMeasure="%")])
    assert coordinator.data[CURRENT].last_error_category == LOCAL_NOT_CURRENT_CATEGORY
    assert shown(coordinator, CURRENT) is None
    assert coordinator.resources[CURRENT].metadata.unit == "C"  # first shape kept
    await coordinator.async_release_retention()


async def test_an_error_marker_keeps_a_generic_sensor(hass: HomeAssistant) -> None:
    signal = "/signals/SRC.CUHP.HP1.ReturnTemp"
    marker = reading(signal, "integerValue", 28, unitOfMeasure="C")
    marker = replace(marker, data=replace(marker.data, matched_states=("open",)))
    # C7: the value type decides, with and without the cloud.
    assert local_sensor(marker)[0] is not None
    assert local_sensor(marker)[0] is not None
    coordinator, runtime = await plant(hass)
    runtime.publish([marker])
    assert signal in coordinator.local_plant_sensors()
    entities: list = []
    track_local_only_sensors(
        SimpleNamespace(async_on_unload=Mock()), coordinator, entities.extend
    )
    (entity,) = entities
    entity.hass = hass
    assert entity.available and entity.native_value is None  # "unknown"
    await coordinator.async_release_retention()


async def test_a_registered_local_only_entity_keeps_its_id(
    hass: HomeAssistant,
) -> None:
    # A paired reading normally takes the cloud form ...
    view = build_local_plant_view(everyday()[:1])
    assert CURRENT in view.resources
    # ... but a registered local-only entity of the path wins.
    view = build_local_plant_view(everyday()[:1], local_only_paths={CURRENT})
    assert not view.resources
    assert [item.path for item in view.local_sensors] == [CURRENT]
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for gateway, path in ((GATEWAY, CURRENT), ("other", ROOM)):
        registry.async_get_or_create(
            "sensor",
            DOMAIN,
            local_only_unique_id(gateway, path),
            config_entry=entry,
        )
    assert registered_local_only_paths(hass, entry.entry_id, GATEWAY) == {CURRENT}
    coordinator, runtime = await plant(hass, local_only_paths={CURRENT})
    runtime.publish(everyday())
    assert CURRENT not in coordinator.resources
    assert CURRENT in coordinator.local_plant_sensors()
    await coordinator.async_release_retention()


async def test_local_only_entities_follow_the_view_and_stay_registered(
    hass: HomeAssistant,
) -> None:
    coordinator, runtime = await plant(hass)
    registry = er.async_get(hass)
    stale = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        local_only_unique_id(GATEWAY, "/heatSources/hs1/oduFanSpeed"),
        config_entry=coordinator.config_entry,
    )
    entities: list = []
    track_local_only_sensors(
        SimpleNamespace(async_on_unload=Mock()), coordinator, entities.extend
    )
    assert not entities
    runtime.publish(everyday())
    by_path = {entity.resource_path: entity for entity in entities}
    assert set(by_path) == {SETPOINT, ROOM}
    for entity in entities:
        entity.hass = hass
    # A documented reading without a P45 entry gets its value from the view.
    assert by_path[ROOM].available and by_path[ROOM].native_value == 21.25
    assert by_path[ROOM].unique_id == local_only_unique_id(GATEWAY, ROOM)
    runtime.publish(everyday()[:1])
    assert not by_path[ROOM].available
    runtime.publish(everyday())
    assert len(entities) == 2  # added once
    # Registered entries are never removed by the view (read-only).
    assert registry.async_get(stale.entity_id) is not None
    await coordinator.async_release_retention()


async def test_no_cloud_reading_for_per_source_entities(hass: HomeAssistant) -> None:
    coordinator, runtime = await plant(hass)
    runtime.publish(everyday())
    assert coordinator.source_snapshot(CURRENT, "cloud") is None
    assert coordinator.source_snapshot(CURRENT, "local").resource.value == 21.5
    await coordinator.async_release_retention()


# ----------------------------------------------------------------- counters


async def _counter(hass, last=None):
    coordinator, runtime = await plant(hass)
    if last is not None:
        counter_source_store(hass).set(
            f"{OWNER}|{TARGET}|{STARTS}", ShownCounter(last[0], {"value": last[1]})
        )
    return coordinator, runtime


def starts(value):
    return reading(STARTS, "floatValue", value)


async def test_a_counter_without_history_stays_unavailable(hass: HomeAssistant) -> None:
    coordinator, runtime = await _counter(hass)
    runtime.publish([starts(140)])
    assert STARTS in coordinator.counter_paths
    view = coordinator.display_snapshot(STARTS)
    assert not view.available and view.last_error_category == COUNTER_HELD_CATEGORY
    assert coordinator.data[STARTS].available  # the reading itself is current
    await coordinator.async_release_retention()


async def test_a_counter_continues_from_the_last_cloud_count(
    hass: HomeAssistant,
) -> None:
    coordinator, runtime = await _counter(hass, ("cloud", 139.0))
    runtime.publish([starts(140)])
    assert shown(coordinator, STARTS) == 140
    assert coordinator.counter_guard.shown[STARTS] == "local"
    await coordinator.async_release_retention()


async def test_a_counter_below_the_last_cloud_count_is_held(
    hass: HomeAssistant,
) -> None:
    coordinator, runtime = await _counter(hass, ("cloud", 150.0))
    runtime.publish([starts(140)])
    view = coordinator.display_snapshot(STARTS)
    assert not view.available and view.last_error_category == COUNTER_HELD_CATEGORY
    # The stored row is untouched: the cloud baseline still binds.
    stored = counter_source_store(hass).get(f"{OWNER}|{TARGET}|{STARTS}")
    assert stored == ShownCounter("cloud", {"value": 150.0})
    await coordinator.async_release_retention()


async def test_a_counter_is_never_shown_before_the_rule_decided(
    hass: HomeAssistant,
) -> None:
    coordinator, runtime = await _counter(hass, ("cloud", 139.0))
    coordinator.async_update_listeners = Mock()  # no update pass yet
    runtime.publish([starts(140)])
    assert coordinator.data[STARTS].available
    assert coordinator.display_snapshot(STARTS) is None
    await coordinator.async_release_retention()


# ------------------------------------------------------------------- faults


def test_the_local_list_alone_decides_without_a_cloud(hass: HomeAssistant) -> None:
    from tests.test_local_faults import Steps

    steps = Steps(hass)
    steps.tracker.local_authority = True
    assert not steps.tracker.has_known_state
    steps.local(ITEM)  # first list: the baseline, without an event
    assert steps.kinds() == []
    assert steps.tracker.has_known_state and steps.tracker.current_state_confirmed
    assert len(steps.tracker.active) == 1
    steps.local()
    assert not steps.tracker.current_state_confirmed  # resolution pending
    steps.local()
    steps.local()
    assert [kind.value for kind in steps.kinds()] == ["resolved"]
    assert steps.tracker.has_known_state and not steps.tracker.active
    steps.not_ready()
    assert not steps.tracker.has_known_state


async def test_a_fault_only_the_cloud_knew_is_never_resolved_by_the_list(
    hass: HomeAssistant,
) -> None:
    """Review P49b: without a cloud, the list's silence ends only its own faults."""
    from tests.test_local_faults import Steps

    other = {**ITEM, "ccd": 700, "dcd": "7A"}
    first = Steps(hass)
    first.cloud(ITEM, other)  # the cloud baseline knows both
    first.local(ITEM)  # the local list reports only one of them
    stored = first.tracker._serialize()

    plant = Steps(hass)
    plant.tracker.local_authority = True
    plant.tracker._store.async_load = AsyncMock(return_value=stored)
    await plant.tracker.async_load()
    for _ in range(4):
        plant.local(ITEM)
    assert plant.kinds() == []
    assert len(plant.tracker.active) == 2
    assert not plant.tracker.current_state_confirmed  # pending, not all-clear
    for _ in range(3):
        plant.local()
    # Only the fault the list reported is resolved by its silence.
    assert [event.fault.code for event in plant.events] == [str(ITEM["ccd"])]
    assert len(plant.tracker.active) == 1


async def test_only_the_latest_attempt_gives_the_baseline_back(
    hass: HomeAssistant,
) -> None:
    """Review P49b: an older, overlapping cloud attempt never resumes."""
    from tests.test_local_faults import Steps

    steps = Steps(hass)
    steps.tracker.local_authority = True
    steps.local(ITEM)
    first, second = object(), object()
    await steps.tracker.async_hand_over(first)
    await steps.tracker.async_hand_over(second)  # a newer attempt takes it
    await steps.tracker.async_resume(first)
    assert steps.tracker._handed_over  # the older attempt cannot give it back
    await steps.tracker.async_resume(second)
    assert not steps.tracker._handed_over


async def test_a_failed_attempt_stops_writing_before_the_view_resumes() -> None:
    """Review P49b: the failed attempt's delayed save can never land last."""
    from custom_components.bosch_buderus_heating.managed_pointt import PointTSource

    order = Mock()
    attempt = SimpleNamespace(
        async_hand_over=AsyncMock(side_effect=lambda: order("attempt"))
    )
    view = SimpleNamespace(
        async_resume_faults=AsyncMock(side_effect=lambda owner: order("view"))
    )
    source = SimpleNamespace(
        _faults_from=view, _attempt_faults=attempt, _published=False
    )
    await PointTSource._resume_faults(source)
    assert [call.args[0] for call in order.call_args_list] == ["attempt", "view"]
    view.async_resume_faults.assert_awaited_once_with(source)
    published = SimpleNamespace(
        _faults_from=view, _attempt_faults=attempt, _published=True
    )
    await PointTSource._resume_faults(published)
    view.async_resume_faults.assert_awaited_once()  # nothing after publishing


def test_a_partial_first_list_is_no_baseline(hass: HomeAssistant) -> None:
    from custom_components.bosch_buderus_heating.pointt import Resource
    from tests.test_local_faults import Steps

    steps = Steps(hass)
    steps.tracker.local_authority = True
    broken = Resource("/notifications", values=({"unexpected": 1},), has_values=True)
    steps.time += timedelta(minutes=1)
    steps.tracker.process_local(
        broken, reading=1.0, ready=True, interval=60, observed_at=steps.time
    )
    assert not steps.tracker.has_known_state and not steps.tracker.active
    steps.local(ITEM)  # the first complete list is the baseline
    assert steps.kinds() == [] and len(steps.tracker.active) == 1


def test_without_the_flag_the_local_list_never_stands_in(hass: HomeAssistant) -> None:
    tracker = FaultTracker(hass, "entry", "gateway")
    assert tracker.local_authority is False
    from tests.test_local_faults import Steps

    steps = Steps(hass)
    steps.local(ITEM)  # before the baseline: ignored, as before
    assert not steps.tracker.active and not steps.tracker.has_known_state


async def test_the_fault_state_follows_the_published_list(
    hass: HomeAssistant,
) -> None:
    coordinator, runtime = await plant(hass)
    runtime.publish(everyday(), faults=fault_view(ITEM, monotonic=1.0))
    faults = coordinator.faults
    assert faults.has_known_state and len(faults.active_faults) == 1
    for number in (2.0, 3.0, 4.0):
        runtime.publish(everyday(), faults=fault_view(monotonic=number))
    assert faults.has_known_state and not faults.active
    runtime.publish(ready=False)
    assert not faults.has_known_state
    await coordinator.async_release_retention()


# ------------------------------------------------------- lifecycle, device


async def test_retained_view_shows_values_and_ends_unavailable(
    hass: HomeAssistant,
) -> None:
    coordinator, runtime = await plant(hass)
    assert coordinator.local_retained and coordinator.retain_for_local()
    runtime.publish(everyday())
    snapshot = coordinator.data[CURRENT]
    assert snapshot.available
    assert snapshot.last_error_category != SOURCE_STOPPED_CATEGORY
    await coordinator.async_release_retention()
    assert not coordinator.local_retained
    assert not coordinator.data[CURRENT].available
    assert coordinator.display_snapshot(CURRENT).ended
    assert not coordinator.last_update_success
    listeners = len(runtime.listeners)
    runtime.publish(everyday(current=25.0))  # no longer listening
    assert listeners == 0 and not coordinator.data[CURRENT].available


async def test_a_known_device_keeps_its_registry_metadata(hass: HomeAssistant) -> None:
    coordinator, runtime = await plant(hass)
    coordinator.connection_id = None  # the brand of a legacy entry
    runtime.publish(everyday())
    info = device_info_for_resource(coordinator, CURRENT)
    # Unknown device: full metadata, as before.
    assert info.get("manufacturer") and "model" in info
    registry = dr.async_get(hass)
    registry.async_get_or_create(
        config_entry_id=coordinator.config_entry.entry_id,
        identifiers=info["identifiers"],
        model="MX400",
        serial_number="serial-1",
        sw_version="1.2.3",
    )
    assert device_info_for_resource(coordinator, CURRENT) == {
        "identifiers": info["identifiers"]
    }
    # A cloud view still sends everything.
    coordinator.read_only_view = False
    assert device_info_for_resource(coordinator, CURRENT).get("manufacturer")
    await coordinator.async_release_retention()


async def test_an_unavailable_reading_keeps_its_last_time(
    hass: HomeAssistant,
) -> None:
    coordinator, runtime = await plant(hass)
    runtime.publish(everyday())
    seen = coordinator.data[CURRENT].last_success
    assert seen == NOW  # the time of the local reading
    runtime.publish([])
    assert coordinator.data[CURRENT].last_success == seen
    await coordinator.async_release_retention()


# ------------------------------------------------ partial energy collections

TOTAL = "/heatSources/emon/totalConsumption"
COMPLETE = {
    "compressor": 10.0,
    "eheater": 2.0,
    "outputProduced": 40.0,
    "=total_electricity": 12.0,
    "=environmental_energy": 28.0,
}


def produced_only(value):
    """A local energy collection without the consumption parts."""
    return reading(TOTAL, "emonValue", values=[{"outputProduced": value}], unit="kWh")


async def _energy(hass):
    coordinator, runtime = await plant(hass)
    counter_source_store(hass).set(
        f"{OWNER}|{TARGET}|{TOTAL}", ShownCounter("cloud", dict(COMPLETE))
    )
    return coordinator, runtime


async def test_a_partial_energy_collection_continues_without_the_cloud(
    hass: HomeAssistant,
) -> None:
    coordinator, runtime = await _energy(hass)
    runtime.publish([produced_only(41)])
    assert TOTAL in coordinator.counter_paths
    view = coordinator.display_snapshot(TOTAL)
    assert view.available
    assert [dict(item) for item in view.resource.values] == [{"outputProduced": 41}]
    assert coordinator.counter_guard.shown[TOTAL] == "local"
    # The missing parts keep their last numbers for a later change of source.
    stored = counter_source_store(hass).get(f"{OWNER}|{TARGET}|{TOTAL}")
    assert stored.values == {**COMPLETE, "outputProduced": 41.0}
    await coordinator.async_release_retention()


async def test_a_lower_partial_energy_collection_is_held(hass: HomeAssistant) -> None:
    coordinator, runtime = await _energy(hass)
    runtime.publish([produced_only(39)])
    view = coordinator.display_snapshot(TOTAL)
    assert not view.available and view.last_error_category == COUNTER_HELD_CATEGORY
    await coordinator.async_release_retention()
