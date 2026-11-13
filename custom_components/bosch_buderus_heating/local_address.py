"""Follow a configured gateway to a new address, proven by its certificate.

After a DHCP change the gateway announces itself at an address the saved
local access does not know. Names, TXT fields and addresses of an
announcement are never identity. The only proof is the certificate binding
confirmed at setup: one TLS handshake on the data port, without HTTP and
without a token, must present exactly that certificate. Completing the
handshake also shows that the answering device holds the certificate's key.

A proven address is kept in memory only and offered as a repair note. The
saved address changes only after the user confirms it, with a fresh
handshake right before the change. Nothing is offered while the access
reaches its gateway, for a certificate that more than one access knows, or
for a gateway that announces an address already configured.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .configuration import LOCAL_KIND, ConfigurationError, load_configuration
from .configuration_management import ConfigurationManagement
from .connection_status import access_status, local_pause_reasons
from .const import DOMAIN, LOCAL_ADDRESS_ISSUE_PREFIX
from .gateway_identity import GatewayNamespace, IdentityContext
from .local_credentials import (
    LocalCredentialError,
    StoredLocalAccess,
    load_local_access,
)
from .local_discovery import MAX_HINTS, dismiss_configured_discoveries
from .local_probe import (
    LocalEndpoint,
    ProbeContext,
    address_candidates,
    certificate_matches,
)
from .local_transport import (
    LocalTransportError,
    PortRole,
    StaleLocalRequest,
    inspect_local_certificate,
)
from .repair_scope import repair_data, repair_id

_CANDIDATES = f"{DOMAIN}.local_address_candidates"
_CHECKS = f"{DOMAIN}.local_address_checks"
# One handshake per announced address within this time; the result is kept.
CHECK_INTERVAL = 300.0
# Handshakes of all announcements together within ``CHECK_WINDOW``.
MAX_CHECKS = 8
CHECK_WINDOW = 600.0
# Offered addresses kept at once (one per local access).
MAX_CANDIDATES = 16
# Access states in which the saved address may be stale. A pause is one only
# when the gateway did not ask for it itself.
_DOWN_STATES = frozenset({"unreachable", "certificate_changed", "starting"})
_GATEWAY_PAUSES = frozenset({"rate_limited", "service_unavailable"})
# The handshake belongs to no access; it never shares a request budget.
_CHECK_CONTEXT = IdentityContext(
    GatewayNamespace.K40_LOCAL, "address_check", "address_check", 0
)


@dataclass(slots=True)
class AddressCandidate:
    """A new address proven for one local access, in memory only."""

    entry_id: str = field(repr=False)
    connection_id: str = field(repr=False)
    endpoint: LocalEndpoint = field(repr=False)
    previous: LocalEndpoint = field(repr=False)
    pin: str = field(repr=False)
    unsubscribe: Callable[[], None] | None = field(default=None, repr=False)


@dataclass(slots=True)
class _Checks:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Per address: when it was checked and the note it led to, if any.
    results: dict[LocalEndpoint, tuple[float, str | None]] = field(default_factory=dict)
    times: deque[float] = field(default_factory=deque)

    def recent(self, endpoint: LocalEndpoint) -> tuple[float, str | None] | None:
        result = self.results.get(endpoint)
        if result is None or monotonic() - result[0] >= CHECK_INTERVAL:
            self.results.pop(endpoint, None)
            return None
        return result

    def admit(self) -> bool:
        now = monotonic()
        while self.times and now - self.times[0] >= CHECK_WINDOW:
            self.times.popleft()
        if len(self.times) >= MAX_CHECKS:
            return False
        self.times.append(now)
        return True

    def note(self, endpoint: LocalEndpoint, issue_id: str | None) -> None:
        if endpoint not in self.results and len(self.results) >= MAX_HINTS:
            del self.results[next(iter(self.results))]
        self.results[endpoint] = (monotonic(), issue_id)


@dataclass(frozen=True, slots=True)
class _Access:
    entry: ConfigEntry[Any] = field(repr=False)
    connection_id: str = field(repr=False)
    access: StoredLocalAccess = field(repr=False)
    enabled: bool


def _candidates(hass: HomeAssistant) -> dict[str, AddressCandidate]:
    return cast(dict[str, AddressCandidate], hass.data.setdefault(_CANDIDATES, {}))


def _checks(hass: HomeAssistant) -> _Checks:
    return cast(_Checks, hass.data.setdefault(_CHECKS, _Checks()))


def _accesses(hass: HomeAssistant) -> Iterator[_Access]:
    """Every saved, verified local access of every entry."""
    for entry in hass.config_entries.async_entries(DOMAIN, include_ignore=False):
        if entry.version != 4:
            continue
        try:
            current = load_configuration(
                entry.entry_id,
                entry.data,
                entry.options,
                version=entry.version,
                minor_version=entry.minor_version,
            )
        except ConfigurationError:
            continue
        options = cast(dict[str, Any], current.to_options()["connections"])
        for key, connection in current.connections.items():
            if connection.kind != LOCAL_KIND:
                continue
            try:
                access = load_local_access(connection.to_data())
            except LocalCredentialError:
                continue
            yield _Access(entry, key, access, options[key].get("enabled") is True)


def access_down(entry: ConfigEntry[Any], connection_id: str) -> bool:
    """The running local access does not reach its gateway right now.

    Not loaded, switched off, refused sign-ins and a pause the gateway asked
    for are not "down": the saved address works or the cause is another one.
    """
    status = access_status(entry, connection_id)
    if status.state in _DOWN_STATES:
        return True
    if status.state != "paused":
        return False
    return not local_pause_reasons(entry, connection_id) & _GATEWAY_PAUSES


async def _handshake(endpoint: LocalEndpoint) -> bytes | None:
    """The data port's full-leaf SHA-256 after one handshake, or None."""
    try:
        observation = await inspect_local_certificate(
            ProbeContext(_CHECK_CONTEXT, endpoint),
            PortRole.RESOURCES,
            is_current=lambda: True,
        )
    except LocalTransportError, StaleLocalRequest:
        return None
    return observation.sha256


