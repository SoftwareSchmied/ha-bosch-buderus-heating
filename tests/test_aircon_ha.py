"""Air conditioners on a PointT gateway: Home Assistant entities and commands."""

import pytest
from homeassistant.components.climate.const import ClimateEntityFeature as Feature
from homeassistant.components.climate.const import HVACMode
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating.aircon_climate import AirconClimate
from custom_components.bosch_buderus_heating.aircon_entities import (
    AirconEndTemporary,
    AirconSelect,
    AirconSolarOperation,
    AirconSwitch,
    AirconTimer,
    _binary_sensors,
    _buttons,
    _selects,
    _switches,
    _timers,
)
from custom_components.bosch_buderus_heating.aircon_entity import option_key
from custom_components.bosch_buderus_heating.aircon_policies import (
    AC_CONTROL,
    ACTIVE_PROGRAM,
    AIR_FLOW_HORIZONTAL,
    AIR_FLOW_VERTICAL,
    ECO_MODE,
    FAN_SPEED,
    OPERATION_MODE,
    PV_ENABLE,
    QUICK_AIR_FLOWS,
    ROOM_TEMPERATURE,
    SETBACK_MODE,
    TEMPERATURE_SETPOINT,
    TEMPORARY_SETTING,
    TIMER_OFF,
    TIMER_ON,
)
from tests.aircon_helpers import (  # noqa: F401
    GATEWAY,
    FakeUnit,
    aircon_cleanup,
    make_coordinator,
)


def climate_of(hass, coordinator):
    entity = AirconClimate(coordinator)
    entity.hass = hass
    return entity


async def test_climate_shows_the_reported_state(hass):
    coordinator, _, _ = await make_coordinator(hass)
    entity = climate_of(hass, coordinator)
    assert entity.unique_id == f"{GATEWAY}:airConditioning:climate"
    assert entity.name is None  # the device name
    assert entity.entity_registry_enabled_default
    assert entity.available
    assert entity.hvac_mode is HVACMode.COOL
    assert entity.hvac_modes == [
        HVACMode.OFF,
        HVACMode.HEAT_COOL,
        HVACMode.COOL,
        HVACMode.HEAT,
        HVACMode.DRY,
        HVACMode.FAN_ONLY,
    ]
    assert entity.hvac_action is None
    assert entity.native_current_temperature == 21.5
    assert entity.native_target_temperature == 22.0
    assert (entity.min_temp, entity.max_temp, entity.target_temperature_step) == (
        16.0,
        30.0,
        1.0,
    )
    assert entity.fan_modes == ["auto", "quiet", "low", "medium", "high", "turbo"]
    assert entity.fan_mode == "auto"
    assert entity.swing_modes == ["auto", "swing", *(f"angle{i}" for i in range(1, 6))]
    assert entity.swing_horizontal_modes == [
        "auto",
        "swing",
        "left",
        "center",
        "right",
        "wide",
        "ultra_wide",
        "fixed",
    ]
    assert entity.swing_horizontal_mode == "center"
    assert entity.preset_modes == ["none", "eco"]
    assert entity.preset_mode == "none"
    assert entity.supported_features == (
        Feature.TURN_ON
        | Feature.TURN_OFF
        | Feature.TARGET_TEMPERATURE
        | Feature.FAN_MODE
        | Feature.SWING_MODE
        | Feature.SWING_HORIZONTAL_MODE
        | Feature.PRESET_MODE
    )


@pytest.mark.parametrize(
    "token,expected",
    [("ultraWide", "ultra_wide"), ("mid", "medium"), ("topLeft", "top_left")],
)
def test_option_keys_are_lower_case(token, expected):
    assert option_key(token) == expected


async def test_switched_off_unit_shows_off(hass):
    unit = FakeUnit()
    unit.set(AC_CONTROL, value="off")
    coordinator, _, _ = await make_coordinator(hass, unit)
    assert climate_of(hass, coordinator).hvac_mode is HVACMode.OFF


async def test_off_as_mode_is_read_as_off(hass):
    unit = FakeUnit()
    unit.set(OPERATION_MODE, value="off")
    coordinator, _, _ = await make_coordinator(hass, unit)
    entity = climate_of(hass, coordinator)
    assert entity.hvac_mode is HVACMode.OFF


