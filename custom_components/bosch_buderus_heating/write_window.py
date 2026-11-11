"""D25: the time a write transaction has left to send a request.

The coordinator opens a window with the deadline of each write transaction.
A write service calls ``ensure_send_window`` right before each request that
may change the gateway. When the time left no longer covers one request and
its read-back, nothing is sent: a request is never cut off by the deadline
while it is on its way, which would leave its outcome unknown. The window
also records whether a request of the transaction went out, so an abort
before the first one changes no reading.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from .pointt.const import DEFAULT_TOTAL_TIMEOUT
from .writes import WriteBusy, WriteDeadlineExceeded


@dataclass(slots=True)
class SendWindow:
    """The deadline of one write transaction (loop time)."""

    deadline: float
    sent: bool = False

    def remaining(self) -> float:
        return self.deadline - _now()


_WINDOW: ContextVar[SendWindow | None] = ContextVar(
    "home_comfort_send_window", default=None
)


def _now() -> float:
    return asyncio.get_running_loop().time()


@contextmanager
def send_window(seconds: float) -> Iterator[SendWindow]:
    """Open the window of a transaction that ends in ``seconds``."""
    window = SendWindow(_now() + seconds)
    token = _WINDOW.set(window)
    try:
        yield window
    finally:
        _WINDOW.reset(token)


def send_budget(read_back_seconds: float) -> float:
    """The time one request and its read-back need: the request timeout and
    the read-back waits."""
    return DEFAULT_TOTAL_TIMEOUT + read_back_seconds


def ensure_send_window(read_back_seconds: float) -> None:
    """Allow the next request, or refuse before anything is sent.

    The time left must cover the request timeout and the read-back waits
    (``read_back_seconds``). Before the first request the refusal is
    ``WriteBusy`` (nothing was sent, the change may be tried again); after a
    request of the same transaction went out it is ``WriteDeadlineExceeded``
    (not confirmed, nothing more is sent). Outside a transaction this is a
    no-op.
    """
    window = _WINDOW.get()
    if window is None:
        return
    if window.remaining() < send_budget(read_back_seconds):
        if window.sent:
            raise WriteDeadlineExceeded(
                "The time of the change ran out; nothing more is sent"
            )
        raise WriteBusy("The change could not start in time; it was not sent")
    window.sent = True
