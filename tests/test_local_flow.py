"""Exercise local dialogs through native HA flow managers and pending storage."""

import asyncio
import ipaddress
import json
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from custom_components.bosch_buderus_heating import local_discovery as discovery
from custom_components.bosch_buderus_heating import local_flow as flow
from custom_components.bosch_buderus_heating import local_setup as setup
from custom_components.bosch_buderus_heating.config_flow import (
    BoschBuderusConfigFlow,
    BoschBuderusOptionsFlow,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.local_auth import AuthState
from custom_components.bosch_buderus_heating.local_completion import (
    ScopedIdentityContract,
)
from custom_components.bosch_buderus_heating.local_probe import SERVICE_TYPE, ProbePath
from custom_components.bosch_buderus_heating.local_support import (
    SUPPORT_KEY,
    LocalSetupSupport,
)
from custom_components.bosch_buderus_heating.local_transport import (
    CertificateObservation,
    LocalProbeExchange,
    PortRole,
)
from tests.helpers.options_menu import connection_choice
from tests.test_local_auth import LABEL
from tests.test_local_auth import auth_gateway as auth_gateway
from tests.test_local_enrollment import journal as journal
from tests.test_local_probe import response
from tests.test_local_setup import TOKEN, registered
from tests.test_local_transport import certificates as certificates
from tests.test_local_transport import wire_reply

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")


@pytest.fixture
def supported(hass, monkeypatch):
    monkeypatch.setattr(BoschBuderusConfigFlow, "MINOR_VERSION", 2)
    hass.data[SUPPORT_KEY] = LocalSetupSupport(
        lambda: True,
        lambda snapshot, target, gateway: ScopedIdentityContract(
            snapshot.owner_id, target, gateway, "synthetic-device-contract"
        ),
    )
    inspections, reads = [], []

    async def inspect(context, role, *, is_current, budget):
        assert is_current()
        inspections.append(role)
        return CertificateObservation(context, role, b"a" * 32, "192.0.2.1", "TLSv1.3")

    async def read(context, token, binding, *, is_current, budget):
        assert is_current() and token == TOKEN
        reads.append(context)
        return LocalProbeExchange(
            tuple(
                response(path, context=context, gatewayId="000123")
                for path in ProbePath
            )
        )

    monkeypatch.setattr(flow, "inspect_local_certificate", inspect)
    monkeypatch.setattr(setup, "read_local_probe", read)
    return inspections, reads


async def settle(hass, manager, result):
    for _ in range(12):
        if result["type"] not in (
            FlowResultType.SHOW_PROGRESS,
            FlowResultType.SHOW_PROGRESS_DONE,
        ):
            return result
        await hass.async_block_till_done()
        result = await manager.async_configure(result["flow_id"])
    raise AssertionError("Flow did not settle")


async def submit(hass, manager, result, data):
    return await settle(
        hass, manager, await manager.async_configure(result["flow_id"], data)
    )


async def start(hass, entry, target):
    manager = hass.config_entries.options
    result = await manager.async_init(entry.entry_id)
    result = await submit(hass, manager, result, {"next_step_id": "local_setup"})
    assert result["step_id"] == "local_target"
    return await submit(hass, manager, result, {"target": target})


async def imported(hass, entry, target):
    manager = hass.config_entries.options
    result = await start(hass, entry, target)
    result = await submit(hass, manager, result, {"address": "gateway.example.invalid"})
    result = await submit(hass, manager, result, {"method": "token"})
    return await submit(hass, manager, result, {"token": TOKEN})


def info(
    *,
    name="arbitrary untrusted name",
    host="192.0.2.1",
    port=9443,
    hostname="gateway.example.invalid.",
):
    return ZeroconfServiceInfo(
        ip_address=ipaddress.ip_address(host),
        ip_addresses=[ipaddress.ip_address(host)],
        port=port,
        hostname=hostname,
        type=SERVICE_TYPE,
        name=name,
        properties={"gatewayId": "untrusted-identity"},
    )


@pytest.mark.parametrize("suspended", [False, True])
async def test_options_import_to_completion_preserves_owner_and_reads_twice(
    hass, supported, journal, monkeypatch, suspended
):
    original = flow.inspect_local_certificate

    async def inspect(*args, **kwargs):
        if suspended:
            await asyncio.sleep(0.001)
        return await original(*args, **kwargs)

    monkeypatch.setattr(flow, "inspect_local_certificate", inspect)
    entry, management, cloud, target = registered(hass)
    before = management.current()
    manager = hass.config_entries.options
    result = await imported(hass, entry, target)
    assert result["step_id"] == "local_certificate"
    assert result["type"] == FlowResultType.FORM
    assert supported[0] == [PortRole.RESOURCES] and not supported[1]
    assert result["data_schema"]({})["confirm"] is False
    result = await submit(hass, manager, result, {"confirm": True})
    assert result.get("step_id") == "local_result", result
    assert len(supported[1]) == 1
    assert management.current() == before
    result = await submit(hass, manager, result, {"confirm": True})
    assert (
        result["type"] == FlowResultType.ABORT
        and result["reason"] == "local_setup_complete"
    )
    assert (
        len(supported[1]) == 2 and len(hass.config_entries.async_entries(DOMAIN)) == 1
    )
    assert management.current().connections[cloud] == before.connections[cloud]
    assert entry.minor_version == 2
    assert (await journal.records())[0].completion is not None
    # Owner decision: a system's first local access uses its local values.
    assert "local_values" not in before.to_options()["targets"][target]
    assert management.current().to_options()["targets"][target]["local_values"]


async def test_a_kept_token_is_sent_only_after_the_new_certificate_is_confirmed(
    hass, supported, journal, monkeypatch
):
    entry, management, _, target = registered(hass)
    manager = hass.config_entries.options
    result = await imported(hass, entry, target)
    result = await submit(hass, manager, result, {"confirm": True})
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["reason"] == "local_setup_complete"
    # The running owner resolves the finished attempt; without one, here.
    (record,) = await journal.records()
    await journal.discard(record.attempt_id, entry.entry_id, confirmed=True)
    management.set_local_values(
        management.edit(
            next(
                key
                for key, value in management.current().connections.items()
                if value.kind == "k40_local"
            )
        ),
        False,
    )
    _, reads = supported
    sent = len(reads)
    original = flow.inspect_local_certificate

    async def changed(context, role, *, is_current, budget):
        observed = await original(context, role, is_current=is_current, budget=budget)
        return replace(observed, sha256=b"b" * 32)  # e.g. after a gateway reset

    monkeypatch.setattr(flow, "inspect_local_certificate", changed)
    local = next(
        key
        for key, value in management.current().connections.items()
        if value.kind == "k40_local"
    )
    result = await manager.async_init(entry.entry_id)
    result = await submit(hass, manager, result, {"next_step_id": "connections"})
    result = await submit(hass, manager, result, connection_choice(hass, result, local))
    result = await submit(hass, manager, result, {"next_step_id": "local_setup"})
    if result["step_id"] == "local_target":
        result = await submit(hass, manager, result, {"target": target})
    assert result["step_id"] == "local_address", result
    result = await submit(hass, manager, result, {"address": "gateway.example.invalid"})
    # The warning step: a new sign-in first and preselected, keeping after it.
    assert result["step_id"] == "local_method_keep"
    selector = next(iter(result["data_schema"].schema.values()))
    assert selector.config["options"] == ["label", "keep", "token"]
    assert result["data_schema"]({})["method"] == "label"
    result = await submit(hass, manager, result, {"method": "keep"})
    assert result["step_id"] == "local_certificate", result.get("errors")
    assert len(reads) == sent  # nothing sent with the token yet
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["step_id"] == "local_result"
    assert len(reads) == sent + 1  # the read check with the kept token
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["reason"] == "local_setup_complete"
    # A changed local access keeps the stored choice of local values.
    assert management.current().to_options()["targets"][target]["local_values"] is (
        False
    )


@pytest.mark.parametrize(
    "step",
    [
        "local_setup",
        "local_owner",
        "local_target",
        "local_address",
        "local_method",
        "local_method_keep",
        "local_certificate",
        "local_auth",
        "local_token",
        "local_pending",
        "local_result",
        "local_retry",
        "local_done",
    ],
)
async def test_hidden_entrypoints_reject_without_runtime(hass, journal, step):
    entry, _, _, _ = registered(hass)
    handler = BoschBuderusOptionsFlow(entry)
    handler.hass = hass
    with patch.object(flow, "inspect_local_certificate", new=AsyncMock()) as inspect:
        result = await getattr(handler, "async_step_" + step)()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "local_setup_unavailable"
    inspect.assert_not_called()
    assert not journal._path.exists()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "local_setup" not in result["menu_options"]


async def test_minor_version_alone_never_enables_local_setup(hass, monkeypatch):
    monkeypatch.setattr(BoschBuderusConfigFlow, "MINOR_VERSION", 2)
    entry, _, _, _ = registered(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "local_setup" not in result["menu_options"]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=info()
    )
    assert result["reason"] == "local_setup_unavailable"


async def test_native_discovery_selects_existing_owner_without_secret_access(
    hass, supported, journal
):
    entry, _, _, target = registered(hass)
    manager = hass.config_entries.flow
    result = await manager.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=info()
    )
    assert (
        result["step_id"] == "local_owner"
        and not supported[0]
        and not journal._path.exists()
    )
    result = await submit(hass, manager, result, {"owner": entry.entry_id})
    result = await submit(hass, manager, result, {"target": target})
    assert result["step_id"] == "local_address"
    schema = next(iter(result["data_schema"].schema.values()))
    assert "192.0.2.1" in str(schema.config)
    result = await submit(hass, manager, result, {"address": "192.0.2.1"})
    result = await submit(hass, manager, result, {"method": "token"})
    result = await submit(hass, manager, result, {"token": TOKEN})
    result = await submit(hass, manager, result, {"confirm": True})
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["reason"] == "local_setup_complete"
    assert [e.entry_id for e in hass.config_entries.async_entries(DOMAIN)] == [
        entry.entry_id
    ]


