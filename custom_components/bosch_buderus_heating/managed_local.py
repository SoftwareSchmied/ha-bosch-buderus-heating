"""Run one verified local gateway independently of cloud connections."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import timedelta
from time import monotonic
from typing import Any, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .configuration import (
    LOCAL_KIND,
    ConfigurationError,
    StoredBinding,
    StoredConnection,
)
from .connection_runtime import ConnectionRuntime, SourceContext
from .gateway_identity import GatewayNamespace
from .local_api.catalog import ReadRequest, request_for_path
from .local_api.errors import LocalResourceError
from .local_api.models import SystemInfo
from .local_budget import LocalBudgetError, LocalReadBudget, local_budget_manager
from .local_credentials import (
    LocalCredentialError,
    StoredLocalAccess,
    load_local_access,
)
from .local_polling.inventory import Freshness
from .local_polling.policy import validate_interval
from .local_polling.service import LocalPollingService, PollSnapshot
from .local_polling.storage import LocalScheduleStore
from .local_probe import (
    ProbeContext,
    ProbeProblem,
    ProbeStatus,
    TransportFailure,
    assess_local_probe,
)
from .local_read import LocalReadClient, ReadResult, ReadState
from .local_selection import paired_cloud_interval
from .local_state import LocalCondition, LocalSourceState
from .local_transport import (
    CertificateBinding,
    PortRole,
    StaleLocalRequest,
    read_local_probe,
)

# An explicit system read waits for the regular reads at most this often,
# each time for at most this long (P49b step 8).
SYSTEM_READ_ATTEMPTS = 6
SYSTEM_READ_WAIT_SECONDS = 4.0
# Within the comparison's 30 s limit, so a busy access is reported as busy.
SYSTEM_READ_SECONDS = 20.0
# A start that failed on the stored local pauses is tried again after these.
START_RETRY = (60.0, 120.0, 240.0, 480.0, 900.0)
# Condition changes kept for the diagnostics.
CONDITION_CHANGES = 20
_DOWN = frozenset(
    {
        LocalCondition.PAUSED,
        LocalCondition.UNAVAILABLE,
        LocalCondition.AUTH_REQUIRED,
        LocalCondition.CERTIFICATE_CHANGED,
    }
)
_LOGGER = logging.getLogger(__name__)


class LocalDriverFactory:
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[Any],
        runtime: Callable[[], ConnectionRuntime],
    ) -> None:
        self.hass, self.entry, self.runtime = hass, entry, runtime

    def __call__(self, identifier: str, record: StoredConnection) -> LocalConnection:
        return LocalConnection(self, identifier, record)


class LocalConnection:
    def __init__(
        self, factory: LocalDriverFactory, identifier: str, record: StoredConnection
    ) -> None:
        self.factory, self.identifier, self.record = factory, identifier, record
        self.sources: dict[str, LocalSource] = {}
        self.closed = False

    async def connect(self) -> None:
        # Verification belongs to the source and never blocks peer startup.
        if self.closed or self.record.kind != LOCAL_KIND:
            raise ConfigurationError("unsupported_connection")

    def source(self, binding_id: str, binding: StoredBinding) -> LocalSource:
        if (
            self.closed
            or binding.connection_id != self.identifier
            or binding.adapter != LOCAL_KIND
        ):
            raise ConfigurationError("local_configuration_changed")
        source = LocalSource(self, binding_id, binding)
        self.sources[binding_id] = source
        return source

    async def close(self) -> None:
        self.closed = True
        await asyncio.gather(*(s.close() for s in tuple(self.sources.values())))


class _IdentityClient:
    """A later system response must still belong to the verified gateway."""

    def __init__(self, client: LocalReadClient, gateway_id: str) -> None:
        self.client, self.gateway_id = client, gateway_id

    async def read(self, request: ReadRequest) -> ReadResult:
        result = await self.client.read(request)
        if (
            result.observation is not None
            and isinstance(result.observation.data, SystemInfo)
            and result.observation.data.gateway_id != self.gateway_id
        ):
            return replace(
                result,
                state=ReadState.ACCESS_ERROR,
                problem="local_identity_conflict",
                observation=None,
            )
        return result

    async def close(self) -> None:
        await self.client.close()


class LocalSource:
    def __init__(
        self, connection: LocalConnection, binding_id: str, binding: StoredBinding
    ) -> None:
        self.connection, self.binding_id, self.binding = connection, binding_id, binding
        self.context: SourceContext | None = None
        self.service: LocalPollingService | None = None
        self.client: LocalReadClient | None = None
        self._closed = False
        # O93: the most recent condition changes, with time and fixed reason.
        self._changes: deque[dict[str, object]] = deque(maxlen=CONDITION_CHANGES)
        self._condition: LocalCondition | None = None
        # Since when the access is paused or unavailable (monotonic seconds).
        self._down_since: float | None = None

    def refresh(self, paths: tuple[str, ...]) -> int:
        """Read these paths again soon, within the existing explicit-read limits.

        Used after a confirmed cloud write. Silently does nothing when the source
        is not running or its queue is full; regular reads continue anyway.
        """
        service = self.service
        if service is None or not self.current():
            return 0
        requests: list[ReadRequest] = []
        for path in paths:
            try:
                requests.append(request_for_path(path))
            except LocalResourceError:
                continue
        sent = 0
        # At most two operations of four reads: the explicit queue holds eight.
        for start in range(0, min(len(requests), 8), 4):
            chunk = tuple(requests[start : start + 4])
            try:
                service.request(chunk)
            except StaleLocalRequest, ValueError:
                break
            sent += len(chunk)
        return sent

    def rescan_state(self) -> str:
        """A6: see ``LocalPollingService.rescan_state``."""
        service = self.service
        if service is None or not self.current():
            return "unavailable"
        return service.rescan_state()

    def rescan(self) -> str:
        """A6: search the local resources again (``LocalPollingService``)."""
        service = self.service
        if service is None or not self.current():
            return "unavailable"
        return service.rescan()

    async def test_connection(self) -> tuple[str, int]:
        """Read ``/system/basicInfo`` once on explicit request (P21).

        Uses the running client and its limits; the identity must still be the
        verified gateway. Returns a result key and the elapsed milliseconds.
        """
        from time import monotonic

        client = self.client
        if client is None or not self.current():
            return "not_running", 0
        locator = cast(dict[str, Any], self.binding.to_data()["locator"])
        started = monotonic()
        try:
            result = await _IdentityClient(client, str(locator["value"])).read(
                request_for_path("/system/basicInfo")
            )
        except StaleLocalRequest:
            return "not_running", 0
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            # The source was closed or replaced during the test.
            return "not_running", 0
        millis = round((monotonic() - started) * 1000)
        if result.state is ReadState.SUCCESS:
            return "ok", millis
        problem = str(result.problem or "")
        if problem == "local_identity_conflict":
            return "other_gateway", millis
        if problem == TransportFailure.TLS_CHANGED:
            return "certificate_changed", millis
        if problem == "auth_required":
            return "auth_required", millis
        if problem == "busy":
            return "busy", millis
        if problem in ("paused", "rate_limited"):
            return "paused", millis
        return "unreachable", millis

    async def read_system(self) -> SystemInfo | str | None:
        """Read the system information once on explicit request (P49b step 8).

        Uses the running client and its read limits; the answer must still
        belong to the verified gateway. While the regular reads hold the
        access, the read waits for its turn a few times; ``"busy"`` if it
        never gets one. None when the source is not running, paused,
        unreachable or the answer is not a complete system record.
        """
        client = self.client
        if client is None or not self.current():
            return None
        from time import monotonic

        locator = cast(dict[str, Any], self.binding.to_data()["locator"])
        reader = _IdentityClient(client, str(locator["value"]))
        deadline = monotonic() + SYSTEM_READ_SECONDS
        for attempt in range(SYSTEM_READ_ATTEMPTS):
            try:
                result = await reader.read(request_for_path("/system/basicInfo"))
            except StaleLocalRequest:
                return None
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
                return None  # the source was closed or replaced during the read
            wait = result.retry_after_seconds
            if result.state is not ReadState.DEFERRED or wait is None:
                break
            busy = result.problem == "busy"
            if (
                wait > SYSTEM_READ_WAIT_SECONDS
                or attempt == SYSTEM_READ_ATTEMPTS - 1
                or monotonic() + wait > deadline
            ):
                # Still busy with the regular reads: try again shortly. A
                # longer pause of the access (rate limit) counts as paused.
                return "busy" if busy or wait <= SYSTEM_READ_WAIT_SECONDS else None
            # Busy with the regular reads, or the gap between two reads.
            await asyncio.sleep(wait)
            if not self.current():
                return None
        observation = result.observation
        if (
            result.state is not ReadState.SUCCESS
            or observation is None
            or not isinstance(observation.data, SystemInfo)
            or not self.current()
        ):
            return None
        return observation.data

    def current(self) -> bool:
        return (
            not self._closed
            and not self.connection.closed
            and self.context is not None
            and self.context.configuration_current
        )

    def diagnostics(self) -> dict[str, object]:
        """Describe ages and discovery without values, credentials or device IDs."""
        state = (
            self.context.runtime.local_state(self.binding_id)
            if self.context is not None and self.current()
            else None
        )
        snapshot = (
            self.service.snapshot()
            if self.service is not None and self.current()
            else None
        )
        reader = getattr(self.client, "health", None)
        health = (
            reader()
            if callable(reader)
            else {
                "failures_by_class": {},
                "response_times": {"samples": 0},
                "deferred_reasons": {},
            }
        )
        reuse = getattr(self.client, "connection_reuse", None)
        return {
            "condition": state.condition.value if state else "unavailable",
            "identity_confirmed": state.identity_confirmed if state else False,
            "problem": state.problem if state else None,
            "request_metrics": asdict(self.client.metrics)
            if self.client is not None
            else {},
            # O93: bounded history and counts, fixed codes only.
            "condition_changes": list(self._changes),
            **health,
            # G4/O56: kept connections, handshakes, fallback and overlap.
            "connection_reuse": reuse() if callable(reuse) else None,
            "discovery_cursor": snapshot.discovery_cursor if snapshot else 0,
            "discovery_complete": snapshot.discovery_exhausted if snapshot else False,
            "discovery_pause_seconds": snapshot.discovery_pause_seconds
            if snapshot
            else 0,
            "gateway_pause_seconds": snapshot.gateway_pause_seconds if snapshot else 0,
            "resources": [
                {
                    "path": v.request.definition.template
                    if v.request.path.startswith(("/devices/", "/signals/"))
                    else v.request.path,
                    # The signal list has no expiry of its own: once read it
                    # is "read", not "unconfirmed".
                    "freshness": "read"
                    if v.freshness is Freshness.UNCONFIRMED
                    and v.observation is not None
                    else v.freshness.value,
                    "available": v.available,
                    "age_seconds": v.age_seconds,
                    "target_interval_seconds": v.target_interval_seconds,
                    "actual_interval_seconds": v.actual_interval_seconds,
                    "problem": v.problem,
                }
                for v in snapshot.resources
            ]
            if snapshot
            else [],
        }

    def _note(self, condition: LocalCondition, reason: str | None) -> None:
        """Keep a condition change for the diagnostics (O93); a recovery after
        a pause or outage is logged once with its duration (O80)."""
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
        if condition in _DOWN:
            if self._down_since is None:
                self._down_since = now
        elif condition is LocalCondition.READY and self._down_since is not None:
            _LOGGER.info(
                "The local access is available again after %d s",
                round(now - self._down_since),
            )
            self._down_since = None

    def _state(
        self,
        condition: LocalCondition,
        problem: str | None = None,
        *,
        reason: str | None = None,
    ) -> None:
        if self.context is not None and self.current():
            self._note(condition, problem or reason)
            self.context.publish_local(
                LocalSourceState(
                    self.context.identity_context(GatewayNamespace.K40_LOCAL),
                    condition,
                    problem=problem,
                )
            )
            self._certificate_issue(condition)

    def _certificate_issue(self, condition: LocalCondition) -> None:
        """A changed gateway certificate is never accepted silently (P39): a
        repair note points to the setup, which asks for confirmation."""
        from homeassistant.helpers import issue_registry as ir

        from .const import DOMAIN
        from .repair_scope import repair_data

        factory = self.connection.factory
        issue_id = self._certificate_issue_id()
        if condition is LocalCondition.CERTIFICATE_CHANGED:
            ir.async_create_issue(
                factory.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="local_certificate_changed",
                data=repair_data(
                    factory.entry, self.connection.identifier, self.binding_id
                ),
            )
        elif condition is LocalCondition.READY:
            ir.async_delete_issue(factory.hass, DOMAIN, issue_id)

    def _certificate_issue_id(self) -> str:
        from .repair_scope import repair_id

        return repair_id(
            "local_certificate_changed_",
            self.connection.factory.entry,
            self.connection.identifier,
            self.binding_id,
        )

    def _publish(self, snapshot: PollSnapshot) -> None:
        if self.context is None or not self.current():
            return
        service = self.service
        self._note(
            snapshot.condition,
            snapshot.problem
            or (
                getattr(service.budget, "pause_reason", None)
                if service is not None and snapshot.condition is LocalCondition.PAUSED
                else None
            ),
        )
        self._certificate_issue(snapshot.condition)
        self.context.publish_local(
            LocalSourceState(
                self.context.identity_context(GatewayNamespace.K40_LOCAL),
                snapshot.condition,
                snapshot.identity_confirmed,
                tuple(
                    v.observation
                    for v in snapshot.resources
                    if v.observation is not None
                ),
                resources=snapshot.resources,
                problem=snapshot.problem,
            )
        )

    async def _verify(
        self,
        access: StoredLocalAccess,
        context: ProbeContext,
        pin: CertificateBinding,
        budget: LocalReadBudget,
    ) -> bool:
        failures = 0
        while self.current():
            if budget.pause_seconds:
                self._state(
                    LocalCondition.PAUSED, reason=getattr(budget, "pause_reason", None)
                )
                await asyncio.sleep(budget.pause_seconds)
                continue
            self._state(LocalCondition.INITIALIZING)
            exchange = await read_local_probe(
                context,
                access.credentials.token,
                pin,
                is_current=self.current,
                budget=budget,
            )
            if not self.current():
                raise StaleLocalRequest()
            assessment = assess_local_probe(exchange.responses, current_context=context)
            if assessment.status is ProbeStatus.READY_FOR_IDENTITY_COMPARISON:
                if (
                    assessment.identity is None
                    or assessment.identity.value != access.gateway_id
                ):
                    self._state(LocalCondition.UNAVAILABLE, "local_identity_conflict")
                    return False
                await budget.bind_gateway(access.gateway_id)
                return True
            problem = assessment.issues[0].problem
            if problem is ProbeProblem.AUTH_REJECTED:
                self._state(LocalCondition.AUTH_REQUIRED, "auth_required")
                return False
            if problem is ProbeProblem.TLS_CHANGED:
                self._state(LocalCondition.CERTIFICATE_CHANGED, "certificate_changed")
                return False
            if problem not in {
                ProbeProblem.CONNECT,
                ProbeProblem.DNS,
                ProbeProblem.TIMEOUT,
                ProbeProblem.RATE_LIMITED,
                ProbeProblem.SERVICE_UNAVAILABLE,
            }:
                self._state(LocalCondition.UNAVAILABLE, problem.value)
                return False
            delay = exchange.retry_after_seconds
            if delay is None:
                delay = (60, 120, 300, 600)[min(failures, 3)]
                failures += 1
            await budget.pause(
                delay,
                "rate_limited"
                if problem is ProbeProblem.RATE_LIMITED
                else "connection",
            )
        return False

    def _paired_cap(
        self, context: SourceContext
    ) -> Callable[[ReadRequest], int | None]:
        """Cloud interval of paired readings, from current options and cloud plan.

        A changed cloud polling profile or plan takes effect with the next due
        time, without restarting the local source.
        """
        runtime, target = context.runtime, self.binding.target_id
        parsed: list[object] = [None, None]

        def scheduled(binding: str, path: str) -> tuple[bool, timedelta | None]:
            lookup = getattr(runtime.source_data(binding), "scheduled_interval", None)
            if lookup is None:
                # The cloud source is not running: no cloud pace to keep up with.
                return True, None
            return cast(tuple[bool, timedelta | None], lookup(path))

        def cap(request: ReadRequest) -> int | None:
            snapshot = runtime.snapshot
            if snapshot is not parsed[0]:
                parsed[0], parsed[1] = snapshot, snapshot.to_options()
            return paired_cloud_interval(
                snapshot,
                target,
                request.path,
                options=cast(dict[str, Any], parsed[1]),
                scheduled=scheduled,
            )

        return cap

    async def run(self, context: SourceContext) -> None:
        self.context = context
        failures = 0
        while True:
            try:
                await self._start(context)
            except LocalBudgetError:
                # The stored pauses could not be read: try again after a
                # bounded, growing pause instead of stopping until a reload.
                self._state(LocalCondition.UNAVAILABLE, "local_budget_storage_failed")
                await self._reset()
                await asyncio.sleep(START_RETRY[min(failures, len(START_RETRY) - 1)])
                failures += 1
                if not self.current():
                    return
                continue
            except StaleLocalRequest:
                return
            break
        # Pause/auth/identity failures remain available for settings and recovery.
        # A changed connection replaces this source through the normal lifecycle.
        await asyncio.Future[None]()

    async def _reset(self) -> None:
        """Close a half-started service or client before the next start."""
        service, client, self.service, self.client = (
            self.service,
            self.client,
            None,
            None,
        )
        if service is not None:
            await service.close()
        elif client is not None:
            await client.close()

    async def _start(self, context: SourceContext) -> None:
        self._state(LocalCondition.INITIALIZING)
        try:
            access = load_local_access(self.connection.record.to_data())
            locator = cast(dict[str, Any], self.binding.to_data()["locator"])
            if (
                locator["namespace"] != GatewayNamespace.K40_LOCAL
                or locator["value"] != access.gateway_id
            ):
                raise LocalCredentialError("local_identity_conflict")
            probe = ProbeContext(
                context.identity_context(GatewayNamespace.K40_LOCAL), access.endpoint
            )
            budget = await local_budget_manager(self.connection.factory.hass).budget(
                probe, target_id=self.binding.target_id, gateway_id=access.gateway_id
            )
            pin = CertificateBinding(
                probe, PortRole.RESOURCES, bytes.fromhex(access.resource_sha256)
            )
            if await self._verify(access, probe, pin, budget):
                if not self.current():
                    raise StaleLocalRequest()
                options = context.runtime.snapshot.to_options()
                settings = cast(dict[str, dict[str, Any]], options["connections"])[
                    self.connection.identifier
                ]
                interval = validate_interval(settings.get("local_interval", 60))
                self.client = LocalReadClient(
                    probe,
                    access.credentials.token,
                    pin,
                    budget=budget,
                    is_current=self.current,
                )
                self.service = LocalPollingService(
                    probe,
                    _IdentityClient(self.client, access.gateway_id),
                    budget,
                    LocalScheduleStore(
                        self.connection.factory.hass,
                        context.runtime.snapshot.owner_id
                        + ":"
                        + self.binding.target_id
                        + ":"
                        + self.binding_id,
                    ),
                    is_current=self.current,
                    publish=self._publish,
                    interval=interval,
                    cap=self._paired_cap(context),
                    access_key=_access_key(context, self.binding.connection_id),
                )
                self.service.confirm_identity(probe.identity)
                await self.service.start()
        except LocalCredentialError:
            self._state(LocalCondition.AUTH_REQUIRED, "local_revalidation_required")

    async def close(self) -> None:
        self._closed = True
        if self.binding_id not in self.connection.factory.runtime().snapshot.bindings:
            # The access is gone: its certificate note goes with it (P39).
            from homeassistant.helpers import issue_registry as ir

            from .const import DOMAIN

            ir.async_delete_issue(
                self.connection.factory.hass, DOMAIN, self._certificate_issue_id()
            )
        if self.service is not None:
            await self.service.close()
        elif self.client is not None:
            await self.client.close()


def _access_key(context: SourceContext, connection_id: str) -> str | None:
    """Sign-in and settings generation of the access (P41), without secrets."""
    record = context.runtime.snapshot.connections.get(connection_id)
    epoch = getattr(record, "auth_epoch", None)
    revision = getattr(record, "config_revision", None)
    if not isinstance(epoch, int) or not isinstance(revision, int):
        return None
    return f"{epoch}:{revision}"
