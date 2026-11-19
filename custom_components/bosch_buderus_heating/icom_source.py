"""Run one verified local iCom gateway as a source of readings.

The source reads the paired readings (icom_plan) from the gateway one after
the other and publishes them as a local source state, so the existing source
choice (local_selection) treats them like any other local readings. Writing
stays with the cloud. The gateway is slow and may hang for good after days of
requests, so the access is careful:

* one request at a time, with a gap after each (icom_client);
* each reading has the period of the K40 polling policy, at least the
  configured local interval and never below 120 s;
* a path the gateway does not offer (404, 403) is asked again after a week;
* two failed requests in a row (timeout, no connection, server error) pause
  the whole access; each further pause in a row is longer (1, 2, 5, 10, 20,
  30 minutes) until a reading works again; an overload answer (429, 503)
  pauses for at least its Retry-After;
* a wrong password (answers that cannot be decrypted) stops the access for
  good until the passwords are entered again;
* the gateway ID read locally must stay the cloud's gateway ID; it is checked
  at the start, hourly and after every pause. Another gateway stops the access.

Nothing in the diagnostics names a password, a key, an address or an ID.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Protocol, cast

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.util import dt as dt_util

from .configuration import (
    LOCAL_KIND,
    ConfigurationError,
    StoredBinding,
    StoredConnection,
)
from .connection_runtime import ConnectionRuntime, SourceContext
from .gateway_identity import GatewayNamespace
from .icom_access import (
    IcomAccess,
    IcomAccessError,
    gateway_ids_match,
    is_icom_connection,
    load_icom_access,
)
from .icom_client import PATH_PROBLEMS, IcomClient, IcomError, IcomProblem
from .icom_plan import DIRECTORIES, UUID_PATH, circuit_ids, readings
from .local_api.catalog import ReadRequest
from .local_api.decoder import decode_resource
from .local_api.errors import LocalResourceError
from .local_api.models import LocalObservation
from .local_polling.inventory import (
    NOT_FOUND_RETRY,
    Freshness,
    ResourceView,
    late_after,
)
from .local_polling.policy import period
from .local_state import LocalCondition, LocalSourceState
from .log_helpers import log_failure

_LOGGER = logging.getLogger(__name__)

# The shortest period of any reading, whatever the local interval says.
MIN_INTERVAL = 120
DEFAULT_INTERVAL = 120
# A reading expires after this many periods without a new answer.
EXPIRY_PERIODS = 3
# Reads per round before the state is published and the next round starts.
ROUND_LIMIT = 25
# The longest wait for due work; shorter than the pauses so a closed or
# changed source is noticed.
IDLE_MAX = 30.0
# Pauses of the whole access in a row, in seconds; the last one repeats.
PAUSES = (60.0, 120.0, 300.0, 600.0, 1200.0, 1800.0)
# Failed requests in a row (nothing usable came back) that pause the access.
FAILURES_BEFORE_PAUSE = 2
# Answers that cannot be decrypted in a row that stop the access for good.
# The first check of an access never confirmed stops at once.
UNDECRYPTABLE_BEFORE_STOP = 2
IDENTITY_PERIOD = 3600.0
DISCOVERY_PERIOD = 86400.0
DISCOVERY_RETRY = 900.0
# A reading that failed once is asked again after this long, at most.
RETRY_SECONDS = 60.0
# Condition changes kept for the diagnostics.
CONDITION_CHANGES = 20
_DOWN = frozenset({LocalCondition.PAUSED, LocalCondition.UNAVAILABLE})
_TRANSPORT = frozenset(
    {
        IcomProblem.CONNECT,
        IcomProblem.TIMEOUT,
        IcomProblem.HTTP_ERROR,
        IcomProblem.INVALID_RESPONSE,
    }
)


class _Factory(Protocol):
    hass: HomeAssistant
    entry: ConfigEntry[Any]
    runtime: Callable[[], ConnectionRuntime]


class IcomConnection:
    """The local connection of an iCom gateway (the counterpart of the K40's)."""

    def __init__(
        self, factory: _Factory, identifier: str, record: StoredConnection
    ) -> None:
        self.factory, self.identifier, self.record = factory, identifier, record
        self.sources: dict[str, IcomSource] = {}
        self.closed = False

    async def connect(self) -> None:
        if (
            self.closed
            or self.record.kind != LOCAL_KIND
            or not is_icom_connection(self.record.to_data())
        ):
            raise ConfigurationError("unsupported_connection")

    def source(self, binding_id: str, binding: StoredBinding) -> IcomSource:
        if (
            self.closed
            or binding.connection_id != self.identifier
            or binding.adapter != LOCAL_KIND
        ):
            raise ConfigurationError("local_configuration_changed")
        source = IcomSource(self, binding_id, binding)
        self.sources[binding_id] = source
        return source

    async def close(self) -> None:
        self.closed = True
        await asyncio.gather(*(s.close() for s in tuple(self.sources.values())))


@dataclass(slots=True)
class _Record:
    request: ReadRequest
    due: float
    observation: LocalObservation | None = None
    attempted: bool = False
    last_attempt: float | None = None
    previous_success: float | None = None
    actual_interval: float | None = None
    scheduled_period: int | None = None
    problem: str | None = None
    # The last failed request that gave nothing usable (a fixed code); it does
    # not withdraw a reading before its expiry.
    last_failure: str | None = None
    failures: int = 0
    # The gateway does not offer this path (asked again after a week).
    absent: bool = False


@dataclass(frozen=True, slots=True)
class _Verdict:
    """What an operation asks of the run loop: nothing, a pause or the end."""

    delay: float | None = None
    reason: str | None = None
    stop: bool = False


_OK = _Verdict()
_STOP = _Verdict(stop=True)


@dataclass(frozen=True, slots=True)
class _ServiceView:
    """What the connection status reads of a running source."""

    gateway_pause_seconds: float


class _ServiceSnapshot:
    def __init__(self, source: IcomSource) -> None:
        self._source = source

    def snapshot(self) -> _ServiceView:
        return _ServiceView(self._source.pause_remaining())


class IcomSource:
    def __init__(
        self, connection: IcomConnection, binding_id: str, binding: StoredBinding
    ) -> None:
        self.connection, self.binding_id, self.binding = connection, binding_id, binding
        self.context: SourceContext | None = None
        self.client: IcomClient | None = None
        self._session: aiohttp.ClientSession | None = None
        self._access: IcomAccess | None = None
        self._records: dict[str, _Record] = {}
        self._circuits: dict[str, tuple[str, ...]] = {}
        self._discovery_due = 0.0
        self._identity_at: float | None = None
        self._confirmed = False
        self._condition: LocalCondition | None = None
        self._problem: str | None = None
        self._closed = False
        self._failures_in_row = 0
        self._undecryptable = 0
        self._pauses = 0
        self._pause_total = 0
        self._pause_until: float | None = None
        self._pause_reason: str | None = None
        self._down_since: float | None = None
        self._changes: deque[dict[str, object]] = deque(maxlen=CONDITION_CHANGES)
        self._wake = asyncio.Event()
        self._expiry: asyncio.TimerHandle | None = None
        self._warned = False

    # ------------------------------------------------------------ interface

    @property
    def service(self) -> _ServiceSnapshot:
        """The pause of the access, as the connection status reads it."""
        return _ServiceSnapshot(self)

    @property
    def pause_reason(self) -> str | None:
        """Why the access pauses now (a fixed code); None without a pause."""
        return self._pause_reason if self._pause_remaining_raw() > 0 else None

    def pause_remaining(self) -> float:
        return round(self._pause_remaining_raw())

    def _pause_remaining_raw(self) -> float:
        if self._pause_until is None:
            return 0.0
        return max(0.0, self._pause_until - monotonic())

    def current(self) -> bool:
        return (
            not self._closed
            and not self.connection.closed
            and self.context is not None
            and self.context.configuration_current
        )

    def refresh(self, paths: tuple[str, ...]) -> int:
        """Read these paths again soon (after a confirmed cloud write).

        Only readings the access already has, at most eight; a paused or
        stopped access ignores it, and the regular gap still holds.
        """
        if not self.current() or self.client is None or self._pause_until is not None:
            return 0
        now = monotonic()
        count = 0
        for path in paths:
            record = self._records.get(path)
            if record is None or record.absent or count >= 8:
                continue
            record.due = min(record.due, now)
            count += 1
        if count:
            self._wake.set()
        return count

    async def test_connection(self) -> tuple[str, int]:
        """Read the gateway ID once on explicit request (the connection test).

        Uses the running client and its gap; the ID must still be the cloud's.
        Returns a result key and the elapsed milliseconds.
        """
        client, access = self.client, self._access
        if client is None or access is None or not self.current():
            return "not_running", 0
        started = monotonic()
        try:
            reply = await client.get(UUID_PATH)
        except IcomError as error:
            millis = round((monotonic() - started) * 1000)
            return _TEST_RESULTS.get(error.problem, "unreachable"), millis
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            return "not_running", 0
        millis = round((monotonic() - started) * 1000)
        if not gateway_ids_match(reply.document.get("value"), access.local_id):
            return "other_gateway", millis
        return "ok", millis

    def diagnostics(self) -> dict[str, object]:
        """Counts, ages and fixed codes; no value, password, address or ID."""
        now = monotonic()
        client = self.client
        metrics = client.metrics if client is not None else None
        return {
            "protocol": "icom",
            "condition": self._condition.value if self._condition else "unavailable",
            "identity_confirmed": self._confirmed,
            "problem": self._problem,
            "request_metrics": {
                "requests": metrics.requests if metrics else 0,
                "answered": metrics.answered if metrics else 0,
                "last_duration_ms": metrics.last_duration_ms if metrics else None,
            },
            "failures_by_class": dict(sorted(metrics.failures.items()))
            if metrics
            else {},
            "condition_changes": list(self._changes),
            "pauses": {
                "in_a_row": self._pauses,
                "total": self._pause_total,
                "remaining_seconds": self.pause_remaining(),
                "reason": self.pause_reason,
            },
            "circuits": {key: len(ids) for key, ids in sorted(self._circuits.items())},
            "discovery_complete": len(self._circuits) == len(DIRECTORIES),
            "identity_check_age_seconds": None
            if self._identity_at is None
            else round(now - self._identity_at),
            "resources": [
                {
                    "path": record.request.definition.template,
                    "freshness": self._view(record, now).freshness.value,
                    "available": self._view(record, now).available,
                    "age_seconds": self._view(record, now).age_seconds,
                    "target_interval_seconds": record.scheduled_period
                    or self.period(record.request),
                    "problem": record.problem or record.last_failure,
                    "absent": record.absent,
                }
                for record in self._records.values()
                if record.attempted
            ],
        }

    # --------------------------------------------------------------- policy

    def interval(self) -> int:
        """The configured local interval, at least 120 s."""
        context = self.context
        value: object = DEFAULT_INTERVAL
        if context is not None:
            options = context.runtime.snapshot.to_options()
            settings = cast(
                Mapping[str, Mapping[str, object]], options.get("connections", {})
            ).get(self.connection.identifier, {})
            value = settings.get("local_interval", DEFAULT_INTERVAL)
        if type(value) is not int:
            return DEFAULT_INTERVAL
        return max(MIN_INTERVAL, value)

    def period(self, request: ReadRequest) -> int:
        """The period of one reading: the K40 policy class, slowed to ours."""
        return period(request, self.interval()) or self.interval()

    # ---------------------------------------------------------------- state

    def _note(self, condition: LocalCondition, reason: str | None) -> None:
        if condition is self._condition:
            return
        self._condition = condition
        self._changes.append(
            {
                "at": dt_util.utcnow().isoformat(timespec="seconds"),
                "condition": condition.value,
                "reason": reason,
            }
        )
        now = monotonic()
        if condition in _DOWN or condition is LocalCondition.AUTH_REQUIRED:
            if self._down_since is None:
                self._down_since = now
        elif condition is LocalCondition.READY and self._down_since is not None:
            _LOGGER.info(
                "The local iCom access is available again after %d s",
                round(now - self._down_since),
            )
            self._down_since = None
            self._warned = False

    def _view(self, record: _Record, now: float) -> ResourceView:
        observation = record.observation
        if observation is None:
            return ResourceView(
                record.request,
                Freshness.UNCONFIRMED,
                False,
                None,
                record.scheduled_period,
                record.actual_interval,
                record.problem,
                None,
            )
        interval = record.scheduled_period or self.period(record.request)
        age = max(0.0, now - observation.observed_monotonic)
        if age >= EXPIRY_PERIODS * interval:
            freshness = Freshness.EXPIRED
        elif age > late_after(interval):
            freshness = Freshness.LATE
        else:
            freshness = Freshness.FRESH
        return ResourceView(
            record.request,
            freshness,
            freshness is not Freshness.EXPIRED and record.problem is None,
            round(age, 1),
            interval,
            record.actual_interval,
            record.problem,
            observation,
        )

    def _publish(
        self,
        condition: LocalCondition,
        problem: str | None = None,
        *,
        reason: str | None = None,
    ) -> None:
        context = self.context
        if context is None or not self.current():
            return
        self._condition_publish(condition, problem, reason)
        now = monotonic()
        identity = context.identity_context(GatewayNamespace.K40_LOCAL)
        confirmed = self._confirmed and condition in (
            LocalCondition.READY,
            LocalCondition.PAUSED,
        )
        records = [r for r in self._records.values() if r.attempted]
        views = tuple(self._view(r, now) for r in records)
        observations = tuple(r.observation for r in records if r.observation)
        context.publish_local(
            LocalSourceState(
                identity,
                condition,
                confirmed,
                observations,
                resources=views,
                problem=problem,
            )
        )
        self._schedule_expiry(views, now)

    def _condition_publish(
        self, condition: LocalCondition, problem: str | None, reason: str | None
    ) -> None:
        self._problem = problem
        self._note(condition, problem or reason)

    def _schedule_expiry(self, views: tuple[ResourceView, ...], now: float) -> None:
        """Publish again when the first reading expires, so it is withdrawn."""
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None
        waits = [
            view.observation.observed_monotonic
            + EXPIRY_PERIODS * (view.target_interval_seconds or MIN_INTERVAL)
            - now
            for view in views
            if view.available and view.observation is not None
        ]
        if not waits or self._closed:
            return
        self._expiry = asyncio.get_running_loop().call_later(
            max(1.0, min(waits)) + 0.5, self._expire
        )

    def _expire(self) -> None:
        self._expiry = None
        if self._condition is not None and self.current():
            self._publish(self._condition, self._problem, reason=self._pause_reason)

    # ------------------------------------------------------------- run loop

    async def run(self, context: SourceContext) -> None:
        self.context = context
        try:
            await self._run()
        finally:
            if self._expiry is not None:
                self._expiry.cancel()
                self._expiry = None
        # A stopped access stays published for the settings and the status;
        # a changed connection replaces this source through the normal
        # lifecycle.
        await asyncio.Future[None]()

    async def _run(self) -> None:
        context = self.context
        assert context is not None
        self._publish(LocalCondition.INITIALIZING)
        try:
            access = load_icom_access(self.connection.record.to_data())
            locator = cast(dict[str, Any], self.binding.to_data()["locator"])
            if (
                locator["namespace"] != GatewayNamespace.K40_LOCAL
                or locator["value"] != access.gateway_id
            ):
                raise IcomAccessError("local_identity_conflict")
        except IcomAccessError:
            self._publish(LocalCondition.AUTH_REQUIRED, "icom_revalidation_required")
            return
        self._access = access
        self._session = async_create_clientsession(self.connection.factory.hass)
        self.client = IcomClient(
            self._session, access.host, access.key, port=access.port
        )
        self._discovery_due = monotonic()
        while self.current():
            verdict = await self._check_identity()
            if verdict is _OK:
                verdict = await self._poll()
            if verdict.stop:
                return
            if verdict.delay is not None:
                await self._pause(verdict)

    async def _pause(self, verdict: _Verdict) -> None:
        """Publish the pause and wait it out; every pause in a row is longer."""
        assert verdict.delay is not None
        self._pauses += 1
        self._pause_total += 1
        self._pause_reason = verdict.reason
        self._pause_until = monotonic() + verdict.delay
        # Readings of a confirmed access stay current until their expiry,
        # but only for the first pause; a longer outage withdraws them.
        condition = (
            LocalCondition.PAUSED
            if self._confirmed and self._pauses == 1
            else LocalCondition.UNAVAILABLE
        )
        if not self._warned and self._pauses == 2:
            self._warned = True
            _LOGGER.warning(
                "The local iCom gateway does not answer; trying again after pauses"
            )
        self._publish(condition, reason=verdict.reason)
        await asyncio.sleep(verdict.delay)
        self._pause_until = None
        # The gateway may have been restarted (or replaced): check it again.
        self._identity_at = None

    def _pause_for(self, error: IcomError, reason: str | None = None) -> _Verdict:
        """The pause after a failure: growing, or the gateway's own request."""
        grown = PAUSES[min(self._pauses, len(PAUSES) - 1)]
        delay = max(grown, error.retry_after or 0.0)
        if reason is None:
            reason = (
                "rate_limited" if error.problem is IcomProblem.BUSY else "connection"
            )
        return _Verdict(delay=delay, reason=reason)

    def _failed(self, error: IcomError, *, first_check: bool = False) -> _Verdict:
        """Judge a failed request: a verdict, or _OK to go on with the next."""
        problem = error.problem
        if problem is IcomProblem.UNDECRYPTABLE:
            self._undecryptable += 1
            limit = (
                1 if first_check and not self._confirmed else UNDECRYPTABLE_BEFORE_STOP
            )
            if self._undecryptable >= limit:
                self._publish(LocalCondition.AUTH_REQUIRED, "auth_required")
                return _STOP
            return _OK
        if problem is IcomProblem.BUSY:
            return self._pause_for(error)
        if problem in _TRANSPORT:
            self._failures_in_row += 1
            if self._failures_in_row >= FAILURES_BEFORE_PAUSE or first_check:
                self._failures_in_row = 0
                return self._pause_for(error)
        return _OK

    def _succeeded(self) -> None:
        self._failures_in_row = 0
        self._undecryptable = 0

    async def _check_identity(self) -> _Verdict:
        """The gateway ID read locally must be the cloud's one."""
        client, access = self.client, self._access
        assert client is not None and access is not None
        if (
            self._identity_at is not None
            and monotonic() - self._identity_at < IDENTITY_PERIOD
        ):
            return _OK
        try:
            reply = await client.get(UUID_PATH)
        except IcomError as error:
            return self._after_identity_failure(error)
        if not self.current():
            return _STOP
        if not gateway_ids_match(reply.document.get("value"), access.local_id):
            self._confirmed = False
            self._publish(LocalCondition.UNAVAILABLE, "local_identity_conflict")
            return _STOP
        self._succeeded()
        self._identity_at = monotonic()
        if not self._confirmed or self._condition is not LocalCondition.READY:
            self._confirmed = True
            self._publish(LocalCondition.READY)
        return _OK

    def _after_identity_failure(self, error: IcomError) -> _Verdict:
        if error.problem in PATH_PROBLEMS:
            # A gateway without an ID cannot be confirmed: tried again later.
            return self._pause_for(error, "identity_unavailable")
        verdict = self._failed(error, first_check=True)
        if verdict is _OK:
            # One undecryptable answer of a confirmed access: look again soon.
            return _Verdict(delay=RETRY_SECONDS, reason="undecryptable")
        return verdict

    async def _poll(self) -> _Verdict:
        while self.current():
            now = monotonic()
            if self._identity_at is None or now - self._identity_at >= IDENTITY_PERIOD:
                return _OK
            if now >= self._discovery_due:
                verdict = await self._discover()
                if verdict is not _OK:
                    return verdict
            due = sorted(
                (r for r in self._records.values() if r.due <= now),
                key=lambda r: r.due,
            )[:ROUND_LIMIT]
            if not due:
                await self._idle(now)
                continue
            for record in due:
                if not self.current():
                    return _STOP
                verdict = await self._read(record)
                if verdict is not _OK:
                    return verdict
            self._publish(LocalCondition.READY)
        return _STOP

    async def _idle(self, now: float) -> None:
        upcoming = [r.due for r in self._records.values()]
        upcoming.append(self._discovery_due)
        if self._identity_at is not None:
            upcoming.append(self._identity_at + IDENTITY_PERIOD)
        wait = min(IDLE_MAX, max(0.5, min(upcoming) - now))
        self._wake.clear()
        try:
            async with asyncio.timeout(wait):
                await self._wake.wait()
        except TimeoutError:
            pass

    async def _discover(self) -> _Verdict:
        """Read the circuit directories and rebuild the paths to read."""
        client = self.client
        assert client is not None
        found: dict[str, tuple[str, ...]] = {}
        complete = True
        for key, (directory, prefix) in DIRECTORIES.items():
            try:
                reply = await client.get(directory)
            except IcomError as error:
                if error.problem in PATH_PROBLEMS:
                    # No such circuits on this installation.
                    found[key] = ()
                    self._failures_in_row = 0
                    continue
                verdict = self._failed(error)
                if verdict is not _OK:
                    return verdict
                complete = False
                continue
            self._succeeded()
            found[key] = circuit_ids(reply.document, directory, prefix)
        previous = dict(self._circuits)
        for key, ids in found.items():
            previous[key] = ids
        self._circuits = previous
        self._discovery_due = monotonic() + (
            DISCOVERY_PERIOD if complete else DISCOVERY_RETRY
        )
        self._rebuild()
        return _OK

    def _rebuild(self) -> None:
        wanted = readings(
            self._circuits.get("hc", ()),
            self._circuits.get("dhw", ()),
            self._circuits.get("hs", ()),
        ).requests
        now = monotonic()
        keep = {request.path for request in wanted}
        for path in tuple(self._records):
            if path not in keep:
                del self._records[path]
        for request in wanted:
            self._records.setdefault(request.path, _Record(request, now))

    async def _read(self, record: _Record) -> _Verdict:
        client, context = self.client, self.context
        assert client is not None and context is not None
        started = monotonic()
        record.attempted = True
        record.last_attempt = started
        try:
            reply = await client.get(record.request.path)
        except IcomError as error:
            return self._after_read_failure(record, error, started)
        if not self.current():
            return _STOP
        self._succeeded()
        interval = self.period(record.request)
        try:
            observation = decode_resource(
                record.request,
                reply.body,
                context=context.identity_context(GatewayNamespace.K40_LOCAL),
                observed_at=datetime.now(UTC),
                observed_monotonic=monotonic(),
            )
        except LocalResourceError, ValueError, KeyError, TypeError:
            # An answer the decoder cannot use is a problem of this path only.
            record.observation, record.problem = None, "local_decode_failed"
            record.due = started + interval
            return _OK
        if record.previous_success is not None:
            record.actual_interval = observation.observed_monotonic - (
                record.previous_success
            )
        record.previous_success = observation.observed_monotonic
        record.observation, record.problem, record.absent = observation, None, False
        record.failures, record.last_failure = 0, None
        record.scheduled_period = interval
        record.due = started + interval
        self._pauses = 0
        return _OK

    def _after_read_failure(
        self, record: _Record, error: IcomError, started: float
    ) -> _Verdict:
        if error.problem in PATH_PROBLEMS:
            # The gateway answered, only not for this path.
            self._succeeded()
            record.observation, record.absent = None, True
            record.problem = error.problem.value
            record.due = started + NOT_FOUND_RETRY
            return _OK
        record.failures += 1
        record.last_failure = error.problem.value
        record.due = started + min(RETRY_SECONDS, float(self.period(record.request)))
        return self._failed(error)

    async def close(self) -> None:
        self._closed = True
        self._wake.set()
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None
        session, self._session = self._session, None
        if session is not None:
            try:
                await session.close()
            except Exception as error:
                log_failure(
                    _LOGGER,
                    logging.ERROR,
                    "A connection resource could not be closed",
                    error=error,
                )


_TEST_RESULTS: dict[IcomProblem, str] = {
    IcomProblem.UNDECRYPTABLE: "auth_required",
    IcomProblem.BUSY: "paused",
    IcomProblem.TIMEOUT: "unreachable",
    IcomProblem.CONNECT: "unreachable",
}
