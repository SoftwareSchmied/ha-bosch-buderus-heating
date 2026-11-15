"""Guards and error branches of the per-access pages of the options dialog."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.bosch_buderus_heating.configuration import ConfigurationError
from custom_components.bosch_buderus_heating.configuration_management import (
    ConfigurationManagement,
)
from tests.test_connection_runtime import entry_for
from tests.test_managed_connections import choose_connection

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

MANAGEMENT = (
    "custom_components.bosch_buderus_heating.configuration_management"
    ".ConfigurationManagement"
)
PAGES = [
    "connection",
    "connection_test",
    "connection_enabled",
    "connection_forget",
    "connection_targets",
    "connection_polling",
    "local_polling",
    "local_values",
    "connection_remove",
]


def _flow(hass, result):
    return hass.config_entries.options._progress[result["flow_id"]]


async def _page(flow, step, user_input=None):
    return await getattr(flow, f"async_step_{step}")(user_input)


def _aborted(shown, reason="configuration_changed"):
    assert shown["type"] is FlowResultType.ABORT, shown
    assert shown["reason"] == reason


@pytest.mark.parametrize("step", PAGES)
async def test_a_page_without_an_open_access_aborts(hass, step) -> None:
    entry, _, _ = entry_for(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    flow = _flow(hass, result)
    assert flow._connection_edit is None
    _aborted(await _page(flow, step))


@pytest.mark.parametrize("step", PAGES)
@pytest.mark.parametrize("kind", ["cloud", "local"])
async def test_a_page_of_an_access_changed_meanwhile_aborts(hass, kind, step) -> None:
    entry, cloud, _ = entry_for(hass)
    result = await choose_connection(hass, entry, cloud if kind == "cloud" else "local")
    flow = _flow(hass, result)
    stale = replace(flow._connection_edit, signature="changed-by-someone-else")
    before = dict(entry.options)
    answer = {"confirm": True} if step == "connection_remove" else {"enabled": False}
    with patch(MANAGEMENT + ".edit", return_value=stale):
        shown = await _page(flow, step, answer)
    if step == "connection_test":
        # A read on request changes nothing and needs no current edit.
        assert shown["type"] is FlowResultType.FORM
    else:
        _aborted(shown)
    assert dict(entry.options) == before


@pytest.mark.parametrize(
    "kind,step",
    [
        ("cloud", "local_polling"),
        ("cloud", "local_values"),
        ("local", "connection_polling"),
    ],
)
async def test_a_page_of_the_other_kind_of_access_aborts(hass, kind, step) -> None:
    entry, cloud, _ = entry_for(hass)
    result = await choose_connection(hass, entry, cloud if kind == "cloud" else "local")
    flow = _flow(hass, result)
    before = dict(entry.options)
    _aborted(await _page(flow, step))
    assert dict(entry.options) == before


async def test_forgetting_needs_a_deselected_system(hass) -> None:
    entry, cloud, _ = entry_for(hass)
    result = await choose_connection(hass, entry, cloud)
    flow = _flow(hass, result)
    # Everything of the access is still selected: nothing can be forgotten.
    _aborted(await _page(flow, "connection_forget"))


async def _deselect(hass, entry, cloud):
    management = ConfigurationManagement(hass, entry)
    management.set_targets_enabled(management.edit(cloud), set())


async def test_an_empty_forget_answer_returns_to_the_access_menu(hass) -> None:
    entry, cloud, _ = entry_for(hass)
    result = await choose_connection(hass, entry, cloud)
    flow = _flow(hass, result)
    await _deselect(hass, entry, cloud)
    flow._connection_edit = ConfigurationManagement(hass, entry).edit(cloud)
    before = dict(entry.data)
    shown = await _page(flow, "connection_forget", {"bindings": []})
    assert shown["type"] is FlowResultType.MENU
    assert shown["step_id"] == "connection"
    assert dict(entry.data) == before


async def test_a_refused_forget_changes_nothing(hass) -> None:
    entry, cloud, _ = entry_for(hass)
    result = await choose_connection(hass, entry, cloud)
    flow = _flow(hass, result)
    await _deselect(hass, entry, cloud)
    flow._connection_edit = ConfigurationManagement(hass, entry).edit(cloud)
    before = dict(entry.data)
    chosen = next(iter(before["bindings"]))
    with patch(
        MANAGEMENT + ".forget_bindings", side_effect=ConfigurationError("refused")
    ):
        _aborted(await _page(flow, "connection_forget", {"bindings": [chosen]}))
    assert dict(entry.data) == before


@pytest.mark.parametrize("submitted", ["not-a-list", ["unknown-binding"]])
async def test_a_target_selection_that_is_not_offered_is_refused(
    hass, submitted
) -> None:
    entry, cloud, _ = entry_for(hass)
    result = await choose_connection(hass, entry, cloud)
    flow = _flow(hass, result)
    before = dict(entry.options)
    _aborted(await _page(flow, "connection_targets", {"targets": submitted}))
    assert dict(entry.options) == before


async def test_a_refused_target_change_is_not_saved(hass) -> None:
    entry, cloud, _ = entry_for(hass)
    result = await choose_connection(hass, entry, cloud)
    flow = _flow(hass, result)
    before = dict(entry.options)
    with patch(
        MANAGEMENT + ".set_targets_enabled", side_effect=ConfigurationError("no")
    ):
        _aborted(await _page(flow, "connection_targets", {"targets": []}))
    assert dict(entry.options) == before


async def test_the_list_of_accesses_of_an_unreadable_entry_aborts(hass) -> None:
    entry, _, _ = entry_for(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    flow = _flow(hass, result)
    with patch(MANAGEMENT + ".current", side_effect=ConfigurationError("broken")):
        _aborted(await flow.async_step_connections())


async def test_an_access_removed_before_opening_aborts(hass) -> None:
    entry, _cloud, _ = entry_for(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    flow = _flow(hass, result)
    _aborted(await flow._open_connection("not-an-access"))
    assert flow._connection_edit is None


async def test_a_saved_change_of_a_vanished_access_returns_to_the_list(hass) -> None:
    entry, cloud, _ = entry_for(hass)
    result = await choose_connection(hass, entry, cloud)
    flow = _flow(hass, result)
    manager = ConfigurationManagement(hass, entry)
    manager.remove(manager.edit(cloud))
    shown = await flow._connection_saved()
    assert shown["type"] is FlowResultType.FORM
    assert shown["step_id"] == "result_connection_saved"
    flow._connection_edit = None
    shown = await flow._connection_saved()
    assert shown["step_id"] == "result_connection_saved"


@pytest.mark.parametrize(
    "step", ["connection", "connection_forget", "connection_targets"]
)
async def test_a_page_of_an_unreadable_configuration_aborts(hass, step) -> None:
    entry, cloud, _ = entry_for(hass)
    result = await choose_connection(hass, entry, cloud)
    flow = _flow(hass, result)
    before = dict(entry.options)
    with patch(MANAGEMENT + ".edit", side_effect=ConfigurationError("broken")):
        _aborted(await _page(flow, step))
    assert dict(entry.options) == before
