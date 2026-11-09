"""Persist pending token creation before HTTP and resume without creating again."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from time import time
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from homeassistant.core import CoreState
from homeassistant.util.file import write_utf8_file_atomic

from .configuration import (
    ConfigurationError,
    ConfigurationSnapshot,
    load_configuration,
    require_identifier,
)
from .const import DOMAIN
from .gateway_identity import GatewayNamespace, IdentityContext
from .local_api.json import unique_object
from .local_auth import (
    AuthOutcome,
    AuthProblem,
    AuthState,
    LabelCredentials,
    request_local_token,
)
from .local_budget import (
    MAX_LOCAL_PAUSE_SECONDS,
    LocalReadBudget,
    local_budget_manager,
)
from .local_completion import (
    CompletionIntent,
    RecoveryStatus,
    ScopedIdentityContract,
    local_state_fingerprint,
)
from .local_credentials import (
    LocalCredentials,
    TokenOrigin,
    import_local_token,
    validate_local_token,
)
from .local_probe import LocalEndpoint, ProbeContext
from .local_setup import (
    LocalSetupEdit,
    VerifiedLocalSetup,
    begin_local_setup,
    commit_local_setup,
    prepare_local_configuration,
)
from .local_transport import CertificateBinding, PortRole

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .configuration_management import ConfigurationManagement, ConnectionEdit

MAX_JOURNAL_BYTES = 1024 * 1024
MAX_PENDING_ENROLLMENTS = 64
JOURNAL_KEY = f"{DOMAIN}.local_enrollment"
PROCESS_ID = uuid4().hex
_LOGGER = logging.getLogger(__name__)


class LocalEnrollmentError(ValueError):
    """Only stable codes cross the storage and setup boundary."""


def _fingerprint(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise LocalEnrollmentError("invalid_enrollment_record")
    return value


def _timestamp(value: object) -> float:
    if type(value) not in (int, float):
        raise LocalEnrollmentError("invalid_enrollment_record")
    result = float(cast(int | float, value))
    if not math.isfinite(result) or result < 0:
        raise LocalEnrollmentError("invalid_enrollment_record")
    return result


@dataclass(frozen=True, slots=True)
class PendingEnrollment:
    """A private historical record, never a live proof or token ownership claim."""

    attempt_id: str = field(repr=False)
    owner_id: str = field(repr=False)
    target_id: str = field(repr=False)
    replacing_connection_id: str | None = field(repr=False)
    endpoint: LocalEndpoint = field(repr=False)
    basis_sha256: str = field(repr=False)
    authentication_sha256: str | None = field(repr=False)
    started_at: float = field(repr=False)
    state: AuthState = AuthState.UNCERTAIN
    credentials: LocalCredentials | None = field(default=None, repr=False)
    problem: AuthProblem | None = AuthProblem.RESPONSE_UNCERTAIN
    retry_not_before: float = field(default=0, repr=False)
    resolved: bool = False
    completion: CompletionIntent | None = field(default=None, repr=False)
    pause_migrated: bool = True

    def to_data(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "owner_id": self.owner_id,
            "target_id": self.target_id,
            "replacing_connection_id": self.replacing_connection_id,
            "host": self.endpoint.host,
            "basis_sha256": self.basis_sha256,
            "authentication_sha256": self.authentication_sha256,
            "started_at": self.started_at,
            "state": self.state.value,
            "credentials": self.credentials.to_data() if self.credentials else None,
            "problem": self.problem.value if self.problem else None,
            "retry_not_before": self.retry_not_before,
            "resolved": self.resolved,
            "completion": self.completion.to_data() if self.completion else None,
            "pause_migrated": self.pause_migrated,
        }


def _record(raw: object, *, version: int = 3) -> PendingEnrollment:
    fields = {
        "attempt_id",
        "owner_id",
        "target_id",
        "replacing_connection_id",
        "host",
        "basis_sha256",
        "authentication_sha256",
        "started_at",
        "state",
        "credentials",
        "problem",
        "retry_not_before",
        "resolved",
    }
    if version >= 2:
        fields.add("completion")
    if version >= 3:
        fields.add("pause_migrated")
    if not isinstance(raw, dict) or set(raw) != fields:
        raise LocalEnrollmentError("invalid_enrollment_record")
    if version >= 3 and type(raw["pause_migrated"]) is not bool:
        raise LocalEnrollmentError("invalid_enrollment_record")
    state = AuthState(raw["state"])
    problem = AuthProblem(raw["problem"]) if raw["problem"] is not None else None
    if type(raw["resolved"]) is not bool:
        raise LocalEnrollmentError("invalid_enrollment_record")
    credentials = None
    if state == AuthState.RECEIVED and not raw["resolved"]:
        value = raw["credentials"]
        if (
            not isinstance(value, dict)
            or set(value) != {"token", "token_type", "origin", "reported_scope"}
            or value["token_type"] != "Bearer"
            or (version == 1 and value["origin"] != TokenOrigin.CREATED)
        ):
            raise LocalEnrollmentError("invalid_enrollment_record")
        scopes = value["reported_scope"]
        if scopes is not None and (
            not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes)
        ):
            raise LocalEnrollmentError("invalid_enrollment_record")
        credentials = LocalCredentials(
            validate_local_token(value["token"]),
            TokenOrigin(value["origin"]),
            tuple(scopes) if scopes is not None else None,
        )
    elif raw["credentials"] is not None:
        raise LocalEnrollmentError("invalid_enrollment_record")
    if (state == AuthState.RECEIVED) != (problem is None):
        raise LocalEnrollmentError("invalid_enrollment_record")
    authentication = raw["authentication_sha256"]
    if authentication is not None:
        _fingerprint(authentication)
    elif version == 1 or (
        credentials is not None and credentials.origin == TokenOrigin.CREATED
    ):
        raise LocalEnrollmentError("invalid_enrollment_record")
    completion = raw.get("completion")
    if completion is not None and state != AuthState.RECEIVED:
        raise LocalEnrollmentError("invalid_enrollment_record")
    return PendingEnrollment(
        require_identifier(raw["attempt_id"]),
        require_identifier(raw["owner_id"]),
        require_identifier(raw["target_id"]),
        require_identifier(raw["replacing_connection_id"])
        if raw["replacing_connection_id"] is not None
        else None,
        LocalEndpoint(raw["host"]),
        _fingerprint(raw["basis_sha256"]),
        authentication,
        _timestamp(raw["started_at"]),
        state,
        credentials,
        problem,
        _timestamp(raw["retry_not_before"]),
        raw["resolved"],
        CompletionIntent.from_data(completion) if completion is not None else None,
        raw["pause_migrated"] if version >= 3 else False,
    )


def _read(path: Path) -> bytes | None:
    try:
        with path.open("rb") as stream:
            return stream.read(MAX_JOURNAL_BYTES + 1)
    except FileNotFoundError:
        return None


def _load(path: Path) -> tuple[PendingEnrollment, ...]:
    body = _read(path)
    return () if body is None else _parse(body)


def _parse(body: bytes) -> tuple[PendingEnrollment, ...]:
    if len(body) > MAX_JOURNAL_BYTES:
        raise LocalEnrollmentError("invalid_enrollment_journal")
    raw = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object)
    if (
        not isinstance(raw, dict)
        or set(raw) != {"version", "attempts"}
        or type(raw["version"]) is not int
        or raw["version"] not in (1, 2, 3)
        or not isinstance(raw["attempts"], list)
        or len(raw["attempts"]) > MAX_PENDING_ENROLLMENTS
    ):
        raise LocalEnrollmentError("invalid_enrollment_journal")
    entries = tuple(_record(v, version=raw["version"]) for v in raw["attempts"])
    if len({v.attempt_id for v in entries}) != len(entries):
        raise LocalEnrollmentError("invalid_enrollment_journal")
    return entries


def _set_aside(path: Path) -> bool:
    """Move a journal with unreadable content aside (FLOW-3).

    False when its content is valid or the file cannot be read at all: then
    the original error stands.
    """
    try:
        _load(path)
    except OSError:
        return False
    except ValueError, KeyError, TypeError, RecursionError:
        path.replace(path.with_name(path.name + ".corrupt"))
        return True
    return False


def _save(path: Path, entries: tuple[PendingEnrollment, ...]) -> None:
    rows = [r.to_data() for r in entries]
    data = {"version": 3, "attempts": rows}
    # Serialized once and compact; files written before with indentation
    # load the same way. Non-finite numbers are refused.
    body = json.dumps(data, allow_nan=False, separators=(",", ":")).encode()
    if len(entries) > MAX_PENDING_ENROLLMENTS or len(body) > MAX_JOURNAL_BYTES:
        raise LocalEnrollmentError("enrollment_journal_full")
    if tuple(_record(row) for row in rows) != entries or _parse(body) != entries:
        raise LocalEnrollmentError("invalid_enrollment_journal")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unlike Store.async_save, this helper propagates write failures. Its atomic
    # replacement is private. A successful call is not a power-loss guarantee.
    write_utf8_file_atomic(str(path), body, True, mode="wb")
    if _read(path) != body:
        raise LocalEnrollmentError("enrollment_storage_unconfirmed")


@dataclass(frozen=True, slots=True)
class ResumedEnrollment:
    edit: LocalSetupEdit = field(repr=False)
    credentials: LocalCredentials = field(repr=False)
    authentication_binding: CertificateBinding | None = field(repr=False)


class LocalEnrollmentJournal:
    """One instance per HA process serializes setup, disk writes and cancellation.

    Every operation reloads the file; a failed final save cannot leave a cached
    success. Only explicit verified completion can change a config entry.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._path = Path(hass.config.path(".storage", JOURNAL_KEY))
        self._lock = asyncio.Lock()
        self._removed_owners: set[str] = set()
        self._removed_connections: set[tuple[str, str]] = set()
        self._startup_seen = False
        self._mutated = False

    def _owner_active(self, owner_id: str, connection_id: str | None = None) -> None:
        if (
            owner_id in self._removed_owners
            or (owner_id, connection_id) in self._removed_connections
        ):
            raise LocalEnrollmentError("enrollment_owner_removed")

    async def initialize(self) -> None:
        """Capture HA's first loaded state before this journal performs any edits.

        A same-process reload or late setup is not evidence of disk persistence.
        Corruption is reported to the caller without affecting cloud setup.
        """
        if self._startup_seen:
            return
        self._startup_seen = True
        eligible = not self._mutated and self._hass.state in (
            CoreState.not_running,
            CoreState.starting,
        )
        if not eligible:
            return
        initial: dict[str, ConfigurationSnapshot | None] = {}
        for entry in self._hass.config_entries.async_entries(DOMAIN):
            try:
                initial[entry.entry_id] = load_configuration(
                    entry.entry_id,
                    entry.data,
                    entry.options,
                    version=entry.version,
                    minor_version=entry.minor_version,
                )
            except ConfigurationError:
                initial[entry.entry_id] = None
        async with self._lock:
            entries = await self._load_records()
            retained = []
            for record in entries:
                if record.owner_id not in initial:
                    self._removed_owners.add(record.owner_id)
                    continue
                snapshot = initial[record.owner_id]
                if (
                    not record.resolved
                    and record.completion is not None
                    and record.completion.process_id != PROCESS_ID
                    and snapshot is not None
                    and record.completion.matches(snapshot, record.target_id)
                ):
                    record = replace(record, resolved=True, credentials=None)
                retained.append(record)
            if tuple(retained) != entries:
                await self._io(lambda: _save(self._path, tuple(retained)))

    async def _io[T](self, operation: Callable[[], T]) -> T:
        # Executor writes cannot be cancelled safely. Keep the lock until the
        # actual operation has finished, including repeated caller cancellation.
        future = self._hass.async_add_executor_job(operation)
        cancelled = False
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        if cancelled:
            if not future.cancelled():
                future.exception()
            raise asyncio.CancelledError
        try:
            return future.result()
        except Exception:
            raise LocalEnrollmentError("local_enrollment_storage_failed") from None

    async def _budget(self, record: PendingEnrollment) -> LocalReadBudget:
        context = ProbeContext(
            IdentityContext(
                GatewayNamespace.K40_LOCAL,
                record.owner_id,
                record.replacing_connection_id or record.attempt_id,
                0,
            ),
            record.endpoint,
        )
        return await local_budget_manager(self._hass).budget(
            context, target_id=record.target_id
        )

    async def _load_records(self) -> tuple[PendingEnrollment, ...]:
        entries = await self._io(lambda: _load(self._path))
        if all(record.pause_migrated for record in entries):
            return entries
        migrated = []
        for record in entries:
            if not record.pause_migrated:
                if record.retry_not_before:
                    # Old UTC-only records cannot prove elapsed time after a
                    # clock jump. Restore the full conservative original span.
                    budget = await self._budget(record)
                    await budget.pause(
                        max(0, record.retry_not_before - record.started_at), "legacy"
                    )
                record = replace(record, pause_migrated=True)
            migrated.append(record)
        result = tuple(migrated)
        await self._io(lambda: _save(self._path, result))
        return result

    async def remaining_pause(self, record: PendingEnrollment) -> float:
        return (await self._budget(record)).pause_seconds

    async def records(self) -> tuple[PendingEnrollment, ...]:
        async with self._lock:
            entries = await self._load_records()
            projected = []
            for record in entries:
                remaining = await self.remaining_pause(record)
                projected.append(
                    replace(
                        record, retry_not_before=time() + remaining if remaining else 0
                    )
                )
            return tuple(projected)

    async def defer(self, attempt_id: str, owner_id: str, seconds: float) -> None:
        """Retain resource Retry-After as well as token-endpoint pauses."""
        seconds = min(_timestamp(seconds), MAX_LOCAL_PAUSE_SECONDS)
        self._mutated = True
        async with self._lock:
            entries = await self._load_records()
            record = self._find(entries, attempt_id, owner_id)
            self._owner_active(owner_id, record.replacing_connection_id)
            budget = await self._budget(record)
            await budget.pause(seconds, "rate_limited")
            updated = replace(record, retry_not_before=time() + budget.pause_seconds)
            await self._io(
                lambda: _save(
                    self._path, tuple(updated if r == record else r for r in entries)
                )
            )

    async def create(
        self,
        edit: LocalSetupEdit,
        label: LabelCredentials,
        authentication_binding: CertificateBinding,
        *,
        current: Callable[[], ConfigurationSnapshot],
        is_active: Callable[[], bool],
    ) -> PendingEnrollment:
        """A user-requested creation. An unresolved attempt always blocks another."""
        if (
            authentication_binding.context != edit.context
            or authentication_binding.role != PortRole.AUTHENTICATION
        ):
            raise LocalEnrollmentError("authentication_binding_required")
        self._mutated = True
        self._owner_active(
            edit.context.identity.owner_id, edit.context.identity.connection_id
        )

        def active() -> bool:
            self._owner_active(
                edit.context.identity.owner_id, edit.context.identity.connection_id
            )
            if not is_active():
                return False
            try:
                edit.check(current())
            except ConfigurationError:
                return False
            return True

        async with self._lock:
            entries = await self._load_records()
            if not active():
                raise LocalEnrollmentError("enrollment_context_changed")
            now = time()
            for previous in entries:
                if (
                    previous.owner_id == edit.context.identity.owner_id
                    and previous.target_id == edit.target_id
                ) or previous.endpoint == edit.context.endpoint:
                    if await self.remaining_pause(previous):
                        raise LocalEnrollmentError("enrollment_retry_later")
                    if not previous.resolved:
                        raise LocalEnrollmentError("enrollment_already_pending")
            entries = tuple(r for r in entries if not r.resolved)
            record = PendingEnrollment(
                uuid4().hex,
                edit.context.identity.owner_id,
                edit.target_id,
                edit.context.identity.connection_id
                if edit.context.identity.connection_id in current().connections
                else None,
                edit.context.endpoint,
                edit.recovery_fingerprint(),
                authentication_binding.sha256.hex(),
                now,
            )
            await self._io(lambda: _save(self._path, (*entries, record)))

            updated: PendingEnrollment | None = None

            async def persist(outcome: AuthOutcome) -> None:
                nonlocal updated
                self._owner_active(record.owner_id, record.replacing_connection_id)
                received = replace(
                    record,
                    state=outcome.state,
                    credentials=outcome.credentials,
                    problem=outcome.problem,
                    retry_not_before=time()
                    + min(outcome.retry_after_seconds, MAX_LOCAL_PAUSE_SECONDS)
                    if outcome.retry_after_seconds is not None
                    else 0,
                    # FLOW-5: a rejected or unsent request certainly created no
                    # token, so it never blocks the next attempt. Its pause
                    # stays in the budget; an uncertain outcome still blocks.
                    resolved=outcome.state in (AuthState.REJECTED, AuthState.NOT_SENT),
                )
                await self._io(lambda: _save(self._path, (*entries, received)))
                if (
                    outcome.retry_after_seconds is not None
                    or outcome.problem == AuthProblem.RATE_LIMITED
                ):
                    await budget.pause(
                        outcome.retry_after_seconds
                        if outcome.retry_after_seconds is not None
                        else 60,
                        "rate_limited",
                    )
                self._owner_active(record.owner_id, record.replacing_connection_id)
                updated = received

            budget = await local_budget_manager(self._hass).budget(
                edit.context, target_id=edit.target_id
            )
            outcome: AuthOutcome = await request_local_token(
                edit.context,
                label,
                authentication_binding,
                is_current=active,
                on_response=persist,
                budget=budget,
            )
            if updated is None:
                await persist(outcome)
            assert updated is not None
            return updated

    async def import_token(
        self,
        edit: LocalSetupEdit,
        token: object,
        *,
        current: Callable[[], ConfigurationSnapshot],
        is_active: Callable[[], bool],
    ) -> PendingEnrollment:
        """Retain a supplied token without auth traffic or invented scope."""
        credentials = import_local_token(token)
        self._mutated = True
        async with self._lock:
            entries = await self._load_records()
            self._owner_active(
                edit.context.identity.owner_id, edit.context.identity.connection_id
            )
            edit.check(current())
            if not is_active():
                raise LocalEnrollmentError("enrollment_context_changed")
            now = time()
            for previous in entries:
                if (
                    previous.owner_id == edit.context.identity.owner_id
                    and previous.target_id == edit.target_id
                ) or previous.endpoint == edit.context.endpoint:
                    if await self.remaining_pause(previous):
                        raise LocalEnrollmentError("enrollment_retry_later")
                    if not previous.resolved:
                        raise LocalEnrollmentError("enrollment_already_pending")
            entries = tuple(r for r in entries if not r.resolved)
            record = PendingEnrollment(
                uuid4().hex,
                edit.context.identity.owner_id,
                edit.target_id,
                edit.context.identity.connection_id
                if edit.context.identity.connection_id in current().connections
                else None,
                edit.context.endpoint,
                edit.recovery_fingerprint(),
                None,
                now,
                AuthState.RECEIVED,
                credentials,
                None,
            )
            await self._io(lambda: _save(self._path, (*entries, record)))
            self._owner_active(record.owner_id, record.replacing_connection_id)
            return record

    @staticmethod
    def _resume_edit(
        record: PendingEnrollment, current: ConfigurationSnapshot
    ) -> LocalSetupEdit:
        intent = record.completion
        return begin_local_setup(
            current,
            record.target_id,
            record.endpoint,
            connection_id=record.replacing_connection_id,
            reserved_ids=(intent.connection_id, intent.binding_id)
            if intent and record.replacing_connection_id is None
            else None,
        )

    @staticmethod
    def _status(
        record: PendingEnrollment, current: ConfigurationSnapshot
    ) -> RecoveryStatus:
        if record.resolved:
            return RecoveryStatus.RESOLVED
        if record.state != AuthState.RECEIVED:
            return RecoveryStatus.UNRESOLVED
        if record.completion is not None and record.completion.matches(
            current, record.target_id
        ):
            return RecoveryStatus.APPLIED
        try:
            edit = LocalEnrollmentJournal._resume_edit(record, current)
        except ConfigurationError:
            return RecoveryStatus.CONFLICT
        return (
            RecoveryStatus.READY
            if edit.recovery_fingerprint() == record.basis_sha256
            else RecoveryStatus.CONFLICT
        )

    async def recovery_status(
        self, attempt_id: str, current: ConfigurationSnapshot
    ) -> RecoveryStatus:
        async with self._lock:
            entries = await self._load_records()
            return self._status(
                self._find(entries, attempt_id, current.owner_id), current
            )

    async def resume(
        self,
        attempt_id: str,
        current: ConfigurationSnapshot,
    ) -> ResumedEnrollment:
        """Recover only credentials. The caller must perform a new complete probe."""
        async with self._lock:
            entries = await self._load_records()
            record = self._find(entries, attempt_id, current.owner_id)
            self._owner_active(record.owner_id, record.replacing_connection_id)
            if record.resolved or record.state != AuthState.RECEIVED:
                raise LocalEnrollmentError("enrollment_has_no_recoverable_token")
            if record.completion is not None and record.completion.matches(
                current, record.target_id
            ):
                raise LocalEnrollmentError("enrollment_already_applied")
            edit = self._resume_edit(record, current)
            if edit.recovery_fingerprint() != record.basis_sha256:
                raise LocalEnrollmentError("enrollment_context_changed")
            assert record.credentials is not None
            return ResumedEnrollment(
                edit,
                record.credentials,
                CertificateBinding(
                    edit.context,
                    PortRole.AUTHENTICATION,
                    bytes.fromhex(record.authentication_sha256),
                )
                if record.authentication_sha256
                else None,
            )

    async def commit_verified(
        self,
        attempt_id: str,
        verification: VerifiedLocalSetup,
        management: ConfigurationManagement,
        *,
        contract: ScopedIdentityContract,
    ) -> ConfigurationSnapshot:
        """Journal the local intent before one native HA update, with no replay."""
        from .cloud_configuration import cloud_snapshots

        management.check_local_access_enabled()
        self._mutated = True
        edit = verification.edit
        async with self._lock:
            entries = await self._load_records()
            current = management.current()
            record = self._find(entries, attempt_id, current.owner_id)
            self._owner_active(record.owner_id, edit.context.identity.connection_id)
            if (
                record.resolved
                or record.state != AuthState.RECEIVED
                or record.target_id != edit.target_id
                or record.endpoint != edit.context.endpoint
            ):
                raise LocalEnrollmentError("enrollment_context_changed")
            if self._status(record, current) == RecoveryStatus.APPLIED:
                return current
            if (
                record.basis_sha256 != edit.recovery_fingerprint()
                or record.credentials != verification.access.credentials
                or record.authentication_sha256
                != verification.access.authentication_sha256
            ):
                raise LocalEnrollmentError("enrollment_context_changed")
            if record.completion is not None and (
                record.completion.connection_id != edit.context.identity.connection_id
                or record.completion.binding_id != edit.binding_id
            ):
                raise LocalEnrollmentError("enrollment_context_changed")
            pairs = contract.verified_pairs(
                current, edit.target_id, verification.access.gateway_id
            )
            proposed = prepare_local_configuration(
                edit,
                verification,
                cloud_snapshots(self._hass),
                verified_namespace_pairs=pairs,
                contract_id=contract.contract_id,
            )
            expected = local_state_fingerprint(
                proposed,
                edit.target_id,
                edit.context.identity.connection_id,
                edit.binding_id,
            )
            assert expected is not None
            intent = CompletionIntent(
                edit.context.identity.connection_id,
                edit.binding_id,
                expected,
                PROCESS_ID,
            )
            armed = replace(record, completion=intent)
            await self._io(
                lambda: _save(
                    self._path, tuple(armed if r == record else r for r in entries)
                )
            )
            self._owner_active(record.owner_id, edit.context.identity.connection_id)
            contract.verified_pairs(
                management.current(), edit.target_id, verification.access.gateway_id
            )
            result = commit_local_setup(
                management,
                edit,
                verification,
                verified_namespace_pairs=pairs,
                contract_id=contract.contract_id,
            )
            if not intent.matches(result, edit.target_id):
                raise LocalEnrollmentError("enrollment_completion_uncertain")
            return result

    async def remove_owner(self, owner_id: str) -> None:
        """Mark deletion before waiting; late responses cannot restore secrets."""
        self._mutated = True
        self._removed_owners.add(owner_id)
        await self._purge(lambda record: record.owner_id == owner_id)

    async def remove_connection(self, owner_id: str, connection_id: str) -> None:
        self._mutated = True
        self._removed_connections.add((owner_id, connection_id))
        await self._purge(
            lambda record: (
                record.owner_id == owner_id
                and (
                    record.replacing_connection_id == connection_id
                    or (
                        record.completion is not None
                        and record.completion.connection_id == connection_id
                    )
                )
            )
        )

    async def remove_access(
        self,
        management: ConfigurationManagement,
        edit: ConnectionEdit,
    ) -> ConfigurationSnapshot:
        """Fence pending responses until the native removal has been rechecked."""
        if management.edit(edit.connection_id) != edit:
            raise ConfigurationError("configuration_changed")
        try:
            await self.remove_connection(edit.owner_id, edit.connection_id)
            return management.remove(edit)
        finally:
            self._removed_connections.discard((edit.owner_id, edit.connection_id))

    async def _removable_records(self) -> tuple[PendingEnrollment, ...]:
        """A removal never fails on an unreadable journal (FLOW-3).

        Its pending attempts cannot be used any more anyway; the file is kept
        beside it for inspection, and the next setup starts with an empty one.
        """
        try:
            return await self._load_records()
        except LocalEnrollmentError:
            if not await self._io(lambda: _set_aside(self._path)):
                raise
        _LOGGER.warning(
            "The unreadable local setup record was set aside as %s.corrupt",
            JOURNAL_KEY,
        )
        return ()

    async def _purge(self, matches: Callable[[PendingEnrollment], bool]) -> None:
        async def finish() -> None:
            async with self._lock:
                entries = await self._removable_records()
                retained = tuple(r for r in entries if not matches(r))
                if retained != entries:
                    await self._io(lambda: _save(self._path, retained))

        task = self._hass.async_create_task(finish(), "local enrollment cleanup")
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        if cancelled:
            if not task.cancelled():
                task.exception()
            raise asyncio.CancelledError
        task.result()

    async def discard(
        self,
        attempt_id: str,
        owner_id: str,
        *,
        confirmed: bool = False,
    ) -> None:
        """Explicitly forget a pending credential, without revoking any token.

        The UI must explain that the gateway may retain a token after an unknown
        outcome. Keep Retry-After even when the user acknowledges the attempt.
        Successful config handover must not call this before durable confirmation.
        """
        if confirmed is not True:
            raise LocalEnrollmentError("enrollment_confirmation_required")
        self._mutated = True
        async with self._lock:
            entries = await self._load_records()
            record = self._find(entries, attempt_id, owner_id)
            updated = replace(record, resolved=True, credentials=None)
            await self._io(
                lambda: _save(
                    self._path,
                    tuple(updated if r == record else r for r in entries),
                )
            )

    @staticmethod
    def _find(
        entries: tuple[PendingEnrollment, ...],
        attempt_id: str,
        owner_id: str,
    ) -> PendingEnrollment:
        for record in entries:
            if record.attempt_id == attempt_id and record.owner_id == owner_id:
                return record
        raise LocalEnrollmentError("enrollment_not_found")


def enrollment_journal(hass: HomeAssistant) -> LocalEnrollmentJournal:
    """Use one lock for all owners, including concurrent setup dialogs."""
    if JOURNAL_KEY not in hass.data:
        hass.data[JOURNAL_KEY] = LocalEnrollmentJournal(hass)
    return cast(LocalEnrollmentJournal, hass.data[JOURNAL_KEY])
