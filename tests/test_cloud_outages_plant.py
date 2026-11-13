"""Short cloud outages of my own plant, replayed with real polling cycles.

The cycles run through the coordinator's own refresh on the anonymized cloud
inventory of my heat pump (K40RF gateway, MX400 controller) with a simulated
clock. Each request takes the simulated time it took on the plant, and the
next cycle starts one interval after the previous one finished.

Replayed outages:

- an answered batch request whose 17 readings all came back with a gateway
  timeout after 10.6 s, then a quick failed cycle in which the fault list and
  its single read timed out as well, then the cloud answered again;
- an answered batch request in which 24 readings came back with server errors
  while the others answered.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating import coordinator as coordinator_module
from custom_components.bosch_buderus_heating.binary_sensor import (
    BoschBuderusHolidayActiveBinarySensor,
    BoschBuderusSystemFaultBinarySensor,
)
from custom_components.bosch_buderus_heating.calendar import (
    BoschBuderusHolidayCalendar,
)
from custom_components.bosch_buderus_heating.const import DOMAIN, PollingProfile
from custom_components.bosch_buderus_heating.coordinator import (
    GATEWAY_TIMEOUT_PAUSE,
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Gateway,
    RequestMetrics,
    Resource,
)
from custom_components.bosch_buderus_heating.pointt.exceptions import (
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.pointt.parsers import resource_error
from custom_components.bosch_buderus_heating.resource_catalog import PollGroup
from custom_components.bosch_buderus_heating.sensor import (
    BoschBuderusActiveFaultsSensor,
    BoschBuderusActiveNotificationsSensor,
    BoschBuderusNextHolidaySensor,
    BoschBuderusSensor,
    build_sensor_descriptions,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.test_cloud_polling_plan import START, Clock, _capture, _patched

COMPRESSOR = "/heatSources/compressor/status"  # read every minute, cloud only
NOTIFICATIONS = "/notifications"
PRESSURE = "/heatSources/systemPressure"
HOLIDAYS = (
    "/holidayMode/activeModes",
    "/holidayMode/configuration",
    "/holidayMode/list",
)
AWAY = "/system/awayMode/enabled"
STATIC = "/gateway/versionHardware"  # read once, never expires
INTERVAL = 60.0
FAULTS_MODULE = "custom_components.bosch_buderus_heating.faults"


class Cloud:
    """The plant's cloud with scripted failures; every request takes time."""

    def __init__(self, resources: dict[str, Resource], clock: Clock) -> None:
        self.resources = resources
        self.clock = clock
        # Every reading of every batch answers with this gateway status.
        self.all_status: int | None = None
        # Only these readings answer with their gateway status.
        self.failing: dict[str, int] = {}
        # Each single read fails with this HTTP status.
        self.get_status: int | None = None
        self.seconds = 0.5
        self.batches: list[tuple[float, tuple[str, ...]]] = []
        self.gets: list[tuple[float, str]] = []
        # Called before each answer is returned (at its simulated time).
        self.before_answer: Callable[[], None] | None = None

    async def bulk(self, gateway: str, paths) -> tuple[BatchItemResult, ...]:
        self.batches.append((self.clock.now, tuple(paths)))
        self.clock.now += self.seconds
        if self.before_answer is not None:
            self.before_answer()
        results = []
        for path in paths:
            status = self.all_status or self.failing.get(path)
            if status is not None:
                results.append(
                    BatchItemResult(
                        gateway,
                        path,
                        status,
                        error=resource_error(path, status),
                        server_status=200,
                        gateway_status=status,
                    )
                )
            elif path in self.resources:
                results.append(
                    BatchItemResult(gateway, path, 200, resource=self.resources[path])
                )
            else:
                results.append(
                    BatchItemResult(gateway, path, 404, error=resource_error(path, 404))
                )
        return tuple(results)

    async def get(self, gateway: str, path: str, **_kwargs) -> Resource:
        self.gets.append((self.clock.now, path))
        self.clock.now += self.seconds
        if self.get_status is not None:
            raise ServiceUnavailable(self.get_status)
        return self.resources[path]

    def read_in(self, start: float, path: str) -> bool:
        return any(at >= start and path in paths for at, paths in self.batches)


