"""Shared local admission, wire pacing and conservative persistent pauses."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, cast

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.util.file import write_utf8_file_atomic

from .const import DOMAIN
from .local_api.json import unique_object

if TYPE_CHECKING:
    from homeassistant.core import Event, HomeAssistant

    from .local_probe import ProbeContext

MINIMUM_START_GAP = 0.25
MAXIMUM_PARALLEL = 2
SETUP_WAIT_LIMIT = 120.0
MAX_GROUPS = 256
MAX_ALIASES = 2048
MAX_STORE_BYTES = 1024 * 1024
# Like the cloud's upper limit: a gateway never pauses the local access longer.
MAX_LOCAL_PAUSE_SECONDS = 86400.0
# A failed save of the pauses is tried again after this long.
SAVE_RETRY_SECONDS = 60.0
BUDGET_KEY = f"{DOMAIN}.local_budget"
_SEQUENCE = count()
_REASONS = frozenset(
    {"rate_limited", "service_unavailable", "connection", "legacy", "failure"}
)
_LOGGER = logging.getLogger(__name__)


class LocalBudgetError(ValueError):
    """Fixed storage/capacity codes only; no endpoint or private response text."""


class BudgetDeferred(ValueError):
    def __init__(self, seconds: float = MINIMUM_START_GAP) -> None:
        self.seconds = max(MINIMUM_START_GAP, seconds)
        super().__init__("local_budget_deferred")


def _seconds(value: object) -> float:
    if type(value) not in (int, float):
        raise LocalBudgetError("invalid_local_read_pause")
    result = float(cast(int | float, value))
    if not math.isfinite(result) or result < 0:
        raise LocalBudgetError("invalid_local_read_pause")
    return result


def _pause_seconds(value: object) -> float:
    """A valid pause, at most ``MAX_LOCAL_PAUSE_SECONDS`` long."""
    return min(_seconds(value), MAX_LOCAL_PAUSE_SECONDS)


def _key(kind: str, *values: str) -> str:
    return hashlib.sha256(json.dumps((kind, *values)).encode()).hexdigest()


@dataclass(eq=False, slots=True)
class _State:
    aliases: set[str] = field(default_factory=set)
    leases: set[BudgetLease] = field(default_factory=set)
    parent: _State | None = None
    admission_at: float = 0.0
    http_at: float = 0.0
    pause_until: float = 0.0
    reason: str = "failure"
    exclusive_waiters: int = 0

    def root(self) -> _State:
        state = self
        while state.parent is not None:
            state = state.parent
        if state is not self:
            self.parent = state
        return state


@dataclass(eq=False, slots=True)
class BudgetLease:
    """A bounded admitted operation, including its DNS/TLS and cleanup."""

    state: _State = field(repr=False)
    exclusive: bool = False
    sequence: int = field(default_factory=lambda: next(_SEQUENCE))
    released: bool = False


class LocalReadBudget:
    """One handle per caller, sharing its gateway state through the manager.

    Standalone handles support isolated transport tests. HA callers obtain
    handles from local_budget_manager; replacing a handle never clears a pause.
    """

    def __init__(
        self,
        context: ProbeContext,
        *,
        clock: Callable[[], float] = monotonic,
        manager: LocalBudgetManager | None = None,
        state: _State | None = None,
    ) -> None:
        self._scope = (context.identity.owner_id, context.identity.connection_id)
        self._clock = clock
        self._manager = manager
        self._state = state or _State()

    def check_scope(self, context: ProbeContext) -> None:
        if self._scope != (context.identity.owner_id, context.identity.connection_id):
            raise ValueError("local_read_budget_scope_mismatch")

    def _usable(self) -> None:
        if self._manager is not None:
            self._manager.check_ready()

    @property
    def busy(self) -> bool:
        state = self._state.root()
        return (
            len(state.leases) >= MAXIMUM_PARALLEL
            or any(lease.exclusive for lease in state.leases)
            or state.exclusive_waiters > 0
        )

    @property
    def remaining_seconds(self) -> float:
        state = self._state.root()
        return max(
            0.0,
            max(state.pause_until, state.admission_at, state.http_at) - self._clock(),
        )

    @property
    def pause_seconds(self) -> float:
        return max(0.0, self._state.root().pause_until - self._clock())

    @property
    def pause_reason(self) -> str | None:
        """Why the access pauses now (a fixed code), or None without a pause."""
        return self._state.root().reason if self.pause_seconds else None

    def defer(self, seconds: float) -> None:
        """Standalone compatibility; managed pauses must be awaited and saved."""
        if self._manager is not None:
            raise LocalBudgetError("local_budget_persistence_required")
        seconds = _pause_seconds(seconds)
        state = self._state.root()
        state.pause_until = max(state.pause_until, self._clock() + seconds)

    async def pause(self, seconds: float, reason: str = "failure") -> None:
        seconds = _pause_seconds(seconds)
        if reason not in _REASONS:
            raise LocalBudgetError("invalid_local_pause_reason")
        state = self._state.root()
        deadline = self._clock() + seconds
        if deadline > state.pause_until:
            state.pause_until = deadline
            state.reason = reason
        if self._manager is not None:
            await self._manager.persist()

    async def bind_peers(self, peers: tuple[str, ...]) -> None:
        if self._manager is not None:
            await self._manager.link(self, {_key("endpoint", peer) for peer in peers})

    async def bind_gateway(self, gateway_id: str) -> None:
        """Load grouping only, called after a successful identity assessment."""
        if not gateway_id or len(gateway_id) > 256:
            raise LocalBudgetError("invalid_local_budget_identity")
        if self._manager is not None:
            await self._manager.link(self, {_key("local_gateway", gateway_id)})

    def claim(self, *, exclusive: bool = False) -> BudgetLease | None:
        self._usable()
        state = self._state.root()
        if max(state.pause_until, state.admission_at) > self._clock() or (
            state.leases if exclusive else self.busy
        ):
            return None
        lease = BudgetLease(state, exclusive)
        state.leases.add(lease)
        state.admission_at = self._clock() + MINIMUM_START_GAP
        return lease

    def release(self, lease: BudgetLease) -> None:
        if lease.released:
            return
        lease.released = True
        lease.state.root().leases.discard(lease)

    def check_lease(self, lease: BudgetLease) -> None:
        self._usable()
        state = self._state.root()
        admitted = sorted(state.leases, key=lambda value: value.sequence)
        if (
            lease.released
            or lease.state.root() is not state
            or lease not in admitted[:MAXIMUM_PARALLEL]
            or (lease.exclusive and len(admitted) != 1)
            or any(other.exclusive and other is not lease for other in admitted)
            or self.pause_seconds
        ):
            raise BudgetDeferred(self.remaining_seconds)

    async def start_http(
        self, lease: BudgetLease, is_current: Callable[[], bool]
    ) -> None:
        """Called by the trace before aiohttp serializes or writes headers."""
        while True:
            if not is_current():
                from .local_transport import StaleLocalRequest

                raise StaleLocalRequest()
            self.check_lease(lease)
            state = self._state.root()
            wait = state.http_at - self._clock()
            if wait <= 0:
                state.http_at = self._clock() + MINIMUM_START_GAP
                return
            await asyncio.sleep(wait)

    @asynccontextmanager
    async def slot(
        self,
        *,
        is_current: Callable[[], bool],
        exclusive: bool = True,
        active_reserve: float = 0.0,
    ) -> AsyncIterator[BudgetLease]:
        """A setup operation waits boundedly; normal readers never queue here."""
        lease = None
        state = self._state.root()
        if exclusive:
            if state.exclusive_waiters >= 8:
                raise BudgetDeferred()
            state.exclusive_waiters += 1
        try:
            async with asyncio.timeout(
                max(0.0, SETUP_WAIT_LIMIT - _seconds(active_reserve))
            ):
                while lease is None:
                    if not is_current():
                        from .local_transport import StaleLocalRequest

                        raise StaleLocalRequest()
                    if self.pause_seconds:
                        raise BudgetDeferred(self.pause_seconds)
                    lease = self.claim(exclusive=exclusive)
                    if lease is None:
                        await asyncio.sleep(
                            max(0.01, min(self.remaining_seconds, 0.25))
                        )
            yield lease
        finally:
            if exclusive:
                state.root().exclusive_waiters -= 1
            if lease is not None:
                self.release(lease)


def _read(path: Path) -> bytes | None:
    try:
        with path.open("rb") as stream:
            return stream.read(MAX_STORE_BYTES + 1)
    except FileNotFoundError:
        return None


def _load(path: Path) -> list[dict[str, object]]:
    body = _read(path)
    return [] if body is None else _parse(body)


def _parse(body: bytes) -> list[dict[str, object]]:
    if len(body) > MAX_STORE_BYTES:
        raise LocalBudgetError("invalid_local_budget_store")
    data = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object)
    if (
        not isinstance(data, dict)
        or set(data) != {"version", "pauses"}
        or type(data["version"]) is not int
        or data["version"] != 1
        or not isinstance(data["pauses"], list)
        or len(data["pauses"]) > MAX_GROUPS
    ):
        raise LocalBudgetError("invalid_local_budget_store")
    seen: set[str] = set()
    for row in data["pauses"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"aliases", "remaining", "reason"}
            or not isinstance(row["aliases"], list)
            or not row["aliases"]
            or not isinstance(row["reason"], str)
            or row["reason"] not in _REASONS
        ):
            raise LocalBudgetError("invalid_local_budget_store")
        for alias in row["aliases"]:
            if (
                not isinstance(alias, str)
                or re.fullmatch(r"[0-9a-f]{64}", alias) is None
                or alias in seen
            ):
                raise LocalBudgetError("invalid_local_budget_store")
            seen.add(alias)
        _seconds(row["remaining"])
    if len(seen) > MAX_ALIASES:
        raise LocalBudgetError("invalid_local_budget_store")
    return cast(list[dict[str, object]], data["pauses"])


def _save(path: Path, rows: list[dict[str, object]]) -> None:
    # Serialized once and compact; the produced bytes are checked before and
    # after writing. Files written before with indentation load the same way.
    try:
        body = json.dumps(
            {"version": 1, "pauses": rows}, allow_nan=False, separators=(",", ":")
        ).encode()
    except ValueError:
        raise LocalBudgetError("invalid_local_read_pause") from None
    if _parse(body) != rows:
        raise LocalBudgetError("local_budget_storage_unconfirmed")
    path.parent.mkdir(parents=True, exist_ok=True)
    write_utf8_file_atomic(str(path), body, True, mode="wb")
    if _read(path) != body:
        raise LocalBudgetError("local_budget_storage_unconfirmed")


class LocalBudgetManager:
    """One HA-wide owner; endpoint aliases and verified IDs share load limits."""

    def __init__(
        self, hass: HomeAssistant, *, clock: Callable[[], float] = monotonic
    ) -> None:
        self._hass = hass
        self._clock = clock
        self._path = Path(hass.config.path(".storage", BUDGET_KEY))
        self._lock = asyncio.Lock()
        self._aliases: dict[str, _State] = {}
        self._loaded = False
        self._closed = False
        # A pause that could not be saved yet: kept in memory and saved later.
        self._unsaved = False
        self._save_warned = False
        self._saving = 0
        self._checkpoint: asyncio.TimerHandle | None = None
        self._stop_unsub: Callable[[], None] | None = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._stopping
        )

    def check_ready(self, *, allow_saving: bool = False) -> None:
        if not self._loaded or self._closed:
            raise LocalBudgetError("local_budget_storage_failed")
        if self._saving and not allow_saving:
            raise BudgetDeferred()

    async def _io[T](self, operation: Callable[[], T]) -> T:
        future = self._hass.async_add_executor_job(operation)
        cancelled = False
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        try:
            value = future.result()
        except Exception:
            raise LocalBudgetError("local_budget_storage_failed") from None
        if cancelled:
            raise asyncio.CancelledError
        return value

    async def initialize(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            rows = await self._io(lambda: _load(self._path))
            for row in rows:
                aliases = set(cast(list[str], row["aliases"]))
                state = _State(
                    aliases,
                    # Never longer than the limit, also from an older file.
                    pause_until=self._clock() + _pause_seconds(row["remaining"]),
                    reason=cast(str, row["reason"]),
                )
                for alias in aliases:
                    self._aliases[alias] = state
            self._loaded = True
            self._schedule_checkpoint()

    async def budget(
        self,
        context: ProbeContext,
        *,
        target_id: str | None = None,
        gateway_id: str | None = None,
    ) -> LocalReadBudget:
        await self.initialize()
        self.check_ready(allow_saving=True)
        aliases = {
            _key("endpoint", context.endpoint.host),
            _key(
                "connection", context.identity.owner_id, context.identity.connection_id
            ),
        }
        if target_id:
            aliases.add(_key("target", context.identity.owner_id, target_id))
        if gateway_id:
            aliases.add(_key("local_gateway", gateway_id))
        handle = LocalReadBudget(context, clock=self._clock, manager=self)
        await self.link(handle, aliases)
        return handle

    async def link(self, handle: LocalReadBudget, aliases: set[str]) -> None:
        self.check_ready(allow_saving=True)
        roots = {self._aliases[a].root() for a in aliases if a in self._aliases}
        roots.add(handle._state.root())
        all_aliases = aliases.union(*(root.aliases for root in roots))
        if len(set(self._aliases) | all_aliases) > MAX_ALIASES:
            raise LocalBudgetError("local_budget_capacity")
        current_roots = {state.root() for state in self._aliases.values()}
        if len(current_roots - roots) + 1 > MAX_GROUPS:
            raise LocalBudgetError("local_budget_capacity")
        state = next((root for root in roots if root.aliases), handle._state.root())
        changed = (
            not all_aliases <= state.aliases
            or sum(bool(root.aliases) for root in roots) > 1
        )
        for other in roots - {state}:
            state.leases.update(other.leases)
            other.leases.clear()
            state.admission_at = max(state.admission_at, other.admission_at)
            state.http_at = max(state.http_at, other.http_at)
            if other.pause_until > state.pause_until:
                state.pause_until, state.reason = other.pause_until, other.reason
            state.exclusive_waiters += other.exclusive_waiters
            other.parent = state
        state.aliases.update(all_aliases)
        for alias in all_aliases:
            self._aliases[alias] = state
        if changed and state.pause_until > self._clock():
            await self.persist()

    def _snapshot(self) -> list[dict[str, object]]:
        return sorted(
            (
                {
                    "aliases": sorted(state.aliases),
                    "remaining": max(0.0, state.pause_until - self._clock()),
                    "reason": state.reason,
                }
                for state in {s.root() for s in self._aliases.values()}
                if state.pause_until > self._clock()
            ),
            key=lambda row: cast(list[str], row["aliases"])[0],
        )

    async def persist(self) -> None:
        # Block new admission before the first await, including other gateways.
        self._saving += 1
        task = asyncio.create_task(self._persist())
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _persist(self) -> None:
        try:
            async with self._lock:
                rows = self._snapshot()
                try:
                    await self._io(lambda: _save(self._path, rows))
                except LocalBudgetError:
                    # The pauses stay in effect in memory; local reads go on
                    # and the save is tried again later.
                    self._unsaved = True
                    if not self._save_warned:
                        self._save_warned = True
                        _LOGGER.warning(
                            "The pauses of the local access could not be saved; "
                            "they stay in effect and saving is tried again later"
                        )
                else:
                    self._unsaved = False
                    self._save_warned = False
        finally:
            self._saving -= 1
            self._schedule_checkpoint()

    def _schedule_checkpoint(self) -> None:
        if self._checkpoint is not None:
            self._checkpoint.cancel()
            self._checkpoint = None
        if self._closed or not self._loaded:
            return
        if not self._unsaved and not self._snapshot():
            return

        async def checkpoint() -> None:
            with suppress(LocalBudgetError):
                await self.persist()

        self._checkpoint = asyncio.get_running_loop().call_later(
            SAVE_RETRY_SECONDS if self._unsaved else 60,
            lambda: self._hass.async_create_task(
                checkpoint(), "local budget checkpoint"
            ),
        )

    async def _stopping(self, event: Event) -> None:
        self._stop_unsub = None
        await self.close()

    async def close(self) -> None:
        self._closed = True
        if self._stop_unsub is not None:
            self._stop_unsub()
            self._stop_unsub = None
        if self._checkpoint is not None:
            self._checkpoint.cancel()
            self._checkpoint = None
        if self._loaded:
            await self.persist()


def local_budget_manager(hass: HomeAssistant) -> LocalBudgetManager:
    if BUDGET_KEY not in hass.data:
        hass.data[BUDGET_KEY] = LocalBudgetManager(hass)
    return cast(LocalBudgetManager, hass.data[BUDGET_KEY])
