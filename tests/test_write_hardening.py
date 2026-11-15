"""Hardening of the heating-circuit writes and the generic flag rule.

- The circuit's ``controlType`` is read again on the gateway before every
  write of a heating climate and its clear button; a change refuses the
  write unsent, ``ISRC`` and ``constants`` allow no temporary setpoint.
- The lower limit of the temporary target follows the manual setpoint's
  reading within its validity limit; a write still needs a fresh one.
- The clear value -1 is written also when the advertised limits leave it
  out (as the manufacturer's app does); any negative reading confirms it.
- Every generic write policy follows the ``used``/``available`` flags.

The shapes follow an iCom gateway (limits 5 to 30, value -1) with
constructed values. Nothing here confirms behaviour on a real device.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating import button as button_platform
from custom_components.bosch_buderus_heating.control import async_set_control
from custom_components.bosch_buderus_heating.coordinator import Freshness
from custom_components.bosch_buderus_heating.heating_climate import (
    HeatingCircuitTemporaryClearButton,
    clear_button_descriptions,
    clear_outside_limits,
    takes_clear_value,
)
from custom_components.bosch_buderus_heating.number import build_number_descriptions
from custom_components.bosch_buderus_heating.pointt import (
    RequestTimeout,
    ServiceUnavailable,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.writes import (
    DHW_OPERATION_MODE_POLICY,
    GENERIC_CONTEXT,
    HEATING_CIRCUIT_TEMPORARY_SETPOINT_POLICY,
    NUMBER_WRITE_POLICIES,
    RRC_OPERATION_MODE_POLICY,
    SILENT_MODE_POLICY,
    ZONE_OPERATION_MODE_POLICY,
    ZONE_TEMPORARY_SETPOINT_POLICY,
    ControlUnused,
    assess_control,
    confirms_value,
    number_policy_for_resource,
    validate_control_value,
)
from tests.test_heating_controls import MODE, ROOT, TARGET, scalar
from tests.test_heating_temporary_setpoint import (
    TEMPORARY,
    _button,
    _circuit,
    _put,
    _temporary,
)
from tests.test_write_guards import _Lifecycle

CONTROL_TYPE = ROOT + "/controlType"
TARGET_FEATURE = ClimateEntityFeature.TARGET_TEMPERATURE


def _control_type(value: str = "outdoorTemperature"):
    return scalar(
        CONTROL_TYPE,
        value,
        options=("outdoorTemperature", "roomTemperature", "constants", "ISRC"),
    )


def _with_control_type(hass, value: str = "outdoorTemperature", **kwargs: Any):
    coordinator, entity, cloud = _circuit(hass, **kwargs)
    control = _control_type(value)
    _put(coordinator, control)
    cloud.state[control.path] = control
    return coordinator, entity, cloud


def _switch_on_gateway(cloud, value: str) -> None:
    cloud.state[CONTROL_TYPE] = replace(cloud.state[CONTROL_TYPE], value=value)


# The control type is read again before every write -----------------------


async def test_auto_target_reads_the_control_type_with_the_guards(hass):
    _, entity, cloud = _with_control_type(hass)
    await entity.async_set_temperature(temperature=22.5)
    assert CONTROL_TYPE in cloud.bulk_calls[0]
    assert cloud.puts == [(TEMPORARY, 22.5)]


async def test_auto_target_is_refused_when_the_control_type_changed(hass):
    """Changed to ``constants`` between display and write."""
    _, entity, cloud = _with_control_type(hass)
    _switch_on_gateway(cloud, "constants")
    with pytest.raises(ServiceValidationError) as error:
        await entity.async_set_temperature(temperature=22.5)
    assert error.value.translation_key == "write_validation_failed"
    assert cloud.puts == []
    # The fresh reading is shown at once: the circuit is read-only now.
    assert entity.supported_features == ClimateEntityFeature(0)


async def test_clear_button_is_refused_when_the_control_type_changed(hass):
    """The same for the clear button."""
    coordinator, _, cloud = _with_control_type(
        hass, temporary=_temporary(22.0, minimum=-1.0)
    )
    _switch_on_gateway(cloud, "roomTemperature")
    with pytest.raises(ServiceValidationError) as error:
        await _button(hass, coordinator).async_press()
    assert error.value.translation_key == "write_validation_failed"
    assert CONTROL_TYPE in cloud.bulk_calls[0]
    assert cloud.puts == []


async def test_manual_target_is_refused_when_the_control_type_changed(hass):
    """The manual setpoint in the mode manual."""
    _, entity, cloud = _with_control_type(hass, mode="manual")
    _switch_on_gateway(cloud, "roomTemperature")
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=20.5)
    assert cloud.puts == []


async def test_mode_change_is_refused_when_the_control_type_changed(hass):
    _, entity, cloud = _with_control_type(hass, mode="manual")
    _switch_on_gateway(cloud, "roomTemperature")
    with pytest.raises(ServiceValidationError):
        await entity.async_set_hvac_mode(HVACMode.AUTO)
    assert cloud.puts == []


async def test_unchanged_control_type_lets_every_write_through(hass):
    coordinator, entity, cloud = _with_control_type(
        hass, mode="manual", temporary=_temporary(22.0, minimum=-1.0)
    )
    await entity.async_set_temperature(temperature=20.5)
    await entity.async_set_hvac_mode(HVACMode.AUTO)
    await _button(hass, coordinator).async_press()
    assert cloud.puts == [(TARGET, 20.5), (MODE, "auto"), (TEMPORARY, -1.0)]
    assert all(CONTROL_TYPE in call for call in cloud.bulk_calls)


async def test_a_circuit_without_a_control_type_writes_as_before(hass):
    """Never reported (404 at discovery): no extra request."""
    _, entity, cloud = _circuit(hass)
    await entity.async_set_temperature(temperature=22.5)
    assert cloud.puts == [(TEMPORARY, 22.5)]
    assert all(CONTROL_TYPE not in call for call in cloud.bulk_calls)
    assert CONTROL_TYPE not in cloud.gets


@pytest.mark.parametrize("mode", ["auto", "manual"])
async def test_a_control_type_without_a_current_reading_refuses(hass, mode):
    """Reported, but its last request failed: refused by the climate itself,
    before the write transaction; nothing is written."""
    coordinator, entity, cloud = _with_control_type(hass, mode=mode)
    snapshot = coordinator.data[CONTROL_TYPE]
    coordinator.data[CONTROL_TYPE] = replace(
        snapshot,
        available=False,
        freshness=Freshness.STALE,
        last_error_category="http_504",
    )
    with pytest.raises(ServiceValidationError) as error:
        await entity.async_set_temperature(temperature=21.5)
    assert error.value.translation_key == "heating_climate_command"
    assert cloud.puts == []
    assert cloud.bulk_calls == []


async def test_isrc_allows_no_temporary_target_or_clearing(hass):
    """As the manufacturer's app, for the temporary setpoint."""
    coordinator, entity, cloud = _with_control_type(
        hass, "ISRC", temporary=_temporary(22.0, minimum=-1.0)
    )
    assert not entity.supported_features & TARGET_FEATURE
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=22.5)
    button = _button(hass, coordinator)
    assert not button.available
    with pytest.raises(ServiceValidationError):
        await button.async_press()
    assert cloud.puts == []


