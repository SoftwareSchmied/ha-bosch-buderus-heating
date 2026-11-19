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
from .icom_access import is_icom_connection

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
        "address_hint": (
            "Tipp: Ändert sich die Adresse des Gateways, etwa nach einem "
            "Neustart des Routers, hilft eine feste Adresse: die IP-Adresse im "
            "Router reservieren (DHCP-Reservierung) oder den Hostnamen des "
            "Gateways eintragen. Meldet sich das Gateway unter einer neuen "
            "Adresse, bietet eine Reparaturmeldung an, sie zu übernehmen."
        ),
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
        "address_hint": (
            "Tip: if the gateway's address changes, for example after a router "
            "restart, a fixed address helps: reserve its IP address in the router "
            "(DHCP reservation) or enter the gateway's host name. If the gateway "
            "announces itself at a new address, a repair note offers to use it."
        ),
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
    from .managed_aircon import ManagedAirconCoordinator
    from .managed_bacon import BaconDeviceAccess
    from .managed_wallbox import ManagedWallboxCoordinator

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
                data := connections.source_data(key),
                BoschBuderusDataUpdateCoordinator
                | ManagedWallboxCoordinator
                | ManagedAirconCoordinator
                | BaconDeviceAccess,
            )
        ]
        # Wallbox, air-conditioner and second-cloud device accesses count as
        # managed sources too, so an account without a heat pump or with
        # second-cloud devices is not stuck in "starting".
        last = _latest(
            [
                item.last_success
                for data in coordinators
                if isinstance(data, BoschBuderusDataUpdateCoordinator)
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
            not data.last_update_success
            for data in coordinators
            if not isinstance(data, BaconDeviceAccess)
        ):
            # The access itself answers, but an appliance's requests fail.
            return AccessStatus("unreachable", last_success=last)
        if any(
            isinstance(data, BaconDeviceAccess) and data.account.error is not None
            for data in coordinators
        ):
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


def local_pause_reasons(entry: ConfigEntry[Any], connection_id: str) -> frozenset[str]:
    """Why the sources of a LAN access pause now (fixed codes), if they pause."""
    connections = _runtime(entry)
    if connections is None:
        return frozenset()
    reasons: set[str] = set()
    for key, binding in connections.snapshot.bindings.items():
        if binding.connection_id != connection_id:
            continue
        reason = getattr(connections.source_driver(key), "pause_reason", None)
        if isinstance(reason, str):
            reasons.add(reason)
    return frozenset(reasons)


def local_unreachable(
    entry: ConfigEntry[Any], connection_id: str, status: AccessStatus | None = None
) -> bool:
    """The LAN access does not reach its gateway: not reachable, or pausing
    after failed connections rather than on the gateway's own request."""
    status = status or access_status(entry, connection_id)
    if status.state == "unreachable":
        return True
    return status.state == "paused" and "connection" in local_pause_reasons(
        entry, connection_id
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
        if local_unreachable(entry, key, status):
            parts.append(texts["address_hint"])
    return " ".join(parts)


# The outcome of an explicit test: a code with its values. The dialog shows
# the page ``connection_test_<code>``, a fixed text in the profile language.
TestOutcome = tuple[str, dict[str, str]]


def _paused(until: datetime | None) -> TestOutcome:
    if until is None:
        return "paused_open", {}
    return "paused", {"until": _clock(until)}


async def test_connection(
    hass: HomeAssistant, entry: ConfigEntry[Any], key: str
) -> TestOutcome:
    """Read once on explicit request and name the outcome."""
    from .pointt import AuthenticationError, PointTError, RateLimited
    from .pointt.exceptions import SourceStopped

    connections = _runtime(entry)
    status = access_status(entry, key)
    if connections is None or status.state in (
        "disabled",
        "not_loaded",
        "starting",
        "unused",
    ):
        return "not_running", {}
    connection = connections.snapshot.connections.get(key)
    if connection is None:
        return "not_running", {}
    started = monotonic()

    def elapsed() -> str:
        return str(round((monotonic() - started) * 1000))

    if connection.kind == CLOUD_KIND:
        # Known causes need no request; the user can act on them directly.
        if status.state == "auth_required":
            return "auth_cloud", {}
        if status.state == "paused":
            return _paused(status.until)
        driver = connections.connection_driver(key)
        client = getattr(driver, "client", None)
        if client is None:
            return "not_running", {}
        try:
            async with asyncio.timeout(30):
                gateways = await client.get_gateways()
        except SourceStopped:
            return "not_running", {}
        except AuthenticationError:
            return "auth_cloud", {}
        except RateLimited:
            remaining = getattr(getattr(driver, "backoff", None), "remaining", 0.0)
            seconds = remaining if isinstance(remaining, int | float) else 0.0
            return _paused(_in(seconds) if seconds > 0 else None)
        except TimeoutError, PointTError:
            return "unreachable", {"ms": elapsed()}
        return "ok_cloud", {"ms": elapsed(), "count": str(len(gateways))}
    # LAN: the published state already names these causes.
    # An iCom access has its own pages: their menu name is the one of its dialog.
    icom = is_icom_connection(connection.to_data())
    auth_page = "auth_icom" if icom else "auth_local"
    if status.state == "auth_required":
        return auth_page, {}
    if status.state == "certificate_changed":
        return "certificate_changed", {}
    if status.state == "paused":
        return _paused(status.until)
    for binding_id, binding in connections.snapshot.bindings.items():
        if binding.connection_id != key:
            continue
        driver = connections.source_driver(binding_id)
        probe = getattr(driver, "test_connection", None)
        if not callable(probe):
            continue
        result, millis = await probe()
        if result == "ok":
            return "ok", {"ms": str(millis)}
        if result == "auth_required":
            return auth_page, {}
        if result == "paused":
            return _paused(access_status(entry, key).until)
        if result == "not_running" and status.state == "unreachable":
            result = "unreachable"
        if result == "unreachable":
            # With the tip on a fixed address.
            return "unreachable_local", {"ms": str(millis)}
        if result == "other_gateway" and icom:
            return "other_gateway_icom", {"ms": str(millis)}
        if result in TEST_CODES:
            return result, {"ms": str(millis)}
        return "error", {"kind": safe_kind(result)}
    return "not_running", {}


# Every code of ``test_connection``; each has its page.
TEST_CODES = frozenset(
    {
        "auth_cloud",
        "auth_icom",
        "auth_local",
        "busy",
        "certificate_changed",
        "error",
        "not_running",
        "ok",
        "ok_cloud",
        "other_gateway",
        "other_gateway_icom",
        "paused",
        "paused_open",
        "unreachable",
        "unreachable_local",
    }
)


def safe_kind(value: object) -> str:
    """A code for the page, never free text."""
    text = str(value)
    return text if text.replace("_", "").isalnum() and len(text) <= 40 else "unknown"