def _matching(hass: HomeAssistant, digest: bytes) -> list[_Access]:
    return [
        item
        for item in _accesses(hass)
        if certificate_matches(digest, item.access.resource_sha256)
    ]


def issue_id_for(entry: ConfigEntry[Any], connection_id: str) -> str:
    return repair_id(LOCAL_ADDRESS_ISSUE_PREFIX, entry, connection_id)


def candidate(hass: HomeAssistant, issue_id: str) -> AddressCandidate | None:
    return _candidates(hass).get(issue_id)


@callback
def withdraw(hass: HomeAssistant, issue_id: str) -> None:
    """Forget an offered address and remove its note."""
    item = _candidates(hass).pop(issue_id, None)
    if item is not None and item.unsubscribe is not None:
        item.unsubscribe()
    ir.async_delete_issue(hass, DOMAIN, issue_id)


@callback
def _offer(hass: HomeAssistant, found: _Access, endpoint: LocalEndpoint) -> str:
    entry, connection_id = found.entry, found.connection_id
    issue_id = issue_id_for(entry, connection_id)
    withdraw(hass, issue_id)
    candidates = _candidates(hass)
    while len(candidates) >= MAX_CANDIDATES:
        withdraw(hass, next(iter(candidates)))
    item = AddressCandidate(
        entry.entry_id,
        connection_id,
        endpoint,
        found.access.endpoint,
        found.access.resource_sha256,
    )
    candidates[issue_id] = item

    @callback
    def recheck() -> None:
        current = hass.config_entries.async_get_entry(item.entry_id)
        if candidates.get(issue_id) is item and (
            current is None or not access_down(current, item.connection_id)
        ):
            withdraw(hass, issue_id)

    # The note goes away as soon as the access reaches its gateway again.
    runtime = getattr(getattr(entry, "runtime_data", None), "connections", None)
    subscribe = getattr(runtime, "subscribe", None)
    if callable(subscribe):
        item.unsubscribe = subscribe(recheck, local_values=False)
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="local_address_moved",
        translation_placeholders={
            "previous": item.previous.host,
            "address": endpoint.host,
        },
        data=repair_data(entry, connection_id),
    )
    return issue_id


