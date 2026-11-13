"""Configured-address hints never replace identity verification or user choices."""

import pytest
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.data_entry_flow import FlowResultType

from custom_components.bosch_buderus_heating import local_discovery as discovery
from custom_components.bosch_buderus_heating import local_flow as flow
from custom_components.bosch_buderus_heating.config_flow import BoschBuderusOptionsFlow
from custom_components.bosch_buderus_heating.configuration import LOCAL_KIND
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.local_probe import LocalEndpoint
from tests.test_local_flow import (
    imported,
    info,
    registered,
    submit,
)
from tests.test_local_flow import (
    journal as journal,
)
from tests.test_local_flow import (
    supported as supported,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")


async def discover(hass, **kwargs):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=info(**kwargs)
    )


async def finish_options(hass, entry, target):
    manager = hass.config_entries.options
    result = await imported(hass, entry, target)
    result = await submit(hass, manager, result, {"confirm": True})
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["reason"] == "local_setup_complete"


@pytest.mark.parametrize("disabled", ["no", "connection", "entry"])
async def test_configured_gateway_not_rediscovered_after_restart(
    hass, supported, journal, disabled
):
    entry, management, _, target = registered(hass)
    await finish_options(hass, entry, target)
    local = next(
        k for k, c in management.current().connections.items() if c.kind == LOCAL_KIND
    )
    if disabled == "connection":
        management.set_enabled(management.edit(local), False)
    elif disabled == "entry":
        await hass.config_entries.async_set_disabled_by(
            entry.entry_id, ConfigEntryDisabler.USER
        )
    # All transient discovery state is gone, as on a cold process start.
    hass.data.pop(discovery._HINTS, None)
    hass.data.pop(discovery._FLOWS, None)
    before = management.current()
    counts = len(supported[0]), len(supported[1])
    result = await discover(hass, hostname="GATEWAY.EXAMPLE.INVALID.")
    assert result["reason"] == "already_configured"
    assert management.current() == before
    assert (len(supported[0]), len(supported[1])) == counts
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert not hass.data.get(discovery._FLOWS)
    management.remove(management.edit(local))
    result = await discover(hass)
    assert result["type"] == FlowResultType.FORM


@pytest.mark.parametrize("advanced", [False, True])
async def test_saving_options_dismisses_only_untouched_matching_notice(
    hass, supported, journal, advanced
):
    entry, _, _, target = registered(hass)
    matching = await discover(hass)
    other = await discover(
        hass, name="another", host="192.0.2.2", hostname="other.example.invalid"
    )
    manager = hass.config_entries.flow
    if advanced:
        matching = await submit(hass, manager, matching, {"owner": entry.entry_id})
    await finish_options(hass, entry, target)
    pending = {r["flow_id"] for r in manager.async_progress_by_handler(DOMAIN)}
    assert (matching["flow_id"] in pending) is advanced
    assert other["flow_id"] in pending
    assert set(hass.data[discovery._FLOWS]) == pending


async def test_repeated_hints_share_only_transient_flow_and_can_be_retried(
    hass, supported, journal
):
    registered(hass)
    first = await discover(hass)
    duplicate = await discover(hass, name="changed name", port=9442)
    assert duplicate["reason"] == "already_in_progress"
    assert hass.config_entries.flow.async_get(first["flow_id"])["context"][
        "title_placeholders"
    ] == {"host": "192.0.2.1"}
    hass.config_entries.flow.async_abort(first["flow_id"])
    assert not hass.data[discovery._FLOWS]
    assert (await discover(hass))["type"] == FlowResultType.FORM
    assert not supported[0] and not supported[1]


async def test_legacy_local_address_without_verified_access_does_not_hide_hint(
    hass, supported, journal
):
    registered(hass, local=True)
    assert not discovery.configured_discovery(
        hass, (LocalEndpoint("gateway.example.invalid"),)
    )
    assert (await discover(hass))["type"] == FlowResultType.FORM


async def test_unrelated_or_invalid_entry_does_not_hide_new_gateway(
    hass, supported, journal
):
    entry, _, _, _ = registered(hass)
    hass.config_entries.async_update_entry(entry, data={})
    assert not discovery.configured_discovery(hass, (LocalEndpoint("192.0.2.1"),))


async def test_resuming_an_already_saved_setup_dismisses_remaining_notice(
    hass, supported, journal, monkeypatch
):
    entry, _, _, target = registered(hass)
    await discover(hass)
    with monkeypatch.context() as patch:
        patch.setattr(flow, "dismiss_configured_discoveries", lambda hass: None)
        await finish_options(hass, entry, target)
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    handler = BoschBuderusOptionsFlow(entry)
    handler.hass = hass
    handler._local_owner_id = entry.entry_id
    handler._local_record = (await journal.records())[0]
    handler._local_phase = "local_pending"
    result = await handler.async_step_local_pending()
    assert result["reason"] == "local_setup_complete"
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_configured_gateway_ip_matches_without_hostname_identity(
    hass, supported, journal
):
    entry, _, _, target = registered(hass)
    manager = hass.config_entries.flow
    result = await discover(hass)
    result = await submit(hass, manager, result, {"owner": entry.entry_id})
    result = await submit(hass, manager, result, {"target": target})
    result = await submit(hass, manager, result, {"address": "192.0.2.1"})
    result = await submit(hass, manager, result, {"method": "token"})
    result = await submit(hass, manager, result, {"token": "synthetic-new-local-token"})
    result = await submit(hass, manager, result, {"confirm": True})
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["reason"] == "local_setup_complete"
    assert not hass.data[discovery._FLOWS]
    assert (await discover(hass, hostname="different.example.invalid"))[
        "reason"
    ] == "already_configured"
    assert (await discover(hass, host="192.0.2.2", hostname="other.example.invalid"))[
        "type"
    ] == FlowResultType.FORM
