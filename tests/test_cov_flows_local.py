"""Error and guard branches of the local setup dialogs (discovery, owner, target)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.bosch_buderus_heating import local_flow as flow
from custom_components.bosch_buderus_heating.config_flow import BoschBuderusOptionsFlow
from custom_components.bosch_buderus_heating.configuration import ConfigurationError
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.local_completion import RecoveryStatus
from custom_components.bosch_buderus_heating.local_probe import (
    LocalProbeError,
    ProbeProblem,
)
from tests.test_local_enrollment import journal as journal
from tests.test_local_flow import imported, info, start, submit
from tests.test_local_flow import supported as supported
from tests.test_local_setup import registered

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")

MANAGEMENT = (
    "custom_components.bosch_buderus_heating.configuration_management"
    ".ConfigurationManagement"
)


def _flow(manager, result):
    return manager._progress[result["flow_id"]]


async def _discovered(hass):
    manager = hass.config_entries.flow
    result = await manager.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=info()
    )
    assert result["step_id"] == "local_discovered"
    return manager, result


async def test_an_announcement_that_cannot_be_read_is_ended(hass) -> None:
    with patch.object(
        flow,
        "remember_discovery",
        side_effect=LocalProbeError(ProbeProblem.INVALID_DISCOVERY),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "zeroconf"}, data=info()
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "local_discovery_invalid"
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_an_answer_after_the_dialog_was_closed_creates_nothing(hass) -> None:
    manager, result = await _discovered(hass)
    _flow(manager, result)._local_closed = True
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "local_session_changed"
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_a_gateway_configured_meanwhile_is_not_set_up_again(hass) -> None:
    manager, result = await _discovered(hass)
    with patch.object(flow, "configured_discovery", return_value=True):
        result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_an_entry_added_while_local_setup_is_unavailable_ends_the_notice(
    hass,
) -> None:
    manager, result = await _discovered(hass)
    registered(hass)
    # No local runtime is ready for the new entry: nothing can be joined.
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "local_setup_unavailable"


async def test_the_setup_cannot_be_started_twice_in_one_dialog(hass, supported) -> None:
    entry, _, _, _ = registered(hass)
    handler = BoschBuderusOptionsFlow(entry)
    handler.hass = hass
    handler._local_phase = "local_target"
    shown = await handler.async_step_local_setup()
    assert shown["type"] is FlowResultType.ABORT
    assert shown["reason"] == "local_session_changed"


async def test_the_owner_choice_ends_when_no_entry_is_left(hass, supported) -> None:
    entry, _, _, _ = registered(hass)
    manager = hass.config_entries.flow
    result = await manager.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=info()
    )
    assert result["step_id"] == "local_owner"
    hass.config_entries._entries.pop(entry.entry_id)
    result = await manager.async_configure(result["flow_id"], {"owner": entry.entry_id})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "local_existing_owner_required"


async def test_an_owner_that_was_not_offered_is_refused(hass, supported) -> None:
    registered(hass)
    manager = hass.config_entries.flow
    result = await manager.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=info()
    )
    assert result["step_id"] == "local_owner"
    handler = _flow(manager, result)
    shown = await handler.async_step_local_owner({"owner": "someone-else"})
    assert shown["type"] is FlowResultType.ABORT
    assert shown["reason"] == "local_session_changed"


async def _target_form(hass, entry):
    manager = hass.config_entries.options
    result = await manager.async_init(entry.entry_id)
    # The local setup is offered in the connections menu.
    result = await submit(hass, manager, result, {"next_step_id": "connections"})
    result = await submit(hass, manager, result, {"next_step_id": "local_setup"})
    assert result["step_id"] == "local_target"
    return manager, result


async def test_a_target_page_of_an_unreadable_configuration_ends(
    hass, supported, journal
) -> None:
    entry, _, _, _ = registered(hass)
    manager = hass.config_entries.options
    result = await manager.async_init(entry.entry_id)
    result = await submit(hass, manager, result, {"next_step_id": "connections"})
    with patch(MANAGEMENT + ".current", side_effect=ConfigurationError("broken")):
        result = await submit(hass, manager, result, {"next_step_id": "local_setup"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "local_session_changed"
    assert not journal._path.exists()


async def test_a_plant_that_was_not_offered_is_refused(
    hass, supported, journal
) -> None:
    entry, _, _, _ = registered(hass)
    manager, result = await _target_form(hass, entry)
    handler = _flow(manager, result)
    shown = await handler.async_step_local_target({"target": "not-a-plant"})
    assert shown["type"] is FlowResultType.ABORT
    assert shown["reason"] == "local_session_changed"
    assert not journal._path.exists()


@pytest.mark.parametrize("choice", ["existing", "new"])
async def test_an_unreadable_setup_record_ends_the_target_choice(
    hass, supported, journal, choice
) -> None:
    entry, _, _, target = registered(hass)
    manager, result = await _target_form(hass, entry)
    answer = target if choice == "existing" else flow.NEW_PLANT
    handler = _flow(manager, result)
    with patch.object(journal, "records", AsyncMock(side_effect=ValueError("io"))):
        shown = await handler.async_step_local_target({"target": answer})
    assert shown["type"] is FlowResultType.ABORT
    assert shown["reason"] == "local_storage_failed"
    assert not journal._path.exists()


async def test_the_token_of_a_setup_without_a_stored_access_is_none(
    hass, supported
) -> None:
    entry, _, _, _ = registered(hass)
    handler = BoschBuderusOptionsFlow(entry)
    handler.hass = hass
    assert handler._stored_local_token() is None
    assert handler._local_previous_pin(flow.PortRole.AUTHENTICATION) is None


async def test_a_dialog_without_its_entry_is_not_active(hass, supported) -> None:
    entry, _, _, _ = registered(hass)
    manager, result = await _target_form(hass, entry)
    handler = _flow(manager, result)
    assert handler._local_active()
    hass.config_entries._entries.pop(entry.entry_id)
    assert not handler._local_active()
    with pytest.raises(ConfigurationError):
        handler._local_management()
    # A late answer is refused with no change.
    shown = await handler.async_step_local_target({"target": "x"})
    assert shown["type"] is FlowResultType.ABORT
    assert shown["reason"] == "local_session_changed"


async def _pending(hass, entry, target):
    """An interrupted setup: the token was kept, the dialog was closed."""
    manager = hass.config_entries.options
    result = await imported(hass, entry, target)
    manager.async_abort(result["flow_id"])
    result = await start(hass, entry, target)
    assert result["step_id"] == "local_pending"
    return manager, result


async def test_a_pending_setup_can_be_left_for_later_without_a_change(
    hass, supported, journal
) -> None:
    entry, _, _, target = registered(hass)
    manager, result = await _pending(hass, entry, target)
    result = await submit(hass, manager, result, {"action": "later"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "local_pending_saved"
    assert (await journal.records())[0].credentials is not None


async def test_an_action_that_was_not_offered_changes_nothing(
    hass, supported, journal
) -> None:
    entry, _, _, target = registered(hass)
    manager, result = await _pending(hass, entry, target)
    handler = _flow(manager, result)
    shown = await handler.async_step_local_pending({"action": "something-else"})
    assert shown["type"] is FlowResultType.ABORT
    assert shown["reason"] == "local_session_changed"
    assert (await journal.records())[0].credentials is not None


async def test_a_pending_setup_that_conflicts_with_the_saved_state_cannot_continue(
    hass, supported, journal
) -> None:
    entry, _, _, target = registered(hass)
    manager, result = await _pending(hass, entry, target)
    handler = _flow(manager, result)
    with patch.object(
        journal, "recovery_status", AsyncMock(return_value=RecoveryStatus.CONFLICT)
    ):
        shown = await handler.async_step_local_pending()
        # Continuing is not offered in that state and is refused if sent.
        refused = await handler.async_step_local_pending({"action": "continue"})
    assert shown["type"] is FlowResultType.FORM
    assert shown["step_id"] == "local_pending"
    assert shown["errors"] == {"base": "local_configuration_changed"}
    assert refused["type"] is FlowResultType.ABORT
    assert refused["reason"] == "local_session_changed"
    # Nothing was resumed or discarded.
    assert (await journal.records())[0].credentials is not None


async def test_an_unreadable_record_while_asking_about_a_pending_setup_ends_it(
    hass, supported, journal
) -> None:
    entry, _, _, target = registered(hass)
    manager, result = await _pending(hass, entry, target)
    handler = _flow(manager, result)
    with patch.object(
        journal, "recovery_status", AsyncMock(side_effect=ValueError("broken"))
    ):
        shown = await handler.async_step_local_pending({"action": "later"})
    assert shown["type"] is FlowResultType.ABORT
    assert shown["reason"] == "local_session_changed"


async def test_a_pending_page_without_its_record_ends(hass, supported, journal) -> None:
    entry, _, _, target = registered(hass)
    manager, result = await _pending(hass, entry, target)
    handler = _flow(manager, result)
    handler._local_record = None
    shown = await handler.async_step_local_pending()
    assert shown["type"] is FlowResultType.ABORT
    assert shown["reason"] == "local_session_changed"