async def test_an_ignored_discovery_stays_ignored(hass, supported, journal):
    entry, _, _, _ = registered(hass)
    manager = hass.config_entries.flow
    result = await manager.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=info()
    )
    assert result["step_id"] == "local_owner"
    unique_id = manager.async_get(result["flow_id"])["context"]["unique_id"]
    assert unique_id == flow.discovery_unique_id(info())
    # Stable and opaque: no announced name, host or address in it.
    assert unique_id.startswith("local_discovery_")
    assert not any(part in unique_id for part in ("192.0.2", "gateway", "arbitrary"))
    assert flow.discovery_unique_id(info(host="192.0.2.9")) == unique_id
    assert flow.discovery_unique_id(info(name="other")) != unique_id
    ignored = await manager.async_init(
        DOMAIN,
        context={"source": "ignore"},
        data={"unique_id": unique_id, "title": "Local gateway"},
    )
    assert ignored["type"] is FlowResultType.CREATE_ENTRY
    assert not manager.async_progress_by_handler(DOMAIN)
    # Also after a restart: the hints in memory are gone, the entry stays.
    hass.data.pop(discovery._HINTS, None)
    hass.data.pop(discovery._FLOWS, None)
    again = await manager.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=info(host="192.0.2.9")
    )
    assert again["type"] is FlowResultType.ABORT
    assert again["reason"] == "already_configured"
    # Another gateway is still offered, and never with the ignored entry.
    other = await manager.async_init(
        DOMAIN,
        context={"source": "zeroconf"},
        data=info(name="other", host="192.0.2.7"),
    )
    assert other["step_id"] == "local_owner"
    owners = next(iter(other["data_schema"].schema.values())).config["options"]
    assert [item["value"] for item in owners] == [entry.entry_id]
    assert entry.unique_id == entry.entry_id  # existing entries keep theirs


