"""Summer/winter switching and heating limit of one heating circuit (0.9.2).

The shapes follow an iCom gateway (``fixtures/contracts/icom/
season-circuit.json``) and the manufacturer's app; every value is
constructed. Nothing here confirms behaviour on a real installation.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating.circuit_controls import (
    SUMMER_WINTER_MODE_POLICY,
    SUMMER_WINTER_THRESHOLD_POLICY,
)
from custom_components.bosch_buderus_heating.coordinator import Freshness
from custom_components.bosch_buderus_heating.pointt import (
    RateLimited,
    ResourceForbidden,
    ResourceNotFound,
    ServiceUnavailable,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.rrc_contract_circuit import (
    SUMMER_WINTER_MODE_POLICY as RRC_SUMMER_WINTER_MODE_POLICY,
)
from custom_components.bosch_buderus_heating.select import build_select_descriptions
from custom_components.bosch_buderus_heating.writes import (
    GENERIC_CONTEXT,
    RRC_CONTEXT,
    assess_control,
    validate_control_value,
)
from tests.circuit_control_helpers import (
    CONTROL_TYPE,
    SWITCH_MODE,
    THRESHOLD,
    Cloud,
    coordinator_for,
    number_entity,
    number_paths,
    select_entity,
    select_paths,
    variant,
    with_flags,
)

FIXTURE = "icom/season-circuit.json"
COMPONENT = Path(__file__).parents[1] / "custom_components/bosch_buderus_heating"


def _setup(hass, key: str = "V1"):
    coordinator = coordinator_for(hass, variant(FIXTURE, key))
    return coordinator, Cloud(coordinator)


# Entities ------------------------------------------------------------------


def test_select_offers_the_advertised_known_codes():
    """V1: automatic, forced, off; no cooling."""
    resources = variant(FIXTURE, "V1")
    (description,) = (
        item
        for item in build_select_descriptions(resources)
        if item.resource_path == SWITCH_MODE
    )
    assert description.write_policy is SUMMER_WINTER_MODE_POLICY
    assert description.options == ["automatic", "forced", "off"]
    assert description.translation_key == "summer_winter_switching"
    assert description.entity_category is EntityCategory.CONFIG
    # Enabled when writable (the decision for these settings); the icon comes
    # from icons.json.
    assert description.entity_registry_enabled_default
    assert description.icon is None


def test_heat_pump_offers_cooling():
    resources = variant(FIXTURE, "V2")
    (description,) = (
        item
        for item in build_select_descriptions(resources)
        if item.resource_path == SWITCH_MODE
    )
    assert description.options == ["automatic", "forced", "cooling", "off"]


async def test_selecting_forced_writes_once_and_confirms(hass):
    coordinator, cloud = _setup(hass, "V2")
    entity = select_entity(hass, coordinator, SWITCH_MODE)
    await entity.async_select_option("forced")
    assert cloud.puts == [(SWITCH_MODE, "forced")]
    # The guards were read again with the setting in one request first.
    assert set(cloud.bulk_calls[0]) == {SWITCH_MODE, CONTROL_TYPE}
    assert entity.current_option == "forced"


def test_unknown_code_is_left_out_and_refused():
    resources = variant(FIXTURE, "V4")
    assessment = assess_control(resources[SWITCH_MODE])
    assert assessment.options == ("automatic", "forced", "off")
    with pytest.raises(WriteValidationError):
        validate_control_value(resources[SWITCH_MODE], "holiday")


async def test_unknown_code_sends_nothing(hass):
    coordinator, cloud = _setup(hass, "V4")
    entity = select_entity(hass, coordinator, SWITCH_MODE)
    assert "holiday" not in entity.options
    with pytest.raises(ServiceValidationError):
        await entity.async_select_option("holiday")
    assert cloud.puts == []


def test_no_control_without_allowed_values():
    resources = variant(FIXTURE, "V5")
    assert assess_control(resources[SWITCH_MODE]).rejection_reason == (
        "missing_options"
    )
    assert SWITCH_MODE not in select_paths(resources)


async def test_code_not_advertised_is_refused_before_sending(hass):
    """``cooling`` on V1 (not advertised) never reaches the gateway."""
    coordinator, cloud = _setup(hass, "V1")
    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(
            SWITCH_MODE, "cooling", SUMMER_WINTER_MODE_POLICY
        )
    assert cloud.puts == []


# Room controller (ISRC) ------------------------------------------------------


async def test_room_controller_refuses_both_settings(hass):
    coordinator, cloud = _setup(hass, "V6")
    select = select_entity(hass, coordinator, SWITCH_MODE)
    number = number_entity(hass, coordinator, THRESHOLD)
    with pytest.raises(ServiceValidationError) as error:
        await select.async_select_option("forced")
    assert error.value.translation_key == "circuit_room_controller"
    with pytest.raises(ServiceValidationError) as error:
        await number.async_set_native_value(18)
    assert error.value.translation_key == "circuit_room_controller"
    assert cloud.puts == []


async def test_room_controller_set_between_display_and_write(hass):
    """The control type is read again on the gateway: no request."""
    coordinator, cloud = _setup(hass, "V1")
    select = select_entity(hass, coordinator, SWITCH_MODE)
    cloud.change(CONTROL_TYPE, value="ISRC")
    with pytest.raises(ServiceValidationError) as error:
        await select.async_select_option("forced")
    assert error.value.translation_key == "write_validation_failed"
    assert CONTROL_TYPE in cloud.bulk_calls[0]
    assert cloud.puts == []
    # The fresh reading is shown at once.
    assert coordinator.data[CONTROL_TYPE].resource.value == "ISRC"


async def test_control_type_without_a_current_reading_refuses(hass):
    coordinator, cloud = _setup(hass, "V1")
    snapshot = coordinator.data[CONTROL_TYPE]
    coordinator.data[CONTROL_TYPE] = replace(
        snapshot, available=False, freshness=Freshness.STALE
    )
    with pytest.raises(ServiceValidationError):
        await select_entity(hass, coordinator, SWITCH_MODE).async_select_option(
            "forced"
        )
    assert cloud.puts == [] and cloud.bulk_calls == []


async def test_without_a_control_type_the_write_goes_on(hass):
    """As the app: a circuit without controlType is not refused."""
    resources = variant(FIXTURE, "V1")
    del resources[CONTROL_TYPE]
    coordinator = coordinator_for(hass, resources)
    cloud = Cloud(coordinator)
    await select_entity(hass, coordinator, SWITCH_MODE).async_select_option("off")
    assert cloud.puts == [(SWITCH_MODE, "off")]
    assert cloud.bulk_calls == [(SWITCH_MODE,)]


# Flags -------------------------------------------------------------------


@pytest.mark.parametrize("used", ["false", False])
async def test_unused_setting_refuses_writes(hass, used):
    resources = variant(FIXTURE, "V7")
    resources = {
        path: with_flags(resource, used=False, available=True)
        if not isinstance(used, str)
        else resource
        for path, resource in resources.items()
    }
    coordinator = coordinator_for(hass, resources)
    cloud = Cloud(coordinator)
    with pytest.raises(ServiceValidationError) as error:
        await select_entity(hass, coordinator, SWITCH_MODE).async_select_option(
            "forced"
        )
    assert error.value.translation_key == "control_unused"
    with pytest.raises(ServiceValidationError) as error:
        await number_entity(hass, coordinator, THRESHOLD).async_set_native_value(18)
    assert error.value.translation_key == "control_unused"
    assert cloud.puts == []


def test_unavailable_setting_has_no_control():
    resources = variant(FIXTURE, "V8")
    assert SWITCH_MODE not in select_paths(resources)
    assert THRESHOLD not in number_paths(resources)
    assert assess_control(resources[SWITCH_MODE]).rejection_reason == "not_available"


# Heating limit -----------------------------------------------------------


async def test_heating_limit_writes_once_on_the_grid(hass):
    coordinator, cloud = _setup(hass, "V1")
    entity = number_entity(hass, coordinator, THRESHOLD)
    assert entity.entity_description.write_policy is SUMMER_WINTER_THRESHOLD_POLICY
    assert (entity.native_min_value, entity.native_max_value) == (10, 30)
    assert entity.native_step == 1.0
    await entity.async_set_native_value(17)
    # Unchanged (17 is the current value): nothing sent.
    assert cloud.puts == []
    await entity.async_set_native_value(18)
    assert cloud.puts == [(THRESHOLD, 18.0)]
    assert set(cloud.bulk_calls[-1]) == {THRESHOLD, CONTROL_TYPE, SWITCH_MODE}


@pytest.mark.parametrize("value", [9, 31, 17.3, 17.5])
async def test_heating_limit_outside_limits_or_grid_sends_nothing(hass, value):
    """Grid 1.0 from the minimum without an advertised step (as the app)."""
    coordinator, cloud = _setup(hass, "V1")
    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(
            THRESHOLD, value, SUMMER_WINTER_THRESHOLD_POLICY
        )
    assert cloud.puts == []


async def test_advertised_step_sets_the_grid_from_the_minimum(hass):
    resources = variant(FIXTURE, "V1")
    resources[THRESHOLD] = with_flags(resources[THRESHOLD], step_size=0.5)
    coordinator = coordinator_for(hass, resources)
    cloud = Cloud(coordinator)
    entity = number_entity(hass, coordinator, THRESHOLD)
    assert entity.native_step == 0.5
    with pytest.raises(ServiceValidationError):
        await entity.async_set_native_value(17.3)
    await entity.async_set_native_value(17.5)
    assert cloud.puts == [(THRESHOLD, 17.5)]


@pytest.mark.parametrize("missing", ["minimum", "maximum"])
def test_no_number_without_both_limits(missing):
    resources = variant(FIXTURE, "V1")
    resources[THRESHOLD] = with_flags(resources[THRESHOLD], **{missing: None})
    assert assess_control(resources[THRESHOLD]).rejection_reason == "missing_bounds"
    assert THRESHOLD not in number_paths(resources)


async def test_fahrenheit_is_shown_and_written_natively(hass):
    resources = variant(FIXTURE, "V1")
    resources[THRESHOLD] = replace(
        resources[THRESHOLD],
        value=62.0,
        metadata=replace(
            resources[THRESHOLD].metadata, unit="F", minimum=50.0, maximum=86.0
        ),
    )
    coordinator = coordinator_for(hass, resources)
    cloud = Cloud(coordinator)
    entity = number_entity(hass, coordinator, THRESHOLD)
    assert entity.native_unit_of_measurement == "°F"
    await entity.async_set_native_value(64)
    assert cloud.puts == [(THRESHOLD, 64.0)]


# Failure paths with real write cycles ---------------------------------------


@pytest.mark.parametrize(
    ("failure", "key"),
    [
        (ResourceForbidden(SWITCH_MODE, 403), "write_failed"),
        (ResourceNotFound(SWITCH_MODE, 404), "write_failed"),
        (ServiceUnavailable(503), "write_not_confirmed"),
        (RateLimited(60), "write_rate_limited"),
    ],
)
async def test_failed_write_is_never_repeated(hass, failure, key):
    coordinator, cloud = _setup(hass, "V1")
    cloud.put_error = failure
    with pytest.raises(HomeAssistantError) as error:
        await select_entity(hass, coordinator, SWITCH_MODE).async_select_option(
            "forced"
        )
    assert error.value.translation_key == key
    assert cloud.puts == [(SWITCH_MODE, "forced")]
    if isinstance(failure, RateLimited):
        assert coordinator._cloud_backoff_until > 0


async def test_read_back_with_the_old_value_is_not_confirmed(hass):
    coordinator, cloud = _setup(hass, "V1")
    cloud.stuck[SWITCH_MODE] = "automatic"
    with pytest.raises(HomeAssistantError) as error:
        await select_entity(hass, coordinator, SWITCH_MODE).async_select_option(
            "forced"
        )
    assert error.value.translation_key == "write_not_confirmed"
    assert cloud.puts == [(SWITCH_MODE, "forced")]
    # Read back with the iCom schedule's patience, never sent twice.
    assert cloud.gets.count(SWITCH_MODE) >= 3


async def test_read_back_timeout_is_not_confirmed(hass):
    coordinator, cloud = _setup(hass, "V1")
    cloud.readback_error = ServiceUnavailable(504)
    with pytest.raises(HomeAssistantError) as error:
        await number_entity(hass, coordinator, THRESHOLD).async_set_native_value(18)
    assert error.value.translation_key == "write_not_confirmed"
    assert cloud.puts == [(THRESHOLD, 18.0)]


async def test_cloud_pause_refuses_the_command(hass):
    coordinator, cloud = _setup(hass, "V1")
    coordinator.control_status = lambda: ("protection_pause", None)
    snapshot = coordinator.data[CONTROL_TYPE]
    coordinator.data[CONTROL_TYPE] = replace(
        snapshot, available=False, freshness=Freshness.STALE
    )
    with pytest.raises(HomeAssistantError) as error:
        await select_entity(hass, coordinator, SWITCH_MODE).async_select_option(
            "forced"
        )
    assert error.value.translation_key == "command_cloud_protection_pause"
    assert cloud.puts == []


# Room controller plants and the central season switching -------------------


def test_room_controller_plant_keeps_its_contract():
    """On an RRC controller only its contract applies (off/automatic)."""
    resources = variant(FIXTURE, "V2")
    mode = resources[SWITCH_MODE]
    assessment = assess_control(mode, RRC_CONTEXT)
    assert assessment.policy is RRC_SUMMER_WINTER_MODE_POLICY
    assert assessment.policy is not SUMMER_WINTER_MODE_POLICY
    assert assess_control(mode, GENERIC_CONTEXT).policy is SUMMER_WINTER_MODE_POLICY
    # The new cooling settings are not offered on a room controller either.
    for path in (
        "/heatingCircuits/hc1/cooling/outdoorThreshold",
        "/heatingCircuits/hc1/cooling/roomTempSetpoint",
    ):
        assert assess_control(resources[path], RRC_CONTEXT).policy is None


def test_k40_shape_has_no_circuit_switching():
    resources = variant(FIXTURE, "V9")
    assert select_paths(resources) == {"/system/seasonOptimizer/mode"}
    assert number_paths(resources) == set()


# Translations ------------------------------------------------------------


def test_names_options_and_errors_in_every_language():
    names = {
        "select": ("summer_winter_switching",),
        "number": ("summer_winter_threshold",),
    }
    for path in sorted((COMPONENT / "translations").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for platform, keys in names.items():
            for key in keys:
                assert data["entity"][platform][key]["name"], (path.name, key)
        states = data["entity"]["select"]["summer_winter_switching"]["state"]
        assert set(states) == {"automatic", "forced", "cooling", "off"}
        assert data["exceptions"]["circuit_room_controller"]["message"]
    assert len(list((COMPONENT / "translations").glob("*.json"))) == 19


# Controls dialog -------------------------------------------------------------


async def _dialog(hass, coordinator):
    from custom_components.bosch_buderus_heating.config_flow import (
        BoschBuderusOptionsFlow,
    )

    options = BoschBuderusOptionsFlow(coordinator.config_entry)
    options.hass = hass
    result = await options.async_step_controls()
    if result.get("step_id") != "operate":
        result = await options.async_step_operate()
    return options, result


async def test_dialog_writes_with_the_control_type_guard(
    hass, enable_custom_integrations
):
    coordinator, cloud = _setup(hass, "V2")
    options, result = await _dialog(hass, coordinator)
    rows = {
        row["key"]: row
        for row in next(iter(result["data_schema"].schema.values())).rows
    }
    row = rows[SWITCH_MODE]
    assert [option["value"] for option in row["options"]] == [
        "automatic",
        "forced",
        "cooling",
        "off",
    ]
    assert row["options"][1]["label"]["en"] == "Heating only"
    assert rows["/heatingCircuits/hc1/cooling/roomTempSetpoint"]["minimum"] == 18
    result = await options.async_step_operate(
        {"controls": {"changes": {SWITCH_MODE: "forced"}}}
    )
    assert not result["errors"]
    assert cloud.puts == [(SWITCH_MODE, "forced")]
    assert set(cloud.bulk_calls[0]) == {SWITCH_MODE, CONTROL_TYPE}


async def test_dialog_sends_nothing_after_a_control_type_change(
    hass, enable_custom_integrations
):
    coordinator, cloud = _setup(hass, "V1")
    options, _ = await _dialog(hass, coordinator)
    cloud.change(CONTROL_TYPE, value="ISRC")
    result = await options.async_step_operate(
        {"controls": {"changes": {THRESHOLD: 18}}}
    )
    assert result["errors"] == {"base": "controls_changed"}
    assert cloud.puts == []


async def test_dialog_refuses_a_room_controller_circuit(
    hass, enable_custom_integrations
):
    coordinator, cloud = _setup(hass, "V6")
    options, _ = await _dialog(hass, coordinator)
    result = await options.async_step_operate(
        {"controls": {"changes": {SWITCH_MODE: "off"}}}
    )
    assert result["errors"] == {"base": "controls_rejected"}
    assert cloud.puts == []