async def test_isrc_keeps_the_manual_setpoint(hass):
    """The app's rule covers the temporary setpoint, not the manual one."""
    _, entity, cloud = _with_control_type(hass, "ISRC", mode="manual")
    assert entity.supported_features & TARGET_FEATURE
    await entity.async_set_temperature(temperature=20.5)
    assert cloud.puts == [(TARGET, 20.5)]


# The lower limit of the temporary target -----------------------------------


def _manual_not_current(coordinator) -> None:
    snapshot = coordinator.data[TARGET]
    coordinator.data[TARGET] = replace(
        snapshot,
        available=False,
        freshness=Freshness.STALE,
        last_error_category="http_504",
    )


def test_lower_limit_stays_while_the_manual_setpoint_is_valid(hass):
    """One missed reading does not drop the range."""
    coordinator, entity, _ = _circuit(hass, temporary=_temporary(minimum=-1.0))
    assert (entity.min_temp, entity.max_temp) == (5, 30)
    _manual_not_current(coordinator)
    assert entity.supported_features & TARGET_FEATURE
    assert (entity.min_temp, entity.max_temp) == (5, 30)


def test_lower_limit_ends_once_the_manual_setpoint_expired(hass, monkeypatch):
    coordinator, entity, _ = _circuit(hass, temporary=_temporary(minimum=-1.0))
    _manual_not_current(coordinator)
    monkeypatch.setattr(coordinator, "_expired", lambda *_: True)
    assert not entity.supported_features & TARGET_FEATURE