async def test_discovery_without_any_entry_offers_a_local_only_setup(hass):
    # A fresh installation: without an entry the integration is not set up,
    # so the local runtime is not ready yet; the notice appears anyway.
    assert SUPPORT_KEY not in hass.data
    manager = hass.config_entries.flow
    result = await manager.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=info()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "local_discovered"
    assert result["description_placeholders"] == {"host": "192.0.2.1"}
    progress = manager.async_get(result["flow_id"])
    # It can be ignored like any notice, and nothing is created before the
    # user continues.
    assert progress["context"]["unique_id"] == flow.discovery_unique_id(info())
    assert not hass.config_entries.async_entries(DOMAIN)
    again = await manager.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=info()
    )
    assert again["type"] is FlowResultType.ABORT


async def test_a_discovered_gateway_without_entry_is_set_up_locally(
    hass, supported, journal
):
    from homeassistant.config_entries import FlowType

    manager = hass.config_entries.flow
    with patch(
        "custom_components.bosch_buderus_heating.async_setup_entry",
        AsyncMock(return_value=True),
    ):
        result = await manager.async_init(
            DOMAIN, context={"source": "zeroconf"}, data=info()
        )
        assert result["step_id"] == "local_discovered"
        # The notice may be older than the address hint in memory.
        hass.data.pop(discovery._HINTS, None)
        result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.title == "Home Comfort (local)" and entry.version == 4
    assert entry.data["connections"] == {} and entry.data["targets"] == {}
    assert entry.unique_id.startswith("installation_")
    assert not manager.async_progress_by_handler(DOMAIN)
    flow_type, _ = result["next_flow"]
    assert flow_type == FlowType.OPTIONS_FLOW
    options = hass.config_entries.options
    # As the frontend does: a new options flow takes over at the address,
    # which offers the found gateway.
    step = await options.async_init(entry.entry_id)
    assert step["step_id"] == "local_address"
    assert "192.0.2.1" in str(next(iter(step["data_schema"].schema.values())).config)
    step = await submit(hass, options, step, {"address": "192.0.2.1"})
    step = await submit(hass, options, step, {"method": "token"})
    step = await submit(hass, options, step, {"token": TOKEN})
    step = await submit(hass, options, step, {"confirm": True})
    step = await submit(hass, options, step, {"confirm": True})
    assert step["reason"] == "local_setup_complete"
    assert [e.entry_id for e in hass.config_entries.async_entries(DOMAIN)] == [
        entry.entry_id
    ]
    # Configured now: the same announcement no longer shows a notice.
    later = await manager.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=info()
    )
    assert later["reason"] == "already_configured"


