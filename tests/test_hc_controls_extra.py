"""Boost, cooling mode, season thresholds and further circuit switches (0.9.2).

Every function has its own gate: without the path, without a writable flag,
without the advertised list or the limits it needs, nothing is created and
nothing is written. Every shape is constructed (nothing here confirms
behaviour on a real installation).
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components.number import NumberDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating import hc_controls_extra as extra
from custom_components.bosch_buderus_heating.circuit_controls import (
    GENERIC_ONLY_POLICIES,
    GUARDED_POLICIES,
)
from custom_components.bosch_buderus_heating.controls_flow import controls
from custom_components.bosch_buderus_heating.number import build_number_descriptions
from custom_components.bosch_buderus_heating.pointt import WriteValidationError
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.resource_catalog import resource_name
from custom_components.bosch_buderus_heating.select import build_select_descriptions
from custom_components.bosch_buderus_heating.switch import (
    BoschBuderusSwitch,
    build_switch_descriptions,
)
from custom_components.bosch_buderus_heating.writes import (
    GENERIC_CONTEXT,
    RRC_CONTEXT,
    UNCHECKED_CONTEXT,
    assess_control,
)
from tests.circuit_control_helpers import (
    Cloud,
    coordinator_for,
    number_entity,
    select_entity,
)

ROOT = "/heatingCircuits/hc1"
BOOST_MODE = ROOT + "/boostMode"
BOOST_DURATION = ROOT + "/boostDuration"
BOOST_TEMPERATURE = ROOT + "/boostTemperature"
COOLING_MODE = ROOT + "/cooling/operationMode"
SETPOINT_OPTIMIZATION = ROOT + "/setpointOptimization"
WINDOW = ROOT + "/openWindowDetection/enabled"
HEATING_THRESHOLD = "/system/seasonOptimizer/heatingThreshold"
COOLING_THRESHOLD = "/system/seasonOptimizer/coolingThreshold"
CIRCUIT_COOLING_THRESHOLD = ROOT + "/suWiCoolingThreshold"
CONTROL_TYPE = ROOT + "/controlType"


def _string(
    path: str, value: str, allowed: list[str] | None, **fields: Any
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": path,
        "type": "stringValue",
        "value": value,
        "writeable": 1,
        **fields,
    }
    if allowed is not None:
        item["allowedValues"] = allowed
    return item


def _float(path: str, value: float, **fields: Any) -> dict[str, Any]:
    return {
        "id": path,
        "type": "floatValue",
        "value": value,
        "writeable": 1,
        "unitOfMeasure": "C",
        "minValue": 5.0,
        "maxValue": 30.0,
        **fields,
    }


def _plant(*items: dict[str, Any]) -> dict[str, Any]:
    base = [
        {
            "id": CONTROL_TYPE,
            "type": "stringValue",
            "value": "outdoorTemperature",
            "writeable": 1,
            "allowedValues": ["outdoorTemperature", "constants"],
        }
    ]
    return {item["id"]: parse_resource(item) for item in (*base, *items)}


def _switch(path: str) -> dict[str, Any]:
    return _string(path, "off", ["on", "off"])


BOOST_DURATION_ITEM = {
    "id": BOOST_DURATION,
    "type": "floatValue",
    "value": 2.0,
    "writeable": 1,
    "unitOfMeasure": "hour",
    "minValue": 1.0,
    "maxValue": 8.0,
    "stepSize": 1.0,
}


def _selects(resources, context=None) -> dict[str, Any]:
    return {
        item.resource_path: item
        for item in build_select_descriptions(resources, context)
    }


def _switches(resources, context=None) -> dict[str, Any]:
    return {
        item.resource_path: item
        for item in build_switch_descriptions(resources, context)
    }


def _numbers(resources, context=None) -> dict[str, Any]:
    return {
        item.resource_path: item
        for item in build_number_descriptions(resources, context)
    }


def _switch_entity(hass, coordinator, path) -> BoschBuderusSwitch:
    entity = BoschBuderusSwitch(coordinator, _switches(coordinator.resources)[path])
    entity.hass = hass
    return entity


# L01 boost switch --------------------------------------------------------------


def test_boost_switch_only_with_a_confirmed_on_off_list():
    resources = _plant(_switch(BOOST_MODE))
    description = _switches(resources)[BOOST_MODE]
    assert description.write_policy is extra.BOOST_MODE_POLICY
    assert description.translation_key == "heating_circuit_boost"
    assert description.entity_category is EntityCategory.CONFIG
    # A new write control stays disabled until the user enables it.
    assert description.entity_registry_enabled_default is False
    assert BOOST_MODE not in _selects(resources)


@pytest.mark.parametrize(
    ("item", "reason"),
    [
        (_string(BOOST_MODE, "auto", None), "missing_options"),
        (_string(BOOST_MODE, "off", []), "missing_options"),
        (_string(BOOST_MODE, "off", ["on"]), "unsupported_options"),
        (_string(BOOST_MODE, "off", ["on", "off", "auto"]), "unsupported_options"),
        (_string(BOOST_MODE, "off", ["Yes", "No"]), "unsupported_options"),
        (_string(BOOST_MODE, "off", ["on", "off"], writeable=0), "not_writable"),
        (
            {
                "id": BOOST_MODE,
                "type": "floatValue",
                "value": 1.0,
                "writeable": 1,
                "allowedValues": ["on", "off"],
            },
            "unsupported_resource_type",
        ),
    ],
)
def test_boost_switch_gate(item, reason):
    resources = _plant(item)
    assert assess_control(resources[BOOST_MODE]).rejection_reason == reason
    assert BOOST_MODE not in _switches(resources)
    assert BOOST_MODE not in _selects(resources)


@pytest.mark.parametrize("value", ["on", "off"])
async def test_a_switch_without_allowed_values_falls_back_to_on_and_off(hass, value):
    """S17: without any allowedValues key, a current on or off stands for
    both; the write sends only on or off."""
    resources = _plant(_string(BOOST_MODE, value, None))
    assert assess_control(resources[BOOST_MODE]).rejection_reason is None
    assert BOOST_MODE in _switches(resources)
    coordinator = coordinator_for(hass, resources)
    cloud = Cloud(coordinator)
    entity = _switch_entity(hass, coordinator, BOOST_MODE)
    await (entity.async_turn_off() if value == "on" else entity.async_turn_on())
    assert cloud.puts == [(BOOST_MODE, "off" if value == "on" else "on")]


def test_foreign_switch_values_are_never_mapped_but_listed():
    """S17: two other advertised values give no switch; the diagnostics list
    them as tokens, never the current value."""
    resources = _plant(
        _string(SETPOINT_OPTIMIZATION, "Yes", ["Yes", "No", "on", "bad value"])
    )
    assessment = assess_control(resources[SETPOINT_OPTIMIZATION])
    assert assessment.rejection_reason == "unsupported_options"
    assert SETPOINT_OPTIMIZATION not in _switches(resources)
    assert extra.switch_diagnostics(
        resources[SETPOINT_OPTIMIZATION], assessment.policy
    ) == {"other_advertised_options": ["No", "Yes"], "allowed_values_missing": False}
    assert extra.switch_diagnostics(resources[SETPOINT_OPTIMIZATION], None) is None


def test_no_boost_switch_without_the_path():
    resources = _plant(_switch(SETPOINT_OPTIMIZATION))
    assert BOOST_MODE not in _switches(resources)
    assert BOOST_DURATION not in _numbers(resources)


async def test_boost_switch_writes_once_and_confirms(hass):
    coordinator = coordinator_for(hass, _plant(_switch(BOOST_MODE)))
    cloud = Cloud(coordinator)
    entity = _switch_entity(hass, coordinator, BOOST_MODE)
    await entity.async_turn_on()
    assert cloud.puts == [(BOOST_MODE, "on")]
    # The setting is read live in one request before the one PUT.
    assert cloud.bulk_calls[0] == (BOOST_MODE,)
    assert entity.is_on is True


async def test_boost_switch_refuses_a_list_that_changed_live(hass):
    coordinator = coordinator_for(hass, _plant(_switch(BOOST_MODE)))
    cloud = Cloud(coordinator)
    entity = _switch_entity(hass, coordinator, BOOST_MODE)
    cloud.change(
        BOOST_MODE, metadata=_changed(coordinator, BOOST_MODE, allowed=("on",))
    )
    with pytest.raises((ServiceValidationError, HomeAssistantError)):
        await entity.async_turn_on()
    assert cloud.puts == []


def _changed(coordinator, path, **fields):
    from dataclasses import replace

    metadata = coordinator.data[path].resource.metadata
    renamed = {
        "allowed": "allowed_values",
        "writable": "writable",
        "minimum": "minimum",
        "maximum": "maximum",
        "used": "used",
    }
    return replace(metadata, **{renamed[key]: value for key, value in fields.items()})


async def test_boost_switch_without_confirmation_stays_one_put(hass):
    coordinator = coordinator_for(hass, _plant(_switch(BOOST_MODE)))
    cloud = Cloud(coordinator)
    cloud.stuck[BOOST_MODE] = "off"
    entity = _switch_entity(hass, coordinator, BOOST_MODE)
    with pytest.raises(HomeAssistantError):
        await entity.async_turn_on()
    assert cloud.puts == [(BOOST_MODE, "on")]


# L01 boost numbers --------------------------------------------------------------


def test_boost_duration_number_needs_limits_and_a_duration_unit():
    resources = _plant(BOOST_DURATION_ITEM)
    description = _numbers(resources)[BOOST_DURATION]
    assert description.write_policy is extra.BOOST_DURATION_POLICY
    assert description.native_min_value == 1.0
    assert description.native_max_value == 8.0
    assert description.native_unit_of_measurement == "h"
    assert description.device_class is NumberDeviceClass.DURATION
    assert description.entity_registry_enabled_default is False
    assert description.entity_category is EntityCategory.CONFIG
    assert description.icon is None
    for broken in (
        {**BOOST_DURATION_ITEM, "minValue": None},
        {**BOOST_DURATION_ITEM, "maxValue": None},
        {**BOOST_DURATION_ITEM, "unitOfMeasure": "C"},
        {**BOOST_DURATION_ITEM, "unitOfMeasure": None},
        {**BOOST_DURATION_ITEM, "writeable": 0},
    ):
        broken = {key: value for key, value in broken.items() if value is not None}
        assert BOOST_DURATION not in _numbers(_plant(broken))


def test_boost_temperature_number_only_with_the_path_and_limits():
    assert BOOST_TEMPERATURE not in _numbers(_plant(BOOST_DURATION_ITEM))
    resources = _plant(_float(BOOST_TEMPERATURE, 20.0, stepSize=0.5))
    description = _numbers(resources)[BOOST_TEMPERATURE]
    assert description.write_policy is extra.BOOST_TEMPERATURE_POLICY
    assert description.device_class is NumberDeviceClass.TEMPERATURE
    assert description.entity_registry_enabled_default is False
    without_limits = {
        key: value
        for key, value in _float(BOOST_TEMPERATURE, 20.0).items()
        if key not in ("minValue", "maxValue")
    }
    assert BOOST_TEMPERATURE not in _numbers(_plant(without_limits))


async def test_boost_duration_writes_on_its_grid(hass):
    coordinator = coordinator_for(hass, _plant(BOOST_DURATION_ITEM))
    cloud = Cloud(coordinator)
    entity = number_entity(hass, coordinator, BOOST_DURATION)
    await entity.async_set_native_value(4.0)
    assert cloud.puts == [(BOOST_DURATION, 4.0)]
    assert entity.native_value == 4.0
    with pytest.raises((ServiceValidationError, WriteValidationError)):
        await entity.async_set_native_value(2.5)
    with pytest.raises((ServiceValidationError, WriteValidationError)):
        await entity.async_set_native_value(9.0)
    assert len(cloud.puts) == 1


async def test_boost_temperature_writes_within_its_limits(hass):
    coordinator = coordinator_for(
        hass, _plant(_float(BOOST_TEMPERATURE, 20.0, stepSize=0.5))
    )
    cloud = Cloud(coordinator)
    entity = number_entity(hass, coordinator, BOOST_TEMPERATURE)
    await entity.async_set_native_value(22.5)
    assert cloud.puts == [(BOOST_TEMPERATURE, 22.5)]
    with pytest.raises((ServiceValidationError, WriteValidationError)):
        await entity.async_set_native_value(31.0)
    assert len(cloud.puts) == 1


def test_a_room_controller_keeps_its_own_boost():
    """No generic boost control on a room controller (``rrc_boost``)."""
    resources = _plant(
        _switch(BOOST_MODE),
        BOOST_DURATION_ITEM,
        _float(BOOST_TEMPERATURE, 20.0, stepSize=0.5),
    )
    for path in (BOOST_MODE, BOOST_DURATION, BOOST_TEMPERATURE):
        assert (
            assess_control(resources[path], RRC_CONTEXT).policy is None
            or assess_control(resources[path], RRC_CONTEXT).rejection_reason
            == "room_controller_contract_only"
        )
    assert BOOST_MODE not in _switches(resources, RRC_CONTEXT)
    assert BOOST_DURATION not in _numbers(resources, RRC_CONTEXT)
    assert BOOST_TEMPERATURE not in _numbers(resources, RRC_CONTEXT)


def test_nothing_before_the_room_controller_check_is_settled():
    resources = _plant(_switch(BOOST_MODE), BOOST_DURATION_ITEM)
    assert BOOST_MODE not in _switches(resources, UNCHECKED_CONTEXT)
    assert BOOST_DURATION not in _numbers(resources, UNCHECKED_CONTEXT)
    assert (
        assess_control(resources[BOOST_MODE], UNCHECKED_CONTEXT).rejection_reason
        == "room_controller_check_pending"
    )


# L03a cooling mode select ----------------------------------------------------


def _cooling_mode(allowed, value="auto", **fields):
    return _string(COOLING_MODE, value, allowed, **fields)


def test_cooling_mode_select_with_the_advertised_known_modes():
    resources = _plant(_cooling_mode(["off", "manual", "auto"]))
    description = _selects(resources)[COOLING_MODE]
    assert description.write_policy is extra.COOLING_MODE_POLICY
    assert description.options == ["off", "manual", "auto"]
    assert description.translation_key == "cooling_operation_mode"
    assert description.entity_registry_enabled_default is False
    assert description.entity_category is EntityCategory.CONFIG
    assert description.icon is None
    assert COOLING_MODE not in _switches(resources)


@pytest.mark.parametrize(
    "item",
    [
        _cooling_mode(None),
        _cooling_mode([]),
        _cooling_mode(["auto"]),
        _cooling_mode(["off", "manual", "auto"], writeable=0),
        _cooling_mode(["comfort", "eco"], value="eco"),
    ],
)
def test_cooling_mode_select_gate(item):
    assert COOLING_MODE not in _selects(_plant(item))


async def test_a_cooling_mode_not_known_is_shown_unknown_and_only_known_ones_written(
    hass,
):
    """S18: a current mode that is not known keeps the selection; it shows no
    option and only the known advertised modes can be written."""
    resources = _plant(_cooling_mode(["off", "manual", "auto"], value="boost"))
    assert _selects(resources)[COOLING_MODE].options == ["off", "manual", "auto"]
    coordinator = coordinator_for(hass, resources)
    cloud = Cloud(coordinator)
    entity = select_entity(hass, coordinator, COOLING_MODE)
    assert entity.current_option is None
    with pytest.raises(ServiceValidationError):
        await entity.async_select_option("boost")
    assert cloud.puts == []
    await entity.async_select_option("auto")
    assert cloud.puts == [(COOLING_MODE, "auto")]


def test_unknown_mode_codes_are_left_out():
    resources = _plant(_cooling_mode(["off", "manual", "auto", "party"]))
    assert _selects(resources)[COOLING_MODE].options == ["off", "manual", "auto"]


def test_no_cooling_select_without_the_path():
    assert COOLING_MODE not in _selects(_plant(_switch(BOOST_MODE)))


async def test_cooling_mode_select_writes_once(hass):
    coordinator = coordinator_for(
        hass, _plant(_cooling_mode(["off", "manual", "auto"], value="auto"))
    )
    cloud = Cloud(coordinator)
    entity = select_entity(hass, coordinator, COOLING_MODE)
    assert entity.current_option == "auto"
    await entity.async_select_option("manual")
    assert cloud.puts == [(COOLING_MODE, "manual")]
    assert entity.current_option == "manual"
    with pytest.raises(ServiceValidationError):
        await entity.async_select_option("party")
    assert len(cloud.puts) == 1


def test_heat_cool_mode_stays_a_reading():
    resources = _plant(_string(ROOT + "/heatCoolMode", "heat", ["heat", "heatCool"]))
    assert ROOT + "/heatCoolMode" not in _selects(resources)
    assert ROOT + "/heatCoolMode" not in _switches(resources)


def test_cooling_mode_has_its_own_names():
    """The cooling paths do not read as the circuit's own operating mode,
    control type or setpoints (identifiers stay)."""
    mode = resource_name(COOLING_MODE, language="en")
    assert mode == "Cooling: operation mode"
    assert resource_name(COOLING_MODE, language="de") == "Kühlen: Betriebsart"
    assert resource_name(ROOT + "/operationMode", language="en") != mode
    assert resource_name(ROOT + "/cooling/controlType", language="en") == (
        "Cooling: control type"
    )
    assert resource_name(ROOT + "/controlType", language="en") != (
        resource_name(ROOT + "/cooling/controlType", language="en")
    )
    assert resource_name(ROOT + "/cooling/manualRoomSetpoint", language="en") == (
        "Cooling: manual setpoint"
    )
    assert resource_name(ROOT + "/cooling/temporaryRoomSetpoint", language="de") == (
        "Kühlen: Vorübergehender Raum-Sollwert"
    )
    # Every one of the 17 further languages has a text of its own.
    for language in [
        "bg",
        "cs",
        "da",
        "el",
        "es",
        "fi",
        "fr",
        "ga",
        "hr",
        "it",
        "nb",
        "nl",
        "pl",
        "pt",
        "sk",
        "sl",
        "sv",
    ]:
        for path in (
            COOLING_MODE,
            ROOT + "/cooling/controlType",
            ROOT + "/cooling/manualRoomSetpoint",
            ROOT + "/cooling/temporaryRoomSetpoint",
        ):
            assert resource_name(path, language=language) != resource_name(
                path, language="en"
            ), (language, path)


# L03a / F2 thresholds --------------------------------------------------------


def _threshold(path, value=15.0, **fields):
    return _float(path, value, minValue=5.0, maxValue=30.0, stepSize=1.0, **fields)


def test_heating_threshold_number_only_with_the_path_and_limits():
    assert HEATING_THRESHOLD not in _numbers(_plant(_switch(BOOST_MODE)))
    description = _numbers(_plant(_threshold(HEATING_THRESHOLD)))[HEATING_THRESHOLD]
    assert description.write_policy is extra.HEATING_THRESHOLD_POLICY
    assert description.translation_key == "season_heating_threshold"
    assert description.device_class is NumberDeviceClass.TEMPERATURE
    assert description.entity_registry_enabled_default is False
    assert description.entity_category is EntityCategory.CONFIG
    for key in ("minValue", "maxValue"):
        item = {k: v for k, v in _threshold(HEATING_THRESHOLD).items() if k != key}
        assert HEATING_THRESHOLD not in _numbers(_plant(item))
    read_only = _threshold(HEATING_THRESHOLD, writeable=0)
    assert HEATING_THRESHOLD not in _numbers(_plant(read_only))


def test_cooling_thresholds_only_when_found():
    """F2: no path, no control; with the path a disabled number."""
    plain = _plant(_threshold(HEATING_THRESHOLD))
    assert COOLING_THRESHOLD not in _numbers(plain)
    assert CIRCUIT_COOLING_THRESHOLD not in _numbers(plain)
    found = _plant(_threshold(COOLING_THRESHOLD), _threshold(CIRCUIT_COOLING_THRESHOLD))
    numbers = _numbers(found)
    assert numbers[COOLING_THRESHOLD].write_policy is extra.COOLING_THRESHOLD_POLICY
    assert (
        numbers[CIRCUIT_COOLING_THRESHOLD].write_policy
        is extra.CIRCUIT_COOLING_THRESHOLD_POLICY
    )
    assert numbers[COOLING_THRESHOLD].translation_key == "season_cooling_threshold"
    assert numbers[CIRCUIT_COOLING_THRESHOLD].translation_key == (
        "circuit_cooling_threshold"
    )
    assert not numbers[COOLING_THRESHOLD].entity_registry_enabled_default
    assert not numbers[CIRCUIT_COOLING_THRESHOLD].entity_registry_enabled_default
    # Without limits nothing is offered.
    open_ended = {
        k: v for k, v in _threshold(COOLING_THRESHOLD).items() if k != "maxValue"
    }
    assert COOLING_THRESHOLD not in _numbers(_plant(open_ended))


async def test_threshold_writes_on_the_grid(hass):
    coordinator = coordinator_for(hass, _plant(_threshold(HEATING_THRESHOLD)))
    cloud = Cloud(coordinator)
    entity = number_entity(hass, coordinator, HEATING_THRESHOLD)
    await entity.async_set_native_value(17.0)
    assert cloud.puts == [(HEATING_THRESHOLD, 17.0)]
    with pytest.raises((ServiceValidationError, WriteValidationError)):
        await entity.async_set_native_value(17.5)
    with pytest.raises((ServiceValidationError, WriteValidationError)):
        await entity.async_set_native_value(40.0)
    assert len(cloud.puts) == 1


def test_the_system_thresholds_are_no_circuit_settings():
    """They exist on every kind of plant; only the circuit paths wait for
    the room controller check."""
    assert extra.HEATING_THRESHOLD_POLICY not in GENERIC_ONLY_POLICIES
    assert extra.COOLING_THRESHOLD_POLICY not in GENERIC_ONLY_POLICIES
    assert extra.CIRCUIT_COOLING_THRESHOLD_POLICY in GENERIC_ONLY_POLICIES
    resources = _plant(_threshold(HEATING_THRESHOLD))
    assert HEATING_THRESHOLD in _numbers(resources, GENERIC_CONTEXT)


# L11a / F11 switches ------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "policy", "key"),
    [
        (
            SETPOINT_OPTIMIZATION,
            extra.SETPOINT_OPTIMIZATION_POLICY,
            "heating_circuit_setpoint_optimization",
        ),
        (WINDOW, extra.WINDOW_DETECTION_POLICY, "heating_circuit_window_detection"),
    ],
)
async def test_circuit_switch_gate_and_write(hass, path, policy, key):
    resources = _plant(_switch(path))
    description = _switches(resources)[path]
    assert description.write_policy is policy
    assert description.translation_key == key
    assert description.entity_registry_enabled_default is False
    assert description.entity_category is EntityCategory.CONFIG
    assert path in _switches(_plant(_string(path, "off", None)))
    for broken in (
        _string(path, "auto", None),
        _string(path, "off", ["on", "off", "auto"]),
        _string(path, "off", ["on", "off"], writeable=0),
    ):
        assert path not in _switches(_plant(broken))
    coordinator = coordinator_for(hass, resources)
    cloud = Cloud(coordinator)
    entity = _switch_entity(hass, coordinator, path)
    await entity.async_turn_on()
    assert cloud.puts == [(path, "on")]
    await entity.async_turn_off()
    assert cloud.puts == [(path, "on"), (path, "off")]
    # A room controller keeps its own contracts for these paths: never the
    # generic policy of this module.
    on_controller = _switches(resources, RRC_CONTEXT)
    assert path not in on_controller or (on_controller[path].write_policy is not policy)


def test_window_detection_only_when_the_path_is_found():
    """F11: EMS plants answer 404 for the path; then nothing exists."""
    resources = _plant(_switch(SETPOINT_OPTIMIZATION), _switch(BOOST_MODE))
    assert WINDOW not in _switches(resources)
    assert WINDOW not in _selects(resources)


def test_window_detection_and_optimization_have_names():
    assert resource_name(WINDOW, language="en") == "Open window detection"
    assert resource_name(WINDOW, language="de") == "Fenster-offen-Erkennung"
    assert resource_name(SETPOINT_OPTIMIZATION, language="de") == (
        "Sollwertoptimierung"
    )


async def test_the_room_controller_wins_on_a_second_gateway_kind(hass):
    """The same paths on a plant that runs a room controller keep the
    contract: no generic switch, no generic write."""
    resources = _plant(_switch(SETPOINT_OPTIMIZATION), _switch(WINDOW))
    for path in (SETPOINT_OPTIMIZATION, WINDOW):
        assessment = assess_control(resources[path], RRC_CONTEXT)
        assert assessment.policy is None or assessment.policy not in (
            extra.SETPOINT_OPTIMIZATION_POLICY,
            extra.WINDOW_DETECTION_POLICY,
        )


# Registration ----------------------------------------------------------------


def test_policies_are_registered_once():
    from custom_components.bosch_buderus_heating import writes

    for policy in extra.ENUM_POLICIES:
        assert policy in writes._ENUM_POLICIES
    for number in extra.NUMBER_POLICIES:
        assert number in writes.NUMBER_WRITE_POLICIES
    # The generic circuit settings wait for the check as the others do and
    # read the control type again before a write.
    assert extra.GENERIC_ONLY_POLICIES <= GENERIC_ONLY_POLICIES
    assert extra.GENERIC_ONLY_POLICIES <= GUARDED_POLICIES


async def test_dialog_lists_the_circuit_rows_only(hass):
    """The controls dialog offers the circuit settings (own label for the
    cooling mode); the system thresholds stay on their entities."""
    coordinator = coordinator_for(
        hass,
        _plant(
            _switch(BOOST_MODE),
            BOOST_DURATION_ITEM,
            _cooling_mode(["off", "manual", "auto"]),
            _threshold(HEATING_THRESHOLD),
        ),
    )
    rows = controls(coordinator)
    assert {BOOST_MODE, BOOST_DURATION, COOLING_MODE} <= set(rows)
    assert HEATING_THRESHOLD not in rows
    from custom_components.bosch_buderus_heating.control_presentation import (
        control_label,
    )

    assert control_label(COOLING_MODE, "x", "en") == "Cooling: operation mode"
    assert control_label(ROOT + "/operationMode", "x", "en") == "Operating mode"


def test_cooling_control_type_stays_a_named_reading(hass):
    """N4: nothing is written; the sensor exists with its own name, apart
    from the circuit's control type, and is not enabled by default."""
    from custom_components.bosch_buderus_heating.pointt import (
        Resource,
        ResourceMetadata,
    )
    from tests.test_sensor import _sensor

    path = ROOT + "/cooling/controlType"
    resource = Resource(
        path=path,
        value="rtcc_with_dewpoint",
        has_value=True,
        metadata=ResourceMetadata(resource_type="stringValue", writable=True),
    )
    sensor = _sensor(hass, resource)
    assert sensor.entity_description.entity_registry_enabled_default is False
    assert sensor.entity_description.translation_key == (
        "heating_circuit_cooling_control_type"
    )
    assert sensor.native_value == "rtcc_with_dewpoint"
    assert "Cooling: control type" in str(sensor.name)
    assert assess_control(resource).policy is None
    assert path not in _selects({path: resource})


