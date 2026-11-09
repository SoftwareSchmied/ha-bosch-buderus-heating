"""Integration tests for heating climates, dialog drafts and guarded writes."""

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.exceptions import ServiceValidationError

from custom_components.bosch_buderus_heating.config_flow import BoschBuderusOptionsFlow
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.controls_flow import (
    ControlsSelector,
    controls,
)
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.entity_resources import (
    fresh_resource,
    temperature,
)
from custom_components.bosch_buderus_heating.heating_climate import (
    HeatingCircuitClimate,
    track_heating_climates,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    Gateway,
    PointTError,
    RateLimited,
    Resource,
    ResourceMetadata,
    WriteNotConfirmed,
    WriteResult,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.runtime import BoschBuderusRuntimeData
from custom_components.bosch_buderus_heating.writes import (
    HEATING_CIRCUIT_OPERATION_MODE_POLICY,
)
from tests.ha_test_helpers import MockConfigEntry

ROOT = "/heatingCircuits/hc2"
MODE = ROOT + "/operationMode"
TARGET = ROOT + "/manualRoomSetpoint"


def scalar(path, value, *, writable=True, unit="C", options=(), minimum=5, maximum=30):
    return Resource(
        path,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="stringValue" if isinstance(value, str) else "floatValue",
            unit=None if isinstance(value, str) else unit,
            writable=writable,
            allowed_values=options,
            minimum=minimum,
            maximum=maximum,
        ),
    )


def setup(hass, *, mode="manual", options=("off", "manual", "auto")):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"brand": "buderus", "gateway_ids": ["gateway"]}
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, client, Gateway("gateway"), entry
    )
    resources = [
        scalar(MODE, mode, options=options),
        scalar(TARGET, 19),
        scalar(ROOT + "/roomtemperature", 20, writable=False),
        scalar(ROOT + "/currentRoomSetpoint", 21, writable=False),
    ]
    coordinator.data = {
        r.path: ResourceSnapshot(r, True, datetime.now(UTC)) for r in resources
    }
    coordinator.resources = {r.path: r for r in resources}
    coordinator.last_update_success = True
    client.get_resource.side_effect = lambda _gateway, path: (
        coordinator.data[path].resource
    )

    async def write(_gateway, resource, value, _policy, **_kwargs):
        desired = replace(resource, value=value)
        return WriteResult(None, desired)

    coordinator._write_service.async_write_number = AsyncMock(side_effect=write)
    coordinator._write_service.async_write_enum = AsyncMock(side_effect=write)
    entry.runtime_data = BoschBuderusRuntimeData(
        client, AsyncMock(), (coordinator.gateway,), (coordinator,)
    )
    entity = HeatingCircuitClimate(coordinator, ROOT)
    entity.hass = hass
    return entry, coordinator, entity


def change(coordinator, path, value):
    before = coordinator.data[path]
    coordinator.data[path] = replace(
        before, resource=replace(before.resource, value=value)
    )


async def flow(hass, **kwargs):
    entry, coordinator, entity = setup(hass, **kwargs)
    options = BoschBuderusOptionsFlow(entry)
    options.hass = hass
    result = await options.async_step_controls()
    return options, coordinator, entity, result


def selector(result):
    return next(iter(result["data_schema"].schema.values()))


def test_climate_identity_and_measurement(hass):
    _, coordinator, entity = setup(hass)
    assert entity.name == "Heating circuit 2"
    assert entity.unique_id == "gateway:heatingCircuits:hc2:climate"
    assert entity.device_info["identifiers"] == {(DOMAIN, "gateway")}
    assert entity.hvac_modes == [HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO]
    assert entity.hvac_mode == HVACMode.HEAT
    assert entity.native_target_temperature == 19
    assert entity.native_current_temperature == 20
    assert entity.min_temp == 5 and entity.max_temp == 30
    assert entity.target_temperature_step == 0.5
    assert entity.capability_attributes["target_temp_step"] == 0.5
    assert entity.hvac_action is None
    assert entity.supported_features == (
        ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.TURN_OFF
    )
    del coordinator.data[ROOT + "/roomtemperature"]
    assert entity.native_current_temperature is None