async def test_an_untouched_notice_ends_once_its_gateway_is_configured(
    hass, monkeypatch
):
    manager = hass.config_entries.flow
    result = await manager.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=info()
    )
    assert result["step_id"] == "local_discovered"
    discovery.dismiss_configured_discoveries(hass)
    assert manager.async_progress_by_handler(DOMAIN)
    # Set up meanwhile through "Local access only": the notice is dismissed.
    monkeypatch.setattr(discovery, "configured_discovery", lambda *_: True)
    discovery.dismiss_configured_discoveries(hass)
    assert not manager.async_progress_by_handler(DOMAIN)


async def test_an_entry_added_meanwhile_turns_the_notice_into_an_owner_choice(
    hass, supported, journal
):
    manager = hass.config_entries.flow
    result = await manager.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=info()
    )
    assert result["step_id"] == "local_discovered"
    entry, _, _, _ = registered(hass)
    result = await manager.async_configure(result["flow_id"], {})
    assert result["step_id"] == "local_owner"
    owners = next(iter(result["data_schema"].schema.values())).config["options"]
    assert [item["value"] for item in owners] == [entry.entry_id]
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


def test_discovery_hints_are_bounded_expiring_and_not_identities(hass, monkeypatch):
    for n in range(70):
        discovery.remember_discovery(
            hass, info(name=f"service{n}", host=f"192.0.2.{n + 1}")
        )
    assert len(hass.data[discovery._HINTS]) == 64
    first = discovery.remember_discovery(hass, info(name="same"))
    second = discovery.remember_discovery(hass, info(name="same"))
    assert first == second
    assert len(discovery.discovery_endpoints(hass)) <= 65
    monkeypatch.setattr(discovery, "monotonic", lambda: float("inf"))
    assert not discovery.discovery_endpoints(hass)