async def test_diagnostics_count_paths_without_naming_them(hass):
    from custom_components.bosch_buderus_heating.circuit_control_writes import (
        circuit_control_diagnostics,
    )

    coordinator = coordinator_for(
        hass,
        _plant(
            _switch(BOOST_MODE),
            BOOST_DURATION_ITEM,
            _cooling_mode(["off", "manual", "auto"], writeable=0),
        ),
    )
    report = circuit_control_diagnostics(coordinator)["extra_settings"]
    assert report["boost_mode"] == {
        "present": 1,
        "writable": 1,
        "with_options": 1,
        "with_limits": 0,
    }
    assert report["boost_duration"]["with_limits"] == 1
    assert report["cooling_mode"]["writable"] == 0
    assert report["window_detection"]["present"] == 0
    assert "hc1" not in str(report)


# Fix round: write order, control-type guard, boost temperature ---------------


def test_the_boost_switch_is_written_after_its_temperature_and_duration():
    """W-1: the dialog writes the boost settings first, then switches it on."""
    from custom_components.bosch_buderus_heating.controls_flow import _write_order

    resources = _plant(
        _switch(BOOST_MODE),
        BOOST_DURATION_ITEM,
        _float(BOOST_TEMPERATURE, 20.0, stepSize=0.5),
    )
    draft = {BOOST_MODE: "on", BOOST_DURATION: 4.0, BOOST_TEMPERATURE: 25.0}
    ordered = sorted(
        draft, key=lambda path: _write_order(path, draft[path], resources[path])
    )
    assert ordered[-1] == BOOST_MODE
    assert set(ordered[:2]) == {BOOST_DURATION, BOOST_TEMPERATURE}


