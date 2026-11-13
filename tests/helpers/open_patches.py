"""Patch stacks that must not outlive the test that opened them.

A stack registered here is active from its creation. When a test fails
between its setup and its own teardown, the teardown check in
``tests/conftest.py`` closes what is still open, so later tests of the same
worker never run against leftover fake answers or a patched clock.
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import Any

_OPEN: list[TrackedStack] = []


class TrackedStack(ExitStack):
    """An ``ExitStack`` that is known to be open until it is closed."""

    def __init__(self) -> None:
        super().__init__()
        _OPEN.append(self)

    def __exit__(self, *exc_info: Any) -> bool:
        try:
            return super().__exit__(*exc_info)
        finally:
            if self in _OPEN:
                _OPEN.remove(self)


def close_open_stacks() -> int:
    """Close every stack a test left open, newest first; return how many.

    A stack that fails to close does not keep the others open; the first
    such error is raised after all of them were tried.
    """
    count, errors = 0, []
    while _OPEN:
        stack = _OPEN[-1]
        count += 1
        try:
            stack.close()
        except Exception as err:
            errors.append(err)
        finally:
            if stack in _OPEN:
                _OPEN.remove(stack)
    if errors:
        raise errors[0]
    return count