async def test_close_after_token_import_resumes_without_auth_post(
    hass, supported, journal
):
    entry, _, _, target = registered(hass)
    manager = hass.config_entries.options
    result = await imported(hass, entry, target)
    record = (await journal.records())[0]
    manager.async_abort(result["flow_id"])
    result = await start(hass, entry, target)
    assert result["step_id"] == "local_pending"
    result = await submit(hass, manager, result, {"action": "continue"})
    assert result["step_id"] == "local_certificate"
    assert (await journal.records())[0].credentials == record.credentials
    assert supported[0] == [PortRole.RESOURCES, PortRole.RESOURCES]


async def test_missing_identity_contract_is_explained_without_override(
    hass, supported, journal
):
    entry, management, _, target = registered(hass)
    hass.data[SUPPORT_KEY] = LocalSetupSupport(lambda: True, lambda *args: None)
    result = await imported(hass, entry, target)
    result = await submit(hass, hass.config_entries.options, result, {"confirm": True})
    assert result.get("step_id") == "local_pending", result
    assert result["errors"]["base"] == "local_identity_unverified"
    assert management.current().minor_version == 1
    assert (await journal.records())[0].completion is None


async def test_resource_pause_survives_closed_flow_and_blocks_retry(
    hass, supported, journal, monkeypatch
):
    entry, _, _, target = registered(hass)

    async def limited(context, token, binding, *, is_current, budget):
        return LocalProbeExchange(
            (
                replace(
                    response(ProbePath.BRAND, context=context), status=429, body=b""
                ),
            ),
            120,
        )

    monkeypatch.setattr(setup, "read_local_probe", limited)
    result = await imported(hass, entry, target)
    result = await submit(hass, hass.config_entries.options, result, {"confirm": True})
    assert (
        result["step_id"] == "local_pending"
        and result["errors"]["base"] == "local_rate_limited"
    )
    record = (await journal.records())[0]
    assert await journal.remaining_pause(record) > 0
    hass.config_entries.options.async_abort(result["flow_id"])
    count = len(supported[0])
    result = await start(hass, entry, target)
    result = await submit(
        hass, hass.config_entries.options, result, {"action": "continue"}
    )
    assert (
        result["errors"]["base"] == "local_rate_limited" and len(supported[0]) == count
    )


async def test_invalid_address_and_token_have_field_errors_without_echoing_secrets(
    hass, supported, journal
):
    entry, _, _, target = registered(hass)
    manager = hass.config_entries.options
    result = await start(hass, entry, target)
    result = await submit(
        hass, manager, result, {"address": "https://private:secret@gateway/path"}
    )
    assert result["errors"] == {"address": "local_invalid_address"}
    assert "secret" not in str(result)
    result = await submit(hass, manager, result, {"address": "gateway.example.invalid"})
    result = await submit(hass, manager, result, {"method": "token"})
    result = await submit(hass, manager, result, {"token": "secret with spaces"})
    assert result["errors"] == {"token": "local_invalid_token"}
    assert "secret with spaces" not in str(result) and not journal._path.exists()


