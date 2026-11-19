"""X16: which entities have to look again after a pass of local readings."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def untouched(coordinator: Any, paths: Iterable[str]) -> bool:
    """Whether an update pass of local readings changed none of ``paths``.

    Only the pass that carries new local readings knows which paths changed
    (``local_changed_paths``); every other pass, and any pass in which more
    than readings changed, leaves it empty, and every entity looks again.
    """
    changed = getattr(coordinator, "local_changed_paths", None)
    return isinstance(changed, frozenset) and changed.isdisjoint(paths)
