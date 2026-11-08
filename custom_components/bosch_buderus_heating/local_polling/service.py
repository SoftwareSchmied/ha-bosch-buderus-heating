"""Own scheduled reads and an independent expiry timer, without HA activation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import monotonic
from typing import Protocol

from ..gateway_identity import GatewayNamespace, IdentityContext
from ..local_api.catalog import ReadRequest
from ..local_budget import LocalBudgetError, LocalReadBudget
from ..local_probe import ProbeContext, TransportFailure
from ..local_read import ReadProblem, ReadResult, ReadState
from ..local_state import LocalCondition
from ..local_transport import StaleLocalRequest
from .inventory import ResourceInventory, ResourceView
from .planner import ExplicitOperation, PollPlanner, Work
from .policy import start_offset
from .storage import LocalScheduleStore, ScheduleStorageError, checkpoint, restore


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
    ) -> None:
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
        self.inventory = ResourceInventory(context.identity, interval, cap)
        self.planner = PollPlanner(
            self.inventory,
            clock(),
            offset=start_offset(
                context.identity.owner_id + context.identity.connection_id
            ),
        )
        self.planner.access_key = access_key
        self._identity_confirmed = False
        self._blocked: str | None = None
        self._closed = False
        self._started = False
        self._loaded = False
        self._storage_ok = True
        self._failures = 0
        self._wake, self._age_wake = asyncio.Event(), asyncio.Event()
        self._main: asyncio.Task[None] | None = None
        self._age: asyncio.Task[None] | None = None
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._close_task: asyncio.Task[None] | None = None
        self._signature: object = None

    @property
    def condition(self) -> LocalCondition:
        if self._blocked == ReadProblem.AUTH_REQUIRED:
            return LocalCondition.AUTH_REQUIRED
        if self._blocked == TransportFailure.TLS_CHANGED:
            return LocalCondition.CERTIFICATE_CHANGED
        if self._closed or self._blocked or not self._current():
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
            or self.planner.discovery_problem
            or self.inventory.signal_problem,
        )

    def _publish(self) -> None:
        snapshot = self.snapshot()
        signature = (
            snapshot.condition,
            snapshot.identity_confirmed,
            snapshot.problem,
            snapshot.discovery_cursor,
            snapshot.discovery_exhausted,
            snapshot.pending_explicit,
            bool(snapshot.discovery_pause_seconds),
            tuple(
                (v.request.target, v.freshness, v.available, v.problem, v.observation)
                for v in snapshot.resources
            ),
        )
        if signature == self._signature:
            return
        self._signature = signature
        try:
            self._publish_callback(snapshot)
        except Exception:
            self._blocked = "local_listener_failed"
            self.wake()

    def wake(self) -> None:
        self._wake.set()
        self._age_wake.set()

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
        if self._closed or not self._current() or self._blocked:
            raise StaleLocalRequest()
        operation = self.planner.explicit(requests, self._clock())
        self.wake()
        return operation

    async def start(self) -> None:
        if self._started or self._closed:
            raise ValueError("local_poll_service_already_started")
        self._started = True
        try:
            saved = await self.store.load()
            if saved is not None:
                restore(self.planner, saved, self._clock())
            self._loaded = True
        except ScheduleStorageError:
            self._storage_ok = False
            self._blocked = "local_schedule_storage_failed"
            self._publish()
            return
        self._main = asyncio.create_task(self._run(), name="local polling")
        self._age = asyncio.create_task(self._expiry(), name="local value expiry")
        self._publish()

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
            "local_budget_storage_failed",
            "local_identity_conflict",
        }:
            self._blocked = result.problem
            self._identity_confirmed = False
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
            if self._closed or not self._current() or self._blocked:
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
        except StaleLocalRequest:
            self._identity_confirmed = False
            self._blocked = "local_configuration_changed"
        except LocalBudgetError:
            self._blocked = "local_budget_storage_failed"
        except asyncio.CancelledError:
            raise
        except Exception:
            self._blocked = "local_poll_failed"
        finally:
            self.planner.abandon(work)
            self._workers.pop(work.request.target, None)
            self.wake()

    async def _save(self) -> None:
        try:
            await self.store.save(checkpoint(self.planner, self._clock()))
        except ScheduleStorageError:
            self._storage_ok = False
            self._blocked = "local_schedule_storage_failed"

    async def _run(self) -> None:
        next_save = self._clock() + 60
        try:
            while not self._closed and self._current():
                self._wake.clear()
                now = self._clock()
                if now >= next_save and self._storage_ok:
                    await self._save()
                    next_save = self._clock() + 60
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
                if self._blocked:
                    for task in tuple(self._workers.values()):
                        task.cancel()
                self._publish()
                delay = (
                    min(60.0, max(0.01, next_save - self._clock()))
                    if self._storage_ok
                    else 60.0
                )
                if self.condition is LocalCondition.READY:
                    delay = min(delay, max(0.01, self.planner.next_wake(now) - now))
                    if self.budget.busy or self.budget.remaining_seconds > 0:
                        delay = min(delay, max(0.25, self.budget.remaining_seconds))
                elif self.condition is LocalCondition.PAUSED:
                    delay = min(delay, self.budget.pause_seconds)
                await self._wait(self._wake, delay)
        except Exception:
            self._blocked = "local_poll_failed"
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
            self.planner.expire_operations(now)
            self._publish()
            deadlines = [op.deadline for op in self.planner.operations]
            expiry = self.inventory.next_expiry(now)
            if expiry is not None:
                deadlines.append(expiry)
            await self._wait(
                self._age_wake, min((d - now for d in deadlines), default=60.0)
            )
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
        if self._loaded and self._storage_ok:
            await self._save()
        self._publish()

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close(), name="close local polling"
            )
        await asyncio.shield(self._close_task)