async def test_discard_requires_explicit_confirmation_and_never_revokes(
    hass, supported, journal
):
    entry, _, _, target = registered(hass)
    manager = hass.config_entries.options
    result = await imported(hass, entry, target)
    manager.async_abort(result["flow_id"])
    result = await start(hass, entry, target)
    result = await submit(hass, manager, result, {"action": "discard"})
    assert result["errors"]["confirm"] == "local_confirmation_required"
    assert (await journal.records())[0].credentials is not None
    result = await submit(hass, manager, result, {"action": "discard", "confirm": True})
    assert result["step_id"] == "local_address"
    assert (await journal.records())[0].credentials is None


async def test_accepted_completion_reopened_does_not_read_or_write_again(
    hass, supported, journal
):
    entry, management, _, target = registered(hass)
    manager = hass.config_entries.options
    result = await imported(hass, entry, target)
    result = await submit(hass, manager, result, {"confirm": True})
    await submit(hass, manager, result, {"confirm": True})
    before = management.current()
    assert (await start(hass, entry, target))["reason"] == "local_setup_complete"
    assert management.current() == before and len(supported[1]) == 2


async def test_native_abort_cancels_probe_without_losing_imported_token(
    hass, supported, journal, monkeypatch
):
    entry, management, _, target = registered(hass)
    manager = hass.config_entries.options
    result = await imported(hass, entry, target)
    started = asyncio.Event()

    async def waiting(*args, **kwargs):
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(setup, "read_local_probe", waiting)
    task = hass.async_create_task(
        manager.async_configure(result["flow_id"], {"confirm": True})
    )
    await started.wait()
    manager.async_abort(result["flow_id"])
    assert (await task)["reason"] == "local_setup_unavailable"
    await hass.async_block_till_done()
    assert (await journal.records())[0].state == AuthState.RECEIVED
    assert management.current().minor_version == 1


@pytest.mark.parametrize(
    "status,expected",
    [
        (200, None),
        (400, "local_invalid_credentials"),
        (412, "local_unlock_required"),
        (429, "local_rate_limited"),
        (507, "local_token_database_full"),
        (503, "local_creation_uncertain"),
    ],
)
async def test_label_dialog_posts_once_and_preserves_outcome(
    hass,
    supported,
    journal,
    auth_gateway,
    monkeypatch,
    status,
    expected,
):
    entry, management, _, target = registered(hass)

    async def reply(head, body, writer):
        payload = {
            "access_token": TOKEN,
            "token_type": "Bearer",
            "scope": "open_api.read",
        }
        writer.write(wire_reply(json.dumps(payload).encode(), status=status))
        await writer.drain()

    calls, _, pin = await auth_gateway(reply)
    inspections = []

    async def inspect(context, role, *, is_current, budget):
        inspections.append(role)
        return CertificateObservation(context, role, pin.sha256, "127.0.0.1", "TLSv1.3")

    monkeypatch.setattr(flow, "inspect_local_certificate", inspect)
    manager = hass.config_entries.options
    result = await start(hass, entry, target)
    result = await submit(hass, manager, result, {"address": "gateway.example.invalid"})
    result = await submit(hass, manager, result, {"method": "label"})
    assert result["step_id"] == "local_certificate" and not calls
    result = await submit(hass, manager, result, {"confirm": False})
    assert result["step_id"] == "local_certificate" and not calls
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["step_id"] == "local_auth" and not calls
    values = {"login": LABEL.login, "password": LABEL.password, "unlocked": True}
    result = await submit(hass, manager, result, {**values, "unlocked": False})
    assert result["errors"] == {"unlocked": "local_unlock_required"}
    result = await submit(hass, manager, result, values)
    assert len(calls) == 1 and management.current().minor_version == 1
    assert (
        LABEL.login not in str(result)
        and LABEL.password not in journal._path.read_text()
    )
    if status == 200:
        assert result["step_id"] == "local_certificate"
        assert inspections == [PortRole.AUTHENTICATION, PortRole.RESOURCES]
        result = await submit(hass, manager, result, {"confirm": True})
        assert result["step_id"] == "local_result"
        result = await submit(hass, manager, result, {"confirm": True})
        assert result["reason"] == "local_setup_complete"
    elif status == 503:
        assert result["step_id"] == "local_pending"
        assert result["errors"]["base"] == expected
        record = (await journal.records())[0]
        assert record.state == AuthState.UNCERTAIN and not record.resolved
        # No usable token means no misleading Continue option.
        selector = next(iter(result["data_schema"].schema.values()))
        assert "continue" not in selector.config["options"]
    else:
        # FLOW-5: certainly rejected, so the label form returns at once with
        # the reason; nothing has to be discarded first.
        assert result["step_id"] == "local_auth"
        assert result["errors"] == {"base": expected}
        record = (await journal.records())[0]
        assert record.state == AuthState.REJECTED and record.resolved
        if status == 429:
            assert await journal.remaining_pause(record) > 0
    assert len(calls) == 1