async def test_manual_target_writes_correct_circuit_and_keeps_mode(hass):
    _, coordinator, entity = setup(hass)
    await entity.async_set_temperature(temperature=20.5)
    assert (
        coordinator._write_service.async_write_number.await_args.args[1].path == TARGET
    )
    assert coordinator._write_service.async_write_number.await_args.args[2] == 20.5
    coordinator._write_service.async_write_enum.assert_not_awaited()
    assert entity.native_target_temperature == 20.5
    assert coordinator.client.get_resource.await_count == 2


@pytest.mark.parametrize("mode,target", [("auto", 21), ("off", None)])
async def test_nonmanual_modes_do_not_write_target(hass, mode, target):
    _, coordinator, entity = setup(hass, mode=mode)
    assert entity.native_target_temperature == target
    assert not entity.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=22)
    coordinator._write_service.async_write_number.assert_not_awaited()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "20", None])
async def test_invalid_climate_values_never_reach_writer(hass, value):
    _, coordinator, entity = setup(hass)
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=value)
    coordinator._write_service.async_write_number.assert_not_awaited()


async def test_combined_commands_are_rejected_without_partial_mode_change(hass):
    _, coordinator, entity = setup(hass, mode="auto")
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=22, hvac_mode=HVACMode.HEAT)
    coordinator._write_service.async_write_enum.assert_not_awaited()


async def test_mode_subset_and_unknown_values(hass):
    _, coordinator, entity = setup(hass, options=("manual", "auto", "vendor_future"))
    assert entity.hvac_modes == [HVACMode.HEAT, HVACMode.AUTO]
    with pytest.raises(ServiceValidationError):
        await entity.async_set_hvac_mode(HVACMode.OFF)
    await entity.async_set_hvac_mode(HVACMode.AUTO)
    assert coordinator._write_service.async_write_enum.await_args.args[2] == "auto"
    change(coordinator, MODE, "vendor_future")
    assert not entity.available and entity.hvac_mode is None


@pytest.mark.parametrize(
    "path,value",
    [
        (ROOT + "/heatCoolMode", "cool"),
        (ROOT + "/currentSuWiMode", "cooling"),
        ("/system/seasonOptimizer/mode", "forcedCool"),
        ("/system/globalSeasonOptimizer/currentMode", "cooling"),
        ("/system/globalSeasonOptimizer/currentMode", "forcedCool"),
        (ROOT + "/currentSuWiMode", "unknown"),
    ],
)
async def test_cooling_context_never_controls_heating_target(hass, path, value):
    _, coordinator, entity = setup(hass)
    coordinator.data[path] = ResourceSnapshot(
        scalar(path, value), True, datetime.now(UTC)
    )
    assert not entity.available
    assert entity.hvac_modes == []
    with pytest.raises(ServiceValidationError):
        await entity.async_set_hvac_mode(HVACMode.HEAT)


async def test_changed_mode_on_gateway_blocks_temperature(hass):
    _, coordinator, entity = setup(hass)
    coordinator.client.get_resource.side_effect = lambda _gateway, path: (
        replace(coordinator.data[path].resource, value="auto")
        if path == MODE
        else coordinator.data[path].resource
    )
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=22)
    coordinator._write_service.async_write_number.assert_not_awaited()
    assert entity.hvac_mode == HVACMode.AUTO


async def test_waiting_write_rechecks_cached_state_inside_lock(hass):
    import asyncio

    _, coordinator, entity = setup(hass)
    async with coordinator._update_lock:
        task = asyncio.create_task(entity.async_set_temperature(temperature=22))
        await asyncio.sleep(0)
        change(coordinator, MODE, "auto")
    with pytest.raises(ServiceValidationError):
        await task
    coordinator.client.get_resource.assert_not_awaited()


