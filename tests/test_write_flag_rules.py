"""Writes checked on the gateway's current reading (synthetic).

A setting's flags (``used``/``available``), the readings a write only reads
for a rule, two coupled levels set at once, the older zone controls, a mode
with a temperature whose flags follow the mode, and the air conditioner's
target in °F and without an on/off setting. Every value is constructed;
nothing here confirms behaviour on a real device.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from homeassistant.components.climate.const import HVACMode
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.bosch_buderus_heating import coordinator as coordinator_module
from custom_components.bosch_buderus_heating.aircon_policies import (
    AC_CONTROL,
    OPERATION_MODE,
    TEMPERATURE_SETPOINT,
)
from custom_components.bosch_buderus_heating.circuit_control_writes import (
    async_set_guarded,
)
from custom_components.bosch_buderus_heating.coordinator import Freshness
from custom_components.bosch_buderus_heating.heating_climate import (
    HeatingCircuitClimate,
    manual_floor_reading,
)
from custom_components.bosch_buderus_heating.pointt import (
    ResourceForbidden,
    ResourceNotFound,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.pointt.models import BatchItemResult
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.writes import assess_control
from tests.aircon_helpers import (  # noqa: F401
    FakeUnit,
    aircon_cleanup,
    make_coordinator,
)
from tests.circuit_control_helpers import Cloud as CircuitCloud
from tests.circuit_control_helpers import coordinator_for
from tests.test_aircon_ha import climate_of as aircon_climate
from tests.test_circuit_older_controls import MANUAL as HC_MANUAL
from tests.test_circuit_older_controls import MODE as HC_MODE
from tests.test_circuit_older_controls import ROOT, TEMPORARY, _resources
from tests.test_zone_climate import (  # noqa: F401
    CONTROL_TYPE,
    MANUAL,
    MODE,
    SEASON,
    changed,
    climate,
    zone_plant,
)


def _flags(resource, **flags):
    return replace(resource, metadata=replace(resource.metadata, **flags))


# The guard rule ------------------------------------------------------------------


def test_a_setting_compared_by_value_also_counts_its_flags():
    """The written setting (or a coupled level) counts as changed when its
    value or what its flags say changed; new limits alone do not."""
    before = parse_resource(
        {
            "id": "/rrc/zones/zn1/userMode",
            "type": "stringValue",
            "value": "manual",
            "allowedValues": ["manual", "clock"],
            "writeable": 1,
        }
    )
    by_value = frozenset({before.path})
    guard_changed = coordinator_module._guard_changed
    assert not guard_changed(before, before, by_value)
    assert guard_changed(before, _flags(before, used=False), by_value)
    assert guard_changed(before, _flags(before, available=False), by_value)
    assert not guard_changed(before, _flags(before, used=True), by_value)
    assert guard_changed(before, replace(before, value="clock"), by_value)
    level = parse_resource(
        {
            "id": "/heatingCircuits/hc1/temperatureLevels/eco",
            "type": "floatValue",
            "value": 19.0,
            "minValue": 5,
            "maxValue": 20.5,
            "writeable": 1,
        }
    )
    moved = replace(level, metadata=replace(level.metadata, maximum=19.5))
    assert not guard_changed(level, moved, frozenset({level.path}))
    assert guard_changed(level, moved, frozenset())


# A reading read only for a rule may be missing -----------------------------------


def _auto_circuit(hass, *, manual_answer, bulk_fails=False):
    """A circuit in auto whose gateway answers ``manual_answer`` (403/404)
    for the manual setpoint, whose earlier reading is still current;
    ``bulk_fails``: the bulk request fails, every path is read alone."""
    resources = _resources()
    resources[HC_MODE] = replace(resources[HC_MODE], value="auto")
    resources[TEMPORARY] = parse_resource(
        {
            "id": TEMPORARY,
            "type": "floatValue",
            "value": -1,
            "writeable": 1,
            "unitOfMeasure": "C",
            "minValue": 5,
            "maxValue": 30,
            "stepSize": 0.5,
        }
    )
    coordinator = coordinator_for(hass, resources)
    cloud = CircuitCloud(coordinator)
    bulk = cloud.bulk

    async def answering(gateway, paths, **kwargs):
        if bulk_fails:
            raise ServiceUnavailable(503)
        answered = await bulk(gateway, [p for p in paths if p != HC_MANUAL])
        return (
            *answered,
            *(
                BatchItemResult(gateway, p, manual_answer)
                for p in paths
                if p == HC_MANUAL
            ),
        )

    async def single(gateway, path, **kwargs):
        cloud.gets.append(path)
        if path == HC_MANUAL:
            error = ResourceNotFound if manual_answer == 404 else ResourceForbidden
            raise error(path, manual_answer)
        return cloud.state[path]

    coordinator.client.get_resources_bulk.side_effect = answering
    coordinator.client.get_resource.side_effect = single
    entity = HeatingCircuitClimate(coordinator, ROOT)
    entity.hass = hass
    return coordinator, cloud, entity


@pytest.mark.parametrize("answer", [403, 404])
async def test_temporary_target_when_the_manual_setpoint_is_missing_now(hass, answer):
    """The manual setpoint is read along only for the lower limit rule; when
    the gateway does not answer for it, the rule does not apply and the
    target is written once (no single read of the missing path)."""
    _coordinator, cloud, entity = _auto_circuit(hass, manual_answer=answer)
    await entity.async_set_temperature(temperature=22)
    assert cloud.puts == [(TEMPORARY, 22.0)]
    assert HC_MANUAL not in cloud.gets


@pytest.mark.parametrize("answer", [403, 404])
async def test_temporary_target_when_only_single_reads_answer(hass, answer):
    """The bulk request fails; read alone, the manual setpoint answers
    403/404: the rule does not apply, the target is written once."""
    _coordinator, cloud, entity = _auto_circuit(
        hass, manual_answer=answer, bulk_fails=True
    )
    await entity.async_set_temperature(temperature=22)
    assert cloud.puts == [(TEMPORARY, 22.0)]
    assert HC_MANUAL in cloud.gets


async def test_a_missing_reading_of_the_setting_itself_still_ends_the_write(hass):
    """Only a reading read for a rule may be missing; the setting must be
    read, or nothing is sent."""
    coordinator, cloud, _entity = _auto_circuit(hass, manual_answer=404)
    from custom_components.bosch_buderus_heating.circuit_controls import (
        MANUAL_SETPOINT_POLICY,
    )

    with pytest.raises(ResourceNotFound):
        await coordinator.async_write_control(
            HC_MANUAL,
            22.0,
            MANUAL_SETPOINT_POLICY,
            fresh=(coordinator.data[HC_MANUAL].resource,),
        )
    assert cloud.puts == []


def test_a_stale_manual_setpoint_is_no_floor_reading(hass):
    coordinator = coordinator_for(hass, _resources())
    assert manual_floor_reading(coordinator, ROOT) == (
        coordinator.data[HC_MANUAL].resource,
    )
    coordinator.data[HC_MANUAL] = replace(
        coordinator.data[HC_MANUAL],
        available=False,
        freshness=Freshness.STALE,
        ended=True,
    )
    assert manual_floor_reading(coordinator, ROOT) == ()


async def test_circuit_manual_target_changed_on_the_gateway_takes_the_new_one(hass):
    """In the mode manual the target was changed in the app since the last
    poll: the new target replaces it, checked on the current reading."""
    coordinator = coordinator_for(hass, _resources())
    cloud = CircuitCloud(coordinator)
    cloud.change(HC_MANUAL, value=19.5)
    entity = HeatingCircuitClimate(coordinator, ROOT)
    entity.hass = hass
    await entity.async_set_temperature(temperature=22)
    assert cloud.puts == [(HC_MANUAL, 22.0)]


async def test_circuit_manual_target_not_used_now_is_refused_unsent(hass):
    coordinator = coordinator_for(hass, _resources())
    cloud = CircuitCloud(coordinator)
    cloud.change(
        HC_MANUAL, metadata=replace(cloud.state[HC_MANUAL].metadata, used=False)
    )
    entity = HeatingCircuitClimate(coordinator, ROOT)
    entity.hass = hass
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=22)
    assert cloud.puts == []


# Zone controls: the flags of the current reading ---------------------------------


@pytest.mark.parametrize("flags", [{"used": False}, {"available": False}])
async def test_zone_mode_not_usable_now_is_refused_unsent(hass, plant, flags):
    """Shown as usable from the last poll; the gateway now reports the flag:
    nothing is sent and the current reading is shown at once."""
    coordinator, cloud = plant
    entity = climate(hass, coordinator)
    cloud.state[MODE] = _flags(cloud.state[MODE], **flags)
    with pytest.raises(ServiceValidationError):
        await entity.async_set_hvac_mode(HVACMode.HEAT)
    assert cloud.puts == []
    assert coordinator.data[MODE].resource == cloud.state[MODE]


@pytest.mark.parametrize("flags", [{"used": False}, {"available": False}])
async def test_zone_manual_target_not_usable_now_is_refused_unsent(hass, plant, flags):
    coordinator, cloud = plant
    changed(coordinator, cloud, MODE, value="manual")
    entity = climate(hass, coordinator)
    cloud.state[MANUAL] = _flags(cloud.state[MANUAL], **flags)
    with pytest.raises(ServiceValidationError) as caught:
        await entity.async_set_temperature(temperature=22)
    assert cloud.puts == []
    if flags == {"used": False}:
        assert caught.value.translation_key == "control_unused"


async def test_zone_mode_with_a_target_whose_flags_follow_the_mode(hass, plant):
    """The manual setpoint is not used in auto and used in manual: the mode
    goes first and the target is checked on its own current reading."""
    coordinator, cloud = plant
    changed(coordinator, cloud, MANUAL, used=False)
    entity = climate(hass, coordinator)
    original = cloud.put

    async def gateway(gw, path, value):
        await original(gw, path, value)
        if path == MODE and value == "manual":
            cloud.state[MANUAL] = _flags(cloud.state[MANUAL], used=True)

    coordinator.client.put_resource_value.side_effect = gateway
    await entity.async_set_temperature(temperature=22, hvac_mode=HVACMode.HEAT)
    assert cloud.puts == [(MODE, "manual"), (MANUAL, 22.0)]


async def test_a_confirmed_mode_reads_its_targets_again_soon(hass, plant):
    """After a new mode the targets' flags may differ: they are read once
    with the next cycle instead of staying locked until their group."""
    coordinator, cloud = plant
    entity = climate(hass, coordinator)
    await entity.async_set_hvac_mode(HVACMode.HEAT)
    assert cloud.puts == [(MODE, "manual")]
    assert MANUAL in coordinator._zone_follow_up
    assert coordinator._zone_follow_up_unsub is not None
    coordinator._cancel_zone_follow_up()
    # The same value again changes nothing and reads nothing again.
    coordinator._zone_follow_up.clear()
    await entity.async_set_hvac_mode(HVACMode.HEAT)
    assert coordinator._zone_follow_up == set()


def test_only_a_mode_has_targets_to_read_again():
    targets = coordinator_module._mode_target_paths
    assert targets("/heatingCircuits/hc1/operationMode") == (
        "/heatingCircuits/hc1/manualRoomSetpoint",
        "/heatingCircuits/hc1/temporaryRoomSetpoint",
    )
    assert targets(MODE) == (MANUAL, MANUAL.replace("manual", "temporary"))
    assert targets("/dhwCircuits/dhw1/operationMode") == ()
    assert targets(MANUAL) == ()


# The older zone number and select ------------------------------------------------


def _policy(coordinator, path):
    policy = assess_control(coordinator.data[path].resource).policy
    assert policy is not None
    return policy


@pytest.mark.parametrize(("path", "value"), [(MANUAL, 22.0), (MODE, "manual")])
async def test_older_zone_controls_read_the_setting_and_the_circuit_again(
    hass, plant, path, value
):
    coordinator, cloud = plant
    if path == MANUAL:
        changed(coordinator, cloud, MODE, value="manual")
    await async_set_guarded(coordinator, path, value, _policy(coordinator, path))
    assert cloud.puts == [(path, value)]
    assert set(cloud.bulk_calls[0]) == {path, SEASON, CONTROL_TYPE}


@pytest.mark.parametrize(("path", "value"), [(MANUAL, 22.0), (MODE, "manual")])
async def test_older_zone_controls_follow_the_current_flags(hass, plant, path, value):
    coordinator, cloud = plant
    if path == MANUAL:
        changed(coordinator, cloud, MODE, value="manual")
    cloud.state[path] = _flags(cloud.state[path], used=False)
    with pytest.raises(ServiceValidationError) as caught:
        await async_set_guarded(coordinator, path, value, _policy(coordinator, path))
    assert caught.value.translation_key == "control_unused"
    assert cloud.puts == []


@pytest.mark.parametrize("on_gateway", [False, True])
async def test_older_zone_controls_on_constant_values_send_nothing(
    hass, plant, on_gateway
):
    """The circuit runs at constant values (since the last poll, or already
    shown so): as on the zone's climate entity, nothing is sent."""
    coordinator, cloud = plant
    if on_gateway:
        cloud.state[CONTROL_TYPE] = replace(
            cloud.state[CONTROL_TYPE], value="constants"
        )
    else:
        changed(coordinator, cloud, CONTROL_TYPE, value="constants")
    with pytest.raises(ServiceValidationError) as caught:
        await async_set_guarded(coordinator, MODE, "manual", _policy(coordinator, MODE))
    assert cloud.puts == []
    if not on_gateway:
        assert caught.value.translation_key == "circuit_constant_control"