async def test_unknown_mode_is_no_mode(hass):
    unit = FakeUnit()
    unit.set(OPERATION_MODE, value="internalCleaning")
    coordinator, _, _ = await make_coordinator(hass, unit)
    entity = climate_of(hass, coordinator)
    assert entity.hvac_mode is None
    assert HVACMode.HEAT in entity.hvac_modes


async def test_without_power_path_no_off(hass):
    unit = FakeUnit()
    unit.status[AC_CONTROL] = 404
    coordinator, _, _ = await make_coordinator(hass, unit)
    entity = climate_of(hass, coordinator)
    assert HVACMode.OFF not in entity.hvac_modes
    assert not entity.supported_features & Feature.TURN_OFF
    assert entity.hvac_mode is HVACMode.COOL
    with pytest.raises(ServiceValidationError):
        await entity.async_turn_off()
    assert unit.puts == []


@pytest.mark.parametrize("unit_name", ["F", "K", None])
async def test_room_temperature_only_in_celsius(hass, unit_name):
    unit = FakeUnit()
    unit.set(ROOM_TEMPERATURE, unitOfMeasure=unit_name)
    unit.set(TEMPERATURE_SETPOINT, unitOfMeasure=unit_name)
    coordinator, _, _ = await make_coordinator(hass, unit)
    entity = climate_of(hass, coordinator)
    assert entity.native_current_temperature is None
    assert not entity.supported_features & Feature.TARGET_TEMPERATURE


async def test_missing_room_temperature_keeps_the_entity(hass):
    unit = FakeUnit()
    unit.status[ROOM_TEMPERATURE] = 404
    coordinator, _, _ = await make_coordinator(hass, unit)
    entity = climate_of(hass, coordinator)
    assert entity.native_current_temperature is None
    assert entity.available


@pytest.mark.parametrize(
    "fields",
    [
        {"writeable": 0},
        {"minValue": 30.0, "maxValue": 16.0},
        {"minValue": 22.0, "maxValue": 22.0},
        {"value": 35.0},
    ],
)
async def test_setpoint_feature_needs_a_valid_writable_range(hass, fields):
    unit = FakeUnit()
    unit.set(TEMPERATURE_SETPOINT, **fields)
    coordinator, _, _ = await make_coordinator(hass, unit)
    entity = climate_of(hass, coordinator)
    assert not entity.supported_features & Feature.TARGET_TEMPERATURE
    assert entity.native_target_temperature is None
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=23)
    assert unit.puts == []


async def test_reported_step_is_the_target_step(hass):
    unit = FakeUnit()
    unit.set(TEMPERATURE_SETPOINT, stepSize=0.5)
    coordinator, _, _ = await make_coordinator(hass, unit)
    assert climate_of(hass, coordinator).target_temperature_step == 0.5


async def test_on_off_louvres_use_the_standard_values(hass):
    unit = FakeUnit()
    unit.set(AIR_FLOW_VERTICAL, value="on", allowedValues=["on", "off"])
    coordinator, _, _ = await make_coordinator(hass, unit)
    entity = climate_of(hass, coordinator)
    assert entity.swing_modes == ["off", "on"]
    assert entity.swing_mode == "on"
    await entity.async_set_swing_mode("off")
    assert unit.puts == [(AIR_FLOW_VERTICAL, "off")]


async def test_unknown_fan_value_is_no_fan_mode(hass):
    unit = FakeUnit()
    unit.set(FAN_SPEED, value="boost")
    coordinator, _, _ = await make_coordinator(hass, unit)
    assert climate_of(hass, coordinator).fan_mode is None


async def test_not_writable_fan_gives_no_fan_modes(hass):
    unit = FakeUnit()
    unit.set(FAN_SPEED, writeable=0)
    coordinator, _, _ = await make_coordinator(hass, unit)
    entity = climate_of(hass, coordinator)
    assert entity.fan_modes == []
    assert not entity.supported_features & Feature.FAN_MODE


