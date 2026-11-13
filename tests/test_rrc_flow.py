"""Native RRC schedule dialogs retain input and never touch holiday settings."""

from dataclasses import replace
from time import monotonic
from unittest.mock import AsyncMock

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.translation import async_get_translations

from custom_components.bosch_buderus_heating.config_flow import BoschBuderusOptionsFlow
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    RateLimited,
    RequestTimeout,
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.runtime import BoschBuderusRuntimeData
from custom_components.bosch_buderus_heating.writes import WriteBusy
from tests import test_dynamic_controls
from tests.helpers.options_menu import outcome
from tests.test_rrc import RESOURCES, WEEK, wire


@pytest.fixture
def environment(hass, enable_custom_integrations):
    yield from test_dynamic_controls.environment.__wrapped__(hass)


def make_flow(hass, environment):
    coordinator, client, _ = environment
    wire(environment)
    entry = coordinator.config_entry
    entry.runtime_data = BoschBuderusRuntimeData(
        client=client,
        token_manager=AsyncMock(),
        gateways=(coordinator.gateway,),
        coordinators=(coordinator,),
    )
    flow = BoschBuderusOptionsFlow(entry)
    flow.hass = hass
    return flow, coordinator


async def open_point(flow):
    first = await flow.async_step_rrc_programs()
    options = next(iter(first["data_schema"].schema.values())).config["options"]
    assert len(options) == 1
    result = await flow.async_step_rrc_programs({"program": options[0]["value"]})
    assert result["step_id"] == "rrc_schedule"
    assert "07:05" in result["description_placeholders"]["mo"]
    await flow.async_step_rrc_schedule({"day": "mo"})
    return await flow.async_step_rrc_day({"point": "1"})


@pytest.mark.parametrize("language", ["de", "en"])
async def test_native_flow_uses_hour_minute_and_commits_only_requested_point(
    hass, environment, language
):
    hass.config.language = language
    flow, coordinator = make_flow(hass, environment)
    first = await flow.async_step_init()
    assert first["menu_options"] == [
        "controls",
        "notifications",
        "holidays",
        "rrc_programs",
    ]
    result = await open_point(flow)
    fields = {str(key): value for key, value in result["data_schema"].schema.items()}
    assert fields["time"].config["type"] == "time"
    assert list(fields) == ["time", "temperature"]
    result = await flow.async_step_rrc_point({"time": "07:10", "temperature": 22})
    # Back in the main menu with the note of the saved point.
    assert await outcome(hass, result) == "rrc_program_saved"
    assert result["step_id"] == "init"
    assert coordinator.data[WEEK].resource.value[1] == {"d": "Mo", "t": 430, "Th": 22}


@pytest.mark.parametrize("time", ["07:11", "25:00", "07:10:01", "x", None])
async def test_invalid_clock_input_preserves_other_fields(hass, environment, time):
    flow, coordinator = make_flow(hass, environment)
    await open_point(flow)
    coordinator.async_edit_rrc_program = AsyncMock()
    result = await flow.async_step_rrc_point({"time": time, "temperature": 22})
    assert result["errors"] == {"time": "rrc_time"}
    fields = {str(key): key for key in result["data_schema"].schema}
    assert fields["temperature"].description["suggested_value"] == 22
    coordinator.async_edit_rrc_program.assert_not_awaited()


@pytest.mark.parametrize(
    "values,reason",
    [
        ({"time": "07:10", "temperature": "hot"}, "rrc_temperature"),
        ({"time": "07:10", "temperature": True}, "rrc_temperature"),
        ({"time": "07:10", "temperature": 32}, "rrc_point_invalid"),
        ({"time": "00:00", "temperature": 20}, "rrc_point_invalid"),
    ],
)
async def test_invalid_setpoint_is_rejected_before_cloud(
    hass, environment, values, reason
):
    flow, coordinator = make_flow(hass, environment)
    await open_point(flow)
    coordinator.async_edit_rrc_program = AsyncMock()
    result = await flow.async_step_rrc_point(values)
    assert reason in result["errors"].values()
    coordinator.async_edit_rrc_program.assert_not_awaited()


@pytest.mark.parametrize(
    "error,key",
    [
        (RateLimited(60), "write_rate_limited"),
        (AuthenticationError("expired"), "write_authentication_failed"),
        (WriteValidationError("changed"), "rrc_changed"),
        (WriteNotConfirmed("lost"), "rrc_not_confirmed"),
        (RequestTimeout("lost"), "rrc_failed"),
        (WriteBusy("busy"), "write_busy"),
    ],
)
async def test_write_failure_has_specific_translated_error(
    hass, environment, error, key
):
    flow, coordinator = make_flow(hass, environment)
    await open_point(flow)
    coordinator.async_edit_rrc_program = AsyncMock(side_effect=error)
    result = await flow.async_step_rrc_point({"time": "07:10", "temperature": 22})
    assert result["errors"] == {"base": key}
    coordinator.async_edit_rrc_program.assert_awaited_once_with(
        RESOURCES[WEEK], 1, 430, 22
    )