async def test_a_write_still_needs_a_fresh_manual_setpoint(hass):
    """The strict rule of a change stays."""
    coordinator, entity, cloud = _circuit(hass, temporary=_temporary(minimum=-1.0))
    _manual_not_current(coordinator)
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=22)
    assert cloud.puts == []


# The clear value outside the advertised limits -----------------------------


def test_clear_value_outside_the_limits_gives_a_disabled_button(hass):
    """The iCom shape (5 to 30) gets a button, created disabled;
    with -1 in the limits it stays enabled as before."""
    coordinator, _, _ = _circuit(hass, temporary=_temporary(21.0))
    resources = {path: item.resource for path, item in coordinator.data.items()}
    (outside,) = clear_button_descriptions(resources, GENERIC_CONTEXT)
    assert outside.key == ROOT
    assert outside.entity_registry_enabled_default is False
    resources[TEMPORARY] = _temporary(21.0, minimum=-1.0)
    (inside,) = clear_button_descriptions(resources, GENERIC_CONTEXT)
    assert inside.entity_registry_enabled_default is True


async def test_platform_creates_the_outside_button_disabled(hass):
    coordinator, _, _ = _circuit(hass, temporary=_temporary(21.0))
    lifecycle = _Lifecycle()
    scope = SimpleNamespace(
        coordinators=(coordinator,),
        bacon=None,
        async_on_unload=lifecycle.async_on_unload,
    )
    added: list[Any] = []
    button_platform._setup_entities(scope, added.extend)
    (clear,) = [
        item for item in added if isinstance(item, HeatingCircuitTemporaryClearButton)
    ]
    assert clear.entity_registry_enabled_default is False
    lifecycle.unload()


async def test_clear_outside_the_limits_writes_minus_one_once(hass):
    coordinator, _, cloud = _circuit(hass, temporary=_temporary(21.0))
    button = _button(hass, coordinator)
    assert button.available
    await button.async_press()
    assert cloud.puts == [(TEMPORARY, -1.0)]
    assert coordinator.data[TEMPORARY].resource.value == -1.0
    assert not button.available


@pytest.mark.parametrize("reading", [-1.0, -2.0, -0.5])
async def test_any_negative_reading_confirms_the_clear_value(hass, reading):
    """The app takes ``>= 0`` as active, so any negative value
    shows no temporary setpoint."""
    coordinator, _, cloud = _circuit(hass, temporary=_temporary(21.0))

    async def put(gateway, path, value):
        cloud.puts.append((path, value))
        cloud.state[path] = replace(cloud.state[path], value=reading)

    coordinator.client.put_resource_value.side_effect = put
    await _button(hass, coordinator).async_press()
    assert cloud.puts == [(TEMPORARY, -1.0)]
    assert coordinator.data[TEMPORARY].resource.value == reading


async def test_an_unchanged_reading_leaves_the_clear_unconfirmed(hass):
    coordinator, _, cloud = _circuit(hass, temporary=_temporary(21.0))

    async def ignored(gateway, path, value):
        cloud.puts.append((path, value))

    coordinator.client.put_resource_value.side_effect = ignored
    with pytest.raises(HomeAssistantError) as error:
        await _button(hass, coordinator).async_press()
    assert error.value.translation_key == "write_not_confirmed"
    assert cloud.puts == [(TEMPORARY, -1.0)]