@pytest.mark.parametrize(
    "field,value", [("available", False), ("freshness", Freshness.STALE)]
)
def test_stale_resources_disable_climate_and_dialog(hass, field, value):
    _, coordinator, entity = setup(hass)
    coordinator.data[MODE] = replace(coordinator.data[MODE], **{field: value})
    assert not entity.available
    assert MODE not in controls(coordinator)
    assert fresh_resource(coordinator, MODE) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("unit", "unknown"),
        ("numeric_error_states_valid", False),
        ("numeric_error_values", (20,)),
        ("minimum", 21),
        ("maximum", 19),
    ],
)
def test_temperature_contract(hass, field, value):
    _, coordinator, entity = setup(hass)
    path = ROOT + "/roomtemperature"
    old = coordinator.data[path]
    coordinator.data[path] = replace(
        old,
        resource=replace(
            old.resource, metadata=replace(old.resource.metadata, **{field: value})
        ),
    )
    assert entity.native_current_temperature is None


@pytest.mark.parametrize("value", [True, None, "20", float("nan"), float("inf")])
def test_temperature_rejects_nonnumeric_values(value):
    assert temperature(scalar(ROOT + "/roomtemperature", value)) is None


def test_fahrenheit_conversion(hass):
    _, coordinator, entity = setup(hass)
    resource = scalar(TARGET, 68, unit="F", minimum=41, maximum=86)
    coordinator.data[TARGET] = ResourceSnapshot(resource, True, datetime.now(UTC))
    assert entity.native_target_temperature == 20
    assert entity.min_temp == 5 and entity.max_temp == 30
    assert entity.target_temperature_step == pytest.approx(0.5 / 1.8)


async def test_climate_discovery_recovers_without_duplicates(hass):
    entry, coordinator, entity = setup(hass, mode="unknown")
    added = []
    track_heating_climates(entry, coordinator, added.extend)
    assert not added
    change(coordinator, MODE, "manual")
    coordinator.async_update_listeners()
    coordinator.async_update_listeners()
    assert len(added) == 1 and added[0].unique_id == entity.unique_id
    await coordinator.async_shutdown()


async def test_dialog_shows_real_metadata_and_localized_options(
    hass, enable_custom_integrations
):
    options, coordinator, _, result = await flow(hass)
    assert result["step_id"] == "operate"
    ui = selector(result)
    assert isinstance(ui, ControlsSelector)
    rows = ui.serialize()["selector"]["bosch-buderus-controls"]["rows"]
    mode = next(row for row in rows if row["key"] == MODE)
    assert mode["label"] == {"de": "Betriebsart", "en": "Operating mode"}
    assert mode["options"][1]["label"] == {"de": "Manuell", "en": "Manual"}
    assert set(controls(coordinator)) == {MODE, TARGET}
    assert options._entry.options == {}
    coordinator.client.get_resource.assert_not_awaited()


async def test_dialog_validates_whole_draft_before_any_write(
    hass, enable_custom_integrations
):
    options, coordinator, _, _ = await flow(hass)
    result = await options.async_step_operate(
        {"controls": {"changes": {MODE: "auto", TARGET: 1000}}}
    )
    assert result["errors"] == {"base": "controls_invalid"}
    coordinator.client.get_resource.assert_not_awaited()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"controls": None},
        {"controls": {"changes": []}},
        {"controls": {"action": "unknown"}},
        {"controls": {"extra": 1}},
        {"controls": {"changes": {"/unknown": "on"}}},
        {"controls": {"changes": {TARGET: True}}},
    ],
)
async def test_invalid_dialog_payloads(hass, enable_custom_integrations, payload):
    options, coordinator, _, _ = await flow(hass)
    if payload is None:
        result = await options.async_step_operate()
        assert not result["errors"]
    else:
        result = await options.async_step_operate(payload)
        assert result["errors"]
    coordinator._write_service.async_write_number.assert_not_awaited()