async def follow_address(
    hass: HomeAssistant, endpoints: tuple[LocalEndpoint, ...]
) -> bool:
    """Offer an announced address to the one local access whose gateway it is.

    True when the announcement belongs to a configured gateway that moved
    (it is then offered as a repair note, not as a new gateway). No token is
    sent; at most ``MAX_ADDRESS_CHECKS`` handshakes per announcement.
    """
    accesses = list(_accesses(hass))
    if not any(a.enabled and access_down(a.entry, a.connection_id) for a in accesses):
        return False
    candidates = _candidates(hass)
    for item in candidates.values():
        if item.endpoint in endpoints:
            return True  # already offered
    checks = _checks(hass)
    configured = {a.access.endpoint for a in accesses}
    async with checks.lock:
        for endpoint in address_candidates(endpoints, configured=configured):
            recent = checks.recent(endpoint)
            if recent is not None:
                if recent[1] is not None and recent[1] in candidates:
                    return True
                continue
            if not checks.admit():
                return False
            digest = await _handshake(endpoint)
            if digest is None:
                checks.note(endpoint, None)
                continue
            # Read again after the handshake: the setup may have changed.
            found = _matching(hass, digest)
            if not found:
                checks.note(endpoint, None)
                continue  # another device; the next address may be the gateway
            if len(found) != 1:
                checks.note(endpoint, None)
                return False  # several accesses know this certificate
            match = found[0]
            if (
                not match.enabled
                or match.access.endpoint == endpoint
                or not access_down(match.entry, match.connection_id)
            ):
                checks.note(endpoint, None)
                return False  # the gateway works at its saved address
            checks.note(endpoint, _offer(hass, match, endpoint))
            return True
    return False


async def apply_address(hass: HomeAssistant, issue_id: str) -> str:
    """Save an offered address after the user's confirmation.

    The certificate is proven again right before the change; the change
    itself follows the proof without an intervening await. Returns
    ``"applied"`` or the reason nothing changed.
    """
    item = candidate(hass, issue_id)
    if item is None:
        return "local_address_expired"
    entry = hass.config_entries.async_get_entry(item.entry_id)
    if entry is None:
        withdraw(hass, issue_id)
        return "entry_not_found"
    if not access_down(entry, item.connection_id):
        withdraw(hass, issue_id)
        return "local_address_recovered"
    digest = await _handshake(item.endpoint)
    if candidate(hass, issue_id) is not item:
        return "configuration_changed"
    # The entry may have been removed or set up again during the handshake.
    if hass.config_entries.async_get_entry(item.entry_id) is not entry:
        withdraw(hass, issue_id)
        return "entry_not_found"
    if digest is None or not certificate_matches(digest, item.pin):
        withdraw(hass, issue_id)
        return "local_address_unproven"
    if len(_matching(hass, digest)) > 1:
        # Another access knows this certificate meanwhile; that this access
        # still holds it is checked with its saved data below.
        withdraw(hass, issue_id)
        return "local_address_unproven"
    if not access_down(entry, item.connection_id):
        withdraw(hass, issue_id)
        return "local_address_recovered"
    management = ConfigurationManagement(hass, entry)
    try:
        edit = management.edit(item.connection_id)
        saved = load_local_access(
            management.current().connections[item.connection_id].to_data()
        )
        if saved.endpoint != item.previous or saved.resource_sha256 != item.pin:
            raise ConfigurationError("configuration_changed")
        management.set_local_address(edit, item.endpoint.host)
    except ConfigurationError, LocalCredentialError, KeyError:
        withdraw(hass, issue_id)
        return "configuration_changed"
    withdraw(hass, issue_id)
    # A notice for the new address belongs to a configured gateway now.
    dismiss_configured_discoveries(hass)
    return "applied"