async def test_a_timed_out_clear_is_resolved_by_reading(hass):
    """The request timed out, the reading shows it applied."""
    coordinator, _, cloud = _circuit(hass, temporary=_temporary(21.0))

    async def timed_out(gateway, path, value):
        cloud.puts.append((path, value))
        cloud.state[path] = replace(cloud.state[path], value=-1.0)
        raise RequestTimeout("timeout")

    coordinator.client.put_resource_value.side_effect = timed_out
    await _button(hass, coordinator).async_press()
    assert cloud.puts == [(TEMPORARY, -1.0)]


async def test_a_failing_read_back_is_never_followed_by_a_second_request(hass):
    """Read-back 503 throughout: not confirmed, no second PUT."""
    coordinator, _, cloud = _circuit(hass, temporary=_temporary(21.0))
    cloud.readback_error = ServiceUnavailable("unavailable")
    with pytest.raises(HomeAssistantError) as error:
        await _button(hass, coordinator).async_press()
    assert error.value.translation_key == "write_not_confirmed"
    assert cloud.puts == [(TEMPORARY, -1.0)]


async def test_no_active_setpoint_offers_nothing_to_clear(hass):
    """A reading of -1: nothing to clear."""
    coordinator, _, cloud = _circuit(hass, temporary=_temporary(-1.0))
    button = _button(hass, coordinator)
    assert not button.available
    with pytest.raises(ServiceValidationError) as error:
        await button.async_press()
    assert error.value.translation_key == "heating_temporary_setpoint_not_active"
    assert cloud.puts == []


def test_not_writable_gives_no_clear_button(hass):
    """Only when the gateway reports the path writable."""
    coordinator, _, _ = _circuit(hass, temporary=_temporary(21.0, writable=False))
    resources = {path: item.resource for path, item in coordinator.data.items()}
    assert clear_button_descriptions(resources, GENERIC_CONTEXT) == ()
    with pytest.raises(WriteValidationError):
        validate_control_value(resources[TEMPORARY], -1.0)


@pytest.mark.parametrize(
    ("mode", "control"), [("auto", "constants"), ("manual", None), ("off", None)]
)
async def test_clear_outside_the_limits_keeps_the_other_conditions(hass, mode, control):
    """A constant control type, or a mode other than auto."""
    coordinator, _, cloud = _circuit(hass, mode=mode, temporary=_temporary(21.0))
    if control is not None:
        resource = _control_type(control)
        _put(coordinator, resource)
        cloud.state[resource.path] = resource
    button = _button(hass, coordinator)
    assert not button.available
    with pytest.raises(ServiceValidationError):
        await button.async_press()
    assert cloud.puts == []


def test_only_the_clear_value_passes_outside_the_limits():
    """The validator lets exactly -1 through, and only outside the limits."""
    outside = _temporary(21.0)
    validate_control_value(outside, -1.0)
    for refused in (-2.0, -0.5, 0.0, 4.5, 30.5):
        with pytest.raises(WriteValidationError):
            validate_control_value(outside, refused)
    # Within the limits the grid still decides: -1 off the grid of 1.0
    # counted from -1.5 is refused, and gives no button.
    off_grid = _temporary(21.0, minimum=-1.5, step_size=1.0)
    with pytest.raises(WriteValidationError):
        validate_control_value(off_grid, -1.0)
    assert not takes_clear_value(off_grid, 1.0)
    assert clear_outside_limits(outside)
    assert not clear_outside_limits(_temporary(21.0, minimum=-1.0))