# Air conditioner -----------------------------------------------------------------


async def test_aircon_target_in_fahrenheit_takes_the_nearest_step(hass):
    """75 °F arrive as 23.89 °C: the nearest whole degree from the minimum."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    coordinator, unit, _ = await make_coordinator(hass)
    entity = aircon_climate(hass, coordinator)
    await entity.async_set_temperature(temperature=(75 - 32) * 5 / 9)
    assert unit.puts == [(TEMPERATURE_SETPOINT, 24.0)]


async def test_aircon_in_celsius_still_refuses_a_value_between_steps(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    entity = aircon_climate(hass, coordinator)
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=22.5)
    assert unit.puts == []


async def test_aircon_without_an_on_off_setting_changes_its_mode(hass):
    unit = FakeUnit()
    del unit.resources[AC_CONTROL]
    coordinator, unit, _ = await make_coordinator(hass, unit)
    entity = aircon_climate(hass, coordinator)
    assert HVACMode.OFF not in entity.hvac_modes
    await entity.async_set_hvac_mode(HVACMode.HEAT)
    assert unit.puts == [(OPERATION_MODE, "heat")]


async def test_aircon_with_an_unreadable_on_off_setting_sends_no_mode(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    entity = aircon_climate(hass, coordinator)
    unit.status[AC_CONTROL] = 503
    with pytest.raises(ServiceValidationError):
        await entity.async_set_hvac_mode(HVACMode.HEAT)
    assert unit.puts == []


async def test_aircon_switched_off_still_refuses_the_mode(hass):
    coordinator, unit, _ = await make_coordinator(hass)
    unit.set(AC_CONTROL, value="off")
    await coordinator.async_refresh()
    entity = aircon_climate(hass, coordinator)
    with pytest.raises(ServiceValidationError) as caught:
        await entity.async_set_hvac_mode(HVACMode.HEAT)
    assert caught.value.translation_key == "aircon_switched_off"
    assert unit.puts == []