@contextmanager
def _plant(
    hass: HomeAssistant, clock: Clock
) -> Iterator[tuple[BoschBuderusDataUpdateCoordinator, Cloud]]:
    """The plant just after its discovery, polled with the simulated clock."""
    resources = _capture()
    cloud = Cloud(resources, clock)
    client = AsyncMock()
    client.metrics = RequestMetrics()
    client.get_resources_bulk.side_effect = cloud.bulk
    client.get_resource.side_effect = cloud.get
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.utc()

    with ExitStack() as stack:
        for item in _patched(clock):
            stack.enter_context(item)
        stack.enter_context(patch(f"{FAULTS_MODULE}.datetime", FakeDatetime))
        coordinator = BoschBuderusDataUpdateCoordinator(
            hass,
            client,
            Gateway("gateway-1", device_type="k40"),
            entry,
            PollingProfile.STANDARD,
        )
        # The test drives each cycle itself.
        coordinator.update_interval = None
        coordinator.resources = dict(resources)
        coordinator.data = {
            path: ResourceSnapshot(resource, True, clock.utc())
            for path, resource in resources.items()
        }
        coordinator._rebuild_poll_groups()
        coordinator._next_update = {
            group: clock.now + interval.total_seconds()
            for group, interval in coordinator._poll_intervals.items()
        }
        yield coordinator, cloud


async def _refresh(coordinator: BoschBuderusDataUpdateCoordinator) -> None:
    """One real cycle, through Home Assistant's refresh."""
    await coordinator.async_refresh()


def _due(coordinator: BoschBuderusDataUpdateCoordinator, clock: Clock, *groups):
    for group in groups:
        coordinator._next_update[group] = clock.now


async def _confirmed_start(
    coordinator: BoschBuderusDataUpdateCoordinator, cloud: Cloud, clock: Clock
) -> None:
    """A normal cycle one minute after the discovery that reads the fault list."""
    clock.now += INTERVAL
    _due(coordinator, clock, PollGroup.NOTIFICATIONS)
    await _refresh(coordinator)
    assert coordinator.last_update_success
    assert cloud.read_in(clock.now - 5, NOTIFICATIONS)
    assert coordinator.faults.current_state_confirmed


def _shown(coordinator: BoschBuderusDataUpdateCoordinator, path: str) -> bool:
    snapshot = coordinator.display_snapshot(path)
    return snapshot is not None and snapshot.available


def _wait(clock: Clock, seconds: float, check: Callable[[], None]) -> None:
    """Let ``seconds`` pass, checking every second between two cycles."""
    end = clock.now + seconds
    while clock.now < end:
        clock.now = min(end, clock.now + 1.0)
        check()


async def _the_outage(
    coordinator: BoschBuderusDataUpdateCoordinator,
    cloud: Cloud,
    clock: Clock,
    check: Callable[[], None],
) -> float:
    """The slow failed cycle, the quick one and the recovery; returns the start
    of the recovery cycle."""
    # 10.6 s: every reading of the answered batch timed out at the gateway.
    _wait(clock, INTERVAL, check)
    cloud.all_status, cloud.get_status, cloud.seconds = 504, 504, 10.56
    await _refresh(coordinator)
    assert not coordinator.last_update_success
    check()
    # 0.5 s: the fault list is due as well; nothing answers.
    _wait(clock, INTERVAL, check)
    cloud.seconds = 0.5
    _due(coordinator, clock, PollGroup.NOTIFICATIONS)
    await _refresh(coordinator)
    assert not coordinator.last_update_success
    check()
    # The cloud answers again; also while that answer is on its way.
    _wait(clock, INTERVAL, check)
    cloud.all_status = cloud.get_status = None
    recovery = clock.now
    cloud.before_answer = check
    await _refresh(coordinator)
    cloud.before_answer = None
    assert coordinator.last_update_success
    check()
    return recovery


# --------------------------------------------------- the validity of readings


