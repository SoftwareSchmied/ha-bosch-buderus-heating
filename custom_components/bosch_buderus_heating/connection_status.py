"""State and explicit test of each access for the settings dialog (P21).

The state comes only from what the running connections already know; opening
a dialog never starts a request. Only the explicit connection test reads once:
the cloud gateway list or the local ``/system/basicInfo`` with an identity
check. Texts never contain tokens, gateway IDs or plant values.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Any, cast

from homeassistant.util import dt as dt_util

from .configuration import CLOUD_KIND, LOCAL_KIND
from .i18n import localize_all

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_TEXTS: dict[str, dict[str, str]] = {
    "de": {
        "connected": "verbunden",
        "auth_required": "Anmeldung nötig",
        "certificate_changed": "Zertifikat geändert, neu einrichten",
        "paused": "pausiert",
        "paused_until": "pausiert bis {until}",
        "unreachable": "nicht erreichbar",
        "access_denied": "Zugriff verweigert, Rechte am Gateway prüfen",
        "disabled": "deaktiviert",
        "unused": "keiner Anlage zugeordnet",
        "starting": "startet",
        "not_loaded": "nicht geladen",
        "cloud": "Cloud-Zugang",
        "state": "Zustand: {state}.",
        "last_cloud": "Letzte erfolgreiche Cloud-Abfrage: {time}.",
        "last_local": "Letzte lokale Antwort: {time}.",
        "never": "noch keine",
        "profile": "Abfrageprofil: {profile}.",
        "standard": "Standard",
        "cloud_friendly": "Cloud-schonend",
        "local_counts": "Lokal verfügbare Werte: {available} von {known}.",
        "local_values": "Lokale Werte für vorhandene Entitäten: {value}.",
        "interval": "Zielintervall für Livewerte: {seconds} s.",
        "on": "ein",
        "off": "aus",
        "ok": "In Ordnung, Antwort nach {ms} ms. Einstellungen bleiben unverändert.",
        "ok_cloud": (
            "In Ordnung, Antwort nach {ms} ms; {count} Gateway(s) im Konto. "
            "Einstellungen bleiben unverändert."
        ),
        "test_auth_cloud": (
            "Anmeldung nötig: Bitte über „Erneut anmelden“ neu anmelden."
        ),
        "test_auth_local": (
            "Anmeldung nötig: Bitte den lokalen Zugang über „Lokalen Zugang "
            "ändern“ neu einrichten."
        ),
        "test_certificate_changed": (
            "Das Zertifikat des Geräts hat sich geändert: Bitte den lokalen "
            "Zugang über „Lokalen Zugang ändern“ neu einrichten."
        ),
        "test_paused": "Nicht getestet: Abfragen sind pausiert bis {until}.",
        "test_paused_open": "Nicht getestet: Abfragen sind gerade pausiert.",
        "test_busy": (
            "Nicht getestet: Der Zugang ist gerade beschäftigt. Bitte gleich "
            "erneut versuchen."
        ),
        "test_unreachable": (
            "Nicht erreichbar: Keine Antwort ({ms} ms). Netzwerk und Gerät prüfen."
        ),
        "test_other_gateway": (
            "Unter dieser Adresse antwortet ein anderes Gateway. Lokalen Zugang "
            "neu einrichten."
        ),
        "test_not_running": (
            "Nicht getestet: Der Zugang läuft gerade nicht (deaktiviert, startet "
            "oder gestoppt)."
        ),
        "test_error": "Fehler beim Test: {kind}.",
    },
    "en": {
        "connected": "connected",
        "auth_required": "sign-in required",
        "certificate_changed": "certificate changed, set up again",
        "paused": "paused",
        "paused_until": "paused until {until}",
        "unreachable": "not reachable",
        "access_denied": "access denied, check the rights on the gateway",
        "disabled": "disabled",
        "unused": "not used by any appliance",
        "starting": "starting",
        "not_loaded": "not loaded",
        "cloud": "Cloud access",
        "state": "State: {state}.",
        "last_cloud": "Last successful cloud request: {time}.",
        "last_local": "Last local answer: {time}.",
        "never": "none yet",
        "profile": "Polling profile: {profile}.",
        "standard": "standard",
        "cloud_friendly": "cloud-friendly",
        "local_counts": "Values available locally: {available} of {known}.",
        "local_values": "Local values for existing entities: {value}.",
        "interval": "Target interval for live values: {seconds} s.",
        "on": "on",
        "off": "off",
        "ok": "OK, answer after {ms} ms. Settings stay unchanged.",
        "ok_cloud": (
            "OK, answer after {ms} ms; {count} gateway(s) in the account. "
            "Settings stay unchanged."
        ),
        "test_auth_cloud": (
            "Sign-in required: please sign in again via “Sign in again”."
        ),
        "test_auth_local": (
            "Sign-in required: please set up the local access again via "
            "“Change local access”."
        ),
        "test_certificate_changed": (
            "The device certificate has changed: please set up the local access "
            "again via “Change local access”."
        ),
        "test_paused": "Not tested: requests are paused until {until}.",
        "test_paused_open": "Not tested: requests are paused right now.",
        "test_busy": (
            "Not tested: the access is busy right now. Please try again shortly."
        ),
        "test_unreachable": (
            "Not reachable: no answer ({ms} ms). Check the network and the device."
        ),
        "test_other_gateway": (
            "A different gateway answers at this address. Set up the local access "
            "again."
        ),
        "test_not_running": (
            "Not tested: the access is not running right now (disabled, starting "
            "or stopped)."
        ),
        "test_error": "Test failed: {kind}.",
    },
}


def _texts(hass: HomeAssistant) -> dict[str, str]:
    language = str(getattr(hass.config, "language", "en") or "en")
    return localize_all(language, _TEXTS["de"], _TEXTS["en"])


def _clock(when: datetime) -> str:
    local = dt_util.as_local(when)
    if local.date() == dt_util.now().date():
        return local.strftime("%H:%M")
    return local.strftime("%Y-%m-%d %H:%M")


def _in(seconds: float) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


@dataclass(frozen=True, slots=True)
class AccessStatus:
    """What the running connection knows; no values, no identifiers."""

    state: str
    until: datetime | None = None
    last_success: datetime | None = None
    available: int | None = None
    known: int | None = None


def _options(entry: ConfigEntry[Any], name: str) -> dict[str, Any]:
    """One options section, empty when an entry has none (older or offline)."""
    value = dict(getattr(entry, "options", None) or {}).get(name)
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _runtime(entry: ConfigEntry[Any]) -> Any | None:
    from .managed_entry import ManagedEntry

    runtime = getattr(entry, "runtime_data", None)
    return runtime.connections if isinstance(runtime, ManagedEntry) else None


def _phase_state(phase: Any) -> str:
    from .connection_runtime import ConnectionPhase

    return {
        ConnectionPhase.READY: "connected",
        ConnectionPhase.CONNECTING: "starting",
        ConnectionPhase.UNAVAILABLE: "unreachable",
        ConnectionPhase.AUTH_REQUIRED: "auth_required",
        ConnectionPhase.DISABLED: "disabled",
        ConnectionPhase.IDLE: "unused",
        ConnectionPhase.UNSUPPORTED: "unreachable",
    }.get(phase, "not_loaded")


def _latest(times: list[datetime]) -> datetime | None:
    return max(times) if times else None


def access_status(entry: ConfigEntry[Any], connection_id: str) -> AccessStatus:
    """Current state of one access from existing runtime data only."""
    from .connection_runtime import ConnectionPhase
    from .coordinator import BoschBuderusDataUpdateCoordinator
    from .local_state import LocalCondition

    connections = _runtime(entry)
    if _options(entry, "connections").get(connection_id, {}).get("enabled") is False:
        return AccessStatus("disabled")
    if connections is None:
        return AccessStatus("not_loaded")
    snapshot = connections.snapshot
    connection = snapshot.connections.get(connection_id)
    if connection is None:
        return AccessStatus("not_loaded")
    phase = connections.connection_phase(connection_id)
    bindings = [
        key
        for key, binding in snapshot.bindings.items()
        if binding.connection_id == connection_id
    ]
    phases = [connections.source_phase(key) for key in bindings]
    active = [
        key
        for key, source_phase in zip(bindings, phases, strict=True)
        if source_phase is not ConnectionPhase.DISABLED
    ]
    if connection.kind == CLOUD_KIND:
        driver = connections.connection_driver(connection_id)
        remaining = getattr(getattr(driver, "backoff", None), "remaining", 0.0)
        remaining = remaining if isinstance(remaining, int | float) else 0.0
        coordinators = [
            data
            for key in bindings
            if isinstance(
                data := connections.source_data(key), BoschBuderusDataUpdateCoordinator
            )
        ]
        last = _latest(
            [
                item.last_success
                for data in coordinators
                for item in (data.data or {}).values()
                if item.last_success is not None
            ]
        )
        if phase is ConnectionPhase.AUTH_REQUIRED or (
            ConnectionPhase.AUTH_REQUIRED in phases
        ):
            return AccessStatus("auth_required", last_success=last)
        if remaining > 0:
            return AccessStatus("paused", until=_in(remaining), last_success=last)
        if phase is not ConnectionPhase.READY:
            return AccessStatus(_phase_state(phase), last_success=last)
        if bindings and not active:
            return AccessStatus("unused", last_success=last)
        if ConnectionPhase.UNAVAILABLE in phases or any(
            not data.last_update_success for data in coordinators
        ):
            # The access itself answers, but an appliance's requests fail.
            return AccessStatus("unreachable", last_success=last)
        if ConnectionPhase.CONNECTING in phases or len(coordinators) < len(active):
            return AccessStatus("starting", last_success=last)
        return AccessStatus("connected", last_success=last)
    # Local access: the published source state of its binding.
    current = next(
        (
            item
            for key in bindings
            if (item := connections.local_state(key)) is not None
        ),
        None,
    )
    if current is None:
        source = next(iter(phases), phase)
        state = _phase_state(source if phase is ConnectionPhase.READY else phase)
        if state == "connected":
            state = "starting"  # running, but nothing published yet
        return AccessStatus(state)
    observations = [
        item.observed_at for item in getattr(current, "last_observations", ()) or ()
    ] or [item.observed_at for item in current.current_observations]
    resources = getattr(current, "resources", ()) or ()
    state = {
        LocalCondition.READY: "connected",
        LocalCondition.INITIALIZING: "starting",
        LocalCondition.PAUSED: "paused",
        LocalCondition.UNAVAILABLE: "unreachable",
        LocalCondition.AUTH_REQUIRED: "auth_required",
        LocalCondition.CERTIFICATE_CHANGED: "certificate_changed",
    }.get(current.condition, "unreachable")
    if (
        state == "unreachable"
        and getattr(current, "problem", None) == "resource_forbidden"
    ):
        state = "access_denied"  # reached, but the gateway refuses (P39)
    until = None
    if state == "paused":
        for key in bindings:
            service = getattr(connections.source_driver(key), "service", None)
            pause = getattr(
                getattr(service, "snapshot", lambda: None)(), "gateway_pause_seconds", 0
            )
            if isinstance(pause, int | float) and pause > 0:
                until = _in(pause)
    return AccessStatus(
        state,
        until=until,
        last_success=_latest(observations),
        available=sum(1 for item in resources if getattr(item, "available", False)),
        known=len(resources),
    )


def _state_text(texts: dict[str, str], status: AccessStatus) -> str:
    if status.state == "paused" and status.until is not None:
        return texts["paused_until"].format(until=_clock(status.until))
    return texts[status.state]


def connection_label(
    hass: HomeAssistant, entry: ConfigEntry[Any], key: str, connection: Any, number: int
) -> str:
    """List entry: kind, LAN address, number and state; never a brand."""
    texts = _texts(hass)
    if connection.kind == CLOUD_KIND:
        label = texts["cloud"]
    elif connection.kind == LOCAL_KIND:
        endpoint = cast(dict[str, Any], connection.to_data().get("endpoint", {}))
        label = f"LAN · {endpoint.get('host', key)}"
    else:
        label = str(connection.kind)
    return f"{label} ({number}): {_state_text(texts, access_status(entry, key))}"


def connection_description(
    hass: HomeAssistant, entry: ConfigEntry[Any], key: str
) -> str:
    """Short status text above the access menu, from existing state only."""
    texts = _texts(hass)
    connections = _runtime(entry)
    status = access_status(entry, key)
    parts = [texts["state"].format(state=_state_text(texts, status))]
    snapshot = connections.snapshot if connections is not None else None
    connection = snapshot.connections.get(key) if snapshot is not None else None
    kind = getattr(connection, "kind", None)
    options = _options(entry, "connections").get(key, {})
    when = _clock(status.last_success) if status.last_success else texts["never"]
    if kind == CLOUD_KIND:
        parts.append(texts["last_cloud"].format(time=when))
        profile = options.get("polling_profile", "standard")
        parts.append(
            texts["profile"].format(profile=texts.get(str(profile), str(profile)))
        )
    elif kind == LOCAL_KIND:
        parts.append(texts["last_local"].format(time=when))
        if status.known:
            parts.append(
                texts["local_counts"].format(
                    available=status.available, known=status.known
                )
            )
        parts.append(
            texts["interval"].format(seconds=options.get("local_interval", 60))
        )
        targets = _options(entry, "targets")
        enabled = (
            any(
                targets.get(binding.target_id, {}).get("local_values") is True
                for binding in snapshot.bindings.values()
                if binding.connection_id == key
            )
            if snapshot is not None
            else False
        )
        parts.append(
            texts["local_values"].format(value=texts["on" if enabled else "off"])
        )
    return " ".join(parts)


def _paused_text(texts: dict[str, str], until: datetime | None) -> str:
    if until is None:
        return texts["test_paused_open"]
    return texts["test_paused"].format(until=_clock(until))


async def test_connection(
    hass: HomeAssistant, entry: ConfigEntry[Any], key: str
) -> str:
    """Read once on explicit request and describe the result."""
    from .pointt import AuthenticationError, PointTError, RateLimited
    from .pointt.exceptions import SourceStopped

    texts = _texts(hass)
    connections = _runtime(entry)
    status = access_status(entry, key)
    if connections is None or status.state in (
        "disabled",
        "not_loaded",
        "starting",
        "unused",
    ):
        return texts["test_not_running"]
    connection = connections.snapshot.connections.get(key)
    if connection is None:
        return texts["test_not_running"]
    started = monotonic()

    def elapsed() -> int:
        return round((monotonic() - started) * 1000)

    if connection.kind == CLOUD_KIND:
        # Known causes need no request; the user can act on them directly.
        if status.state == "auth_required":
            return texts["test_auth_cloud"]
        if status.state == "paused":
            return _paused_text(texts, status.until)
        driver = connections.connection_driver(key)
        client = getattr(driver, "client", None)
        if client is None:
            return texts["test_not_running"]
        try:
            async with asyncio.timeout(30):
                gateways = await client.get_gateways()
        except SourceStopped:
            return texts["test_not_running"]
        except AuthenticationError:
            return texts["test_auth_cloud"]
        except RateLimited:
            remaining = getattr(getattr(driver, "backoff", None), "remaining", 0.0)
            seconds = remaining if isinstance(remaining, int | float) else 0.0
            return _paused_text(texts, _in(seconds) if seconds > 0 else None)
        except TimeoutError, PointTError:
            return texts["test_unreachable"].format(ms=elapsed())
        return texts["ok_cloud"].format(ms=elapsed(), count=len(gateways))
    # LAN: the published state already names these causes.
    if status.state == "auth_required":
        return texts["test_auth_local"]
    if status.state == "certificate_changed":
        return texts["test_certificate_changed"]
    if status.state == "paused":
        return _paused_text(texts, status.until)
    for binding_id, binding in connections.snapshot.bindings.items():
        if binding.connection_id != key:
            continue
        driver = connections.source_driver(binding_id)
        probe = getattr(driver, "test_connection", None)
        if not callable(probe):
            continue
        result, millis = await probe()
        if result == "ok":
            return texts["ok"].format(ms=millis)
        if result == "auth_required":
            return texts["test_auth_local"]
        if result == "paused":
            return _paused_text(texts, access_status(entry, key).until)
        if result == "not_running" and status.state == "unreachable":
            return texts["test_unreachable"].format(ms=millis)
        return texts.get(f"test_{result}", texts["test_error"]).format(
            ms=millis, kind=result
        )
    return texts["test_not_running"]
