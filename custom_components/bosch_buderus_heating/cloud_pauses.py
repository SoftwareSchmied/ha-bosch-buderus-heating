"""Retain server-requested cloud pauses across Home Assistant restarts.

A pause belongs to one saved cloud access, identified by its configuration
entry and connection ID. New accesses receive new random connection IDs, so a
pause is never transferred to an unknown account. Removing an access deletes
its pause.

Each row stores the remaining seconds and the wall-clock time of the save.
After a restart, real elapsed time is deducted, but a pause never becomes
longer than saved. If the wall clock moved backwards, the saved remaining time
applies unchanged. A forward clock jump during downtime cannot be told apart
from real downtime and therefore counts as elapsed time.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Mapping
from time import monotonic, time
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN
from .pointt.retry_after import MAX_CLOUD_PAUSE_SECONDS
from .pointt.transport import RateLimitBackoff

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.cloud_pauses"
MAX_ROWS = 256
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")

type ConnectionKey = tuple[str, str]

_STORES: HassKey[CloudPauseStore] = HassKey(f"{DOMAIN}.cloud_pause_store")


class CloudPauseStore:
    """HA-wide owner of persisted pauses for all saved cloud accesses."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        wall_clock: Callable[[], float] = time,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY, private=True, atomic_writes=True
        )
        self._wall_clock = wall_clock
        self._clock = clock
        self._loaded = False
        self._backoffs: dict[ConnectionKey, RateLimitBackoff] = {}
        # Loaded pauses whose access has not started yet keep their deadline.
        self._pending: dict[ConnectionKey, float] = {}

    @property
    def store(self) -> Store[dict[str, Any]]:
        return self._store

    async def async_initialize(self) -> None:
        """Load once; a damaged file starts without pauses and is not fatal."""
        if self._loaded:
            return
        try:
            stored = await self._store.async_load()
        except Exception:
            _LOGGER.warning("Saved cloud pauses could not be read")
            stored = None
        if self._loaded:
            return
        now_wall, now = self._wall_clock(), self._clock()
        for key, remaining in _rows(stored, now_wall):
            self._pending[key] = now + remaining
        self._loaded = True
        for key, backoff in self._backoffs.items():
            self._restore(key, backoff)
        if any(backoff.remaining > 0 for backoff in self._backoffs.values()):
            # Save a pause that started while the file was still loading.
            self._changed()

    @callback
    def attach(self, key: ConnectionKey, backoff: RateLimitBackoff) -> None:
        """Persist changes of one access pause and resume a saved one."""
        self._backoffs[key] = backoff
        backoff.on_change = self._changed
        if self._loaded:
            self._restore(key, backoff)

    @callback
    def discard(self, key: ConnectionKey) -> None:
        """Forget the pause of a removed access."""
        backoff = self._backoffs.pop(key, None)
        if backoff is not None:
            backoff.on_change = None
        if self._pending.pop(key, None) is not None or backoff is not None:
            self._changed()

    @callback
    def discard_owner(self, owner_id: str) -> None:
        """Forget the pauses of every access of a removed entry."""
        for key in {*self._backoffs, *self._pending}:
            if key[0] == owner_id:
                self.discard(key)

    def diagnostics(self, key: ConnectionKey) -> dict[str, Any]:
        """Describe one pause without identifiers."""
        backoff = self._backoffs.get(key)
        if backoff is not None:
            remaining = backoff.remaining
            restored = backoff.restored and remaining > 0
        else:
            remaining = max(0.0, self._pending.get(key, 0.0) - self._clock())
            restored = remaining > 0
        return {
            "active": remaining > 0,
            "remaining_seconds": round(remaining),
            "restored_after_restart": restored,
            "persistence_available": self._loaded,
        }

    def _restore(self, key: ConnectionKey, backoff: RateLimitBackoff) -> None:
        deadline = self._pending.pop(key, None)
        if deadline is not None:
            backoff.restore(deadline - self._clock())

    @callback
    def _changed(self) -> None:
        # Never overwrite saved pauses before they were loaded.
        if self._loaded:
            self._store.async_delay_save(self._snapshot, 0)

    def _snapshot(self) -> dict[str, Any]:
        now_wall, now = self._wall_clock(), self._clock()
        remaining: dict[ConnectionKey, float] = {
            key: deadline - now for key, deadline in self._pending.items()
        }
        for key, backoff in self._backoffs.items():
            remaining[key] = max(remaining.get(key, 0.0), backoff.remaining)
        rows = [
            {
                "owner": owner,
                "connection": connection,
                "remaining": round(seconds, 3),
                "saved_at": now_wall,
            }
            for (owner, connection), seconds in sorted(remaining.items())
            if seconds > 0
        ]
        return {"pauses": rows[:MAX_ROWS]}


def _rows(stored: object, now_wall: float) -> list[tuple[ConnectionKey, float]]:
    if not isinstance(stored, Mapping):
        return []
    rows = stored.get("pauses")
    if not isinstance(rows, list):
        return []
    result: dict[ConnectionKey, float] = {}
    for row in rows[:MAX_ROWS]:
        if not isinstance(row, Mapping):
            continue
        owner, connection = row.get("owner"), row.get("connection")
        saved_remaining, saved_at = row.get("remaining"), row.get("saved_at")
        if (
            not isinstance(owner, str)
            or not isinstance(connection, str)
            or not _ID.fullmatch(owner)
            or not _ID.fullmatch(connection)
            or not _finite(saved_remaining)
            or not _finite(saved_at)
        ):
            continue
        saved = min(MAX_CLOUD_PAUSE_SECONDS, float(saved_remaining))  # type: ignore[arg-type]
        elapsed = now_wall - float(saved_at)  # type: ignore[arg-type]
        remaining = saved if elapsed < 0 else min(saved, saved - elapsed)
        if remaining > 0:
            key = (owner, connection)
            result[key] = max(result.get(key, 0.0), remaining)
    return list(result.items())


def _finite(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


@callback
def cloud_pause_store(hass: HomeAssistant) -> CloudPauseStore:
    """Return the HA-wide pause store, creating it without I/O."""
    if (store := hass.data.get(_STORES)) is None:
        store = hass.data[_STORES] = CloudPauseStore(hass)
    return store