def _isrc_plant():
    plant = _plant(
        _switch(BOOST_MODE),
        BOOST_DURATION_ITEM,
        _float(BOOST_TEMPERATURE, 20.0, stepSize=0.5),
        _string(COOLING_MODE, "off", ["off", "manual", "auto"]),
        _switch(SETPOINT_OPTIMIZATION),
        _switch(WINDOW),
        _float(CIRCUIT_COOLING_THRESHOLD, 22.0, stepSize=1.0),
    )
    plant[CONTROL_TYPE] = parse_resource(
        {
            "id": CONTROL_TYPE,
            "type": "stringValue",
            "value": "ISRC",
            "writeable": 1,
            "allowedValues": ["outdoorTemperature", "ISRC"],
        }
    )
    return plant


def test_the_circuit_settings_are_guarded_like_the_other_circuit_settings():
    """W-5: the new circuit settings read the control type again."""
    for policy in extra.GENERIC_ONLY_POLICIES:
        assert policy in GUARDED_POLICIES
        assert policy.value_check is not None
    plant = _isrc_plant()
    for path in (
        BOOST_MODE,
        BOOST_DURATION,
        BOOST_TEMPERATURE,
        COOLING_MODE,
        SETPOINT_OPTIMIZATION,
        WINDOW,
        CIRCUIT_COOLING_THRESHOLD,
    ):
        policy = assess_control(plant[path], GENERIC_CONTEXT).policy
        assert policy is not None
        assert policy.value_check(plant[path], "on", plant) == "circuit_room_controller"
    # The season thresholds are not circuit paths: never refused.
    assert extra.HEATING_THRESHOLD_POLICY.value_check is None


