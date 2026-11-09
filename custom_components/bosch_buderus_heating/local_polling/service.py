"""Own scheduled reads and an independent expiry timer, without HA activation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import monotonic
from typing import Protocol

from ..gateway_identity import GatewayNamespace, IdentityContext
from ..local_api.catalog import ReadRequest
from ..local_api.models import LocalObservation
from ..local_budget import LocalBudgetError, LocalReadBudget
from ..local_probe import ProbeContext, TransportFailure
from ..local_read import ReadProblem, ReadResult, ReadState
from ..local_state import LocalCondition
from ..local_transport import StaleLocalRequest
from .inventory import ResourceInventory, ResourceRecord, ResourceView
from .planner import ExplicitOperation, PollPlanner, Work
from .policy import start_offset
from .storage import (
    LocalScheduleStore,
    ScheduleCorrupt,
    ScheduleStorageError,
    checkpoint,
    restore,
    schedule_state,
)

_LOGGER = logging.getLogger(__name__)

# A recoverable problem (an unexpected error, a failed store or listener)
# pauses the reads for a bounded, growing time; they then start again.
FAULT_BACKOFF = (60.0, 120.0, 240.0, 480.0, 900.0)
# A changed schedule (search progress, confirmed or failed records) is saved
# at most this often; only its timing changed: at most every 30 minutes.
SAVE_GAP = 60.0
SAVE_INTERVAL = 1800.0
# A failed save is tried again after these delays; the reads go on meanwhile.
SAVE_RETRY = (60.0, 120.0, 300.0, 600.0, 1800.0)


def _firmware_string(observation: object) -> str | None:
    document = getattr(observation, "document", None)
    value = document.get("value") if isinstance(document, Mapping) else None
    return value if isinstance(value, str) and 0 < len(value) <= 64 else None


def _below_minimum_firmware(observation: object) -> bool:
    """A known version below the minimum; an unknown format keeps running."""
    from ..local_probe import MINIMUM_FIRMWARE, LocalProbeError, parse_firmware_version

    document = getattr(observation, "document", None)
    value = document.get("value") if isinstance(document, Mapping) else None
    try:
        return parse_firmware_version(value) < MINIMUM_FIRMWARE
    except LocalProbeError:
        return False


class ReadClient(Protocol):
    async def read(self, request: ReadRequest) -> ReadResult: ...
    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PollSnapshot:
    condition: LocalCondition
    identity_confirmed: bool
    resources: tuple[ResourceView, ...]
    discovery_cursor: int
    discovery_exhausted: bool
    discovery_pause_seconds: float
    gateway_pause_seconds: float
    pending_explicit: int
    problem: str | None


@dataclass(slots=True, eq=False)
class _Shown:
    """O13: one resource as last published, and what its view came from."""

    record: ResourceRecord
    observation: LocalObservation | None
    invalid: bool
    active: bool
    problem: str | None
    actual: float | None
    scheduled: int | None
    interval: int | None
    # When the freshness changes next while nothing else changes.
    renew: float
    view: ResourceView
    part: tuple[object, ...]

    def current(self, record: ResourceRecord, interval: int | None) -> bool:
        return (
            self.record is record
            and self.observation is record.observation
            and self.invalid == record.invalid
            and self.active == record.active
            and self.problem == record.problem
            and self.actual == record.actual_interval
            and self.scheduled == record.scheduled_period
            and self.interval == interval
        )


def _same(first: tuple[object, ...] | None, second: tuple[object, ...]) -> bool:
    return (
        first is not None
        and len(first) == len(second)
        and all(a is b for a, b in zip(first, second, strict=True))
    )


class LocalPollingService:
    """Requires an explicit current identity confirmation before sending reads."""

    def __init__(
        self,
        context: ProbeContext,
        client: ReadClient,
        budget: LocalReadBudget,
        store: LocalScheduleStore,
        *,
        is_current: Callable[[], bool],
        publish: Callable[[PollSnapshot], None],
        interval: int = 60,
        cap: Callable[[ReadRequest], int | None] | None = None,
        clock: Callable[[], float] = monotonic,
        access_key: str | None = None,
        floor: Callable[[ReadRequest], int | None] | None = None,
        period_token: Callable[[], tuple[object, ...]] | None = None,
    ) -> None:
        """``floor`` slows readings nobody shows (G3). ``period_token``
        returns objects that are replaced whenever ``cap`` or ``floor`` may
        answer differently; without it every publication asks them again."""
        budget.check_scope(context)
        if context.identity.namespace is not GatewayNamespace.K40_LOCAL:
            raise ValueError("local_source_required")
        self.context, self.client, self.budget, self.store = (
            context,
            client,
            budget,
            store,
        )
        self._current, self._publish_callback, self._clock = is_current, publish, clock
        self.inventory = ResourceInventory(context.identity, interval, cap, floor)
        self.planner = PollPlanner(
            self.inventory,
            clock(),
            offset=start_offset(
                context.identity.owner_id + context.identity.connection_id
            ),
        )
        self.planner.access_key = access_key
        self._identity_confirmed = False
        # Stops the reads until the source is replaced (sign-in, certificate,
        # configuration, identity, firmware).
        self._blocked: str | None = None
        # Pauses the reads until ``_fault_until``; then they start again.
        self._fault: str | None = None
        self._fault_until = 0.0
        self._faults = 0
        self._closed = False
        self._started = False
        self._loaded = False
        self._failures = 0
        # What the schedule file holds, without the time that passes.
        self._saved_state: tuple[object, object] | None = None
        self._last_save = clock()
        self._next_save_check = self._last_save + SAVE_GAP
        self._save_failures = 0
        self._save_warned = False
        self._wake, self._age_wake = asyncio.Event(), asyncio.Event()
        self._main: asyncio.Task[None] | None = None
        self._age: asyncio.Task[None] | None = None
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._close_task: asyncio.Task[None] | None = None
        # O13: the published state, carried forward per resource.
        self._period_token = period_token
        self._head: tuple[object, ...] | None = None
        self._shown: dict[str, _Shown] = {}
        self._shown_records: dict[str, ResourceRecord] | None = None
        self._shown_ready: bool | None = None
        self._shown_token: tuple[object, ...] | None = None

    @property
    def condition(self) -> LocalCondition:
        if self._blocked == ReadProblem.AUTH_REQUIRED:
            return LocalCondition.AUTH_REQUIRED
        if self._blocked == TransportFailure.TLS_CHANGED:
            return LocalCondition.CERTIFICATE_CHANGED
        if self._closed or self._blocked or self._fault or not self._current():
            return LocalCondition.UNAVAILABLE
        if not self._identity_confirmed:
            return LocalCondition.INITIALIZING
        if self.budget.pause_seconds:
            return LocalCondition.PAUSED
        return LocalCondition.READY

    def snapshot(self) -> PollSnapshot:
        now = self._clock()
        condition = self.condition
        return PollSnapshot(
            condition,
            self._identity_confirmed,
            tuple(
                self.inventory.view(
                    r, now, source_ready=condition is LocalCondition.READY
                )
                for r in self.inventory.records.values()
            ),
            self.planner.cursor,
            self.planner.exhausted,
            max(0.0, self.planner.discovery_pause_until - now),
            self.budget.pause_seconds,
            len(self.planner.operations),
            self._blocked
            or self._fault
            or self.planner.discovery_problem
            or self.inventory.signal_problem,
        )

    def _carry_forward(self, now: float, ready: bool) -> bool:
        """O13: work out again only the views that can have changed.

        A view is kept while its record, its freshness period and the
        readiness of the source are the same and its freshness cannot have
        changed with time. Returns whether a published part (resource,
        freshness, availability, problem, observation) or the order changed.
        """
        inventory = self.inventory
        records = inventory.records
        token = self._period_token() if self._period_token is not None else None
        same_periods = token is not None and _same(self._shown_token, token)
        self._shown_token = token
        everything = (
            records is not self._shown_records or ready is not self._shown_ready
        )
        previous = self._shown
        shown: dict[str, _Shown] = {}
        changed = len(previous) != len(records)
        replaced = False
        for target, record in records.items():
            item = previous.get(target)
            if item is None or item.record is not record:
                replaced = True
            interval = (
                item.interval
                if same_periods
                and item is not None
                and item.record is record
                and item.scheduled == record.scheduled_period
                else inventory.freshness_period(record)
            )
            if (
                not everything
                and item is not None
                and now < item.renew
                and item.current(record, interval)
            ):
                shown[target] = item
                continue
            view, renew = inventory.timed_view(
                record, now, source_ready=ready, interval=interval
            )
            part = (
                target,
                view.freshness,
                view.available,
                view.problem,
                view.observation,
            )
            if item is None or item.part != part:
                changed = True
            shown[target] = _Shown(
                record,
                record.observation,
                record.invalid,
                record.active,
                record.problem,
                record.actual_interval,
                record.scheduled_period,
                interval,
                renew,
                view,
                part,
            )
        if not changed and replaced and list(previous) != list(shown):
            changed = True
        self._shown, self._shown_records, self._shown_ready = shown, records, ready
        return changed

    def _publish(self) -> None:
        now = self._clock()
        condition = self.condition
        changed = self._carry_forward(now, condition is LocalCondition.READY)
        pause = max(0.0, self.planner.discovery_pause_until - now)
        problem = (
            self._blocked
            or self._fault
            or self.planner.discovery_problem
            or self.inventory.signal_problem
        )
        head = (
            condition,
            self._identity_confirmed,
            problem,
            self.planner.cursor,
            self.planner.exhausted,
            len(self.planner.operations),
            bool(pause),
        )
        if not changed and head == self._head:
            return
        self._head = head
        snapshot = PollSnapshot(
            condition,
            self._identity_confirmed,
            tuple(item.view for item in self._shown.values()),
            self.planner.cursor,
            self.planner.exhausted,
            pause,
            self.budget.pause_seconds,
            len(self.planner.operations),
            problem,
        )
        try:
            self._publish_callback(snapshot)
        except Exception:
            _LOGGER.debug("A local reading listener failed", exc_info=True)
            self._recover("local_listener_failed")

    def _recover(self, problem: str) -> None:
        """Pause the reads after a recoverable problem, for a bounded time.

        The pause grows with repeated problems (``FAULT_BACKOFF``) and starts
        again from its first step after a successful read.
        """
        if self._closed or self._blocked or self._fault is not None:
            return
        delay = FAULT_BACKOFF[min(self._faults, len(FAULT_BACKOFF) - 1)]
        if not self._faults:
            _LOGGER.warning(
                "Local readings paused after an unexpected problem (%s); "
                "trying again in %d s",
                problem,
                delay,
            )
        else:
            _LOGGER.debug("Local readings paused again (%s) for %d s", problem, delay)
        self._faults += 1
        self._fault, self._fault_until = problem, self._clock() + delay
        self.wake()

    def wake(self) -> None:
        self._wake.set()
        self._age_wake.set()

    def periods_changed(self) -> None:
        """G3: the floor or cap may answer differently now (for example an
        entity was enabled): a shorter period applies from now on."""
        self.inventory.periods_changed(self._clock())
        self._shown_token = None
        self.wake()

    def confirm_identity(self, context: IdentityContext) -> None:
        if (
            context != self.context.identity
            or not self._current()
            or self._closed
            or self._blocked
        ):
            raise StaleLocalRequest()
        self._identity_confirmed, self._failures = True, 0
        self.wake()

    def rescan_state(self) -> str:
        """A6: whether a requested search can start now.

        ``ready``, ``paused`` (the read budget pauses), ``running`` (the
        search of the local resources has not finished) or ``unavailable``.
        """
        condition = self.condition
        if condition is LocalCondition.PAUSED:
            return "paused"
        if condition is not LocalCondition.READY:
            return "unavailable"
        if not self.planner.exhausted:
            return "running"
        return "ready"

    def rescan(self) -> str:
        """A6: search the local resources again, in the usual search slots.

        Also asks again for resources not confirmed so far; every read stays
        within the read budget and the search pauses. ``planned`` or the
        reason it is refused (``rescan_state``).
        """
        state = self.rescan_state()
        if state != "ready":
            return state
        # At once: a second request sees the search as running.
        self.planner.restart_search(self._clock())
        self.wake()
        return "planned"

    def request(self, requests: tuple[ReadRequest, ...]) -> ExplicitOperation:
        if self._closed or not self._current() or self._blocked or self._fault:
            raise StaleLocalRequest()
        operation = self.planner.explicit(requests, self._clock())
        self.wake()
        return operation

    async def start(self) -> None:
        if self._started or self._closed:
            raise ValueError("local_poll_service_already_started")
        self._started = True
        await self._load()
        self._main = asyncio.create_task(self._run(), name="local polling")
        self._age = asyncio.create_task(self._expiry(), name="local value expiry")
        self._publish()

    async def _load(self) -> None:
        """Restore the saved schedule.

        The schedule only caches the polling progress: an unusable one is kept
        as a backup and the reads start with a new schedule. A read error is
        tried again later; until then nothing is read and nothing overwritten.
        """
        try:
            saved = await self.store.load()
            if saved is not None:
                try:
                    restore(self.planner, saved, self._clock())
                except ScheduleStorageError:
                    raise ScheduleCorrupt() from None
        except ScheduleCorrupt:
            try:
                await self.store.set_aside()
            except ScheduleStorageError:
                self._recover("local_schedule_storage_failed")
                return
            _LOGGER.warning(
                "The saved local reading schedule could not be used; it was kept "
                "as a backup and the local readings start with a new schedule"
            )
            saved = None
        except ScheduleStorageError:
            self._recover("local_schedule_storage_failed")
            return
        self._loaded = True
        self._last_save = self._clock()
        self._next_save_check = self._last_save + SAVE_GAP
        if saved is not None:
            self._saved_state = schedule_state(self.planner)

    async def _wait(self, event: asyncio.Event, delay: float) -> None:
        try:
            async with asyncio.timeout(max(0.01, delay)):
                await event.wait()
        except TimeoutError:
            pass

    async def _access_error(self, result: ReadResult) -> None:
        if result.problem in {
            ReadProblem.AUTH_REQUIRED,
            TransportFailure.TLS_CHANGED,
            "local_identity_conflict",
        }:
            self._blocked = result.problem
            self._identity_confirmed = False
            return
        if result.problem == "local_budget_storage_failed":
            self._recover(result.problem)
            return
        if result.problem == ReadProblem.RATE_LIMITED:
            delay = (
                result.retry_after_seconds
                if result.retry_after_seconds is not None
                else 60
            )
            reason = "rate_limited"
        else:
            delay = (60, 120, 300, 600)[min(self._failures, 3)]
            self._failures = min(4, self._failures + 1)
            reason = (
                "service_unavailable"
                if result.problem == ReadProblem.SERVICE_UNAVAILABLE
                else "connection"
            )
        await self.budget.pause(delay, reason)

    async def _read(self, work: Work) -> None:
        try:
            result = await self.client.read(work.request)
            if self._closed or not self._current() or self._blocked or self._fault:
                return
            if result.state is ReadState.ACCESS_ERROR:
                await self._access_error(result)
            previous = self.inventory.records.get(work.request.target)
            firmware_changed = (
                work.request.path == "/gateway/versionFirmware"
                and result.observation is not None
                and previous is not None
                and previous.observation is not None
                and previous.observation.data != result.observation.data
            )
            if (
                work.request.path == "/gateway/versionFirmware"
                and result.observation is not None
                and (version := _firmware_string(result.observation)) is not None
            ):
                # P41: also a change while Home Assistant was stopped counts.
                if (
                    not firmware_changed
                    and (previous is None or previous.observation is None)
                    and self.planner.restored_firmware is not None
                    and version != self.planner.restored_firmware
                ):
                    firmware_changed = True
                self.planner.restored_firmware = None
                self.planner.firmware = version
            if result.state is ReadState.SUCCESS:
                self._failures = 0  # the backoff restarts after a working read
            self.planner.finish(work, result, self._clock())
            if firmware_changed:
                self.planner.reconsider(self._clock())
                if _below_minimum_firmware(result.observation):
                    # Below the reviewed minimum the local API is not trusted
                    # (P22): pause with the setup's cause instead of reading on.
                    self._blocked = "firmware_update_required"

            self._publish()
            if result.state is ReadState.SUCCESS and self._fault is None:
                self._faults = 0  # the next problem pauses briefly again
        except StaleLocalRequest:
            self._identity_confirmed = False
            self._blocked = "local_configuration_changed"
        except LocalBudgetError:
            self._recover("local_budget_storage_failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.debug("A local read failed unexpectedly", exc_info=True)
            self._recover("local_poll_failed")
        finally:
            self.planner.abandon(work)
            self._workers.pop(work.request.target, None)
            self.wake()

    def _save_due(self, now: float) -> tuple[object, object] | None:
        """The schedule to save now, or None when the file is still current.

        A changed search or record result is saved within ``SAVE_GAP``; due
        times and pauses alone at most every ``SAVE_INTERVAL``. Nothing is
        written while the schedule did not change.
        """
        if not self._loaded or now < self._next_save_check:
            return None
        state = schedule_state(self.planner)
        saved = self._saved_state
        if (
            saved is None
            or state[0] != saved[0]
            or (state[1] != saved[1] and now - self._last_save >= SAVE_INTERVAL)
        ):
            return state
        self._next_save_check = now + SAVE_GAP
        return None

    async def _save(self, state: tuple[object, object] | None = None) -> None:
        """Save the schedule; a failure keeps the reads going and retries."""
        if state is None:
            state = schedule_state(self.planner)
        try:
            await self.store.save(checkpoint(self.planner, self._clock()))
        except ScheduleStorageError:
            delay = SAVE_RETRY[min(self._save_failures, len(SAVE_RETRY) - 1)]
            self._save_failures += 1
            self._next_save_check = self._clock() + delay
            if not self._save_warned:
                self._save_warned = True
                _LOGGER.warning(
                    "The local reading schedule could not be saved; the reads "
                    "go on and saving is tried again later"
                )
            return
        self._saved_state, self._last_save = state, self._clock()
        self._next_save_check = self._last_save + SAVE_GAP
        self._save_failures, self._save_warned = 0, False

    async def _step(self) -> float:
        """One pass of the scheduler; returns how long to wait at most."""
        now = self._clock()
        if self._fault is not None and now >= self._fault_until:
            # Start again with a clean state: nothing in flight, publish anew.
            self._fault, self._head = None, None
        if not self._loaded and self._fault is None:
            await self._load()
            now = self._clock()
        if (state := self._save_due(now)) is not None:
            await self._save(state)
        self.inventory.prune(now)
        if (
            self.condition is LocalCondition.READY
            and not self.budget.busy
            and self.budget.remaining_seconds <= 0
        ):
            work = self.planner.pick(now)
            if work is not None:
                self._workers[work.request.target] = asyncio.create_task(
                    self._read(work), name="local scheduled read"
                )
        if self._blocked or self._fault:
            for task in tuple(self._workers.values()):
                task.cancel()
        self._publish()
        delay = 60.0
        if self._loaded:
            delay = min(delay, max(0.01, self._next_save_check - self._clock()))
        if self._fault is not None:
            delay = min(delay, max(0.01, self._fault_until - self._clock()))
        if self.condition is LocalCondition.READY:
            delay = min(delay, max(0.01, self.planner.next_wake(now) - now))
            if self.budget.busy or self.budget.remaining_seconds > 0:
                delay = min(delay, max(0.25, self.budget.remaining_seconds))
        elif self.condition is LocalCondition.PAUSED:
            delay = min(delay, self.budget.pause_seconds)
        return delay

    async def _run(self) -> None:
        try:
            while not self._closed and self._current():
                self._wake.clear()
                try:
                    delay = await self._step()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _LOGGER.debug("Local polling failed unexpectedly", exc_info=True)
                    self._recover("local_poll_failed")
                    delay = (
                        max(0.01, self._fault_until - self._clock())
                        if self._fault is not None
                        else 60.0
                    )
                await self._wait(self._wake, delay)
        finally:
            if not self._current():
                self._identity_confirmed = False
            for task in tuple(self._workers.values()):
                task.cancel()
            self._publish()
            self._age_wake.set()

    async def _expiry(self) -> None:
        while not self._closed and self._current():
            self._age_wake.clear()
            now = self._clock()
            try:
                self.planner.expire_operations(now)
                self._publish()
                deadlines = [op.deadline for op in self.planner.operations]
                expiry = self.inventory.next_expiry(now)
                if expiry is not None:
                    deadlines.append(expiry)
                delay = min((d - now for d in deadlines), default=60.0)
            except Exception:
                _LOGGER.debug("Local value expiry failed unexpectedly", exc_info=True)
                self._recover("local_poll_failed")
                delay = 60.0
            await self._wait(self._age_wake, delay)
            self._wake.set()

    async def _close(self) -> None:
        self._closed = True
        self.wake()
        tasks = tuple(
            t for t in (self._main, self._age, *self._workers.values()) if t is not None
        )
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for operation in tuple(self.planner.operations):
            self.planner.cancel(operation)
        await self.client.close()
        if self._loaded:
            await self._save()  # also unchanged: the remaining times are current
        self._publish()

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close(), name="close local polling"
            )
        await asyncio.shield(self._close_task)