async def test_a_slow_failed_cycle_then_a_quick_one_keeps_live_values(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=coordinator_module.__name__)
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        await _confirmed_start(coordinator, cloud, clock)
        read_at = coordinator.data[COMPRESSOR].last_success
        gaps: list[float] = []

        def check() -> None:
            if not _shown(coordinator, COMPRESSOR):
                gaps.append(clock.now)

        recovery = await _the_outage(coordinator, cloud, clock, check)
        # The value was older than three intervals plus the quick cycle: the
        # longest cycle since its last success counts, not the last one.
        assert _utc(recovery) - read_at > timedelta(seconds=(INTERVAL + 0.5) * 3)
        assert gaps == []
        assert coordinator.data[COMPRESSOR].last_success >= _utc(recovery)
        await coordinator.async_shutdown()

        # One warning for the answered batches with gateway timeouts.
        warnings = [
            record
            for record in caplog.records
            if record.name == coordinator_module.__name__
            and record.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert re.fullmatch(
            r"PointT answered a batch request, but \d+ readings came back with "
            r"server errors \(HTTP 504\)\. Further such answers of this outage "
            r"are logged at debug level",
            warnings[0].getMessage(),
        )


async def test_a_reading_still_expires_when_no_cycle_answers(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        await _confirmed_start(coordinator, cloud, clock)
        read_at = coordinator.data[COMPRESSOR].last_success
        cloud.all_status, cloud.get_status = 504, 504
        cloud.seconds = 10.56
        for _ in range(4):
            clock.now += INTERVAL
            await _refresh(coordinator)
            cloud.seconds = 0.5
        # Expired after three intervals plus the longest cycle since.
        limit = timedelta(seconds=(INTERVAL + 10.56) * 3)
        clock.now = _monotonic(read_at + limit) - 0.5
        assert _shown(coordinator, COMPRESSOR)
        clock.now += 1.0
        assert not _shown(coordinator, COMPRESSOR)
        await coordinator.async_shutdown()


def _utc(monotonic: float) -> datetime:
    return START + timedelta(seconds=monotonic - 1000.0)


def _monotonic(moment: datetime) -> float:
    return 1000.0 + (moment - START).total_seconds()


async def test_a_cycle_in_progress_extends_the_limit_up_to_one_interval(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        await _confirmed_start(coordinator, cloud, clock)
        snapshot = coordinator.data[COMPRESSOR]
        base = coordinator.validity_limit(snapshot)
        coordinator._cycle_running_since = clock.now
        clock.now += 20.0
        assert coordinator.validity_limit(snapshot) == timedelta(
            seconds=(INTERVAL + 20.0) * 3
        )
        clock.now += 3600.0
        assert coordinator.validity_limit(snapshot) == timedelta(
            seconds=INTERVAL * 2 * 3
        )
        coordinator._cycle_running_since = None
        assert coordinator.validity_limit(snapshot) == base
        await coordinator.async_shutdown()


# ------------------------------------------------------------ the fault list


async def test_fault_entities_keep_the_confirmed_state_and_read_it_again(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        await _confirmed_start(coordinator, cloud, clock)
        entities = (
            BoschBuderusActiveFaultsSensor(coordinator),
            BoschBuderusActiveNotificationsSensor(coordinator),
            BoschBuderusSystemFaultBinarySensor(coordinator),
        )
        gaps: list[float] = []

        def check() -> None:
            if not all(entity.available for entity in entities):
                gaps.append(clock.now)

        recovery = await _the_outage(coordinator, cloud, clock, check)
        assert gaps == []
        # Not paused for 15 minutes: read in the cycle that answered again,
        # after the other readings.
        assert NOTIFICATIONS not in coordinator._negative_until
        at, paths = cloud.batches[-1]
        assert at >= recovery and paths == (NOTIFICATIONS,)
        assert any(
            at >= recovery and COMPRESSOR in paths for at, paths in cloud.batches[:-1]
        )
        # During the outage nothing was sent for the fault list beside the
        # failing batch.
        assert not any(
            recovery - 2 * INTERVAL < at < recovery and NOTIFICATIONS in paths
            for at, paths in cloud.batches
        )
        assert coordinator.faults.current_state_confirmed
        assert not coordinator.faults.held
        assert not coordinator._fault_reread_due
        assert entities[0].native_value == 0
        await coordinator.async_shutdown()


async def test_the_fault_list_alone_timing_out_is_no_reason_to_pause_it(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        await _confirmed_start(coordinator, cloud, clock)
        sensor = BoschBuderusActiveFaultsSensor(coordinator)
        # Only the fault list is due; its batch and its single read time out.
        clock.now += 30.0
        _due(coordinator, clock, PollGroup.NOTIFICATIONS)
        cloud.failing = {NOTIFICATIONS: 504}
        cloud.get_status = 504
        await _refresh(coordinator)
        assert cloud.batches[-1][1] == (NOTIFICATIONS,)
        assert cloud.gets[-1][1] == NOTIFICATIONS
        assert not coordinator.last_update_success
        assert NOTIFICATIONS not in coordinator._negative_until
        assert sensor.available and coordinator.faults.held
        # The next cycle answers: the fault list is read after the others.
        cloud.failing, cloud.get_status = {}, None
        clock.now += INTERVAL
        start = clock.now
        await _refresh(coordinator)
        assert coordinator.last_update_success
        assert cloud.batches[-1] == (cloud.batches[-1][0], (NOTIFICATIONS,))
        assert cloud.batches[-1][0] > start
        assert coordinator.faults.current_state_confirmed
        await coordinator.async_shutdown()


async def test_the_fault_list_is_read_once_the_cloud_answers_again(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        await _confirmed_start(coordinator, cloud, clock)
        # A failed cycle without the fault list: its state is no longer
        # confirmed, although it was not due.
        cloud.all_status = cloud.get_status = 504
        clock.now += INTERVAL
        await _refresh(coordinator)
        assert not coordinator.faults.current_state_confirmed
        assert coordinator._fault_reread_due
        cloud.all_status = cloud.get_status = None
        clock.now += INTERVAL
        start = clock.now
        await _refresh(coordinator)
        assert coordinator.last_update_success
        assert cloud.read_in(start, NOTIFICATIONS)
        assert coordinator.faults.current_state_confirmed
        # Once: the next cycle keeps the cadence of the fault list again.
        clock.now += INTERVAL
        start = clock.now
        await _refresh(coordinator)
        assert not cloud.read_in(start, NOTIFICATIONS)
        await coordinator.async_shutdown()


async def test_a_slow_fault_list_is_still_paused_while_the_rest_answers(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        await _confirmed_start(coordinator, cloud, clock)
        cloud.failing = {NOTIFICATIONS: 504}
        cloud.get_status = 504
        # The fault list is read first and fails the cycle, as before.
        clock.now += INTERVAL
        _due(coordinator, clock, PollGroup.NOTIFICATIONS)
        await _refresh(coordinator)
        assert not coordinator.last_update_success
        assert NOTIFICATIONS not in coordinator._negative_until
        # Then after the others, which answer: now it is the slow reading.
        clock.now += INTERVAL
        await _refresh(coordinator)
        assert coordinator.last_update_success
        assert cloud.batches[-1][1] == (NOTIFICATIONS,)
        remaining = coordinator.resource_pause_remaining_seconds(NOTIFICATIONS)
        assert GATEWAY_TIMEOUT_PAUSE.total_seconds() - 5 < remaining
        assert not coordinator._fault_reread_due
        assert NOTIFICATIONS not in coordinator._failing_batch_paths
        # Paused: the next cycles read the others only and keep working.
        for _ in range(3):
            clock.now += INTERVAL
            await _refresh(coordinator)
            assert coordinator.last_update_success
            assert NOTIFICATIONS not in cloud.batches[-1][1]
        await coordinator.async_shutdown()


async def test_a_held_fault_state_ends_with_its_validity(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        await _confirmed_start(coordinator, cloud, clock)
        confirmed = clock.utc() - timedelta(seconds=cloud.seconds)
        sensor = BoschBuderusActiveFaultsSensor(coordinator)
        cloud.all_status = cloud.get_status = 504
        clock.now += INTERVAL
        await _refresh(coordinator)
        until = coordinator.faults.held_until()
        # Three intervals of the fault list (five minutes) plus a cycle.
        assert until is not None
        assert (
            timedelta(minutes=15)
            < until - confirmed
            < timedelta(minutes=15, seconds=10)
        )
        # The expiry timer of the readings also ends a held state.
        coordinator.async_add_listener(lambda: None)
        data = coordinator.data
        coordinator.data = {STATIC: data[STATIC]}
        coordinator._schedule_validity_refresh()
        assert coordinator._validity_due == until
        coordinator.data = data
        clock.now = _monotonic(until) - 1
        assert sensor.available
        clock.now += 2
        assert not sensor.available
        await coordinator.async_shutdown()


async def test_an_explicit_answer_ends_a_held_fault_state(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        await _confirmed_start(coordinator, cloud, clock)
        sensor = BoschBuderusActiveFaultsSensor(coordinator)
        cloud.all_status = cloud.get_status = 504
        clock.now += INTERVAL
        await _refresh(coordinator)
        assert sensor.available
        # The cloud answers again, but the fault list is now forbidden.
        cloud.all_status = cloud.get_status = None
        cloud.failing = {NOTIFICATIONS: 403}
        clock.now += INTERVAL
        await _refresh(coordinator)
        assert coordinator.last_update_success
        assert not coordinator.faults.held
        assert not sensor.available
        await coordinator.async_shutdown()


# ------------------------------------------- display values of an answered batch


async def test_display_values_keep_their_value_when_their_readings_fail(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=coordinator_module.__name__)
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        await _confirmed_start(coordinator, cloud, clock)
        status_description = next(
            item
            for item in build_sensor_descriptions(coordinator.resources)
            if item.unique_key == "system_pressure_status"
        )
        status = BoschBuderusSensor(coordinator, status_description)
        holiday = BoschBuderusHolidayActiveBinarySensor(coordinator)
        next_holiday = BoschBuderusNextHolidaySensor(coordinator)
        calendar = BoschBuderusHolidayCalendar(coordinator)
        shown = (status, holiday, next_holiday, calendar)
        clock.now += INTERVAL
        _due(coordinator, clock, PollGroup.CONTROL)
        await _refresh(coordinator)
        before = (status.native_value, holiday.is_on, next_holiday.native_value)
        features = calendar.supported_features
        assert features  # the plant offers holiday changes
        assert all(entity.available for entity in shown)
        assert before[0] == "normal"

        # 24 readings of the answered batches come back with server errors.
        control = coordinator._paths_by_group[PollGroup.CONTROL]
        others = [
            path for path in control if path not in (*HOLIDAYS, AWAY, NOTIFICATIONS)
        ]
        failing = (*HOLIDAYS, AWAY, PRESSURE, *others[: 24 - 5])
        cloud.failing = dict.fromkeys(failing, 503)
        cloud.get_status = 503
        clock.now += INTERVAL
        _due(coordinator, clock, PollGroup.CONTROL)
        await _refresh(coordinator)
        assert coordinator.last_update_success
        assert all(not coordinator.data[path].available for path in failing)
        assert all(entity.available for entity in shown)
        assert (status.native_value, holiday.is_on, next_holiday.native_value) == (
            before
        )
        # Changes need current readings: the calendar offers none meanwhile.
        assert calendar.supported_features == 0
        # A server error is no reason for a long pause of these readings.
        assert not any(path in coordinator._negative_until for path in failing)

        # The same outage in the next cycle: no second warning.
        clock.now += INTERVAL
        await _refresh(coordinator)
        # A cycle without such answers ends it; the next one warns again.
        cloud.failing, cloud.get_status = {}, None
        clock.now += INTERVAL
        _due(coordinator, clock, PollGroup.CONTROL)
        await _refresh(coordinator)
        assert calendar.supported_features == features
        cloud.failing = {PRESSURE: 502}
        cloud.get_status = 502
        clock.now += INTERVAL
        await _refresh(coordinator)

        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == coordinator_module.__name__
            and record.levelno == logging.WARNING
        ]
        assert messages == [
            "PointT answered a batch request, but 24 readings came back with "
            "server errors (HTTP 503). Further such answers of this outage are "
            "logged at debug level",
            "PointT answered a batch request, but 1 readings came back with "
            "server errors (HTTP 502). Further such answers of this outage are "
            "logged at debug level",
        ]
        assert any(
            record.levelno == logging.DEBUG
            and record.getMessage().startswith(
                "PointT answered a batch request again with server errors"
            )
            for record in caplog.records
        )

        # A whole failed cycle shows them as well, but offers no change.
        cloud.failing, cloud.get_status = {}, None
        clock.now += INTERVAL
        _due(coordinator, clock, PollGroup.CONTROL)
        await _refresh(coordinator)
        assert calendar.supported_features == features
        cloud.all_status = cloud.get_status = 504
        clock.now += INTERVAL
        await _refresh(coordinator)
        assert not coordinator.last_update_success
        assert all(entity.available for entity in shown)
        assert calendar.supported_features == 0
        cloud.all_status = None

        # Only within their validity: without a new answer they end.
        cloud.failing = dict.fromkeys(failing, 503)
        cloud.get_status = 503
        for _ in range(16):
            clock.now += INTERVAL
            await _refresh(coordinator)
        assert not status.available
        await coordinator.async_shutdown()
