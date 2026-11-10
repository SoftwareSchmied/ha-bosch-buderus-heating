"""Native RRC schedule dialogs retain input and never touch holiday settings."""

from dataclasses import replace
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
    assert result["type"] is FlowResultType.CREATE_ENTRY
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
    assert (await flow.async_step_rrc_readonly({}))[
        "type"
    ] is FlowResultType.CREATE_ENTRY
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
    assert result["reason"] == "no_rrc_schedules"


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
