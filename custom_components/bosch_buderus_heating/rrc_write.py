"""Structured RRC writes and the write counters of the RRC contracts.

``RrcStructWrite`` sends one non-scalar write (an event list entry, a boost
structure) exactly once and confirms it by reading (S5 to S7). Its pending
guard keeps a second creation from following one whose outcome is unknown
(the pattern of C36 for holidays). ``RrcWriteStats`` counts the writes of
each contract without values (S12) and the late confirmations of the
regular polls (CT-C07).

Pure: no Home Assistant import. The coordinator owns one instance of each
and serialises every call with polling (``async_run_rrc_operation``).
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic
from typing import Final

import aiohttp

from .pointt import (
    AuthenticationError,
    PointTError,
    ProtocolError,
    RateLimited,
    RequestTimeout,
    Resource,
    ResourceError,
    ServiceUnavailable,
    TransportError,
    UnexpectedHttpStatus,
    WriteValidationError,
)
from .write_policies import RRC_READ_BACK_DELAYS, RrcWriteNotConfirmed
from .write_window import ensure_send_window

# C36: an unresolved write blocks a repetition only this long. After it, a
# fresh preflight decides whether the first one took effect.
PENDING_EXPIRY_SECONDS: Final = 600.0
# CT-C07: how long a regular poll may still confirm an unconfirmed write;
# covers the slowest group of the cloud-friendly profile (30 minutes).
LATE_CONFIRMATION_WINDOW_SECONDS: Final = 3600.0
# The bucket of RRC writes outside any contract (zone mode, manual
# temperature and program selection of phase 1).
CORE_CONTRACT: Final = "core"
COUNTERS: Final = (
    "writes_sent",
    "writes_confirmed",
    "writes_unconfirmed",
    "late_confirmed",
    "writes_rejected",
)


class RrcWritePending(WriteValidationError):
    """An earlier write of the same kind is still unresolved; nothing was sent.

    ``translation_key`` names the error shown (the caller's choice).
    """

    def __init__(self, reason: str = "rrc_write_pending_confirmation") -> None:
        self.reason = reason
        self.translation_key = reason
        super().__init__("An earlier change is not confirmed yet; nothing was sent")


@dataclass(frozen=True, slots=True)
class _Pending:
    # The unresolved write's own state and read-back, not the next one's.
    confirm: Callable[[], Awaitable[object]]
    sent_at: float


def never_sent(err: BaseException) -> bool:
    """C36: whether the connection failed before any request was sent."""
    return isinstance(
        err.__cause__, aiohttp.ClientConnectorError | aiohttp.ConnectionTimeoutError
    )


def _never_left(err: BaseException) -> bool:
    """A failed send whose request did not leave (no connection)."""
    return isinstance(err, RequestTimeout | ServiceUnavailable) and never_sent(err)


class RrcStructWrite:
    """Fresh preflight, exactly one write, read-back with RRC patience."""

    def __init__(
        self,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        delays: tuple[float, ...] = RRC_READ_BACK_DELAYS,
        clock: Callable[[], float] = monotonic,
        pending_expiry: float = PENDING_EXPIRY_SECONDS,
    ) -> None:
        if not delays or any(delay < 0 for delay in delays):
            raise ValueError("Read-back delays must be non-negative")
        self._sleep = sleep
        self._delays = delays
        self._clock = clock
        self._expiry = pending_expiry
        self._pending: dict[str, _Pending] = {}

    def pending(self, key: str) -> bool:
        """Whether an unresolved write with this guard key blocks another."""
        entry = self._pending.get(key)
        return entry is not None and self._clock() - entry.sent_at < self._expiry

    async def async_write[T, R](
        self,
        *,
        preflight: Callable[[], Awaitable[T]],
        send: Callable[[T], Awaitable[object]],
        confirm: Callable[[T], Awaitable[R | None]],
        guard: str | None = None,
        pending_reason: str = "rrc_write_pending_confirmation",
        on_sent: Callable[[], None] | None = None,
    ) -> R:
        """Run one write; return what ``confirm`` read, or raise.

        ``preflight`` reads the current state and refuses with a
        ``WriteValidationError`` (nothing sent). ``send`` makes exactly one
        request. ``confirm`` reads and returns a result once the write shows,
        else ``None``. With a ``guard`` key, a write whose outcome stayed
        unknown blocks the next one with the same key until a reading
        confirms it or the guard expires (``RrcWritePending``).
        """
        if guard is not None and (entry := self._pending.get(guard)) is not None:
            if self._clock() - entry.sent_at >= self._expiry:
                del self._pending[guard]
            else:
                if await entry.confirm() is None:
                    raise RrcWritePending(pending_reason)
                # The earlier write did take effect; a fresh preflight sees it.
                del self._pending[guard]
        state = await preflight()
        # D25: refused before sending (and before the guard is recorded) when
        # the transaction's time cannot cover the request and its read-back.
        ensure_send_window(sum(self._delays))
        if guard is not None:
            # Recorded before sending: a lost answer or a cancellation must
            # not allow a second non-idempotent write.
            self._pending[guard] = _Pending(lambda: confirm(state), self._clock())
        try:
            await send(state)
        except BaseException as err:
            # S12: a request that never left (no connection) is not counted
            # as sent; any other outcome, a cancellation included, is.
            if on_sent is not None and not _never_left(err):
                on_sent()
            if not self._read_after(err, guard):
                raise
        else:
            if on_sent is not None:
                on_sent()
        last_temporary: PointTError | None = None
        for delay in self._delays:
            await self._sleep(delay)
            try:
                result = await confirm(state)
            except (RequestTimeout, ServiceUnavailable) as err:
                last_temporary = err
                continue
            except PointTError as err:
                raise RrcWriteNotConfirmed(
                    "RRC read-back failed after the write was sent"
                ) from err
            if result is not None:
                self._release(guard)
                return result
        if last_temporary is not None:
            raise RrcWriteNotConfirmed(
                "RRC read-back could not be completed"
            ) from last_temporary
        raise RrcWriteNotConfirmed("RRC read-back did not confirm the write")

    def _read_after(self, err: BaseException, guard: str | None) -> bool:
        """Whether reading resolves the outcome of a failed send; otherwise
        the error stands, and a final refusal releases the guard."""
        if isinstance(err, RateLimited):
            self._release(guard)
            return False
        if isinstance(err, RequestTimeout | ServiceUnavailable):
            # Without a connection nothing was sent; otherwise the outcome is
            # unknown and only reading resolves it, never a second request.
            if never_sent(err):
                self._release(guard)
                return False
            return True
        if isinstance(err, UnexpectedHttpStatus):
            if 400 <= err.status < 500:
                # The server's final refusal: nothing changed.
                self._release(guard)
                return False
            return True
        if isinstance(err, AuthenticationError | ResourceError | ProtocolError):
            self._release(guard)
            return False
        return isinstance(err, TransportError)

    def _release(self, guard: str | None) -> None:
        if guard is not None:
            self._pending.pop(guard, None)


@dataclass(slots=True)
class _Late:
    contract: str
    check: Callable[[Resource], bool]
    since: float


class RrcWriteStats:
    """S12: write counters per contract; no values, names or identifiers."""

    def __init__(self, window: float = LATE_CONFIRMATION_WINDOW_SECONDS) -> None:
        self._window = window
        self._counts: dict[str, Counter[str]] = {}
        self._reasons: dict[str, Counter[str]] = {}
        self._late: dict[str, _Late] = {}

    def _count(self, contract: str, counter: str) -> None:
        self._counts.setdefault(contract, Counter())[counter] += 1

    def sent(self, contract: str) -> None:
        self._count(contract, "writes_sent")

    def sent_count(self, contract: str) -> int:
        return self._counts.get(contract, Counter())["writes_sent"]

    def confirmed(self, contract: str, path: str) -> None:
        self._late.pop(path, None)
        self._count(contract, "writes_confirmed")

    def rejected(self, contract: str, reason: str) -> None:
        self._count(contract, "writes_rejected")
        self._reasons.setdefault(contract, Counter())[reason] += 1

    def unconfirmed(
        self,
        contract: str,
        path: str,
        check: Callable[[Resource], bool] | None,
        now: float,
    ) -> None:
        """A sent write stayed unconfirmed; a later poll may still show it."""
        self._count(contract, "writes_unconfirmed")
        if check is not None:
            self._late[path] = _Late(contract, check, now)
        else:
            self._late.pop(path, None)

    def observe(self, resource: Resource, now: float) -> None:
        """A regular poll read a path; count a late confirmation once."""
        late = self._late.get(resource.path)
        if late is None:
            return
        if now - late.since > self._window:
            del self._late[resource.path]
        elif late.check(resource):
            del self._late[resource.path]
            self._count(late.contract, "late_confirmed")

    def awaiting(self) -> int:
        return len(self._late)

    def snapshot(self, contract: str) -> dict[str, object]:
        counts = self._counts.get(contract, Counter())
        return {
            **{counter: counts[counter] for counter in COUNTERS},
            "awaiting_late_confirmation": sum(
                item.contract == contract for item in self._late.values()
            ),
            "rejection_reasons": dict(
                sorted(self._reasons.get(contract, Counter()).items())
            ),
        }
