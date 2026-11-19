"""Error and guard branches of the token pages of the local access."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.bosch_buderus_heating.local_auth import (
    AuthProblem,
    RevokeOutcome,
    RevokeState,
)
from custom_components.bosch_buderus_heating.local_budget import (
    BudgetDeferred,
    LocalBudgetError,
)
from custom_components.bosch_buderus_heating.local_transport import (
    StaleLocalRequest,
)
from tests.helpers.options_menu import outcome
from tests.test_connection_runtime import entry_for
from tests.test_local_auth import auth_gateway as auth_gateway
from tests.test_local_tokens import (
    BOTH,
    FOREIGN,
    OLD,
    OWN,
    Gateway,
    _choices,
    _open_list,
    _open_menu,
    _remove_with_revoke,
)
from tests.test_local_tokens import (
    dialog as dialog,
)
from tests.test_local_transport import certificates as certificates

pytestmark = pytest.mark.usefixtures("socket_enabled")

FLOW = "custom_components.bosch_buderus_heating.local_token_flow"
MANAGEMENT = (
    "custom_components.bosch_buderus_heating.configuration_management"
    ".ConfigurationManagement"
)


def _flow(hass, result):
    return hass.config_entries.options._progress[result["flow_id"]]


def _aborted(shown, reason="configuration_changed"):
    assert shown["type"] is FlowResultType.ABORT, shown
    assert shown["reason"] == reason


# --- opening the list ---------------------------------------------------------


async def test_the_list_without_an_open_access_aborts(hass, enable_custom_integrations):
    entry, _, _ = entry_for(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    flow = _flow(hass, result)
    _aborted(await flow.async_step_local_tokens())
    # The removal variant never starts without its own answer.
    _aborted(await flow.async_step_local_tokens_remove())


async def test_the_list_of_a_changed_access_aborts(hass, dialog):
    entry, requests = await dialog(Gateway())
    result = await _open_menu(hass, entry)
    flow = _flow(hass, result)
    stale = SimpleNamespace(connection_id="local")
    with patch(MANAGEMENT + ".edit", return_value=stale):
        _aborted(await flow.async_step_local_tokens())
    assert requests == []


async def test_the_list_of_a_cloud_access_aborts(hass, enable_custom_integrations):
    from tests.test_managed_connections import choose_connection

    entry, cloud, _ = entry_for(hass)
    result = await choose_connection(hass, entry, cloud)
    flow = _flow(hass, result)
    _aborted(await flow.async_step_local_tokens())


async def test_an_access_without_a_usable_token_has_no_list(
    hass, enable_custom_integrations
):
    from tests.test_managed_connections import choose_connection

    entry, _, _ = entry_for(hass)
    result = await choose_connection(hass, entry, "local")
    flow = _flow(hass, result)
    _aborted(await flow.async_step_local_tokens(), "local_tokens_unavailable")


@pytest.mark.parametrize(
    "error,key",
    [
        (StaleLocalRequest(), None),
        (BudgetDeferred(60.0), "local_busy"),
        (LocalBudgetError("closed"), "local_storage_failed"),
    ],
)
async def test_the_sign_in_certificate_check_failures_are_explained(
    hass, dialog, error, key
):
    entry, requests = await dialog(
        Gateway(), imported=True, auth_pin=False, resource="61" * 32
    )
    with patch(FLOW + ".inspect_local_certificate", AsyncMock(side_effect=error)):
        result = await _open_list(hass, entry)
    if key is None:
        _aborted(result)
    else:
        assert result["step_id"] == "local_tokens"
        assert result["errors"] == {"base": key}
    assert requests == []


async def test_the_certificate_page_guards_its_state(hass, dialog):
    entry, requests = await dialog(
        Gateway(), imported=True, auth_pin=False, resource="61" * 32
    )
    result = await _open_list(hass, entry)
    assert result["step_id"] == "local_tokens_certificate"
    flow = _flow(hass, result)
    # Shown again without an answer; nothing was sent.
    shown = await flow.async_step_local_tokens_certificate()
    assert shown["step_id"] == "local_tokens_certificate"
    # The access changed while the page was open: the answer is refused.
    with patch.object(type(flow), "_token_current", return_value=False):
        _aborted(await flow.async_step_local_tokens_certificate({"confirm": True}))
    assert requests == []
    # Without its state the page ends.
    flow._token_observation = None
    _aborted(await flow.async_step_local_tokens_certificate({"confirm": True}))


async def test_a_list_whose_context_changed_while_reading_aborts(hass, dialog):
    entry, _ = await dialog(Gateway())
    changed = SimpleNamespace(tokens=None, problem=AuthProblem.CONTEXT_CHANGED)
    with patch(FLOW + ".list_gateway_tokens", AsyncMock(return_value=changed)):
        result = await _open_list(hass, entry)
    _aborted(result)


async def test_the_dialog_is_closed_when_the_access_cannot_be_reread(hass, dialog):
    entry, _ = await dialog(Gateway())
    result = await _open_list(hass, entry)
    flow = _flow(hass, result)
    assert flow._token_current()
    with patch(MANAGEMENT + ".edit", side_effect=KeyError("gone")):
        assert not flow._token_current()


async def test_the_marks_of_other_accesses_may_be_missing_without_harm(hass, dialog):
    entry, requests = await dialog(Gateway())
    with patch(FLOW + ".saved_local_accesses", side_effect=RuntimeError("broken")):
        result = await _open_list(hass, entry)
    assert result["step_id"] == "local_tokens"
    assert _choices(result)
    assert len(requests) == 1


# --- answers of the list and of the revocation --------------------------------


async def test_an_answer_without_a_list_is_refused(hass, dialog):
    entry, _ = await dialog(Gateway())
    result = await _open_list(hass, entry)
    flow = _flow(hass, result)
    flow._token_choices = None
    _aborted(await flow.async_step_local_tokens({"token": "token_1"}))


async def test_a_token_that_is_not_ours_is_never_offered_for_revocation(hass, dialog):
    gateway = Gateway()
    entry, _ = await dialog(gateway)
    result = await _open_list(hass, entry)
    flow = _flow(hass, result)
    foreign = next(
        token
        for token in flow._token_choices.values()
        if token.token_id == "tid-own-old"
    )
    flow._token_choices = {
        "token_9": SimpleNamespace(**{**vars_of(foreign), "own": False})
    }
    _aborted(await flow.async_step_local_tokens({"token": "token_9"}))
    assert gateway.posts == []


def vars_of(token):
    return {name: getattr(token, name) for name in token.__dataclass_fields__}


async def test_the_revocation_page_guards_its_state(hass, dialog):
    gateway = Gateway()
    entry, _ = await dialog(gateway)
    result = await _open_list(hass, entry)
    flow = _flow(hass, result)
    _aborted(await flow.async_step_local_token_revoke(BOTH))
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"token": next(iter(_choices(result).values()))}
    )
    assert result["step_id"] == "local_token_revoke"
    # Shown again without an answer; nothing was sent.
    shown = await flow.async_step_local_token_revoke()
    assert shown["step_id"] == "local_token_revoke"
    assert gateway.posts == []


async def test_a_closed_storage_at_the_revocation_sends_nothing(hass, dialog):
    gateway = Gateway()
    entry, _ = await dialog(gateway)
    result = await _open_list(hass, entry)
    flow_id = result["flow_id"]
    result = await hass.config_entries.options.async_configure(
        flow_id, {"token": next(iter(_choices(result).values()))}
    )
    flow = _flow(hass, result)
    with patch.object(
        type(flow), "_token_budget", AsyncMock(side_effect=LocalBudgetError("closed"))
    ):
        result = await hass.config_entries.options.async_configure(flow_id, BOTH)
    assert result["step_id"] == "local_token_revoke"
    assert result["errors"] == {"base": "local_storage_failed"}
    assert gateway.posts == []


async def test_a_revocation_for_a_changed_context_ends_the_dialog(hass, dialog):
    gateway = Gateway()
    entry, _ = await dialog(gateway)
    result = await _open_list(hass, entry)
    flow_id = result["flow_id"]
    await hass.config_entries.options.async_configure(
        flow_id, {"token": next(iter(_choices(result).values()))}
    )
    changed = RevokeOutcome(RevokeState.NOT_SENT, AuthProblem.CONTEXT_CHANGED)
    with patch(FLOW + ".revoke_gateway_token", AsyncMock(return_value=changed)):
        result = await hass.config_entries.options.async_configure(flow_id, BOTH)
    _aborted(result)
    assert gateway.posts == []


async def test_an_access_set_up_again_meanwhile_keeps_its_new_token(hass, dialog):
    gateway = Gateway()
    entry, _ = await dialog(gateway)
    result = await _open_list(hass, entry)
    flow_id = result["flow_id"]
    newest = list(_choices(result).values())[-1]
    await hass.config_entries.options.async_configure(flow_id, {"token": newest})
    data = deepcopy(dict(entry.data))
    original = entry.data["connections"]["local"]["credentials"]["token"]
    # The access is replaced by a new token after the answer was read; the old
    # one is revoked on the gateway, the new one must stay enabled.
    real = type(_flow(hass, result))._token_current

    def swap(self):
        current = real(self)
        data["connections"]["local"]["credentials"]["token"] = "set-up-again"
        hass.config_entries.async_update_entry(entry, data=data)
        return current

    with patch.object(type(_flow(hass, result)), "_token_current", swap):
        result = await hass.config_entries.options.async_configure(flow_id, BOTH)
    assert original != "set-up-again"
    assert await outcome(hass, result) in {
        "local_token_revoked",
        "configuration_changed",
        "local_token_access_ended",
    }
    assert entry.options["connections"]["local"]["enabled"] is True


async def test_a_vanished_access_after_a_revocation_returns_to_the_list(hass, dialog):
    from custom_components.bosch_buderus_heating.configuration import (
        ConfigurationError,
    )

    gateway = Gateway((OWN, OLD, FOREIGN))
    entry, _ = await dialog(gateway)
    result = await _open_list(hass, entry)
    flow = _flow(hass, result)
    with patch(MANAGEMENT + ".edit", side_effect=ConfigurationError("gone")):
        shown = await flow._token_note(flow._token_access.edit, "local_token_revoked")
    assert shown["step_id"] == "result_local_token_revoked"
    assert flow._connection_edit is None


async def test_removal_menu_is_shown_again_for_its_own_state(hass, dialog):
    gateway = Gateway((FOREIGN,))
    entry, _ = await dialog(gateway)
    flow_id, result = await _remove_with_revoke(hass, entry)
    assert result["step_id"] == "local_tokens_remove_none"
    flow = hass.config_entries.options._progress[flow_id]
    shown = await flow.async_step_local_tokens_remove_none()
    assert shown["type"] is FlowResultType.MENU
    assert shown["step_id"] == "local_tokens_remove_none"
    # Outside of that state the step refuses.
    flow._token_remove_none = False
    _aborted(await flow.async_step_local_tokens_remove_none())
    assert "local" in entry.data["connections"] and gateway.posts == []