async def test_climate_commands_write_one_path_each(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    entity = climate_of(hass, coordinator)
    await entity.async_set_fan_mode("medium")
    await entity.async_set_swing_mode("angle3")
    await entity.async_set_swing_horizontal_mode("ultra_wide")
    await entity.async_set_preset_mode("eco")
    await entity.async_set_temperature(temperature=24)
    await entity.async_set_hvac_mode(HVACMode.HEAT)
    await entity.async_set_hvac_mode(HVACMode.OFF)
    await entity.async_turn_on()
    assert unit.puts == [
        (FAN_SPEED, "mid"),
        (AIR_FLOW_VERTICAL, "angle3"),
        (AIR_FLOW_HORIZONTAL, "ultraWide"),
        (ECO_MODE, "on"),
        (TEMPERATURE_SETPOINT, 24.0),
        (OPERATION_MODE, "heat"),
        (AC_CONTROL, "off"),
        (AC_CONTROL, "on"),
    ]


async def test_mode_on_a_switched_off_unit_is_refused_with_a_hint(hass):
    unit = FakeUnit()
    unit.set(AC_CONTROL, value="off")
    coordinator, _, _ = await make_coordinator(hass, unit)
    entity = climate_of(hass, coordinator)
    with pytest.raises(ServiceValidationError) as err:
        await entity.async_set_hvac_mode(HVACMode.HEAT)
    assert err.value.translation_key == "aircon_switched_off"
    assert unit.puts == []


async def test_temperature_with_mode_is_refused(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    entity = climate_of(hass, coordinator)
    with pytest.raises(ServiceValidationError) as err:
        await entity.async_set_temperature(temperature=24, hvac_mode=HVACMode.HEAT)
    assert err.value.translation_key == "bacon_separate_settings"
    assert unit.puts == []


@pytest.mark.parametrize(
    "call",
    [
        lambda entity: entity.async_set_fan_mode("mid"),
        lambda entity: entity.async_set_swing_mode("ultra_wide"),
        lambda entity: entity.async_set_preset_mode("boost"),
        lambda entity: entity.async_set_hvac_mode(HVACMode.AUTO),
        lambda entity: entity.async_set_temperature(temperature=True),
        lambda entity: entity.async_set_temperature(temperature=22, extra=1),
    ],
)
async def test_unoffered_commands_are_refused_unsent(hass, call):
    coordinator, unit, _ = await make_coordinator(hass)
    with pytest.raises(ServiceValidationError):
        await call(climate_of(hass, coordinator))
    assert unit.puts == []


async def test_cloud_pause_refuses_with_its_reason(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    unit.backoff.remaining = 300.0
    with pytest.raises(HomeAssistantError) as err:
        await climate_of(hass, coordinator).async_set_fan_mode("high")
    assert err.value.translation_key == "command_cloud_rate_limited"
    assert unit.puts == [] and len(unit.bulk_calls) == 2


async def test_unconfirmed_write_has_its_own_message(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    unit.apply_puts = False
    with pytest.raises(HomeAssistantError) as err:
        await climate_of(hass, coordinator).async_set_fan_mode("high")
    assert err.value.translation_key == "aircon_write_not_confirmed"
    assert len(unit.puts) == 1


async def test_unused_setting_message(hass):
    unit = FakeUnit()
    unit.set(FAN_SPEED, used=False)
    coordinator, _, _ = await make_coordinator(hass, unit)
    with pytest.raises(ServiceValidationError) as err:
        await climate_of(hass, coordinator).async_set_fan_mode("high")
    assert err.value.translation_key == "control_unused"
    assert unit.puts == []


async def test_offline_unit_makes_the_climate_unavailable(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    entity = climate_of(hass, coordinator)
    for path in list(unit.resources):
        unit.status[path] = 503
    await coordinator.async_refresh()
    assert not entity.available
    assert entity.hvac_mode is None


# Additional controls


def keys(factories):
    return [key for key, _ in factories]


async def test_extra_controls_and_their_defaults(hass):
    coordinator, _, _ = await make_coordinator(hass)
    assert keys(_switches(coordinator)) == [
        "airConditioning:fullPowerMode",
        "airConditioning:sleepMode",
        "airConditioning:setbackMode",
        "airConditioning:airPurificationMode",
        "airConditioning:switchPrograms:enabled",
    ]
    assert keys(_selects(coordinator)) == [
        "airConditioning:quickAirFlows",
        "airConditioning:switchPrograms:activeProgram",
    ]
    assert keys(_timers(coordinator)) == [
        "airConditioning:timers:on",
        "airConditioning:timers:off",
    ]
    assert keys(_buttons(coordinator)) == [
        "airConditioning:switchPrograms:temporarySetting:enabled"
    ]
    assert keys(_binary_sensors(coordinator)) == ["pv:enable"]
    entities = [
        factory()
        for build in (_switches, _selects, _timers, _buttons, _binary_sensors)
        for _, factory in build(coordinator)
    ]
    for entity in entities:
        assert entity.entity_registry_enabled_default is False
        assert entity.unique_id.startswith(f"{GATEWAY}:")
    assert {entity.entity_category for entity in entities} == {
        EntityCategory.CONFIG,
        EntityCategory.DIAGNOSTIC,
    }
    # Eco is the climate preset, never a switch of its own.
    assert "airConditioning:ecoMode" not in keys(_switches(coordinator))


@pytest.mark.parametrize(
    "detection,key",
    [
        ("midea", "aircon_setback_8"),
        ("midea_gateway_connector", "aircon_setback_8"),
        ("sharp", "aircon_setback_10"),
    ],
)
async def test_frost_protection_names_its_temperature(hass, detection, key):
    coordinator, _, _ = await make_coordinator(hass, FakeUnit(detection))
    switch = dict(_switches(coordinator))["airConditioning:setbackMode"]()
    assert switch.translation_key == key


async def test_unreferenced_or_not_writable_functions_get_no_entity(hass):
    unit = FakeUnit()
    unit.set(SETBACK_MODE, writeable=0)
    unit.resources["/airConditioning/advancedFunctions"]["references"] = [
        {"id": "/airConditioning/ecoMode"},
        {"id": SETBACK_MODE},
    ]
    coordinator, _, _ = await make_coordinator(hass, unit)
    assert keys(_switches(coordinator)) == ["airConditioning:switchPrograms:enabled"]


async def test_switch_writes_on_and_off(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    switch = dict(_switches(coordinator))["airConditioning:sleepMode"]()
    switch.hass = hass
    assert isinstance(switch, AirconSwitch)
    assert switch.is_on is False
    await switch.async_turn_on()
    assert unit.puts == [("/airConditioning/sleepMode", "on")]
    assert switch.is_on is True


async def test_spot_air_direction_select(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    select = dict(_selects(coordinator))["airConditioning:quickAirFlows"]()
    select.hass = hass
    assert isinstance(select, AirconSelect)
    assert select.options == [
        "off",
        "top_left",
        "top_center",
        "top_right",
        "bottom_center",
    ]
    assert select.current_option == "off"
    await select.async_select_option("top_left")
    assert unit.puts == [(QUICK_AIR_FLOWS, "topLeft")]
    with pytest.raises(ServiceValidationError):
        await select.async_select_option("bottom_left")
    assert len(unit.puts) == 1


async def test_active_schedule_select(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    select = dict(_selects(coordinator))[
        "airConditioning:switchPrograms:activeProgram"
    ]()
    select.hass = hass
    assert select.options == ["program1", "program2"]
    assert select.extra_state_attributes is None
    await coordinator.async_refresh()
    assert [
        point["time"] for point in select.extra_state_attributes["switch_points"]
    ] == [
        "07:00",
        "22:00",
    ]
    assert "switch_points" in select._unrecorded_attributes
    await select.async_select_option("program2")
    assert unit.puts == [(ACTIVE_PROGRAM, "program2")]


async def test_timer_number(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    timer = dict(_timers(coordinator))["airConditioning:timers:off"]()
    timer.hass = hass
    assert isinstance(timer, AirconTimer)
    assert (timer.native_min_value, timer.native_max_value, timer.native_step) == (
        0.0,
        1440.0,
        30.0,
    )
    assert timer.native_value == 0.0
    await timer.async_set_native_value(90)
    assert unit.puts == [(TIMER_OFF, 90.0)]
    with pytest.raises(ServiceValidationError):
        await timer.async_set_native_value(100)
    assert len(unit.puts) == 1


@pytest.mark.parametrize(
    "fields",
    [
        {"minValue": 0.0},
        {"minValue": 30.0, "maxValue": 50.0},
        {"unitOfMeasure": "C"},
        {"writeable": 0},
    ],
)
async def test_invalid_timer_leaf_gives_no_timer(hass, fields):
    unit = FakeUnit()
    unit.set(TIMER_ON, **fields)
    coordinator, _, _ = await make_coordinator(hass, unit)
    assert "airConditioning:timers:on" not in keys(_timers(coordinator))


async def test_end_temporary_button_sends_off(hass):
    unit = FakeUnit()
    unit.set(TEMPORARY_SETTING, value="on")
    coordinator, _, _ = await make_coordinator(hass, unit)
    button = dict(_buttons(coordinator))[
        "airConditioning:switchPrograms:temporarySetting:enabled"
    ]()
    button.hass = hass
    assert isinstance(button, AirconEndTemporary)
    await button.async_press()
    assert unit.puts == [(TEMPORARY_SETTING, "off")]


async def test_solar_operation_is_read_only(hass):
    unit = FakeUnit()
    unit.set(PV_ENABLE, value="on")
    coordinator, _, _ = await make_coordinator(hass, unit)
    sensor = dict(_binary_sensors(coordinator))["pv:enable"]()
    assert isinstance(sensor, AirconSolarOperation)
    assert sensor.is_on is True
    assert not hasattr(sensor, "async_turn_on")


async def test_no_air_conditioner_builds_no_controls(hass):
    coordinator, _, _ = await make_coordinator(hass, FakeUnit("no_bus"))
    for build in (_switches, _selects, _timers, _buttons, _binary_sensors):
        assert keys(build(coordinator)) == []


async def test_standard_function_list_hides_an_unlisted_function(hass):
    unit = FakeUnit()
    listing = unit.resources["/airConditioning/standardFunctions"]["references"]
    listing[:] = [item for item in listing if item["id"] != FAN_SPEED]
    coordinator, _, _ = await make_coordinator(hass, unit)
    entity = climate_of(hass, coordinator)
    assert entity.fan_modes == []
    assert entity.hvac_mode is HVACMode.COOL
    # Schedules and timers are no listed functions.
    assert keys(_timers(coordinator)) == [
        "airConditioning:timers:on",
        "airConditioning:timers:off",
    ]


async def test_what_091_left_of_an_air_conditioner_is_removed(hass):
    """0.9.1 set the unit up like a heating system; its fault and
    notification entities and disabled readings are no longer provided and
    go, unless customised. The backend's own entries and those of other
    gateways or integrations stay."""
    from homeassistant.helpers import entity_registry as er

    from custom_components.bosch_buderus_heating.aircon_entity import (
        remove_heating_leftovers,
    )
    from custom_components.bosch_buderus_heating.const import DOMAIN
    from custom_components.bosch_buderus_heating.platform_scope import (
        PlatformScope,
    )

    coordinator, _, _ = await make_coordinator(hass)
    entry = coordinator.config_entry
    registry = er.async_get(hass)

    def add(domain, unique_id, platform=DOMAIN, **kwargs):
        return registry.async_get_or_create(
            domain, platform, unique_id, config_entry=entry, **kwargs
        ).entity_id

    disabled = er.RegistryEntryDisabler.INTEGRATION
    leftovers = [
        add("binary_sensor", f"{GATEWAY}:system_fault"),
        add("event", f"{GATEWAY}:system_notifications"),
        add("sensor", f"{GATEWAY}:active_faults"),
        add("sensor", f"{GATEWAY}:active_notifications"),
        add("sensor", f"{GATEWAY}:system:bus", disabled_by=disabled),
        add("sensor", f"{GATEWAY}:system:appliance:model", disabled_by=disabled),
        add("sensor", f"{GATEWAY}:pv:enable", disabled_by=disabled),
    ]
    kept = [
        add("climate", f"{GATEWAY}:airConditioning:climate"),
        add("switch", f"{GATEWAY}:airConditioning:sleepMode", disabled_by=disabled),
        add("binary_sensor", f"{GATEWAY}:pv:enable", disabled_by=disabled),
        add("sensor", "other-gateway:active_faults"),
        add("sensor", f"{GATEWAY}:active_faults", platform="other_integration"),
        add("sensor", f"{entry.entry_id}:request_metrics:total"),
    ]
    customised = add("binary_sensor", f"{GATEWAY}:cloud_control")
    registry.async_update_entity(customised, name="Mine")
    scope = PlatformScope("", lambda _: None, aircons=(coordinator,))
    remove_heating_leftovers(scope)
    for entity_id in leftovers:
        assert registry.async_get(entity_id) is None, entity_id
    for entity_id in (*kept, customised):
        assert registry.async_get(entity_id) is not None, entity_id
    # Repeating it changes nothing.
    before = set(registry.entities)
    remove_heating_leftovers(scope)
    assert set(registry.entities) == before