async def test_rejected_label_sign_in_retries_without_new_confirmations(
    hass, supported, journal, auth_gateway, monkeypatch
):
    """FLOW-5: a typo costs one more submission, not a restart of the dialog."""
    entry, management, _, target = registered(hass)
    statuses = [401, 200]

    async def reply(head, body, writer):
        payload = {
            "access_token": TOKEN,
            "token_type": "Bearer",
            "scope": "open_api.read",
        }
        status = statuses.pop(0)
        writer.write(wire_reply(json.dumps(payload).encode(), status=status))
        await writer.drain()

    calls, _, pin = await auth_gateway(reply)
    inspections = []

    async def inspect(context, role, *, is_current, budget):
        inspections.append(role)
        return CertificateObservation(context, role, pin.sha256, "127.0.0.1", "TLSv1.3")

    monkeypatch.setattr(flow, "inspect_local_certificate", inspect)
    manager = hass.config_entries.options
    result = await start(hass, entry, target)
    result = await submit(hass, manager, result, {"address": "gateway.example.invalid"})
    result = await submit(hass, manager, result, {"method": "label"})
    result = await submit(hass, manager, result, {"confirm": True})
    values = {"login": LABEL.login, "password": LABEL.password, "unlocked": True}
    result = await submit(hass, manager, result, values)
    assert result["step_id"] == "local_auth"
    assert result["errors"] == {"base": "local_invalid_credentials"}
    result = await submit(hass, manager, result, values)
    assert len(calls) == 2 and not statuses
    assert result["step_id"] == "local_certificate"
    assert inspections == [PortRole.AUTHENTICATION, PortRole.RESOURCES]
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["step_id"] == "local_result"
    assert management.current().minor_version == 1


@pytest.mark.parametrize("certificate", ["same", "changed"])
async def test_continue_after_a_temporary_error_keeps_the_confirmed_certificate(
    hass, supported, journal, monkeypatch, certificate
):
    """FLOW-5: Continue asks again only for a different certificate."""
    entry, management, _, target = registered(hass)
    fingerprints = [b"a" * 32, b"a" * 32 if certificate == "same" else b"b" * 32]
    inspections = []

    async def inspect(context, role, *, is_current, budget):
        inspections.append(role)
        return CertificateObservation(
            context, role, fingerprints.pop(0), "192.0.2.1", "TLSv1.3"
        )

    read = setup.read_local_probe
    failures = [ValueError("timeout")]

    async def flaky(*args, **kwargs):
        if failures:
            raise failures.pop()
        return await read(*args, **kwargs)

    monkeypatch.setattr(flow, "inspect_local_certificate", inspect)
    monkeypatch.setattr(setup, "read_local_probe", flaky)
    manager = hass.config_entries.options
    result = await imported(hass, entry, target)
    assert result["step_id"] == "local_certificate"
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["step_id"] == "local_pending"
    result = await submit(hass, manager, result, {"action": "continue"})
    assert inspections == [PortRole.RESOURCES, PortRole.RESOURCES]
    assert result["step_id"] == (
        "local_result" if certificate == "same" else "local_certificate"
    )
    assert management.current().minor_version == 1


