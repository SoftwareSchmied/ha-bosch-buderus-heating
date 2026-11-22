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
from dataclasses import dataclass, replace
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
    IDENTITY_UNVERIFIED,
    IDENTITY_UUID,
    IDENTITY_VALUES,
    IcomAccess,
    IcomAccessError,
    comparable_id,
    describe_reported_id,
    is_icom_connection,
    load_icom_access,
)
from .icom_client import (
    PATH_PROBLEMS,
    RESET,
    IcomClient,
    IcomError,
    IcomProblem,
    other_private_password,
)
from .icom_identity import FALLBACK_PATHS, Verdict, compare, fingerprint
from .icom_plan import DIRECTORIES, circuit_ids, readings
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
# Answers that cannot be decrypted in a row that stop the access for good;
# a single one (after a restart too) is looked at again after RETRY_SECONDS.
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
        # The last undecryptable answer fits an empty private password: the
        # gateway's private password was reset (checked offline only).
        self._reset_seen = False
        # The last check of the gateway's own ID while running: kinds, counts
        # and a verdict only (never the ID).
        self._identity_check: dict[str, object] | None = None
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

    @property
    def private_password_reset(self) -> bool:
        """Whether the gateway answers as one whose private password was reset."""
        return self._reset_seen

    def _note_undecryptable(self, error: IcomError) -> None:
        """Look offline whether the answer fits a reset private password.

        Uses the answer already received; the access keeps reading with its
        stored key only.
        """
        access = self._access
        if access is None or error.answer is None:
            return
        found = other_private_password(
            access.gateway_password, access.private_password, error.answer
        )
        self._reset_seen = found is not None and found.variant == RESET

    async def test_connection(self) -> tuple[str, int]:
        """Read the gateway ID once on explicit request (the connection test).

        Uses the running client and its gap. An access set up by its own ID
        whose gateway now reports another, an unusable or no ID is judged by
        its values, as at setup: only a clear contradiction is another
        gateway. Returns a result key and the elapsed milliseconds.
        """
        client, access = self.client, self._access
        if client is None or access is None or not self.current():
            return "not_running", 0
        started = monotonic()

        def elapsed() -> int:
            return round((monotonic() - started) * 1000)

        try:
            try:
                reply = await client.get(access.identity_path)
            except IcomError as error:
                if error.problem not in PATH_PROBLEMS:
                    raise
                if access.identity_method == IDENTITY_UNVERIFIED:
                    # The gateway answered; an unverified access checks no more.
                    return "ok", elapsed()
                value: object = None
                offered = False
            else:
                value, offered = reply.document.get("value"), True
                if access.usable(value) and access.confirms(value):
                    return "ok", elapsed()
            # No longer confirmed by its ID or version: the values decide.
            verdict, _ = await self._compare_identity(value, offered=offered)
        except IcomError as error:
            if error.problem is IcomProblem.UNDECRYPTABLE:
                self._note_undecryptable(error)
                if self._reset_seen:
                    return "auth_reset", elapsed()
            return _TEST_RESULTS.get(error.problem, "unreachable"), elapsed()
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            return "not_running", 0
        if verdict is Verdict.CONTRADICTION:
            return "other_gateway", elapsed()
        return "ok", elapsed()

    def diagnostics(self) -> dict[str, object]:
        """Counts, ages and fixed codes; no value, password, address or ID."""
        now = monotonic()
        client = self.client
        metrics = client.metrics if client is not None else None
        return {
            "protocol": "icom",
            "condition": self._condition.value if self._condition else "unavailable",
            "identity_confirmed": self._confirmed,
            "identity_method": self._access.identity_method
            if self._access is not None
            else None,
            # Which form of the private password the setup proved, and whether
            # an answer showed a reset private password: codes only.
            "private_password": {
                "variant": self._access.private_password_variant
                if self._access is not None
                else None,
                "reset_detected": self._reset_seen,
            },
            # How the setup recognised the gateway: what /gateway/uuid
            # answered (kind and length, a text masked), whether it was the
            # cloud's ID, the verdict of the values and how many matched.
            "setup_check": dict(self._access.setup_check)
            if self._access is not None
            else {},
            # The last check of the own ID while running (same fields).
            "identity_check": dict(self._identity_check)
            if self._identity_check is not None
            else None,
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
        self._session = async_create_clientsession(
            self.connection.factory.hass, auto_cleanup=False
        )
        self.client = IcomClient(
            self._session, access.host, access.key, port=access.port
        )
        self._discovery_due = monotonic()
        while self.current():
            if not self._has_cloud_system():
                await self._wait_for_cloud_system()
                continue
            verdict = await self._check_identity()
            if verdict is _OK:
                verdict = await self._poll()
            if verdict.stop:
                return
            if verdict.delay is not None:
                await self._pause(verdict)

    def _has_cloud_system(self) -> bool:
        """Whether a cloud connection still supplies the system of this access.

        The readings only complete the cloud's; without it nothing uses them,
        so the gateway is not asked (it is slow and easily overloaded).
        """
        context = self.context
        if context is None:
            return True
        return any(
            binding.target_id == self.binding.target_id
            and binding.adapter != LOCAL_KIND
            for binding in context.runtime.snapshot.bindings.values()
        )

    async def _wait_for_cloud_system(self) -> None:
        """Make no request until a cloud connection supplies the system again."""
        if self._condition is not LocalCondition.UNAVAILABLE or self._pause_until:
            self._publish(LocalCondition.UNAVAILABLE, reason="no_cloud_system")
        self._identity_at = None
        self._wake.clear()
        try:
            async with asyncio.timeout(IDLE_MAX):
                await self._wake.wait()
        except TimeoutError:
            pass

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
        delay = max(self._grown_pause(), error.retry_after or 0.0)
        if reason is None:
            reason = (
                "rate_limited" if error.problem is IcomProblem.BUSY else "connection"
            )
        return _Verdict(delay=delay, reason=reason)

    def _grown_pause(self) -> float:
        return PAUSES[min(self._pauses, len(PAUSES) - 1)]

    def _failed(self, error: IcomError, *, first_check: bool = False) -> _Verdict:
        """Judge a failed request: a verdict, or _OK to go on with the next."""
        problem = error.problem
        if problem is IcomProblem.UNDECRYPTABLE:
            self._note_undecryptable(error)
            self._undecryptable += 1
            if self._undecryptable >= UNDECRYPTABLE_BEFORE_STOP:
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
        self._reset_seen = False

    async def _check_identity(self) -> _Verdict:
        """The gateway must still be the one that was recognised at setup.

        An access recognised by its own ID or by a stored version reading
        whose gateway no longer confirms it (no ID, "-1", another ID, no
        version reading or another one, for example after a firmware update)
        is judged by its values as at setup: only a clear contradiction
        pauses it, and it is never stopped for good.
        """
        client, access = self.client, self._access
        assert client is not None and access is not None
        if (
            self._identity_at is not None
            and monotonic() - self._identity_at < IDENTITY_PERIOD
        ):
            return _OK
        try:
            reply = await client.get(access.identity_path)
        except IcomError as error:
            if (
                error.problem in PATH_PROBLEMS
                and access.identity_method != IDENTITY_UNVERIFIED
            ):
                return await self._identity_by_values(None, offered=False)
            return self._after_identity_failure(error)
        if not self.current():
            return _STOP
        value = reply.document.get("value")
        if not (access.usable(value) and access.confirms(value)):
            # The values decide, as at setup.
            return await self._identity_by_values(value, offered=True)
        if access.identity_method == IDENTITY_UUID:
            self._identity_check = {
                "uuid": describe_reported_id(value),
                "uuid_matches_cloud": True,
                "method": IDENTITY_UUID,
            }
        elif access.identity_method == IDENTITY_VALUES:
            self._identity_check = {
                "fingerprint_matches": True,
                "method": IDENTITY_VALUES,
            }
        self._identity_confirmed()
        return _OK

    async def _compare_identity(
        self, reported: object, *, offered: bool
    ) -> tuple[Verdict, dict[str, object]]:
        """Compare the gateway's values with the cloud's, as the setup does.

        For an access whose own ID or stored version reading no longer
        confirms the gateway. Reads only the paths the cloud has a current
        reading of; the outcome is noted for the log and diagnostics (kinds
        and counts, never an ID or a version). Returns the verdict and the
        local readings. Raises IcomError for a failed request other than a
        missing path.
        """
        from .icom_setup import readings_of

        client, context, access = self.client, self.context, self._access
        assert client is not None and access is not None
        cloud = (
            readings_of(
                context.runtime, context.runtime.snapshot, self.binding.target_id
            )
            if context is not None
            else {}
        )
        local: dict[str, object] = {}
        for path in FALLBACK_PATHS:
            if path not in cloud:
                continue
            try:
                reply = await client.get(path)
            except IcomError as error:
                if error.problem in PATH_PROBLEMS:
                    continue
                raise
            local[path] = reply.document.get("value")
        outcome = compare(local, cloud)
        check: dict[str, object]
        if access.identity_method == IDENTITY_VALUES:
            check = {"fingerprint_matches": False if offered else None}
        else:
            usable = isinstance(reported, str) and comparable_id(reported) is not None
            check = {
                "uuid": describe_reported_id(reported, read=offered),
                "uuid_matches_cloud": False if usable else None,
            }
        check.update(
            {
                "values_verdict": outcome.verdict.value,
                "values_matching": outcome.matches,
                "values_read": len(local),
                "method": IDENTITY_VALUES
                if outcome.verdict is Verdict.CONFIRMED
                else IDENTITY_UNVERIFIED
                if outcome.verdict is Verdict.INSUFFICIENT
                else None,
            }
        )
        self._identity_check = check
        _LOGGER.debug(
            "iCom identity check while running: own ID %s, the cloud's ID: %s, "
            "stored version reading matches: %s, values: %s (%s of %s matching), "
            "reading on: %s",
            check.get("uuid", "not used"),
            check.get("uuid_matches_cloud"),
            check.get("fingerprint_matches", "not used"),
            check["values_verdict"],
            check["values_matching"],
            check["values_read"],
            check["method"] or "no, paused",
        )
        return outcome.verdict, local

    async def _identity_by_values(self, reported: object, *, offered: bool) -> _Verdict:
        """The values decide whether a gateway that no longer confirms reads on."""
        try:
            verdict, local = await self._compare_identity(reported, offered=offered)
        except IcomError as error:
            return self._after_identity_failure(error)
        if not self.current():
            return _STOP
        if verdict is Verdict.CONTRADICTION:
            # Only a clear contradiction: the readings are withdrawn and the
            # gateway is looked at again after a pause.
            self._confirmed = False
            return _Verdict(delay=self._grown_pause(), reason="identity_contradiction")
        if verdict is Verdict.CONFIRMED:
            self._renew_fingerprint(local)
        self._identity_confirmed()
        return _OK

    def _renew_fingerprint(self, local: Mapping[str, object]) -> None:
        """After a confirming check, recognise the gateway by its new version.

        Only for an access recognised by a version reading, for example after
        a firmware update. The entry is updated once (the access then starts
        again with the new reading); the diagnostics note it.
        """
        access = self._access
        renewed = fingerprint(local)
        if (
            access is None
            or access.identity_method != IDENTITY_VALUES
            or renewed is None
            or renewed == (access.fingerprint_path, access.fingerprint)
        ):
            return
        self._access = replace(
            access, fingerprint_path=renewed[0], fingerprint=renewed[1]
        )
        if self._identity_check is not None:
            self._identity_check["fingerprint_updated"] = True
        _LOGGER.debug("iCom access: the stored version reading was renewed")
        hass = self.connection.factory.hass
        hass.loop.call_soon(self._store_fingerprint, renewed)

    def _store_fingerprint(self, renewed: tuple[str, str]) -> None:
        context = self.context
        if context is None or not self.current():
            return
        try:
            context.runtime.management.update_icom_fingerprint(
                self.connection.identifier, renewed
            )
        except Exception:  # the access reads on with it in memory
            _LOGGER.debug(
                "The renewed version reading of the iCom access was not stored"
            )

    def _identity_confirmed(self) -> None:
        self._succeeded()
        self._identity_at = monotonic()
        if not self._confirmed or self._condition is not LocalCondition.READY:
            self._confirmed = True
            self._publish(LocalCondition.READY)

    def _after_identity_failure(self, error: IcomError) -> _Verdict:
        access = self._access
        if (
            error.problem in PATH_PROBLEMS
            and access is not None
            and access.identity_method == IDENTITY_UNVERIFIED
        ):
            # An unverified access has nothing to confirm: a gateway that
            # does not offer the path still answered.
            self._identity_confirmed()
            return _OK
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
            if not self._has_cloud_system():
                return _OK
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
                # Created without auto_cleanup: detached, never closed (the
                # connector belongs to Home Assistant).
                session.detach()
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
