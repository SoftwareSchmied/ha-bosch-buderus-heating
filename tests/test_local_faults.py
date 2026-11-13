"""P42: the local error list as a second source of the same fault display."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating import managed_pointt
from custom_components.bosch_buderus_heating.faults import (
    LOCAL_FAULT_MAX_AGE,
    FaultEventType,
    FaultTracker,
    local_fault_resource,
)
from custom_components.bosch_buderus_heating.local_api.catalog import request_for_path
from custom_components.bosch_buderus_heating.local_api.models import Fault, FaultList
from custom_components.bosch_buderus_heating.pointt import Resource

NOW = datetime(2026, 11, 6, 3, 0, tzinfo=UTC)
ITEM = {"ccd": 521, "dcd": "4U", "orig": "8", "dlv": "8", "fc": "4"}


def cloud(*items) -> Resource:
    return Resource("/notifications", values=tuple(items), has_values=True)


def local(*faults) -> Resource:
    observation = SimpleNamespace(
        request=request_for_path("/notifications"),
        data=FaultList(
            tuple(
                Fault(
                    display_code=f["dcd"],
                    cause_code=Decimal(str(f["ccd"])),
                    origin=f["orig"],
                    level=f["dlv"],
                    fault_class=f["fc"],
                )
                for f in faults
            )
        ),
    )
    resource = local_fault_resource(observation)
    assert resource is not None
    return resource


class Steps:
    def __init__(self, hass):
        self.tracker = FaultTracker(hass, "entry", "gateway")
        self.events = []
        self.tracker.async_add_listener(self.events.append)
        self.time = NOW
        self.reading = 0.0
        # The monotonic clock follows the test time unless a test moves only
        # the wall clock.
        self.tracker._clock = lambda: (self.time - NOW).total_seconds()

    def cloud(self, *items):
        self.time += timedelta(minutes=1)
        resource = cloud(*items)
        return self.tracker.process_resources(
            {resource.path: resource}, observed_at=self.time
        )

    def local(self, *items, ready=True, same=False, interval=60):
        self.time += timedelta(minutes=1)
        if not same:
            self.reading += 60
        return self.tracker.process_local(
            local(*items) if items is not None else None,
            reading=self.reading,
            ready=ready,
            interval=interval,
            observed_at=self.time,
        )

    def no_local_answer(self):
        """Ready, but no new list (late reading or none offered)."""
        self.time += timedelta(minutes=1)
        return self.tracker.process_local(
            None, reading=None, ready=True, observed_at=self.time
        )

    def not_ready(self):
        self.time += timedelta(minutes=1)
        return self.tracker.process_local(
            None, reading=None, ready=False, observed_at=self.time
        )

    def kinds(self):
        return [event.event_type for event in self.events]


def test_the_same_fault_from_both_sources_is_one_fault(hass: HomeAssistant) -> None:
    steps = Steps(hass)
    steps.cloud(ITEM)  # first read: baseline
    steps.local(ITEM)
    assert len(steps.tracker.active_faults) == 1
    assert steps.events == []  # no second "appeared" for the same fault


def test_a_decimal_code_gives_the_cloud_fingerprint() -> None:
    local_resource = local(ITEM)
    assert local_resource.values[0]["ccd"] == 521
    assert type(local_resource.values[0]["ccd"]) is int


def test_a_fault_only_the_local_list_reports_appears_once(hass: HomeAssistant) -> None:
    steps = Steps(hass)
    steps.cloud()  # baseline: nothing
    steps.local(ITEM)
    steps.local(ITEM)
    assert steps.kinds() == [FaultEventType.APPEARED]
    assert len(steps.tracker.active_faults) == 1


def test_resolution_needs_every_attached_source(hass: HomeAssistant) -> None:
    steps = Steps(hass)
    steps.cloud(ITEM)
    steps.local(ITEM)
    # The cloud no longer reports it, but the local list still does.
    steps.cloud()
    steps.cloud()
    steps.cloud()
    assert len(steps.tracker.active_faults) == 1
    # Still confirmed: the local list reports it.
    assert steps.tracker.current_state_confirmed
    # Now the local list confirms the absence twice as well.
    steps.local()
    assert len(steps.tracker.active_faults) == 1
    assert not steps.tracker.current_state_confirmed  # pending resolution
    steps.local()
    assert steps.tracker.active_faults == ()
    assert steps.kinds() == [FaultEventType.RESOLVED]


def test_a_missing_or_repeated_local_answer_resolves_nothing(
    hass: HomeAssistant,
) -> None:
    steps = Steps(hass)
    steps.cloud(ITEM)
    steps.local(ITEM)
    steps.cloud()
    steps.cloud()
    steps.local()
    steps.not_ready()  # not ready: no evidence of its own
    assert len(steps.tracker.active_faults) == 1
    steps.local(same=True)  # the same reading is no new evidence
    assert len(steps.tracker.active_faults) == 1
    steps.local()  # the second new reading without it
    assert steps.tracker.active_faults == ()


def test_brief_pauses_between_readings_never_block_forever(
    hass: HomeAssistant,
) -> None:
    """Review P42 round 3: a pause between readings keeps the count."""
    steps = Steps(hass)
    steps.cloud(ITEM)
    steps.local(ITEM)
    steps.cloud()
    steps.cloud()
    steps.local()
    steps.not_ready()
    steps.local()
    assert steps.tracker.active_faults == ()


def test_a_list_back_from_silence_is_saved_again(hass: HomeAssistant) -> None:
    """Review P42 round 3: a restart after the return must know the list."""
    steps = Steps(hass)
    steps.cloud()
    steps.local(ITEM)  # only the local list knows it
    steps.time += LOCAL_FAULT_MAX_AGE
    steps.cloud()  # silent too long: saved as not taking part
    assert steps.tracker.diagnostics()["local_error_list"] == "stale"
    steps.tracker._schedule_save = MagicMock()
    steps.local(ITEM)  # back with the same set
    steps.tracker._schedule_save.assert_called()
    assert steps.tracker._serialize()["local_faults"]


def test_a_local_access_that_is_not_ready_resolves_nothing(
    hass: HomeAssistant,
) -> None:
    steps = Steps(hass)
    steps.cloud(ITEM)
    steps.local(ITEM)
    steps.cloud()
    steps.cloud()
    for _ in range(3):
        steps.local(ready=False)
    assert len(steps.tracker.active_faults) == 1


def test_without_a_local_list_the_cloud_decides_alone(hass: HomeAssistant) -> None:
    steps = Steps(hass)
    steps.cloud(ITEM)
    for _ in range(3):
        steps.no_local_answer()  # this gateway never offered the list
    steps.cloud()
    steps.cloud()
    assert steps.tracker.active_faults == ()


def test_a_removed_local_access_lets_the_cloud_resolve(hass: HomeAssistant) -> None:
    steps = Steps(hass)
    steps.cloud(ITEM)
    steps.local(ITEM)
    steps.cloud()
    steps.cloud()
    assert len(steps.tracker.active_faults) == 1
    steps.tracker.detach_local()
    steps.cloud()
    assert steps.tracker.active_faults == ()


def test_local_readings_before_the_baseline_are_ignored(hass: HomeAssistant) -> None:
    steps = Steps(hass)
    assert steps.local(ITEM) == ()
    assert steps.tracker.active_faults == ()
    steps.cloud(ITEM)
    assert steps.events == []


def test_device_error_lists_are_not_used_yet() -> None:
    observation = SimpleNamespace(
        request=SimpleNamespace(path="/devices/device1/errors"), data=object()
    )
    assert local_fault_resource(observation) is None


def test_the_fault_hook_never_blocks_local_value_updates(caplog) -> None:
    import logging

    from custom_components.bosch_buderus_heating.connection_runtime import (
        RepeatedFailure,
    )
    from custom_components.bosch_buderus_heating.managed_pointt import (
        _LOGGER,
        ManagedPointTCoordinator,
    )

    caplog.set_level(logging.DEBUG, logger=_LOGGER.name)
    failing = [True]

    def broken(selection):
        if failing:
            raise RuntimeError("unexpected value 21.5")
        return False

    fake = SimpleNamespace(
        source=SimpleNamespace(current=lambda: True),
        local_retained=False,
        local_selection=SimpleNamespace(changed=lambda: True),
        _local_faults_updated=broken,
        _local_fault_failure=RepeatedFailure(
            _LOGGER, "The local error list could not be applied"
        ),
        async_update_listeners=MagicMock(),
    )
    for _ in range(3):
        ManagedPointTCoordinator._local_updated(fake)
    assert fake.async_update_listeners.call_count == 3

    # C61 (PRIV-2): once with the error class only, never its message; the
    # traceback and the repetitions at debug level.
    def warnings():
        return [
            record
            for record in caplog.records
            if record.levelno >= logging.WARNING and record.name == _LOGGER.name
        ]

    assert [record.getMessage() for record in warnings()] == [
        "The local error list could not be applied (RuntimeError); further "
        "failures are logged at debug level"
    ]
    assert warnings()[0].exc_info is None
    assert any(record.exc_info for record in caplog.records)
    failing.clear()
    ManagedPointTCoordinator._local_updated(fake)  # a success ends the series
    failing.append(True)
    ManagedPointTCoordinator._local_updated(fake)
    assert len(warnings()) == 2


def test_the_fault_reading_takes_only_a_fresh_error_list() -> None:
    from custom_components.bosch_buderus_heating.local_polling.inventory import (
        Freshness,
    )
    from custom_components.bosch_buderus_heating.local_selection import (
        LocalSelection,
    )
    from custom_components.bosch_buderus_heating.local_state import LocalCondition
    from tests.test_local_selection import TARGET, FakeRuntime

    request = request_for_path("/notifications")
    observation = object()

    def view(freshness, available=True):
        return SimpleNamespace(
            request=request,
            freshness=freshness,
            available=available,
            observation=observation,
            target_interval_seconds=60,
        )

    runtime = FakeRuntime([])
    selection = LocalSelection(runtime, TARGET)
    for freshness, expected in (
        (Freshness.FRESH, (True, observation, 60)),
        (Freshness.LATE, (True, None, None)),  # e.g. after a timeout
    ):
        runtime.local_state = lambda binding, f=freshness: SimpleNamespace(
            condition=LocalCondition.READY,
            resources=(view(f),),
            current_observations=(),
        )
        assert selection.fault_reading() == expected


def test_the_local_list_alone_never_ends_a_fault_the_cloud_knew(
    hass: HomeAssistant,
) -> None:
    """Without a complete cloud confirmation the local list cannot resolve."""
    steps = Steps(hass)
    steps.cloud(ITEM)
    steps.local(ITEM)
    for _ in range(4):
        steps.local()  # the cloud has not confirmed the absence yet
    assert len(steps.tracker.active_faults) == 1
    assert steps.events == []


def test_a_late_local_reading_keeps_the_count(hass: HomeAssistant) -> None:
    """Review P42: a late reading is no evidence, but no reset either."""
    steps = Steps(hass)
    steps.cloud(ITEM)
    steps.local(ITEM)
    steps.cloud()
    steps.cloud()
    steps.local()
    for _ in range(3):
        steps.no_local_answer()  # ready, but the reading is late
    steps.local()
    assert steps.tracker.active_faults == ()
    assert steps.kinds() == [FaultEventType.RESOLVED]


def test_a_silent_local_list_stops_blocking_after_its_maximum_age(
    hass: HomeAssistant,
) -> None:
    steps = Steps(hass)
    steps.cloud(ITEM)
    steps.local(ITEM)
    steps.cloud()
    steps.cloud()
    assert len(steps.tracker.active_faults) == 1
    steps.time += LOCAL_FAULT_MAX_AGE
    steps.cloud()
    assert steps.tracker.active_faults == ()
    assert steps.tracker.diagnostics()["local_error_list"] == "stale"
    # A new reading makes it take part again: its fault holds against the cloud.
    steps.local(ITEM)
    assert steps.tracker.diagnostics()["local_error_list"] == "success"
    steps.cloud()
    steps.cloud()
    assert len(steps.tracker.active_faults) == 1


def test_a_local_list_within_its_maximum_age_still_blocks(
    hass: HomeAssistant,
) -> None:
    steps = Steps(hass)
    steps.cloud(ITEM)
    steps.local(ITEM)
    steps.time += LOCAL_FAULT_MAX_AGE - timedelta(minutes=3)
    steps.cloud()
    steps.cloud()  # just within the maximum age: still taking part
    assert len(steps.tracker.active_faults) == 1


def test_every_new_local_reading_renews_its_age(hass: HomeAssistant) -> None:
    steps = Steps(hass)
    steps.cloud(ITEM)
    steps.local(ITEM)
    steps.time += LOCAL_FAULT_MAX_AGE - timedelta(minutes=3)
    steps.local(ITEM)
    steps.time += LOCAL_FAULT_MAX_AGE - timedelta(minutes=3)
    steps.cloud()
    steps.cloud()
    assert len(steps.tracker.active_faults) == 1
    assert steps.tracker.diagnostics()["local_error_list"] == "success"


def test_a_fault_one_source_reports_stays_confirmed(hass: HomeAssistant) -> None:
    """Review P42: faults of a single source never stay unconfirmed."""
    steps = Steps(hass)
    steps.cloud(ITEM)  # only the cloud knows it
    steps.local()
    steps.local()
    steps.local()
    steps.cloud(ITEM)
    assert steps.tracker.current_state_confirmed
    other = dict(ITEM, ccd=600, dcd="5H")
    steps.local(other)  # only the local list knows this one
    steps.cloud(ITEM)
    steps.cloud(ITEM)
    assert len(steps.tracker.active_faults) == 2
    assert steps.tracker.current_state_confirmed


async def test_a_restart_keeps_a_local_only_fault(hass: HomeAssistant) -> None:
    """Review P42: no spurious resolved/appeared pair after a restart."""
    first = Steps(hass)
    first.cloud()
    first.local(ITEM)
    stored = first.tracker._serialize()
    assert stored["local_faults"]

    second = Steps(hass)
    second.tracker._store.async_load = AsyncMock(return_value=stored)
    await second.tracker.async_load()
    second.cloud()
    second.cloud()
    second.cloud()  # the local access is not back yet
    assert len(second.tracker.active_faults) == 1
    second.local(ITEM)
    assert second.events == []
    second.local()
    second.local()
    assert second.kinds() == [FaultEventType.RESOLVED]


async def test_after_a_restart_without_a_local_list_the_cloud_decides(
    hass: HomeAssistant,
) -> None:
    first = Steps(hass)
    first.cloud()
    first.local(ITEM)
    stored = first.tracker._serialize()
    second = Steps(hass)
    second.tracker._store.async_load = AsyncMock(return_value=stored)
    await second.tracker.async_load()
    second.cloud()
    second.cloud()
    assert len(second.tracker.active_faults) == 1
    second.time += LOCAL_FAULT_MAX_AGE
    second.cloud()
    assert second.tracker.active_faults == ()


def test_without_a_current_local_list_nothing_extra_is_stored(
    hass: HomeAssistant,
) -> None:
    steps = Steps(hass)
    steps.cloud(ITEM)
    assert "local_faults" not in steps.tracker._serialize()
    steps.local(ITEM)
    steps.tracker.detach_local()
    assert "local_faults" not in steps.tracker._serialize()


def test_a_local_sighting_verifies_a_restored_fault(hass: HomeAssistant) -> None:
    steps = Steps(hass)
    steps.cloud()
    steps.local(ITEM)
    fingerprint = steps.tracker.active_faults[0].fingerprint
    steps.tracker._unverified_restored.add(fingerprint)
    steps.local(ITEM)
    assert fingerprint not in steps.tracker._unverified_restored


class _Faults:
    def __init__(self, changes):
        self.state_version = 0
        self.changes = changes
        self.process_local = MagicMock(side_effect=self._process)
        self.detach_local = MagicMock(side_effect=self._process)
        self.process_device_errors = MagicMock(return_value=False)

    def _process(self, *args, **kwargs):
        if self.changes:
            self.state_version += 1


def _coordinator(has_local_access=True, events=(), interval=None):
    selection = SimpleNamespace(
        has_local_access=has_local_access,
        fault_reading=lambda: (True, None, interval),
        device_error_lists=lambda: (),
    )
    return SimpleNamespace(faults=_Faults(bool(events))), selection


def test_the_interval_reaches_the_tracker(monkeypatch) -> None:
    monkeypatch.setattr(managed_pointt, "LOCAL_FAULT_SOURCE", True)
    fake, selection = _coordinator(interval=900)
    managed_pointt.ManagedPointTCoordinator._local_faults_updated(fake, selection)
    assert fake.faults.process_local.call_args.kwargs["interval"] == 900


def test_a_switched_off_local_access_detaches(monkeypatch) -> None:
    monkeypatch.setattr(managed_pointt, "LOCAL_FAULT_SOURCE", True)
    fake, selection = _coordinator(has_local_access=False)
    assert not managed_pointt.ManagedPointTCoordinator._local_faults_updated(
        fake, selection
    )
    fake.faults.detach_local.assert_called_once()
    fake.faults.process_local.assert_not_called()


def test_a_local_fault_event_refreshes_the_entities(monkeypatch) -> None:
    monkeypatch.setattr(managed_pointt, "LOCAL_FAULT_SOURCE", True)
    fake, selection = _coordinator(events=(object(),))
    assert managed_pointt.ManagedPointTCoordinator._local_faults_updated(
        fake, selection
    )
    fake, selection = _coordinator(events=())
    assert not managed_pointt.ManagedPointTCoordinator._local_faults_updated(
        fake, selection
    )
    hook = SimpleNamespace(
        source=SimpleNamespace(current=lambda: True),
        local_retained=False,
        local_selection=SimpleNamespace(changed=lambda: False),
        _local_faults_updated=lambda selection: True,
        _local_fault_failure=MagicMock(),
        async_update_listeners=MagicMock(),
    )
    managed_pointt.ManagedPointTCoordinator._local_updated(hook)
    hook.async_update_listeners.assert_called_once()


def test_the_local_fault_source_is_on_and_can_be_switched_off(monkeypatch) -> None:
    """Switched off, the local list changes nothing."""
    assert managed_pointt.LOCAL_FAULT_SOURCE is True
    monkeypatch.setattr(managed_pointt, "LOCAL_FAULT_SOURCE", False)
    fake, selection = _coordinator(events=(object(),))
    assert not managed_pointt.ManagedPointTCoordinator._local_faults_updated(
        fake, selection
    )
    fake.faults.process_local.assert_not_called()
    fake.faults.detach_local.assert_not_called()


def test_a_slow_local_interval_never_counts_as_silent(hass: HomeAssistant) -> None:
    """Review P42: with a 900 s interval the list stays taking part."""
    steps = Steps(hass)
    steps.cloud()
    steps.local(ITEM, interval=900)  # only the local list knows it
    for _ in range(16):
        steps.cloud()  # one cloud read per minute, the next list is due later
    steps.local(ITEM, interval=900)
    assert steps.kinds() == [FaultEventType.APPEARED]
    assert len(steps.tracker.active_faults) == 1


def test_a_wall_clock_jump_does_not_end_the_local_evidence(
    hass: HomeAssistant,
) -> None:
    """Review P42: the age uses a monotonic clock."""
    steps = Steps(hass)
    steps.tracker._clock = lambda: 100.0  # monotonic time stands still
    steps.cloud()
    steps.local(ITEM)
    steps.time += timedelta(hours=1)  # only the wall clock jumps
    steps.cloud()
    steps.cloud()
    assert len(steps.tracker.active_faults) == 1
    assert steps.tracker.diagnostics()["local_error_list"] == "success"


def test_the_local_list_never_stands_in_for_an_unreadable_cloud(
    hass: HomeAssistant,
) -> None:
    """Review P42: a failed cloud read stays an unknown state."""
    from custom_components.bosch_buderus_heating.pointt.models import (
        BatchItemResult,
    )

    steps = Steps(hass)
    steps.cloud()
    steps.local()
    steps.tracker.record_results(
        [
            BatchItemResult(
                gateway_id="gateway", path="/notifications", resource=None, status=500
            )
        ]
    )
    assert not steps.tracker.has_known_state


def test_a_silent_local_list_is_saved_as_not_taking_part(
    hass: HomeAssistant,
) -> None:
    """Review P42: after a restart a stale list must not block again."""
    steps = Steps(hass)
    steps.cloud(ITEM)
    steps.local(ITEM)
    steps.tracker._schedule_save = MagicMock()
    steps.time += LOCAL_FAULT_MAX_AGE
    steps.cloud(ITEM)
    steps.tracker._schedule_save.assert_called()
    assert "local_faults" not in steps.tracker._serialize()


def test_a_changed_local_set_is_saved(hass: HomeAssistant) -> None:
    steps = Steps(hass)
    steps.cloud(ITEM)
    steps.local()
    steps.tracker._schedule_save = MagicMock()
    steps.local(ITEM)  # known to the cloud already: no event, but a new set
    steps.tracker._schedule_save.assert_called()
    steps.tracker._schedule_save.reset_mock()
    steps.local(ITEM)
    steps.tracker._schedule_save.assert_not_called()


def test_a_not_ready_local_access_confirms_nothing(hass: HomeAssistant) -> None:
    """Review P42: the last list is no evidence while the access is not ready."""
    steps = Steps(hass)
    steps.cloud(ITEM)
    steps.local(ITEM)
    steps.cloud()  # the cloud dropped it, the local list still had it
    assert steps.tracker.current_state_confirmed
    steps.not_ready()
    assert not steps.tracker.current_state_confirmed


def test_absence_counters_stay_bounded(hass: HomeAssistant) -> None:
    steps = Steps(hass)
    steps.cloud(ITEM)
    other = dict(ITEM, ccd=600, dcd="5H")
    steps.local(other)
    for _ in range(5):
        steps.cloud(ITEM)  # the cloud never reports the local-only fault
        steps.local(other)  # the local list never reports the cloud fault
    assert max(steps.tracker._absence_counts.values()) == 2
    assert max(steps.tracker._local_absence.values()) == 2
    assert len(steps.tracker.active_faults) == 2


def test_a_state_change_without_an_event_refreshes_the_entities(
    monkeypatch,
) -> None:
    """Review P42: e.g. not ready, silent or switched off."""
    monkeypatch.setattr(managed_pointt, "LOCAL_FAULT_SOURCE", True)
    fake, selection = _coordinator(has_local_access=False, events=(object(),))
    assert managed_pointt.ManagedPointTCoordinator._local_faults_updated(
        fake, selection
    )


def test_the_state_version_follows_local_state_changes(hass: HomeAssistant) -> None:
    steps = Steps(hass)
    steps.cloud(ITEM)
    steps.local(ITEM)
    before = steps.tracker.state_version
    steps.not_ready()
    assert steps.tracker.state_version != before
    before = steps.tracker.state_version
    steps.tracker.detach_local()
    assert steps.tracker.state_version != before


def test_an_unchanged_local_reading_does_not_refresh(hass: HomeAssistant) -> None:
    steps = Steps(hass)
    steps.cloud(ITEM)
    steps.local(ITEM)
    steps.local(ITEM)
    before = steps.tracker.state_version
    steps.local(ITEM)  # a new reading with the same content
    assert steps.tracker.state_version == before