async def test_a_change_during_the_protection_pause_says_so(hass, environment):
    """Nothing is sent during the pause after repeated failed readings; the
    dialog names the pause instead of a changed program."""
    flow, coordinator = make_flow(hass, environment)
    await open_point(flow)
    coordinator._circuit_open_until = monotonic() + 600
    result = await flow.async_step_rrc_point({"time": "07:10", "temperature": 22})
    assert result["errors"] == {"base": "switch_program_paused"}
    coordinator.client.put_resource_value.assert_not_awaited()


@pytest.mark.parametrize("empty", [True, False])
async def test_readonly_or_empty_week_can_be_viewed_without_writes(
    hass, environment, empty
):
    flow, coordinator = make_flow(hass, environment)
    resource = (
        replace(RESOURCES[WEEK], value=[])
        if empty
        else replace(RESOURCES[WEEK], unknown_fields=frozenset({"future"}))
    )
    coordinator.data[WEEK] = replace(coordinator.data[WEEK], resource=resource)
    result = await flow.async_step_rrc_programs({"program": f"1:{WEEK}"})
    assert result["step_id"] == "rrc_readonly"
    assert not result["data_schema"].schema
    shown = await flow.async_step_rrc_readonly({})
    # Only shown: back in the main menu, without a note.
    assert shown["type"] is FlowResultType.MENU and shown["step_id"] == "init"
    assert shown["description_placeholders"]["result"] == ""
    coordinator.client.put_resource_value.assert_not_awaited()


async def test_multiple_gateways_and_disappearing_programs_are_explicit(
    hass, environment
):
    flow, coordinator = make_flow(hass, environment)
    flow._entry.runtime_data.coordinators = (coordinator, coordinator)
    assert len(flow._rrc_choices()) == 2
    assert all("[" in item.label for item in flow._rrc_choices().values())
    coordinator.data = {}
    result = await flow.async_step_rrc_programs({"program": f"1:{WEEK}"})
    assert await outcome(hass, result) == "no_rrc_schedules"


@pytest.mark.parametrize("language", ["de", "en"])
async def test_all_rrc_flow_and_enum_translations_are_available(
    hass, enable_custom_integrations, language
):
    options = await async_get_translations(hass, language, "options", {DOMAIN})
    prefix = f"component.{DOMAIN}.options."
    for step in ("rrc_programs", "rrc_schedule", "rrc_readonly", "rrc_point"):
        assert options[prefix + f"step.{step}.title"]
    for error in (
        "rrc_time",
        "rrc_temperature",
        "rrc_point_invalid",
        "rrc_changed",
        "rrc_not_confirmed",
        "rrc_failed",
        "switch_program_paused",
    ):
        assert options[prefix + f"error.{error}"]
    entities = await async_get_translations(hass, language, "entity", {DOMAIN})
    selectors = await async_get_translations(hass, language, "selector", {DOMAIN})
    assert selectors[f"component.{DOMAIN}.selector.rrc_weekday.options.mo"] == (
        "Montag" if language == "de" else "Monday"
    )
    for platform in ("sensor", "select"):
        for value in ("manual", "clock"):
            assert entities[
                f"component.{DOMAIN}.entity.{platform}.rrc_user_mode.state.{value}"
            ]


async def test_a_gateway_model_cannot_add_markup_to_the_program_dialogs(
    hass, environment
):
    """D43: the program label (with the gateway model from the cloud on a
    connection with several gateways) is shown as plain text."""
    from tests.test_markdown_text import _rendered, _strip

    flow, coordinator = make_flow(hass, environment)
    model = "![](https://t.invalid/p)[a](b)"
    coordinator.gateway = replace(coordinator.gateway, model=model)
    flow._entry.runtime_data.coordinators = (coordinator, coordinator)
    week = await flow.async_step_rrc_programs({"program": f"1:{WEEK}"})
    day = await flow.async_step_rrc_schedule({"day": "mo"})
    point = await flow.async_step_rrc_day({"point": "1"})
    for result in (week, day, point):
        shown = result["description_placeholders"]["program"]
        assert _rendered(shown).startswith(f"{model} [1] / ")
        assert _rendered(shown).endswith("(#3)")
        active = _strip(shown)
        assert "![" not in active and "](" not in active and "://" not in active


async def test_the_program_dialog_shows_the_controller_program_names(hass, environment):
    """The programs carry the names the zone program select shows (#122);
    a program without a name keeps its number, and a name is plain text in
    the dialog descriptions."""

    from tests.test_markdown_text import _rendered, _strip
    from tests.test_rrc_program_selection import program_list

    flow, coordinator = make_flow(hass, environment)
    labels = {choice.label for choice in flow._rrc_choices().values()}
    assert labels == {"Program 3 (#3)"}  # the name the controller reports
    markup = "![](https://t.invalid/p)"
    listed = program_list(value=[{"id": 3, "name": markup}, {"id": 7, "name": "Bad"}])
    saved = coordinator.data.pop(listed.path)
    # Without a program list: the number.
    assert {choice.label for choice in flow._rrc_choices().values()} == {"#3"}
    coordinator.data[listed.path] = replace(saved, resource=listed)
    (choice,) = flow._rrc_choices().values()
    assert choice.label == f"{markup} (#3)"
    week = await flow.async_step_rrc_programs({"program": f"1:{WEEK}"})
    shown = week["description_placeholders"]["program"]
    assert _rendered(shown) == f"{markup} (#3)"
    assert "![" not in _strip(shown) and "://" not in _strip(shown)