async def test_dialog_saves_and_stays_open(hass, enable_custom_integrations):
    options, _coordinator, entity, _ = await flow(hass)
    result = await options.async_step_operate({"controls": {"changes": {TARGET: 20}}})
    assert result["step_id"] == "operate" and not result["errors"]
    assert selector(result).status == "saved"
    assert entity.native_target_temperature == 20
    assert options._entry.options == {}


async def test_dialog_refresh_is_local_and_rebases_changes(
    hass, enable_custom_integrations
):
    options, coordinator, _, _ = await flow(hass)
    change(coordinator, TARGET, 21)
    failed = await options.async_step_operate({"controls": {"changes": {TARGET: 22}}})
    assert failed["errors"] == {"base": "controls_invalid"}
    refreshed = await options.async_step_operate({"controls": {"action": "refresh"}})
    assert not refreshed["errors"]
    assert options._control_displayed[TARGET].value == 21
    coordinator.client.get_resource.assert_not_awaited()


@pytest.mark.parametrize(
    "error,key",
    [
        (AuthenticationError("expired"), "controls_auth"),
        (RateLimited(60), "controls_rate_limit"),
        (WriteNotConfirmed("lost"), "controls_unconfirmed"),
        (WriteValidationError("changed"), "controls_changed"),
        (PointTError("lost"), "controls_failed"),
    ],
)
async def test_dialog_partial_write_failure_is_explicit(
    hass, enable_custom_integrations, error, key
):
    options, coordinator, _, _ = await flow(hass)
    coordinator._write_service.async_write_enum.side_effect = error
    result = await options.async_step_operate(
        {"controls": {"changes": {TARGET: 20, MODE: "auto"}}}
    )
    assert result["errors"] == {"base": key}
    assert coordinator.data[TARGET].resource.value == 20
    assert options._control_displayed[TARGET].value == 20


async def test_options_menu_has_controls_and_unloaded_abort(hass):
    entry = MockConfigEntry(domain=DOMAIN)
    options = BoschBuderusOptionsFlow(entry)
    options.hass = hass
    result = await options.async_step_init()
    assert result["menu_options"][0] == "controls"
    assert (await options.async_step_controls())["reason"] == "controls_not_loaded"


async def test_multiple_gateways_require_explicit_selection(
    hass, enable_custom_integrations
):
    options, coordinator, _, _ = await flow(hass)
    second = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("second"), options._entry
    )
    second.data = coordinator.data.copy()
    second.last_update_success = True
    options._entry.runtime_data.coordinators = (coordinator, second)
    result = await options.async_step_controls()
    assert result["step_id"] == "controls"
    key = list(options._control_gateways())[1]
    result = await options.async_step_controls({"installation": key})
    assert result["step_id"] == "operate"
    assert options._control_gateway == key
    coordinator.client.get_resource.assert_not_awaited()


async def test_guard_rejects_wrong_response_path_and_stale_metadata(hass):
    _, coordinator, _ = setup(hass)
    before = coordinator.data[MODE].resource
    coordinator.client.get_resource.return_value = replace(before, path="/other")
    coordinator.client.get_resource.side_effect = None
    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(
            MODE, "auto", HEATING_CIRCUIT_OPERATION_MODE_POLICY, expected=(before,)
        )
    coordinator._write_service.async_write_enum.assert_not_awaited()
    assert coordinator.data[MODE].resource == before


async def test_real_options_flow_selector_round_trip(hass, enable_custom_integrations):
    entry, coordinator, _ = setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "controls"}
    )
    assert result["step_id"] == "operate"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"controls": {"changes": {TARGET: 20}}}
    )
    assert not result["errors"]
    assert coordinator.data[TARGET].resource.value == 20


