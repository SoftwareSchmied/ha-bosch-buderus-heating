"""The last setup and sign-in attempts of one entry, without data.

When the local setup or a cloud sign-in needs several tries, the diagnostics
should tell why. Each attempt keeps only when it happened (UTC, to the
second), the dialog step and the error code the dialog showed, or that it
succeeded. Login, password, token, address, certificate data, sign-in links
and their codes, e-mail addresses and account or gateway identifiers are
never part of it. Each entry has one list for the local access and one for
the cloud; each keeps the last ``MAX_ATTEMPTS`` attempts and survives a
restart.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION: Final = 1
MAX_ATTEMPTS: Final = 10
# Fixed codes only: a dialog step, an error key of the dialog, or the type of
# an unexpected error.
_STEP: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
_CODE: Final = re.compile(
    r"[a-z][a-z0-9_]{0,63}|unexpected:[A-Za-z_][A-Za-z0-9_]{0,63}"
)
SUCCEEDED: Final = "succeeded"
FAILED: Final = "failed"

LOCAL: Final = "local"
CLOUD: Final = "cloud"
_KINDS: Final = {
    LOCAL: ("local_setup_history", "A local setup attempt failed: %s"),
    CLOUD: ("cloud_sign_in_history", "A cloud sign-in attempt failed: %s"),
}

_HISTORIES: HassKey[dict[tuple[str, str], LocalSetupHistory]] = HassKey(
    f"{DOMAIN}.setup_histories"
)


def _storage_key(entry_id: str, kind: str = LOCAL) -> str:
    return f"{DOMAIN}.{_KINDS[kind][0]}.{entry_id}"


def now_text() -> str:
    """The current UTC time as kept in an attempt."""
    return dt_util.utcnow().replace(microsecond=0).isoformat()


def safe_code(value: object) -> str | None:
    """The code if it is a fixed code, otherwise ``None``."""
    if isinstance(value, str) and _CODE.fullmatch(value):
        return value
    return None


def _attempt(value: object) -> dict[str, str | None] | None:
    """A stored attempt with exactly the known fields, or None."""
    if not isinstance(value, Mapping):
        return None
    at, step, result, error = (
        value.get("at"),
        value.get("step"),
        value.get("result"),
        value.get("error"),
    )
    if not isinstance(at, str) or not isinstance(step, str):
        return None
    try:
        parsed = datetime.fromisoformat(at)
    except ValueError:
        return None
    if parsed.tzinfo is None or not _STEP.fullmatch(step):
        return None
    if result == SUCCEEDED and error is None:
        return {"at": at, "step": step, "result": result, "error": None}
    if result == FAILED and (code := safe_code(error)) is not None:
        return {"at": at, "step": step, "result": result, "error": code}
    return None


class LocalSetupHistory:
    """The attempts of one entry; adding needs no I/O in the event loop."""

    def __init__(self, hass: HomeAssistant, entry_id: str, kind: str = LOCAL) -> None:
        self._hass = hass
        self._message = _KINDS[kind][1]
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, _storage_key(entry_id, kind), private=True
        )
        self._attempts: list[dict[str, str | None]] | None = None
        self._pending: list[dict[str, str | None]] = []
        self._lock = asyncio.Lock()

    @callback
    def add(
        self,
        step: str,
        error: str | None,
        *,
        at: str | None = None,
        log: bool = True,
    ) -> None:
        """Note one attempt: failed with ``error``, or succeeded without.

        ``at`` keeps the time of an attempt noted earlier (``log`` is then
        False: it was logged when it happened).
        """
        if not _STEP.fullmatch(step):
            return
        if error is None:
            attempt = {"result": SUCCEEDED, "error": None}
        else:
            attempt = {"result": FAILED, "error": safe_code(error) or "unknown"}
            if log:
                # Once per failed attempt, with nothing but the code.
                _LOGGER.info(self._message, attempt["error"])
        when = (
            at
            if at is not None and _attempt({"at": at, "step": step, **attempt})
            else now_text()
        )
        self._pending.append({"at": when, "step": step, **attempt})
        del self._pending[:-MAX_ATTEMPTS]
        self._hass.async_create_task(self._flush(), "local setup history")

    async def _flush(self) -> None:
        async with self._lock:
            if self._attempts is None:
                try:
                    stored = await self._store.async_load()
                except Exception:  # a broken file starts anew
                    _LOGGER.warning("Local setup history could not be read")
                    stored = None
                items = stored.get("attempts") if isinstance(stored, dict) else None
                self._attempts = [
                    attempt
                    for item in (items if isinstance(items, list) else [])
                    if (attempt := _attempt(item)) is not None
                ][-MAX_ATTEMPTS:]
            if not self._pending:
                return
            self._attempts.extend(self._pending)
            self._pending.clear()
            del self._attempts[:-MAX_ATTEMPTS]
            self._store.async_delay_save(self._data, 1)

    def _data(self) -> dict[str, Any]:
        return {"attempts": [dict(item) for item in self._attempts or ()]}

    async def async_attempts(self) -> list[dict[str, str | None]]:
        """The kept attempts, oldest first."""
        await self._flush()
        return [dict(item) for item in self._attempts or ()]

    async def async_remove(self) -> None:
        async with self._lock:
            self._attempts, self._pending = [], []
            await self._store.async_remove()


def _history(hass: HomeAssistant, entry_id: str, kind: str) -> LocalSetupHistory:
    histories = hass.data.setdefault(_HISTORIES, {})
    if (history := histories.get((kind, entry_id))) is None:
        history = histories[(kind, entry_id)] = LocalSetupHistory(hass, entry_id, kind)
    return history


@callback
def local_setup_history(hass: HomeAssistant, entry_id: str) -> LocalSetupHistory:
    return _history(hass, entry_id, LOCAL)


@callback
def cloud_sign_in_history(hass: HomeAssistant, entry_id: str) -> LocalSetupHistory:
    return _history(hass, entry_id, CLOUD)


async def async_forget_local_setup_history(hass: HomeAssistant, entry_id: str) -> None:
    """Delete both histories of a removed entry."""
    for kind in _KINDS:
        history = hass.data.setdefault(_HISTORIES, {}).pop((kind, entry_id), None)
        if history is None:
            history = LocalSetupHistory(hass, entry_id, kind)
        await history.async_remove()
