"""Bounded resource observations and independent monotonic freshness."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from ..gateway_identity import IdentityContext
from ..local_api.catalog import ReadRequest, make_request, response_schema
from ..local_api.models import JsonValue, LocalObservation, ReferenceList
from ..local_probe import TransportFailure
from ..local_read import ReadResult, ReadState
from .policy import (
    MAX_RECORDS,
    MAX_RETAINED_BYTES,
    MAX_RETAINED_NODES,
    MAX_SIGNALS,
    PollClass,
    category,
    period,
    validate_interval,
)

NOT_FOUND_STATUSES = frozenset({403, 404, 406})
NOT_FOUND_RETRY = 7 * 86400
# O13: a view is worked out again this long before its freshness changes.
RENEW_MARGIN = 0.001


class Freshness(StrEnum):
    UNCONFIRMED = "unconfirmed"
    FRESH = "fresh"
    LATE = "late"
    EXPIRED = "expired"


@dataclass(slots=True)
class ResourceRecord:
    request: ReadRequest = field(repr=False)
    due: float
    created: float
    confirmed: bool = False
    observation: LocalObservation | None = field(default=None, repr=False)
    invalid: bool = False
    active: bool = True
    failures: int = 0
    signal_misses: int = 0
    last_attempt: float | None = None
    previous_success: float | None = None
    actual_interval: float | None = None
    problem: str | None = None
    retained_bytes: int = 0
    retained_nodes: int = 0
    # When the gateway stopped listing this signal (P41); None while listed.
    inactive_since: float | None = None
    # Period scheduled at the last read: a later, shorter cap must not expire a
    # reading before its next read is even due.
    scheduled_period: int | None = None


@dataclass(frozen=True, slots=True)
class ResourceView:
    request: ReadRequest = field(repr=False)
    freshness: Freshness
    available: bool
    age_seconds: float | None
    target_interval_seconds: int | None
    actual_interval_seconds: float | None
    problem: str | None
    observation: LocalObservation | None = field(repr=False)


def represented_size(value: JsonValue) -> tuple[int, int]:
    """Account for represented UTF-8 JSON, not exact Python heap size."""
    if isinstance(value, Mapping):
        costs = [
            (len(json.dumps(k, ensure_ascii=False).encode()) + 1, 0) for k in value
        ]
        costs.extend(represented_size(v) for v in value.values())
        return 2 + max(0, len(value) - 1) + sum(c[0] for c in costs), 1 + sum(
            c[1] for c in costs
        )
    if isinstance(value, tuple):
        costs = [represented_size(v) for v in value]
        return 2 + max(0, len(value) - 1) + sum(c[0] for c in costs), 1 + sum(
            c[1] for c in costs
        )
    encoded = (
        str(value)
        if isinstance(value, Decimal)
        else json.dumps(value, ensure_ascii=False)
    )
    return len(encoded.encode()), 1


class ResourceInventory:
    def __init__(
        self,
        context: IdentityContext,
        interval: int = 60,
        cap: Callable[[ReadRequest], int | None] | None = None,
        floor: Callable[[ReadRequest], int | None] | None = None,
    ) -> None:
        self.context = context
        self.interval = validate_interval(interval)
        self._cap = cap
        self._floor = floor
        self.records: dict[str, ResourceRecord] = {}
        self.signal_ids: frozenset[str] = frozenset()
        self.signal_problem: str | None = None
        self.retained_bytes = 0
        self.retained_nodes = 0

    def add(self, request: ReadRequest, now: float) -> ResourceRecord:
        response_schema(request)
        if (
            category(request) in {PollClass.HISTORY, PollClass.ADMINISTRATION}
            or request.query
        ):
            raise ValueError("local_periodic_request_required")
        if (
            request.definition.template == "/signals/{signalId}"
            and request.path.removeprefix("/signals/") not in self.signal_ids
        ):
            raise ValueError("unconfirmed_local_signal")
        if (record := self.records.get(request.target)) is not None:
            return record
        if len(self.records) >= MAX_RECORDS:
            raise ValueError("local_resource_limit")
        record = ResourceRecord(request, now, now)
        self.records[request.target] = record
        return record

    def period(self, request: ReadRequest) -> int | None:
        """Effective period, including the cap of a paired cloud reading.

        ``floor`` slows a reading nobody shows (G3): never below it, and it
        never makes a reading more frequent.
        """
        value = period(
            request, self.interval, self._cap(request) if self._cap else None
        )
        if value is None or self._floor is None:
            return value
        slowest = self._floor(request)
        return value if slowest is None else max(value, slowest)

    def periods_changed(self, now: float) -> bool:
        """A shorter period applies at once, not only after the longer wait.

        A reading scheduled with a longer period (for example while its
        entities were disabled) is due when its new period has passed since
        the last attempt. Returns whether a due time moved.
        """
        moved = False
        for record in self.records.values():
            if (
                not record.confirmed
                or record.last_attempt is None
                or record.scheduled_period is None
            ):
                continue
            interval = self.period(record.request)
            if interval is None or interval >= record.scheduled_period:
                continue
            due = max(now, record.last_attempt + interval)
            if due < record.due:
                record.due, moved = due, True
        return moved

    def freshness_period(self, record: ResourceRecord) -> int | None:
        """Period for ageing: never shorter than the one scheduled at the read."""
        current = self.period(record.request)
        if current is None or record.scheduled_period is None:
            return current
        return max(current, record.scheduled_period)

    def _drop_observation(self, record: ResourceRecord) -> None:
        self.retained_bytes -= record.retained_bytes
        self.retained_nodes -= record.retained_nodes
        record.retained_bytes = record.retained_nodes = 0
        record.observation = None

    def accept(
        self, result: ReadResult, now: float, *, started: float | None = None
    ) -> bool:
        """Record a read that ended at ``now``.

        T3: the next read is due one period after the read *started*, so the
        time a read takes does not add to every interval. A deferred read
        changes nothing (it is due again at once); never earlier than ``now``,
        so one read is never repeated at once.
        """
        record = self.records[result.request.target]
        interval = self.period(record.request)
        assert interval is not None
        if result.state is ReadState.DEFERRED:
            return False
        began = now if started is None else min(started, now)
        record.last_attempt = began
        record.due = max(now, began + interval)
        record.scheduled_period = interval
        if result.state is ReadState.ACCESS_ERROR:
            return False  # Source health and the shared pause have their own owner.
        if result.state is ReadState.SUCCESS:
            observation = result.observation
            if (
                observation is None
                or observation.context != self.context
                or observation.request != record.request
                or not math.isfinite(observation.observed_monotonic)
                or observation.observed_monotonic > now
            ):
                raise ValueError("invalid_local_poll_observation")
            if (
                record.observation is not None
                and observation.observed_monotonic
                < record.observation.observed_monotonic
            ):
                return False
            size, nodes = represented_size(observation.document)
            if (
                self.retained_bytes - record.retained_bytes + size > MAX_RETAINED_BYTES
                or self.retained_nodes - record.retained_nodes + nodes
                > MAX_RETAINED_NODES
            ):
                record.invalid, record.problem = True, "local_memory_limit"
                return False
            if observation.request.path == "/signals" and not self._signals(
                observation, now
            ):
                record.invalid, record.problem = True, self.signal_problem
                return False
            prior = record.previous_success
            record.actual_interval = (
                observation.observed_monotonic - prior if prior is not None else None
            )
            record.previous_success = observation.observed_monotonic
            self._drop_observation(record)
            self.retained_bytes += size
            self.retained_nodes += nodes
            record.retained_bytes, record.retained_nodes = size, nodes
            record.observation = observation
            record.confirmed, record.invalid, record.failures = True, False, 0
            record.problem = None
            return True
        record.problem = result.problem
        timed_out = result.problem == TransportFailure.TIMEOUT
        if record.confirmed:
            record.invalid = not timed_out
        else:
            schedule = (
                (300, 900, 3600, 86400)
                if timed_out
                # O42: after the third "not found/denied" only weekly; a
                # firmware or access change and a new search ask at once.
                else (
                    (3600, 21600, NOT_FOUND_RETRY)
                    if result.status in NOT_FOUND_STATUSES
                    else (900, 3600, 86400)
                )
            )
            record.due = now + schedule[min(record.failures, len(schedule) - 1)]
            record.failures = min(record.failures + 1, 4)
        return False

    def _signals(self, observation: LocalObservation, now: float) -> bool:
        data = observation.data
        if not isinstance(data, ReferenceList) or len(data.references) != len(
            data.signal_ids
        ):
            self.signal_problem = "invalid_local_signal_list"
            return False
        ids = data.signal_ids
        known = self.signal_ids or any(
            record.request.definition.template == "/signals/{signalId}"
            for record in self.records.values()
        )
        if not ids and known:
            # An empty list after known signals is no answer, not removal (P41).
            self.signal_problem = "empty_local_signal_list"
            return False
        if len(ids) > MAX_SIGNALS:
            self.signal_problem = "local_signal_limit"
            return False
        requests = [
            make_request(
                "/signals/{signalId}", parameters={"signalId": name}, signal_ids=ids
            )
            for name in sorted(ids)
        ]
        if (
            len(self.records) + sum(r.target not in self.records for r in requests)
            > MAX_RECORDS
        ):
            self.signal_problem = "local_resource_limit"
            return False
        self.signal_ids = ids
        self.signal_problem = None
        for record in self.records.values():
            if record.request.definition.template != "/signals/{signalId}":
                continue
            if record.request.path.removeprefix("/signals/") not in ids:
                record.signal_misses = min(2, record.signal_misses + 1)
                if record.signal_misses == 2:
                    record.active = False
                    if record.inactive_since is None:
                        record.inactive_since = now
            else:
                if not record.active:
                    record.due, record.invalid = now, True
                record.active, record.signal_misses = True, 0
                record.inactive_since = None
        for request in requests:
            self.add(request, now)
        return True

    def view(
        self, record: ResourceRecord, now: float, *, source_ready: bool
    ) -> ResourceView:
        return self.timed_view(
            record,
            now,
            source_ready=source_ready,
            interval=self.freshness_period(record),
        )[0]

    def timed_view(
        self,
        record: ResourceRecord,
        now: float,
        *,
        source_ready: bool,
        interval: int | None,
    ) -> tuple[ResourceView, float]:
        """The view with ``interval`` as its freshness period, and the time
        its freshness changes next while nothing else changes (O13)."""
        observation = record.observation
        age = max(0.0, now - observation.observed_monotonic) if observation else None
        freshness = Freshness.UNCONFIRMED
        renew = math.inf
        if (
            age is not None
            and observation is not None
            and interval is not None
            and category(record.request) is not PollClass.DISCOVERY
        ):
            freshness = (
                Freshness.EXPIRED
                if age >= 3 * interval
                else Freshness.LATE
                if age >= interval
                else Freshness.FRESH
            )
            # A little early: rounding never keeps an outdated freshness.
            if freshness is Freshness.FRESH:
                renew = observation.observed_monotonic + interval - RENEW_MARGIN
            elif freshness is Freshness.LATE:
                renew = observation.observed_monotonic + 3 * interval - RENEW_MARGIN
        available = (
            source_ready
            and record.active
            and not record.invalid
            and freshness in {Freshness.FRESH, Freshness.LATE}
        )
        return ResourceView(
            record.request,
            freshness,
            available,
            age,
            interval,
            record.actual_interval,
            record.problem,
            observation,
        ), renew

    def next_expiry(self, now: float) -> float | None:
        deadlines = []
        for record in self.records.values():
            interval = self.freshness_period(record)
            if (
                record.observation is None
                or interval is None
                or category(record.request) is PollClass.DISCOVERY
            ):
                continue
            for multiplier in (1, 3):
                deadline = record.observation.observed_monotonic + multiplier * interval
                if deadline > now:
                    deadlines.append(deadline)
        return min(deadlines, default=None)

    def prune(self, now: float) -> None:
        for key, record in tuple(self.records.items()):
            if (
                record.request.definition.template == "/signals/{signalId}"
                and record.signal_misses == 2
                and not record.active
                # Confirmed signals the gateway stopped listing are removed a
                # day after that, so changing IDs cannot fill the record limit.
                and (
                    now - record.inactive_since >= 86400
                    if record.confirmed and record.inactive_since is not None
                    else not record.confirmed and now - record.created >= 86400
                )
            ):
                self._drop_observation(record)
                del self.records[key]