@pytest.mark.parametrize("language,prefix", [("de", "HK 2"), ("en", "HC 2")])
def test_control_names_icons_categories_and_custom_groups(hass, language, prefix):
    from homeassistant.const import EntityCategory

    from custom_components.bosch_buderus_heating.control_presentation import (
        control_name,
    )
    from custom_components.bosch_buderus_heating.number import (
        BoschBuderusNumber,
        build_number_descriptions,
    )

    hass.config.language = language
    _, coordinator, _ = setup(hass)
    description = build_number_descriptions(
        {TARGET: coordinator.data[TARGET].resource}
    )[0]
    entity = BoschBuderusNumber(coordinator, description)
    assert entity.name.startswith(prefix + ": ")
    assert description.icon == "mdi:thermometer"
    assert description.entity_category is EntityCategory.CONFIG
    assert description.mode == "box"
    name = scalar(ROOT + "/name", "Obergeschoss", writable=False)
    coordinator.data[name.path] = ResourceSnapshot(name, True, datetime.now(UTC))
    assert control_name(coordinator, TARGET, "unused").startswith("Obergeschoss: ")
    assert entity.unique_id == "gateway:heatingCircuits:hc2:manualRoomSetpoint:control"


@pytest.mark.parametrize(
    "path",
    [
        "/dhwCircuits/dhw2/temperatureLevels/eco",
        "/dhwCircuits/dhw2/temperatureLevels/high",
        "/dhwCircuits/dhw2/temperatureLevels/low",
    ],
)
def test_dhw_names_keep_start_temperature_semantics(path):
    from custom_components.bosch_buderus_heating.control_presentation import (
        control_label,
    )

    assert "Starttemperatur" in control_label(path, "fallback", "de")
    assert "start temperature" in control_label(path, "fallback", "en")


async def test_frontend_registration_for_both_modules(hass):
    from types import SimpleNamespace
    from unittest.mock import Mock, patch

    from custom_components.bosch_buderus_heating.holiday_ui import (
        async_setup_controls_ui,
        async_setup_holiday_ui,
    )

    hass.config.components.add("frontend")
    hass.http = SimpleNamespace(async_register_static_paths=AsyncMock())
    with patch(
        "homeassistant.components.frontend.add_extra_js_url", new=Mock()
    ) as register:
        await async_setup_controls_ui(hass)
        await async_setup_holiday_ui(hass)
        await async_setup_controls_ui(hass)
    assert register.call_count == 2
    assert "controls-dialog-" in register.call_args_list[0].args[1]
    assert "holiday-dialog-" in register.call_args_list[1].args[1]


async def test_empty_offline_dialog_and_unload(hass, enable_custom_integrations):
    options, coordinator, _, _ = await flow(hass)
    coordinator.last_update_success = False
    result = await options.async_step_operate({"controls": {"action": "refresh"}})
    assert selector(result).status == "empty"
    del options._entry.runtime_data
    assert (await options.async_step_operate())["reason"] == "controls_not_loaded"


async def test_new_enum_codes_use_select_without_losing_the_wire_value(
    hass, enable_custom_integrations
):
    options, coordinator, _, _ = await flow(hass)
    path = "/dhwCircuits/dhw1/charge"
    resource = scalar(path, "boost", options=("start", "stop", "boost"))
    coordinator.data[path] = ResourceSnapshot(resource, True, datetime.now(UTC))
    rows = await options._control_rows({path: resource}, current={path: resource})
    assert rows[0]["kind"] == "select"
    assert rows[0]["options"][-1]["value"] == "boost"
    assert rows[0]["options"][-1]["label"] == {"de": "boost", "en": "boost"}


async def test_already_applied_draft_causes_no_write(hass, enable_custom_integrations):
    options, coordinator, _, _ = await flow(hass)
    result = await options.async_step_operate({"controls": {"changes": {TARGET: 19}}})
    assert not result["errors"]
    coordinator.client.get_resource.assert_not_awaited()
