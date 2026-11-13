"""Address follow-up of a moved gateway against synthetic loopback servers.

Identity comes only from the certificate confirmed at setup: one TLS
handshake without HTTP or token. Nothing changes without confirmation.
"""

from __future__ import annotations

import asyncio
import ipaddress
from contextlib import suppress
from dataclasses import replace
from types import SimpleNamespace

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir

from custom_components.bosch_buderus_heating import connection_status as status
from custom_components.bosch_buderus_heating import local_address as address
from custom_components.bosch_buderus_heating import local_flow as flow
from custom_components.bosch_buderus_heating import local_transport as transport
from custom_components.bosch_buderus_heating.configuration import (
    LOCAL_KIND,
    ConfigurationError,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.local_credentials import (
    load_local_access,
)
from custom_components.bosch_buderus_heating.local_probe import (
    MAX_ADDRESS_CHECKS,
    LocalEndpoint,
    address_candidates,
    certificate_matches,
)
from custom_components.bosch_buderus_heating.local_state import LocalCondition
from custom_components.bosch_buderus_heating.managed_local import LocalSource
from custom_components.bosch_buderus_heating.repairs import (
    InvalidRepairFlow,
    LocalAddressRepairFlow,
    async_create_fix_flow,
)
from tests.test_connection_status import LAN, Runtime, entry_with, ready_local
from tests.test_local_discovery import discover, finish_options
from tests.test_local_flow import journal as journal
from tests.test_local_flow import registered
from tests.test_local_flow import supported as supported
from tests.test_local_transport import certificates as certificates

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")

MOVED = "192.0.2.50"
OTHER = "192.0.2.60"


# ------------------------------------------------------------ pure helpers


def test_candidates_are_literal_addresses_ipv4_first_and_bounded() -> None:
    endpoints = tuple(
        LocalEndpoint(host)
        for host in (
            "fe80::1%eth0",
            "gateway.example.invalid",
            "2001:db8::5",
            "192.0.2.7",
            "192.0.2.8",
        )
    )
    assert address_candidates(endpoints, limit=8) == (
        LocalEndpoint("192.0.2.7"),
        LocalEndpoint("192.0.2.8"),
        LocalEndpoint("2001:db8::5"),
        LocalEndpoint("fe80::1%eth0"),
    )
    assert address_candidates(endpoints) == endpoints[3:5]
    assert len(address_candidates(endpoints)) == MAX_ADDRESS_CHECKS
    # A configured address is never checked again; names never at all.
    assert address_candidates(
        endpoints, configured={LocalEndpoint("192.0.2.7")}, limit=1
    ) == (LocalEndpoint("192.0.2.8"),)
    assert address_candidates((LocalEndpoint("gateway.example.invalid"),)) == ()


@pytest.mark.parametrize(
    ("observed", "pinned", "expected"),
    [
        (b"\x01" * 32, "01" * 32, True),
        (b"\x01" * 32, "02" * 32, False),
        (b"\x01" * 31, "01" * 31, False),
        (b"\x01" * 32, "01" * 31, False),
        (b"\xab" * 32, "ab" * 32, True),
        (b"\xab" * 32, "AB" * 32, False),
        (b"\x01" * 32, None, False),
        ("01" * 32, "01" * 32, False),
    ],
)
def test_certificate_must_match_exactly(observed, pinned, expected) -> None:
    assert certificate_matches(observed, pinned) is expected


# ---------------------------------------------- stable-address hint (status)


HINT_DE = "Tipp: Ändert sich die Adresse des Gateways"
HINT_EN = "Tip: if the gateway's address changes"


@pytest.mark.parametrize(
    ("condition", "reason", "shown"),
    [
        (LocalCondition.UNAVAILABLE, None, True),
        (LocalCondition.PAUSED, "connection", True),
        (LocalCondition.PAUSED, "rate_limited", False),
        (LocalCondition.PAUSED, None, False),
        (LocalCondition.READY, None, False),
        (LocalCondition.AUTH_REQUIRED, None, False),
        (LocalCondition.CERTIFICATE_CHANGED, None, False),
    ],
)
async def test_address_hint_only_while_the_gateway_is_not_reached(
    hass, condition, reason, shown
) -> None:
    hass.config.language = "de"
    local = ready_local()
    local.condition = condition
    runtime = Runtime(hass, local=local)
    runtime.local_driver.pause_reason = reason
    entry = entry_with(runtime)
    text = status.connection_description(hass, entry, LAN)
    assert (HINT_DE in text) is shown
    hass.config.language = "en"
    assert (HINT_EN in status.connection_description(hass, entry, LAN)) is shown
    # Never on the cloud access, and never with a request.
    assert HINT_EN not in status.connection_description(hass, entry, "cloud")
    runtime.local_driver.test_connection.assert_not_awaited()


@pytest.mark.parametrize("language", ["sv", "cs", "fr"])
async def test_address_hint_in_other_languages(hass, language) -> None:
    hass.config.language = language
    local = ready_local()
    local.condition = LocalCondition.UNAVAILABLE
    text = status.connection_description(
        hass, entry_with(Runtime(hass, local=local)), LAN
    )
    assert HINT_EN not in text and "DHCP" in text


@pytest.mark.parametrize(
    ("result", "shown"),
    [("unreachable", True), ("ok", False), ("other_gateway", False)],
)
async def test_address_hint_after_a_failed_test(hass, result, shown) -> None:
    hass.config.language = "en"
    runtime = Runtime(hass, local=ready_local())
    runtime.local_driver.test_connection.return_value = (result, 40)
    text = await status.test_connection(hass, entry_with(runtime), LAN)
    assert (HINT_EN in text) is shown


def test_local_source_names_its_pause_reason() -> None:
    source = LocalSource.__new__(LocalSource)
    source.service = None
    source._condition = LocalCondition.PAUSED
    source._changes = [{"condition": "paused", "reason": "connection"}]
    assert source.pause_reason == "connection"
    source._condition = LocalCondition.INITIALIZING
    assert source.pause_reason is None
    source.service = SimpleNamespace(
        budget=SimpleNamespace(pause_reason="rate_limited")
    )
    assert source.pause_reason == "rate_limited"


@pytest.mark.parametrize(
    ("condition", "reason", "down"),
    [
        (LocalCondition.UNAVAILABLE, None, True),
        (LocalCondition.CERTIFICATE_CHANGED, None, True),
        (LocalCondition.INITIALIZING, None, True),
        (LocalCondition.PAUSED, "connection", True),
        (LocalCondition.PAUSED, None, True),
        (LocalCondition.PAUSED, "rate_limited", False),
        (LocalCondition.PAUSED, "service_unavailable", False),
        (LocalCondition.READY, None, False),
        (LocalCondition.AUTH_REQUIRED, None, False),
    ],
)
async def test_which_states_count_as_not_reaching_the_gateway(
    hass, condition, reason, down
) -> None:
    local = ready_local()
    local.condition = condition
    runtime = Runtime(hass, local=local)
    runtime.local_driver.pause_reason = reason
    assert address.access_down(entry_with(runtime), LAN) is down
    assert not address.access_down(SimpleNamespace(runtime_data=None, options={}), LAN)


# ------------------------------------------------ loopback gateways (K40 API)


@pytest.fixture
async def lan(certificates, monkeypatch):
    """Synthetic gateways on loopback ports behind documentation IPs."""
    servers, handshakes, requests = [], [], []
    routes: dict[str, int] = {}

    async def start(announced: str, certificate: int):
        async def handle(reader, writer):
            handshakes.append(announced)
            with suppress(asyncio.IncompleteReadError, ConnectionError, TimeoutError):
                async with asyncio.timeout(0.5):
                    data = await reader.read(1)
                    if data:
                        requests.append(announced)
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

        server = await asyncio.start_server(
            handle, "127.0.0.1", 0, ssl=certificates[certificate][0]
        )
        servers.append(server)
        routes[announced] = server.sockets[0].getsockname()[1]
        return server

    async def resolve(endpoint):
        # The production resolver rejects loopback; only these routes map,
        # each announced address to its own loopback port.
        if endpoint.host not in routes:
            raise transport.LocalTransportError(transport.TransportFailure.CONNECT)
        monkeypatch.setattr(LocalEndpoint, "resource_port", routes[endpoint.host])
        return ("127.0.0.1",)

    monkeypatch.setattr(transport, "_resolve", resolve)
    yield SimpleNamespace(
        start=start, handshakes=handshakes, requests=requests, routes=routes
    )
    for server in servers:
        server.close()
        await server.wait_closed()


@pytest.fixture
def down(monkeypatch):
    """Which local accesses do not reach their gateway (default: all)."""
    state = {"down": True}
    monkeypatch.setattr(address, "access_down", lambda entry, key: state["down"])
    return state


async def configured(hass, monkeypatch, certificates, *, certificate=0):
    """A finished local access whose data port pin is a loopback certificate."""
    entry, management, _, target = registered(hass)
    original = flow.inspect_local_certificate

    async def pinned(context, role, *, is_current, budget):
        observed = await original(context, role, is_current=is_current, budget=budget)
        return replace(observed, sha256=certificates[certificate][1])

    monkeypatch.setattr(flow, "inspect_local_certificate", pinned)
    await finish_options(hass, entry, target)
    local = next(
        k for k, c in management.current().connections.items() if c.kind == LOCAL_KIND
    )
    return entry, management, local


def access(management, local):
    return load_local_access(management.current().connections[local].to_data())


def issues(hass):
    return [
        issue
        for (domain, _), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN and issue.translation_key == "local_address_moved"
    ]


async def repair(hass, issue_id):
    handler = await async_create_fix_flow(
        hass, issue_id, dict(ir.async_get(hass).async_get_issue(DOMAIN, issue_id).data)
    )
    assert isinstance(handler, LocalAddressRepairFlow)
    handler.hass = hass
    return handler


async def test_moved_gateway_is_offered_and_saved_only_after_confirmation(
    hass, supported, journal, lan, down, monkeypatch, certificates
):
    _, management, local = await configured(hass, monkeypatch, certificates)
    before = management.current()
    await lan.start(MOVED, 0)
    result = await discover(hass, host=MOVED, hostname="renamed.example.invalid.")
    assert result["reason"] == "local_address_moved"
    # One handshake, no HTTP request and no token, nothing saved yet.
    assert lan.handshakes == [MOVED] and not lan.requests
    assert management.current() == before
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    (issue,) = issues(hass)
    assert issue.is_fixable and not issue.is_persistent
    assert issue.translation_placeholders["address"] == MOVED
    handler = await repair(hass, issue.issue_id)
    form = await handler.async_step_init()
    assert form["step_id"] == "confirm"
    assert form["data_schema"]({})["confirm"] is False
    assert form["description_placeholders"]["address"] == MOVED
    # Submitting without the tick changes nothing and sends nothing.
    again = await handler.async_step_confirm({"confirm": False})
    assert again["errors"] == {"base": "confirmation_required"}
    assert management.current() == before and lan.handshakes == [MOVED]
    done = await handler.async_step_confirm({"confirm": True})
    assert done["type"] == FlowResultType.CREATE_ENTRY
    # The certificate was proven again right before the change.
    assert lan.handshakes == [MOVED, MOVED] and not lan.requests
    moved, old = (
        access(management, local),
        load_local_access(before.connections[local].to_data()),
    )
    assert moved.endpoint == LocalEndpoint(MOVED)
    assert moved.credentials == old.credentials
    assert moved.resource_sha256 == old.resource_sha256
    assert moved.authentication_sha256 == old.authentication_sha256
    assert moved.gateway_id == old.gateway_id
    assert not issues(hass)
    # The gateway is now configured at that address: no notice, no handshake.
    result = await discover(hass, host=MOVED, hostname="renamed.example.invalid.")
    assert result["reason"] == "already_configured"
    assert lan.handshakes == [MOVED, MOVED]


async def test_forged_announcement_with_a_foreign_certificate_changes_nothing(
    hass, supported, journal, lan, down, monkeypatch, certificates
):
    _, management, _ = await configured(hass, monkeypatch, certificates)
    before = management.current()
    # Same name and TXT fields as the gateway, but another certificate.
    await lan.start(MOVED, 1)
    result = await discover(hass, host=MOVED, hostname="gateway.example.invalid.")
    assert result["reason"] == "already_configured"  # name matches the saved host
    result = await discover(hass, host=MOVED, hostname="renamed.example.invalid.")
    assert result["type"] == FlowResultType.FORM  # an ordinary discovery notice
    assert lan.handshakes == [MOVED] and not lan.requests
    assert management.current() == before and not issues(hass)


async def test_two_gateways_only_the_own_certificate_is_offered(
    hass, supported, journal, lan, down, monkeypatch, certificates
):
    _, management, local = await configured(hass, monkeypatch, certificates)
    await lan.start(OTHER, 1)
    await lan.start(MOVED, 0)
    other = await discover(
        hass, name="another", host=OTHER, hostname="other.example.invalid."
    )
    assert other["type"] == FlowResultType.FORM
    assert not issues(hass)
    own = await discover(hass, host=MOVED, hostname="renamed.example.invalid.")
    assert own["reason"] == "local_address_moved"
    (issue,) = issues(hass)
    assert issue.translation_placeholders["address"] == MOVED
    assert lan.handshakes == [OTHER, MOVED] and not lan.requests
    assert access(management, local).endpoint == LocalEndpoint(
        "gateway.example.invalid"
    )


async def test_announcement_during_operation_sends_nothing(
    hass, supported, journal, lan, down, monkeypatch, certificates
):
    _, management, _ = await configured(hass, monkeypatch, certificates)
    before = management.current()
    down["down"] = False
    await lan.start(MOVED, 0)
    result = await discover(hass, host=MOVED, hostname="renamed.example.invalid.")
    assert result["type"] == FlowResultType.FORM  # unchanged behaviour
    assert not lan.handshakes and not issues(hass)
    assert management.current() == before


async def test_repeated_announcements_are_checked_once(
    hass, supported, journal, lan, down, monkeypatch, certificates
):
    await configured(hass, monkeypatch, certificates)
    await lan.start(MOVED, 0)
    for _ in range(3):
        result = await discover(hass, host=MOVED, hostname="renamed.example.invalid.")
        assert result["reason"] == "local_address_moved"
    assert lan.handshakes == [MOVED] and len(issues(hass)) == 1


async def test_unanswered_address_is_not_checked_again_at_once(
    hass, supported, journal, lan, down, monkeypatch, certificates
):
    await configured(hass, monkeypatch, certificates)
    calls = []

    async def handshake(endpoint):
        calls.append(endpoint)

    monkeypatch.setattr(address, "_handshake", handshake)
    for _ in range(2):
        result = await discover(hass, host=MOVED, hostname="renamed.example.invalid.")
        assert result["type"] in (FlowResultType.FORM, FlowResultType.ABORT)
        for progress in hass.config_entries.flow.async_progress_by_handler(DOMAIN):
            hass.config_entries.flow.async_abort(progress["flow_id"])
    assert calls == [LocalEndpoint(MOVED)] and not issues(hass)


async def test_total_handshakes_are_bounded(hass, monkeypatch):
    checks = address._Checks()
    assert all(checks.admit() for _ in range(address.MAX_CHECKS))
    assert not checks.admit()


async def test_certificate_known_to_two_accesses_is_never_offered(
    hass, monkeypatch, certificates
):
    pin = certificates[0][1].hex()
    item = SimpleNamespace(
        entry=SimpleNamespace(entry_id="e"),
        connection_id="c",
        access=SimpleNamespace(
            endpoint=LocalEndpoint("192.0.2.1"), resource_sha256=pin
        ),
        enabled=True,
    )
    twin = SimpleNamespace(**{**vars(item), "connection_id": "d"})
    monkeypatch.setattr(address, "_accesses", lambda hass: iter([item, twin]))
    monkeypatch.setattr(address, "access_down", lambda entry, key: True)

    async def handshake(endpoint):
        return certificates[0][1]

    monkeypatch.setattr(address, "_handshake", handshake)
    assert not await address.follow_address(hass, (LocalEndpoint(MOVED),))
    assert not address._candidates(hass)
    monkeypatch.setattr(address, "_accesses", lambda hass: iter([item]))
    address._checks(hass).results.clear()
    offered = []
    monkeypatch.setattr(
        address, "_offer", lambda hass, found, endpoint: offered.append(endpoint) or "i"
    )
    assert await address.follow_address(hass, (LocalEndpoint(MOVED),))
    assert offered == [LocalEndpoint(MOVED)]


async def test_certificate_changed_before_confirmation_changes_nothing(
    hass, supported, journal, lan, down, monkeypatch, certificates
):
    _, management, _ = await configured(hass, monkeypatch, certificates)
    before = management.current()
    server = await lan.start(MOVED, 0)
    await discover(hass, host=MOVED, hostname="renamed.example.invalid.")
    (issue,) = issues(hass)
    handler = await repair(hass, issue.issue_id)
    await handler.async_step_init()
    # Another device took the address before the user confirmed.
    server.close()
    await server.wait_closed()
    lan.routes.clear()
    await lan.start(MOVED, 1)
    result = await handler.async_step_confirm({"confirm": True})
    assert result["reason"] == "local_address_unproven"
    assert management.current() == before and not issues(hass)
    assert not lan.requests


async def test_recovered_access_needs_no_change(
    hass, supported, journal, lan, down, monkeypatch, certificates
):
    _, management, _ = await configured(hass, monkeypatch, certificates)
    before = management.current()
    await lan.start(MOVED, 0)
    await discover(hass, host=MOVED, hostname="renamed.example.invalid.")
    (issue,) = issues(hass)
    handler = await repair(hass, issue.issue_id)
    await handler.async_step_init()
    down["down"] = False
    result = await handler.async_step_confirm({"confirm": True})
    assert result["reason"] == "local_address_recovered"
    assert management.current() == before and not issues(hass)
    assert lan.handshakes == [MOVED]


async def test_changed_access_before_confirmation_changes_nothing(
    hass, supported, journal, lan, down, monkeypatch, certificates
):
    _, management, local = await configured(hass, monkeypatch, certificates)
    await lan.start(MOVED, 0)
    await discover(hass, host=MOVED, hostname="renamed.example.invalid.")
    (issue,) = issues(hass)
    handler = await repair(hass, issue.issue_id)
    await handler.async_step_init()
    # The user set the access to another address meanwhile.
    management.set_local_address(management.edit(local), "192.0.2.99")
    changed = management.current()
    result = await handler.async_step_confirm({"confirm": True})
    assert result["reason"] == "configuration_changed"
    assert management.current() == changed and not issues(hass)


async def test_note_is_withdrawn_when_the_access_works_again(hass, monkeypatch):
    listeners = []
    runtime = SimpleNamespace(
        subscribe=lambda listener, local_values: (
            listeners.append(listener) or (lambda: listeners.remove(listener))
        )
    )
    entry = SimpleNamespace(
        entry_id="e",
        version=3,
        title="t",
        runtime_data=SimpleNamespace(connections=runtime),
    )
    monkeypatch.setattr(hass.config_entries, "async_get_entry", lambda key: entry)
    state = {"down": True}
    monkeypatch.setattr(address, "access_down", lambda entry, key: state["down"])
    found = SimpleNamespace(
        entry=entry,
        connection_id="c",
        access=SimpleNamespace(
            endpoint=LocalEndpoint("192.0.2.1"), resource_sha256="00" * 32
        ),
    )
    issue_id = address._offer(hass, found, LocalEndpoint(MOVED))
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    listeners[0]()
    assert address.candidate(hass, issue_id) is not None
    state["down"] = False
    listeners[0]()
    assert address.candidate(hass, issue_id) is None and not listeners
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_note_after_a_restart_has_no_address_to_offer(
    hass, supported, journal, lan, down, monkeypatch, certificates
):
    await configured(hass, monkeypatch, certificates)
    await lan.start(MOVED, 0)
    await discover(hass, host=MOVED, hostname="renamed.example.invalid.")
    (issue,) = issues(hass)
    handler = await repair(hass, issue.issue_id)
    hass.data.pop(address._CANDIDATES)  # kept in memory only
    result = await handler.async_step_init()
    assert result["reason"] == "local_address_expired"
    assert not issues(hass)


async def test_repair_for_a_foreign_issue_id_is_rejected(
    hass, supported, journal, monkeypatch, certificates
):
    entry, _, local = await configured(hass, monkeypatch, certificates)
    issue_id = address.issue_id_for(entry, local)
    for data in (
        {"entry_id": entry.entry_id, "connection_id": "other"},
        {"entry_id": entry.entry_id, "connection_id": local, "binding_id": "b"},
    ):
        assert isinstance(
            await async_create_fix_flow(hass, issue_id, data), InvalidRepairFlow
        )


async def test_saved_address_needs_a_valid_different_host(
    hass, supported, journal, monkeypatch, certificates
):
    _, management, local = await configured(hass, monkeypatch, certificates)
    for host in ("gateway.example.invalid", "127.0.0.1", "not a host"):
        with pytest.raises(ConfigurationError):
            management.set_local_address(management.edit(local), host)
    cloud = next(
        k for k, c in management.current().connections.items() if c.kind != LOCAL_KIND
    )
    with pytest.raises(ConfigurationError, match="unsupported_connection"):
        management.set_local_address(management.edit(cloud), MOVED)
    assert ipaddress.ip_address(MOVED)  # documentation address only


async def test_unanswered_announcement_stays_an_ordinary_notice(
    hass, supported, journal, lan, down, monkeypatch, certificates
):
    _, management, _ = await configured(hass, monkeypatch, certificates)
    before = management.current()
    # No gateway answers at the announced address (connection refused).
    result = await discover(hass, host=MOVED, hostname="renamed.example.invalid.")
    assert result["type"] == FlowResultType.FORM
    assert not lan.handshakes and not issues(hass)
    assert management.current() == before


def fake_access(key, pin, *, enabled=True, host="192.0.2.1"):
    return SimpleNamespace(
        entry=SimpleNamespace(entry_id="e"),
        connection_id=key,
        access=SimpleNamespace(endpoint=LocalEndpoint(host), resource_sha256=pin),
        enabled=enabled,
    )


@pytest.mark.parametrize("reason", ["running", "disabled"])
async def test_matching_access_that_works_or_is_off_is_not_offered(
    hass, monkeypatch, reason
):
    match = fake_access("c", "01" * 32, enabled=reason != "disabled")
    other = fake_access("d", "02" * 32, host="192.0.2.2")
    monkeypatch.setattr(address, "_accesses", lambda hass: iter([match, other]))
    # Only the other access is down; the gateway itself works or is off.
    monkeypatch.setattr(address, "access_down", lambda entry, key: key == "d")

    async def handshake(endpoint):
        return b"\x01" * 32

    monkeypatch.setattr(address, "_handshake", handshake)
    assert not await address.follow_address(hass, (LocalEndpoint(MOVED),))
    assert not address._candidates(hass)


async def test_exhausted_check_budget_sends_nothing(hass, monkeypatch):
    monkeypatch.setattr(
        address, "_accesses", lambda hass: iter([fake_access("c", "01" * 32)])
    )
    monkeypatch.setattr(address, "access_down", lambda entry, key: True)
    calls = []

    async def handshake(endpoint):
        calls.append(endpoint)

    monkeypatch.setattr(address, "_handshake", handshake)
    checks = address._checks(hass)
    while checks.admit():
        pass
    assert not await address.follow_address(hass, (LocalEndpoint(MOVED),))
    assert not calls


def test_bookkeeping_is_bounded(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(address, "monotonic", lambda: now[0])
    checks = address._Checks()
    for _ in range(address.MAX_CHECKS):
        assert checks.admit()
    assert not checks.admit()
    now[0] += address.CHECK_WINDOW
    assert checks.admit()
    for number in range(address.MAX_HINTS + 3):
        checks.note(LocalEndpoint(f"192.0.2.{number + 1}"), None)
    assert len(checks.results) == address.MAX_HINTS
    assert checks.recent(LocalEndpoint("192.0.2.1")) is None
    assert checks.recent(LocalEndpoint("192.0.2.67")) is not None
    now[0] += address.CHECK_INTERVAL
    assert checks.recent(LocalEndpoint("192.0.2.67")) is None


async def test_offered_addresses_are_bounded(hass):
    for number in range(address.MAX_CANDIDATES + 2):
        entry = SimpleNamespace(
            entry_id=f"e{number}", version=3, title="t", runtime_data=None
        )
        found = SimpleNamespace(
            entry=entry,
            connection_id="c",
            access=SimpleNamespace(
                endpoint=LocalEndpoint("192.0.2.1"), resource_sha256="00" * 32
            ),
        )
        address._offer(hass, found, LocalEndpoint(MOVED))
    assert len(address._candidates(hass)) == address.MAX_CANDIDATES
    assert len(issues(hass)) == address.MAX_CANDIDATES


@pytest.mark.parametrize(
    "change", ["removed", "replaced", "twin", "recovered", "repinned"]
)
async def test_confirmation_rechecks_after_the_handshake(
    hass, supported, journal, lan, down, monkeypatch, certificates, change
):
    entry, management, local = await configured(hass, monkeypatch, certificates)
    before = management.current()
    await lan.start(MOVED, 0)
    await discover(hass, host=MOVED, hostname="renamed.example.invalid.")
    (issue,) = issues(hass)
    original = address._handshake

    async def handshake(endpoint):
        digest = await original(endpoint)
        if change == "replaced":
            address._candidates(hass)[issue.issue_id] = replace(
                address.candidate(hass, issue.issue_id)
            )
        elif change == "twin":
            monkeypatch.setattr(
                address,
                "_matching",
                lambda hass, digest: [
                    fake_access(local, digest.hex()),
                    fake_access("d", digest.hex()),
                ],
            )
        elif change == "recovered":
            down["down"] = False
        elif change == "repinned":
            # The access was set up again meanwhile with another certificate.
            stored = address.load_local_access
            monkeypatch.setattr(
                address,
                "load_local_access",
                lambda data: replace(stored(data), resource_sha256="00" * 32),
            )
        return digest

    monkeypatch.setattr(address, "_handshake", handshake)
    if change == "removed":
        monkeypatch.setattr(hass.config_entries, "async_get_entry", lambda key: None)
        expected = "entry_not_found"
    else:
        expected = {
            "replaced": "configuration_changed",
            "twin": "local_address_unproven",
            "recovered": "local_address_recovered",
            "repinned": "configuration_changed",
        }[change]
    assert await address.apply_address(hass, issue.issue_id) == expected
    assert entry.data == before.to_data() and entry.options == before.to_options()
    assert not lan.requests