async def test_runtime_gate_removed_during_request_prevents_next_form(
    hass, supported, journal, monkeypatch
):
    entry, management, _, target = registered(hass)
    result = await imported(hass, entry, target)
    started = asyncio.Event()
    finished = asyncio.Event()

    async def waiting(*args, **kwargs):
        started.set()
        await finished.wait()
        raise ValueError("configuration_changed")

    monkeypatch.setattr(setup, "read_local_probe", waiting)
    manager = hass.config_entries.options
    task = hass.async_create_task(
        manager.async_configure(result["flow_id"], {"confirm": True})
    )
    await started.wait()
    hass.data.pop(SUPPORT_KEY)
    finished.set()
    result = await task
    assert result["reason"] == "local_setup_unavailable"
    assert management.current().minor_version == 1


async def test_result_refresh_does_not_repeat_probe_or_save(hass, supported, journal):
    entry, management, _, target = registered(hass)
    manager = hass.config_entries.options
    result = await imported(hass, entry, target)
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["step_id"] == "local_result"
    for _ in range(2):
        result = await manager.async_configure(result["flow_id"])
        assert result["step_id"] == "local_result"
    assert len(supported[1]) == 1 and management.current().minor_version == 1


@pytest.mark.parametrize(
    "stage",
    [
        "local_certificate",
        "local_auth",
        "local_token",
        "local_pending",
        "local_result",
        "local_done",
    ],
)
async def test_direct_stage_cannot_skip_prior_steps(hass, supported, stage):
    entry, _, _, _ = registered(hass)
    handler = BoschBuderusOptionsFlow(entry)
    handler.hass = hass
    result = await getattr(handler, "async_step_" + stage)(
        {"confirm": True, "token": TOKEN}
    )
    assert result["reason"] == "local_session_changed"


@pytest.mark.parametrize("change", ["different", "same"])
async def test_saved_certificate_is_reused_only_when_exactly_equal(
    hass, supported, journal, monkeypatch, change
):
    entry, _management, _, target = registered(hass)
    manager = hass.config_entries.options
    result = await imported(hass, entry, target)
    result = await submit(hass, manager, result, {"confirm": True})
    await submit(hass, manager, result, {"confirm": True})
    record = (await journal.records())[0]
    await journal.discard(record.attempt_id, entry.entry_id, confirmed=True)

    async def inspect(context, role, *, is_current, budget):
        return CertificateObservation(
            context,
            role,
            (b"b" if change == "different" else b"a") * 32,
            "192.0.2.1",
            "TLSv1.3",
        )

    monkeypatch.setattr(flow, "inspect_local_certificate", inspect)
    result = await imported(hass, entry, target)
    assert result["step_id"] == (
        "local_certificate" if change == "different" else "local_result"
    )
    if change == "different":
        assert result["description_placeholders"]["previous"] == (b"a" * 32).hex()
        assert result["description_placeholders"]["fingerprint"] == (b"b" * 32).hex()


async def test_owner_removed_while_token_form_is_open_never_saves(
    hass, supported, journal
):
    entry, _, _, target = registered(hass)
    manager = hass.config_entries.options
    result = await start(hass, entry, target)
    result = await submit(hass, manager, result, {"address": "gateway.example.invalid"})
    result = await submit(hass, manager, result, {"method": "token"})
    # Core removes the owner before the integration removal callback.
    hass.config_entries._entries.pop(entry.entry_id)
    result = await submit(hass, manager, result, {"token": TOKEN})
    assert result["errors"]["base"] == "local_configuration_changed"
    assert not journal._path.exists()
