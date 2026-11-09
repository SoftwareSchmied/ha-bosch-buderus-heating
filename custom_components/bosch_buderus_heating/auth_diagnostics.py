"""Persistent, privacy-safe history of SingleKey ID token refreshes.

The history explains *why* a refresh failed without storing credentials. Each
refresh token is represented only by a keyed fingerprint. The random key stays
in Home Assistant's local storage and is never exported, so the fingerprints
cannot be compared with tokens from elsewhere. Diagnostics export only
relations such as "this token was already used before" and relative ages.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import re
import secrets
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from time import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util.hass_dict import HassKey

from .configuration import CLOUD_KIND
from .configuration_migration import legacy_cloud_connection_id
from .const import DOMAIN
from .pointt import (
    AuthTokens,
    RateLimited,
    RefreshAttempt,
    RefreshRefused,
    RefreshTokenRejected,
    TokenEndpointRejection,
    TokenManager,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
MAX_EVENTS = 60
MAX_ISSUED = 20
_KEY_PATTERN = re.compile(r"[0-9a-f]{64}")
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,64}")

type HistoryKey = tuple[str, str]

_RECORDERS: HassKey[dict[HistoryKey, AuthDiagnostics]] = HassKey(
    f"{DOMAIN}.auth_diagnostics"
)
_LOADED_OWNERS: HassKey[set[str]] = HassKey(f"{DOMAIN}.auth_diagnostics_owners")


def _storage_key(owner_id: str, connection_id: str) -> str:
    # The migrated original cloud access keeps the per-entry history file used
    # before managed connections, so its history continues after the update.
    if connection_id == legacy_cloud_connection_id(owner_id):
        return f"{DOMAIN}.auth_history.{owner_id}"
    return f"{DOMAIN}.auth_history.{owner_id}.{connection_id}"


def _cloud_connection_ids(entry: ConfigEntry[Any]) -> set[str]:
    ids = {legacy_cloud_connection_id(entry.entry_id)}
    connections = entry.data.get("connections")
    if isinstance(connections, Mapping):
        ids.update(
            key
            for key, value in connections.items()
            if isinstance(key, str)
            and isinstance(value, Mapping)
            and value.get("kind") == CLOUD_KIND
        )
    return ids


async def async_load_auth_histories(
    hass: HomeAssistant, entry: ConfigEntry[Any], *, clock: Callable[[], float] = time
) -> None:
    """Load the histories of every saved cloud access of one entry once."""
    recorders = hass.data.setdefault(_RECORDERS, {})
    for connection_id in sorted(_cloud_connection_ids(entry)):
        key = (entry.entry_id, connection_id)
        if key in recorders:
            continue
        store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, _storage_key(*key), private=True
        )
        try:
            stored = await store.async_load()
        except Exception:
            _LOGGER.warning("Token refresh history could not be read; starting anew")
            stored = None
        recorders.setdefault(key, AuthDiagnostics(store, stored, clock=clock))
    hass.data.setdefault(_LOADED_OWNERS, set()).add(entry.entry_id)


@callback
def auth_diagnostics_for(
    hass: HomeAssistant, owner_id: str, connection_id: str
) -> AuthDiagnostics | None:
    """Return the history of one access without I/O.

    A new access added after loading starts an empty history. Before its entry
    was loaded, nothing is recorded, so an existing file is never overwritten.
    """
    recorders = hass.data.setdefault(_RECORDERS, {})
    key = (owner_id, connection_id)
    if (recorder := recorders.get(key)) is not None:
        return recorder
    if owner_id not in hass.data.get(_LOADED_OWNERS, set()):
        return None
    recorder = recorders[key] = AuthDiagnostics(
        Store(hass, STORAGE_VERSION, _storage_key(*key), private=True), None
    )
    return recorder


@callback
def loaded_auth_history(
    hass: HomeAssistant, owner_id: str, connection_id: str
) -> AuthDiagnostics | None:
    """Return an existing history without creating one."""
    return hass.data.get(_RECORDERS, {}).get((owner_id, connection_id))


@callback
def forget_auth_history(
    hass: HomeAssistant, owner_id: str, connection_id: str | None = None
) -> None:
    """Delete the history of a removed access, or of every access of an entry."""
    recorders = hass.data.get(_RECORDERS, {})
    for key in tuple(recorders):
        if key[0] == owner_id and connection_id in (None, key[1]):
            recorder = recorders.pop(key)
            hass.async_create_task(
                recorder.store.async_remove(), "remove token refresh history"
            )
    if connection_id is None:
        hass.data.get(_LOADED_OWNERS, set()).discard(owner_id)


class AuthDiagnostics:
    """Record token lifecycle events for one saved cloud access."""

    def __init__(
        self,
        store: Store[dict[str, Any]],
        stored: object,
        *,
        clock: Callable[[], float] = time,
    ) -> None:
        self.store = store
        self._clock = clock
        self._events: list[dict[str, Any]] = []
        self._issued: list[dict[str, Any]] = []
        self._counters: dict[str, int] = {}
        self._history_reset = False
        if not self._restore(stored):
            self._key = secrets.token_hex(32)
            self._created_at = clock()
            self._history_reset = stored is not None
        self._loaded_at = clock()

    # ------------------------------------------------------------------ input

    @callback
    def credentials_loaded(self, tokens: AuthTokens, *, reason: str) -> None:
        """Record that a token manager started from stored credentials."""
        now = self._clock()
        relation = self._relation(tokens.refresh_token, now)
        if relation["state"] == "first_seen" and tokens.refresh_token:
            self._register(tokens.refresh_token, "loaded", now)
        event: dict[str, Any] = {
            "event": "credentials_loaded",
            "reason": reason,
            "refresh_token": relation,
            "access_token_remaining_s": _seconds(tokens.expires_at - now),
            "seconds_since_previous_event": self._since_previous(now),
        }
        self._count("credentials_loaded")
        if relation["state"] == "older_than_latest":
            self._count("stale_credentials_loaded")
            _LOGGER.warning(
                "The stored SingleKey ID refresh token is older than the newest "
                "one this integration received %s ago; the stored sign-in data "
                "may be outdated. The next refresh can be rejected",
                _duration(relation.get("newer_token_age_s")),
            )
        self._append(now, event)

    @callback
    def refresh_finished(self, attempt: RefreshAttempt) -> None:
        """Record one refresh attempt reported by the token manager."""
        now = self._clock()
        presented = self._relation(attempt.presented_refresh_token, now)
        previous_success = self._last_event_age(now, "refresh", "success")
        event: dict[str, Any] = {
            "event": "refresh",
            "trigger": _safe(attempt.trigger),
            "outcome": _safe(attempt.outcome),
            "duration_ms": round(attempt.duration * 1000),
            "access_token_remaining_s": _seconds(attempt.access_token_remaining),
            "presented_refresh_token": presented,
            "last_successful_refresh_age_s": previous_success,
        }
        self._mark_presented(attempt.presented_refresh_token, attempt.started_at)
        new_tokens = attempt.new_tokens
        if attempt.outcome == "success" and new_tokens is not None:
            rotated = (
                new_tokens.refresh_token is not None
                and new_tokens.refresh_token != attempt.presented_refresh_token
            )
            event["refresh_token_rotated"] = rotated
            event["refresh_token_returned"] = new_tokens.refresh_token_returned
            if new_tokens.issued_at is not None:
                event["access_token_lifetime_s"] = _seconds(
                    new_tokens.expires_at - new_tokens.issued_at
                )
            if new_tokens.refresh_expires_in is not None:
                event["refresh_token_lifetime_s"] = _seconds(
                    new_tokens.refresh_expires_in
                )
            self._set_accepted(attempt.presented_refresh_token, True)
            if rotated and new_tokens.refresh_token:
                self._register(new_tokens.refresh_token, "refresh", now)
            self._count("refresh_success")
            _LOGGER.debug(
                "SingleKey ID token refresh succeeded (trigger %s, rotated %s)",
                attempt.trigger,
                rotated,
            )
        elif isinstance(attempt.error, RefreshTokenRejected):
            rejection = attempt.error.rejection
            if rejection is not None:
                event.update(_rejection_fields(rejection))
                reason = rejection.summary()
            else:
                event["reason"] = "no_refresh_token"
                reason = "no refresh token available"
            self._set_accepted(attempt.presented_refresh_token, False)
            self._count("refresh_rejected")
            _LOGGER.warning(
                "SingleKey ID rejected the token refresh (%s). Trigger: %s. "
                "Presented refresh token: %s, first seen %s ago, used %s time(s) "
                "before. Last successful refresh: %s ago. Credentials loaded "
                "%s ago",
                reason,
                attempt.trigger,
                presented["state"],
                _duration(presented.get("age_s")),
                presented.get("presented_before", 0),
                _duration(previous_success),
                _duration(self._last_event_age(now, "credentials_loaded")),
            )
        else:
            error = attempt.error
            if error is not None:
                event["error_type"] = _safe(type(error).__name__)
                status = getattr(error, "status", None)
                if isinstance(status, int):
                    event["http_status"] = status
            if isinstance(error, RateLimited) and error.retry_after is not None:
                event["retry_after_s"] = _seconds(error.retry_after)
            if isinstance(error, RefreshRefused):
                # Refused without a definite OAuth error: retried later.
                event.update(_rejection_fields(error.rejection))
                _LOGGER.debug(
                    "SingleKey ID refused the token refresh (%s); it is "
                    "retried later and no new sign-in is requested yet",
                    error.rejection.summary(),
                )
            self._count(f"refresh_{_safe(attempt.outcome)}")
        self._append(now, event)

    @callback
    def persist_result(self, outcome: str) -> None:
        """Record whether rotated credentials were written to the entry."""
        now = self._clock()
        self._count(f"persist_{_safe(outcome)}")
        if outcome != "saved":
            _LOGGER.warning(
                "A refreshed SingleKey ID token was not saved (%s)", outcome
            )
        self._append(now, {"event": "persist", "outcome": _safe(outcome)})

    # ----------------------------------------------------------------- output

    def diagnostics(self, manager: TokenManager | None) -> dict[str, Any]:
        """Return the exported history without fingerprints or token values."""
        now = self._clock()
        report: dict[str, Any] = {
            "history_available": True,
            "recorded_since": _iso(self._created_at),
            "history_reset": self._history_reset,
            "process_loaded_age_s": _seconds(now - self._loaded_at),
            "counters": dict(sorted(self._counters.items())),
            "issued_refresh_tokens": [
                {
                    "newest_rank": rank,
                    "source": item["source"],
                    "age_s": _seconds(now - item["first_seen_at"]),
                    "presented": item["presented"],
                    "accepted": item.get("accepted"),
                }
                for rank, item in enumerate(reversed(self._issued), start=1)
            ],
            "events": [
                {**event, "at": _iso(event["at"]), "age_s": _seconds(now - event["at"])}
                for event in self._events
            ],
        }
        if manager is not None:
            tokens = manager.tokens
            report["current"] = {
                "access_token_remaining_s": _seconds(tokens.expires_at - now),
                "has_refresh_token": bool(tokens.refresh_token),
                "refresh_token": self._relation(tokens.refresh_token, now),
                "scope": [_safe(item) for item in tokens.scope],
                "refresh_rejected": manager.refresh_rejected,
            }
        return report

    # -------------------------------------------------------------- internals

    def _fingerprint(self, token: str) -> str:
        return hmac.new(
            bytes.fromhex(self._key), token.encode("utf-8"), hashlib.sha256
        ).hexdigest()[:32]

    def _find(self, token: str | None) -> tuple[int, dict[str, Any]] | None:
        if not token:
            return None
        fingerprint = self._fingerprint(token)
        for index, item in enumerate(self._issued):
            if item["fingerprint"] == fingerprint:
                return index, item
        return None

    def _relation(self, token: str | None, now: float) -> dict[str, Any]:
        if not token:
            return {"state": "missing"}
        found = self._find(token)
        if found is None:
            return {"state": "first_seen"}
        index, item = found
        relation: dict[str, Any] = {
            "state": "latest_known"
            if index == len(self._issued) - 1
            else "older_than_latest",
            "source": item["source"],
            "age_s": _seconds(now - item["first_seen_at"]),
            "presented_before": item["presented"],
            "accepted_before": item.get("accepted"),
        }
        if relation["state"] == "older_than_latest":
            newest = self._issued[-1]
            relation["newer_tokens"] = len(self._issued) - 1 - index
            relation["newer_token_age_s"] = _seconds(now - newest["first_seen_at"])
        return relation

    def _register(self, token: str, source: str, now: float) -> None:
        fingerprint = self._fingerprint(token)
        self._issued = [
            item for item in self._issued if item["fingerprint"] != fingerprint
        ]
        self._issued.append(
            {
                "fingerprint": fingerprint,
                "source": source,
                "first_seen_at": now,
                "presented": 0,
                "accepted": None,
            }
        )
        del self._issued[:-MAX_ISSUED]

    def _mark_presented(self, token: str | None, at: float) -> None:
        if (found := self._find(token)) is not None:
            found[1]["presented"] += 1
            found[1]["last_presented_at"] = at

    def _set_accepted(self, token: str | None, accepted: bool) -> None:
        if (found := self._find(token)) is not None:
            found[1]["accepted"] = accepted

    def _count(self, name: str) -> None:
        self._counters[name] = self._counters.get(name, 0) + 1

    def _since_previous(self, now: float) -> float | None:
        return _seconds(now - self._events[-1]["at"]) if self._events else None

    def _last_event_age(
        self, now: float, event: str, outcome: str | None = None
    ) -> float | None:
        for item in reversed(self._events):
            if item.get("event") == event and (
                outcome is None or item.get("outcome") == outcome
            ):
                return _seconds(now - item["at"])
        return None

    def _append(self, now: float, event: dict[str, Any]) -> None:
        self._events.append({"at": now, **event})
        del self._events[:-MAX_EVENTS]
        # Save promptly: the history should reach the disk no later than the
        # rotated credentials, so a lost credential write remains detectable.
        self.store.async_delay_save(self._snapshot, 0)

    def _snapshot(self) -> dict[str, Any]:
        return {
            "key": self._key,
            "created_at": self._created_at,
            "counters": self._counters,
            "issued": self._issued,
            "events": self._events,
        }

    def _restore(self, stored: object) -> bool:
        if not isinstance(stored, Mapping):
            return False
        key = stored.get("key")
        created_at = stored.get("created_at")
        events = stored.get("events")
        issued = stored.get("issued")
        counters = stored.get("counters")
        if (
            not isinstance(key, str)
            or not _KEY_PATTERN.fullmatch(key)
            or not _finite(created_at)
            or not isinstance(events, list)
            or not isinstance(issued, list)
            or not isinstance(counters, Mapping)
        ):
            return False
        valid_events = [
            dict(item)
            for item in events
            if isinstance(item, Mapping) and _finite(item.get("at"))
        ]
        valid_issued = [
            dict(item)
            for item in issued
            if isinstance(item, Mapping)
            and isinstance(item.get("fingerprint"), str)
            and _finite(item.get("first_seen_at"))
            and isinstance(item.get("presented"), int)
            and isinstance(item.get("source"), str)
        ]
        self._key = key
        self._created_at = float(created_at)  # type: ignore[arg-type]
        self._events = valid_events[-MAX_EVENTS:]
        self._issued = valid_issued[-MAX_ISSUED:]
        self._counters = {
            str(name): value
            for name, value in counters.items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
        return True


def _finite(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _rejection_fields(rejection: TokenEndpointRejection) -> dict[str, Any]:
    return {
        "http_status": rejection.http_status,
        "oauth_error": rejection.oauth_error,
        "oauth_error_description": rejection.oauth_error_description,
        "body_format": rejection.body_format,
        "content_type": rejection.content_type,
    }


def _seconds(value: float | None) -> int | None:
    # Whole seconds are precise enough and avoid fractional digit noise in exports.
    if value is None or not math.isfinite(value):
        return None
    return round(value)


def _duration(value: float | None) -> str:
    if value is None:
        return "unknown"
    seconds = int(value)
    if abs(seconds) < 120:
        return f"{seconds} s"
    if abs(seconds) < 7200:
        return f"{seconds // 60} min"
    if abs(seconds) < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} d"


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")


def _safe(value: object) -> str:
    candidate = str(value)
    return candidate if _SAFE_TOKEN.fullmatch(candidate) else "other"