def test_the_confirmation_rule_belongs_to_the_clear_value():
    policy = HEATING_CIRCUIT_TEMPORARY_SETPOINT_POLICY
    assert confirms_value(policy, -3.0, -1.0)
    assert not confirms_value(policy, 21.0, -1.0)
    assert not confirms_value(policy, True, -1.0)
    assert not confirms_value(policy, float("-inf"), -1.0)
    # A target is confirmed only by itself.
    assert not confirms_value(policy, -1.0, 21.5)
    assert confirms_value(policy, 21.5, 21.5)
    # The temporary setpoint of a zone shares it; no other policy has it.
    assert confirms_value(ZONE_TEMPORARY_SETPOINT_POLICY, -3.0, -1.0)
    assert not confirms_value(ZONE_TEMPORARY_SETPOINT_POLICY, -1.0, 21.5)
    assert all(
        item.confirm_if is None and item.sentinel_value is None
        for item in NUMBER_WRITE_POLICIES
        if item not in (policy, ZONE_TEMPORARY_SETPOINT_POLICY)
    )


# The flags of every generic policy ------------------------------------------


def _flagged(path: str, value: Any, **fields: Any):
    data: dict[str, Any] = {
        "id": path,
        "type": "stringValue" if isinstance(value, str) else "floatValue",
        "writeable": 1,
        "value": value,
        **fields,
    }
    return parse_resource(data)


SILENT = "/system/silentMode/enabled"
LEVEL = "/dhwCircuits/dhw1/temperatureLevels/high"


def _silent(**flags: Any):
    return _flagged(SILENT, "off", allowedValues=["off", "auto", "on"], **flags)


def _level(**flags: Any):
    return _flagged(LEVEL, 55.0, unitOfMeasure="C", minValue=40, maxValue=60, **flags)


@pytest.mark.parametrize("build", [_silent, _level])
def test_missing_flags_change_nothing(build):
    resource = build()
    assessment = assess_control(resource)
    assert assessment.eligible
    validate_control_value(resource, "on" if build is _silent else 50.0)


@pytest.mark.parametrize("build", [_silent, _level])
@pytest.mark.parametrize("used", ["true", "false"])
def test_a_generic_setting_not_available_has_no_control(build, used):
    assessment = assess_control(build(available="false", used=used))
    assert assessment.rejection_reason == "not_available"


@pytest.mark.parametrize("build", [_silent, _level])
def test_a_generic_setting_not_used_refuses_to_write(build):
    resource = build(used="false")
    assert assess_control(resource).eligible
    with pytest.raises(ControlUnused) as raised:
        validate_control_value(resource, "on" if build is _silent else 50.0)
    assert raised.value.contract is False


@pytest.mark.parametrize("build", [_silent, _level])
def test_malformed_generic_flags_give_no_control(build):
    assessment = assess_control(build(available="no"))
    assert assessment.rejection_reason == "invalid_flags"


def test_an_unavailable_generic_number_gets_no_entity():
    assert build_number_descriptions({LEVEL: _level()})
    assert build_number_descriptions({LEVEL: _level(available="false")}) == ()


def test_the_zone_controls_keep_their_own_flag_rules():
    assert not ZONE_OPERATION_MODE_POLICY.honor_flags
    assert not RRC_OPERATION_MODE_POLICY.honor_flags
    assert SILENT_MODE_POLICY.honor_flags and DHW_OPERATION_MODE_POLICY.honor_flags
    own_rules = [item for item in NUMBER_WRITE_POLICIES if not item.honor_flags]
    assert [item.path_pattern for item in own_rules] == [
        NUMBER_WRITE_POLICIES[0].path_pattern,
        NUMBER_WRITE_POLICIES[1].path_pattern,
    ]


async def test_an_unused_setting_names_the_appliance_not_a_room_controller(hass):
    """The general message; nothing is sent."""
    coordinator, _, cloud = _circuit(hass, mode="manual")
    target = coordinator.data[TARGET].resource
    unused = replace(target, metadata=replace(target.metadata, used=False))
    _put(coordinator, unused)
    cloud.state[TARGET] = unused
    policy = number_policy_for_resource(unused)
    assert policy is not None
    with pytest.raises(ServiceValidationError) as error:
        await async_set_control(coordinator, TARGET, 20.5, policy)
    assert error.value.translation_key == "control_unused"
    assert cloud.puts == []