async def test_a_boost_write_on_a_room_controller_circuit_sends_nothing(hass):
    plant = _plant(
        _float(BOOST_TEMPERATURE, 20.0, stepSize=0.5),
    )
    coordinator = coordinator_for(hass, plant)
    cloud = Cloud(coordinator)
    entity = number_entity(hass, coordinator, BOOST_TEMPERATURE)
    cloud.change(
        CONTROL_TYPE,
        value="ISRC",
    )
    with pytest.raises((HomeAssistantError, ServiceValidationError)):
        await entity.async_set_native_value(22.5)
    assert cloud.puts == []


def test_the_boost_temperature_is_not_offered_on_a_heat_pump():
    """W-7: as the app, the boost temperature only without a heat pump."""
    from custom_components.bosch_buderus_heating.pointt import Resource

    def reading(path: str, value: str) -> dict[str, Resource]:
        return {
            path: parse_resource({"id": path, "type": "stringValue", "value": value})
        }

    assert extra.offered(BOOST_TEMPERATURE, {})
    assert extra.offered(BOOST_TEMPERATURE, reading("/heatSources/hs1/type", "gas"))
    assert not extra.offered(
        BOOST_TEMPERATURE, reading("/heatSources/hs1/type", "heatPump")
    )
    assert not extra.offered(
        BOOST_TEMPERATURE, reading("/heatSources/hs1/heatPumpType", "airToWater")
    )
    assert extra.offered(
        BOOST_TEMPERATURE, reading("/heatSources/hs1/heatPumpType", "unknown")
    )
    # Other paths are not decided here.
    assert extra.offered(BOOST_DURATION, reading("/heatSources/hs1/type", "heatPump"))


async def test_the_boost_temperature_number_follows_the_heat_pump(hass):
    from custom_components.bosch_buderus_heating.circuit_control_writes import (
        offered_on,
    )

    plant = _plant(
        _float(BOOST_TEMPERATURE, 20.0, stepSize=0.5),
        _string("/heatSources/hs1/type", "heatPump", None, writeable=0),
    )
    coordinator = coordinator_for(hass, plant)
    assert not offered_on(coordinator, BOOST_TEMPERATURE)
    assert offered_on(coordinator, BOOST_DURATION)
    coordinator = coordinator_for(hass, _plant(_float(BOOST_TEMPERATURE, 20.0)))
    assert offered_on(coordinator, BOOST_TEMPERATURE)
