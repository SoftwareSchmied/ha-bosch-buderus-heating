"""Fair, bounded scheduling with resumable discovery and shared explicit work."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from ..local_api.catalog import ReadRequest, response_schema
from ..local_budget import MINIMUM_START_GAP
from ..local_read import ReadResult, ReadState
from .inventory import ResourceInventory
from .policy import (
    EXPLICIT_LIMIT,
    EXPLICIT_TIMEOUT,
    MAX_RECORDS,
    RESCAN_SECONDS,
    SECTION_RESERVE,
    SLICE_ATTEMPTS,
    SLICE_PAUSE,
    SLICE_SECONDS,
    PollClass,
    candidates,
    category,
    validate_explicit,
)


class Lane(StrEnum):
    REGULAR = "regular"
    DISCOVERY = "discovery"
    EXPLICIT = "explicit"


WHEEL = (Lane.REGULAR,) * 4 + (Lane.DISCOVERY,) + (Lane.REGULAR,) * 4 + (Lane.EXPLICIT,)


@dataclass(eq=False, slots=True)
class ExplicitOperation:
    requests: tuple[ReadRequest, ...] = field(repr=False)
    deadline: float
    results: dict[str, ReadResult] = field(default_factory=dict, repr=False)
    done: bool = False
    problem: str | None = None
    received_bytes: int = 0


@dataclass(frozen=True, slots=True)
class Work:
    request: ReadRequest = field(repr=False)
    lane: Lane
    sequence: int
    # T3: when the read was started; its next due time counts from here.
    started: float | None = None


class PollPlanner:
    def __init__(
        self, inventory: ResourceInventory, now: float, *, offset: float = 0
    ) -> None:
        self.inventory = inventory
        self.epoch = now + offset
        self.next_start = self.epoch
        # T1: the spacing between two starts; the service sets the budget's
        # current spacing before each pick.
        self.start_gap = MINIMUM_START_GAP
        self.wheel = 0
        self.sequence = 0
        self.cursor = 0
        self.exhausted = False
        self.rescan_at = 0.0
        # P41: what the stored negatives were learned with; a change of either
        # makes the not-yet-confirmed paths worth asking again.
        self.access_key: str | None = None
        self.firmware: str | None = None
        self.restored_firmware: str | None = None
        self.slice_started: float | None = None
        self.slice_attempts = 0
        self.discovery_pause_until = 0.0
        self.inflight: dict[str, Work] = {}
        self.operations: list[ExplicitOperation] = []
        self._candidates = candidates()
        self.discovery_problem: str | None = None
        self.attempts = {lane: 0 for lane in Lane}

    def hints(self, requests: Iterable[ReadRequest], now: float) -> None:
        """Hints are validated candidates, never a successful local observation."""
        for index, request in enumerate(requests):
            if index >= MAX_RECORDS:
                raise ValueError("local_hint_limit")
            self.inventory.add(request, now)

    def explicit(
        self, requests: tuple[ReadRequest, ...], now: float
    ) -> ExplicitOperation:
        validate_explicit(requests)
        for request in requests:
            response_schema(request)
            if (
                request.definition.template == "/signals/{signalId}"
                and request.path.removeprefix("/signals/")
                not in self.inventory.signal_ids
            ):
                raise ValueError("unconfirmed_local_signal")
        self.expire_operations(now)
        for operation in self.operations:
            if operation.requests == requests:
                return operation
        pending = {
            r.target
            for op in self.operations
            for r in op.requests
            if r.target not in op.results
        }
        if (
            len(self.operations) >= EXPLICIT_LIMIT
            or len(pending | {r.target for r in requests}) > EXPLICIT_LIMIT
        ):
            raise ValueError("local_explicit_queue_full")
        for request in requests:
            if category(request) not in {PollClass.HISTORY, PollClass.ADMINISTRATION}:
                self.inventory.add(request, now)
        operation = ExplicitOperation(requests, now + EXPLICIT_TIMEOUT)
        self.operations.append(operation)
        return operation

    def cancel(self, operation: ExplicitOperation) -> None:
        if operation in self.operations:
            operation.done, operation.problem = True, "cancelled"
            self.operations.remove(operation)

    def reconsider(self, now: float, templates: frozenset[str] | None = None) -> None:
        """A firmware/catalog hint rechecks negatives within ordinary search slots."""
        index = 0
        for record in self.inventory.records.values():
            if not record.confirmed and (
                templates is None or record.request.definition.template in templates
            ):
                record.due = min(record.due, now + index * 0.25)
                record.failures = 0
                index += 1

    def expire_operations(self, now: float) -> None:
        for operation in tuple(self.operations):
            if now >= operation.deadline:
                operation.done, operation.problem = True, "deadline"
                self.operations.remove(operation)

    def _regular(self, now: float) -> ReadRequest | None:
        due = [
            r
            for r in self.inventory.records.values()
            if r.confirmed
            and r.active
            and r.due <= now
            and r.request.target not in self.inflight
        ]
        return (
            min(due, key=lambda r: (r.due, r.created, r.request.target)).request
            if due
            else None
        )

    def _explicit(self, now: float) -> ReadRequest | None:
        for operation in self.operations:
            if operation.deadline - now <= 5:
                continue
            for request in operation.requests:
                if (
                    request.target not in operation.results
                    and request.target not in self.inflight
                ):
                    return request
        return None

    def restart_search(self, now: float) -> None:
        """A6: search all candidates again from the start, in the usual
        search slots, and ask again for resources not confirmed so far."""
        self._candidates = candidates()
        self.cursor, self.exhausted = 0, False
        self.reconsider(now)

    def _advance_slice(self, now: float) -> None:
        if self.slice_started is not None and (
            self.slice_attempts >= SLICE_ATTEMPTS
            or now - self.slice_started >= SLICE_SECONDS
        ):
            ended = (
                now
                if self.slice_attempts >= SLICE_ATTEMPTS
                else self.slice_started + SLICE_SECONDS
            )
            self.discovery_pause_until = max(
                self.discovery_pause_until, ended + SLICE_PAUSE
            )
            self.slice_attempts, self.slice_started = 0, None
        if self.exhausted and now >= self.rescan_at:
            self._candidates = candidates()
            self.cursor, self.exhausted = 0, False

    def _discovery(self, now: float) -> ReadRequest | None:
        self._advance_slice(now)
        reserved = sum(w.lane is Lane.DISCOVERY for w in self.inflight.values())
        if (
            now < self.discovery_pause_until
            or self.slice_attempts + reserved >= SLICE_ATTEMPTS
        ):
            return None
        due = [
            r
            for r in self.inventory.records.values()
            if not r.confirmed
            and r.active
            and r.due <= now
            and r.request.target not in self.inflight
        ]
        if due:
            return min(due, key=lambda r: (r.due, r.created, r.request.target)).request
        if self.exhausted:
            return None
        if len(self.inventory.records) >= MAX_RECORDS:
            self.discovery_problem = "local_resource_limit"
            # Treat the search as finished until the next rescan; otherwise the
            # service would wake four times a second at the limit (P41).
            self.exhausted, self.rescan_at = True, now + RESCAN_SECONDS
            return None
        # Bound synchronous iteration even when every candidate is already known.
        for _ in range(128):
            request = next(self._candidates, None)
            if request is None:
                self.exhausted, self.rescan_at = True, now + RESCAN_SECONDS
                return None
            self.cursor += 1
            if request.target not in self.inventory.records:
                self.inventory.add(request, now)
                self.discovery_problem = None
                return request
        return None

    def pick(self, now: float) -> Work | None:
        self.expire_operations(now)
        if now < self.next_start or now < self.epoch or len(self.inflight) >= 2:
            return None
        if (now - self.epoch) % 60 >= 60 - SECTION_RESERVE:
            return None
        lane = WHEEL[self.wheel]
        order = {
            Lane.REGULAR: (Lane.REGULAR, Lane.DISCOVERY, Lane.EXPLICIT),
            Lane.DISCOVERY: (Lane.DISCOVERY, Lane.EXPLICIT, Lane.REGULAR),
            Lane.EXPLICIT: (Lane.EXPLICIT, Lane.DISCOVERY, Lane.REGULAR),
        }[lane]
        for selected in order:
            request = {
                Lane.REGULAR: self._regular,
                Lane.DISCOVERY: self._discovery,
                Lane.EXPLICIT: self._explicit,
            }[selected](now)
            if request is not None:
                self.sequence += 1
                work = Work(request, selected, self.sequence, now)
                self.inflight[request.target] = work
                self.wheel = (self.wheel + 1) % len(WHEEL)
                self.next_start = now + self.start_gap
                if selected is Lane.DISCOVERY and self.slice_started is None:
                    self.slice_started = now
                return work
        return None

    def finish(self, work: Work, result: ReadResult, now: float) -> None:
        if self.inflight.get(work.request.target) is not work:
            return
        del self.inflight[work.request.target]
        if result.request != work.request:
            raise ValueError("local_poll_result_mismatch")
        if result.state is ReadState.DEFERRED:
            self.next_start = max(
                self.next_start, now + (result.retry_after_seconds or self.start_gap)
            )
            return
        self.attempts[work.lane] += 1
        if work.lane is Lane.DISCOVERY:
            self.slice_attempts += 1
        if work.request.target in self.inventory.records:
            self.inventory.accept(result, now, started=work.started)
        self.expire_operations(now)
        for operation in tuple(self.operations):
            if (
                work.request not in operation.requests
                or work.request.target in operation.results
            ):
                continue
            size = result.response_bytes
            if operation.received_bytes + size > 1024 * 1024:
                operation.done, operation.problem = True, "local_explicit_size_limit"
            else:
                operation.received_bytes += size
                operation.results[work.request.target] = result
                operation.done = (
                    len(operation.results) == len(operation.requests)
                    or result.state is ReadState.ACCESS_ERROR
                )
                if result.state is ReadState.ACCESS_ERROR:
                    operation.problem = "access_error"
            if operation.done:
                self.operations.remove(operation)
        self._advance_slice(now)

    def abandon(self, work: Work) -> None:
        if self.inflight.get(work.request.target) is work:
            del self.inflight[work.request.target]

    def next_wake(self, now: float) -> float:
        if now < self.epoch:
            return self.epoch
        phase = (now - self.epoch) % 60
        if phase >= 60 - SECTION_RESERVE:
            return now + 60 - phase
        if now < self.next_start:
            return self.next_start
        # T1: regular or explicit work that is already due starts as soon as
        # the shared budget admits it, not only at the next future due time.
        room = len(self.inflight) < 2
        if room and self._explicit(now) is not None:
            return now
        # Unexamined candidates and skipped known candidates need bounded progress.
        searching = not self.exhausted and now >= self.discovery_pause_until
        due: list[float] = []
        for r in self.inventory.records.values():
            if not r.active or r.request.target in self.inflight:
                continue
            if r.confirmed:
                if room and r.due <= now:
                    return now
                due.append(r.due)
            elif now >= self.discovery_pause_until:
                due.append(r.due)
        if searching:
            return now + self.start_gap
        due += [self.discovery_pause_until, self.rescan_at]
        due += [op.deadline for op in self.operations]
        return min((value for value in due if value > now), default=now + 60)
