"""The climate entity and clear button of ConnectKey zones (synthetic).

The readings come from the zone controller fixture: eight zones with gaps,
heating leaves readable below a refused ``heating`` directory, zone 2 with
fewer modes and a lower maximum, zone 4 read-only and zone 10 with
unavailable setpoints. Every value is constructed; nothing here confirms
behaviour on a real device.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.climate.const import (
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.bosch_buderus_heating import zone_climate
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.derived_activity import Derivation
from custom_components.bosch_buderus_heating.diagnostics import _control_diagnostics
from custom_components.bosch_buderus_heating.number import build_number_descriptions
from custom_components.bosch_buderus_heating.pointt import Gateway, Resource
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.writes import (
    ZONE_TEMPORARY_SETPOINT_POLICY,
    assess_control,
)
from custom_components.bosch_buderus_heating.zone_climate import (
    ZoneClimate,
    ZoneTemporaryClearButton,
    assigned_circuit,
    track_zone_climates,
    zone_clear_button_descriptions,
    zone_climate_diagnostics,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.test_write_guards import Cloud

FIXTURE = json.loads(
    (
        Path(__file__).parent / "fixtures/contracts/connectkey/zone-climate.json"
    ).read_text(encoding="utf-8")
)
RESOURCES = {item["id"]: parse_resource(item) for item in FIXTURE["resources"]}
Z1 = "/zones/zone1"
MODE = f"{Z1}/heating/operationMode"
MANUAL = f"{Z1}/heating/manualRoomSetpoint"
TEMPORARY = f"{Z1}/heating/temporaryRoomSetpoint"
COOL_MODE = f"{Z1}/cooling/operationMode"
SEASON = "/heatingCircuits/hc1/currentSuWiMode"
CONTROL_TYPE = "/heatingCircuits/hc1/controlType"
TARGET = ClimateEntityFeature.TARGET_TEMPERATURE


def _snapshot(resource: Resource) -> ResourceSnapshot:
    return ResourceSnapshot(resource, True, datetime.now(UTC))


@pytest.fixture(name="plant")
def zone_plant(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"gateway_ids": ["gateway"]})
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, client, Gateway("gateway"), entry
    )
    coordinator.data = {path: _snapshot(r) for path, r in RESOURCES.items()}
    coordinator.resources = dict(RESOURCES)
    coordinator.last_update_success = True
    cloud = Cloud(coordinator, with_context=False)
    return coordinator, cloud


def put(coordinator, cloud, resource: Resource) -> None:
    coordinator.data[resource.path] = _snapshot(resource)
    coordinator.resources[resource.path] = resource
    cloud.state[resource.path] = resource


def changed(coordinator, cloud, path, **changes) -> Resource:
    resource = coordinator.data[path].resource
    value = changes.pop("value", resource.value)
    updated = replace(
        resource, value=value, metadata=replace(resource.metadata, **changes)
    )
    put(coordinator, cloud, updated)
    return updated


def climate(hass, coordinator, root=Z1) -> ZoneClimate:
    entity = ZoneClimate(coordinator, root)
    entity.hass = hass
    return entity


def cooling(coordinator, cloud, *, readable=True) -> None:
    changed(coordinator, cloud, SEASON, value="cooling")
    if readable:
        for leaf, value, extra in (
            ("operationMode", "manual", {"allowedValues": ["off", "manual", "auto"]}),
            (
                "manualRoomSetpoint",
                25.0,
                {"unitOfMeasure": "C", "minValue": 18, "maxValue": 30},
            ),
        ):
            put(
                coordinator,
                cloud,
                parse_resource(
                    {
                        "id": f"{Z1}/cooling/{leaf}",
                        "type": "stringValue"
                        if isinstance(value, str)
                        else "floatValue",
                        "value": value,
                        "writeable": True,
                        **extra,
                    }
                ),
            )


# Creation, identity and modes -------------------------------------------


async def test_one_climate_per_listed_zone_with_its_own_identity(hass, plant):
    coordinator, _ = plant
    added = []
    unload = []
    entry = SimpleNamespace(async_on_unload=unload.append)
    track_zone_climates(entry, coordinator, added.extend)
    assert sorted(e.unique_id for e in added) == sorted(
        f"gateway:zones:zone{n}:climate" for n in (1, 2, 4, 5, 6, 7, 8, 10)
    )
    # An RRC zone (``znN``) never gets this climate.
    assert not any(":zones:zn" in e.unique_id for e in added)
    coordinator.async_set_updated_data(coordinator.data)
    assert len(added) == 8
    for callback in unload:
        callback()


async def test_modes_follow_each_zone_s_offered_values(hass, plant):
    coordinator, _ = plant
    assert climate(hass, coordinator).hvac_modes == [
        HVACMode.OFF,
        HVACMode.HEAT,
        HVACMode.AUTO,
    ]
    zone2 = climate(hass, coordinator, "/zones/zone2")
    assert zone2.hvac_modes == [HVACMode.OFF, HVACMode.AUTO]
    assert zone2.max_temp == 25
    # Zone 4 is only shown: its current mode, no command.
    zone4 = climate(hass, coordinator, "/zones/zone4")
    assert zone4.available and zone4.hvac_mode == HVACMode.AUTO
    assert zone4.hvac_modes == [HVACMode.AUTO]
    assert zone4.supported_features == ClimateEntityFeature(0)


async def test_auto_shows_the_program_target_and_the_temporary_one(hass, plant):
    coordinator, cloud = plant
    entity = climate(hass, coordinator)
    assert entity.hvac_mode == HVACMode.AUTO
    assert entity.supported_features & TARGET
    assert (entity.min_temp, entity.max_temp, entity.target_temperature_step) == (
        5,
        30,
        0.5,
    )
    assert entity.native_target_temperature == 21.0  # currentRoomSetpoint
    assert entity.native_current_temperature == 20.0
    assert entity.current_humidity == 45.0
    attributes = entity.extra_state_attributes
    assert attributes["temporary_override"] is False
    assert attributes["zone_side"] == "heating"
    assert attributes["child_lock"] == "off"
    assert attributes["heating_circuit"] == "hc1"
    changed(coordinator, cloud, TEMPORARY, value=22.5)
    assert entity.native_target_temperature == 22.5
    assert entity.extra_state_attributes["temporary_override"] is True


async def test_manual_shows_the_manual_setpoint_and_off_none(hass, plant):
    coordinator, cloud = plant
    entity = climate(hass, coordinator)
    changed(coordinator, cloud, MODE, value="manual")
    assert entity.hvac_mode == HVACMode.HEAT
    assert entity.native_target_temperature == 21.0
    assert entity.supported_features & TARGET
    changed(coordinator, cloud, MODE, value="off")
    assert entity.hvac_mode == HVACMode.OFF
    assert entity.native_target_temperature is None
    assert not entity.supported_features & TARGET
    assert entity.supported_features & ClimateEntityFeature.TURN_OFF


@pytest.mark.parametrize(
    "change",
    [
        {"value": "unknown"},
        {"allowed_values": ("manual",)},
        {"used": False},
        {"available": False},
    ],
)
async def test_invalid_or_unused_mode_makes_the_climate_unavailable(
    hass, plant, change
):
    coordinator, cloud = plant
    entity = climate(hass, coordinator)
    changed(coordinator, cloud, MODE, **change)
    assert not entity.available
    assert entity.hvac_modes == []
    with pytest.raises(ServiceValidationError) as error:
        await entity.async_set_hvac_mode(HVACMode.HEAT)
    assert error.value.translation_key == (
        "control_unused" if change == {"used": False} else "zone_climate_command"
    )
    assert cloud.puts == []


async def test_stale_mode_reading_hides_and_refuses(hass, plant):
    coordinator, cloud = plant
    entity = climate(hass, coordinator)
    coordinator.data[MODE] = replace(coordinator.data[MODE], freshness=Freshness.STALE)
    assert not entity.available
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=22)
    assert cloud.puts == []


async def test_unavailable_setpoints_offer_no_target(hass, plant):
    coordinator, _ = plant
    zone10 = climate(hass, coordinator, "/zones/zone10")
    assert zone10.available and zone10.hvac_mode == HVACMode.AUTO
    assert not zone10.supported_features & TARGET
    with pytest.raises(ServiceValidationError):
        await zone10.async_set_temperature(temperature=22)


# Writes -----------------------------------------------------------------


async def test_auto_target_reads_fresh_and_writes_the_temporary_setpoint_once(
    hass, plant
):
    coordinator, cloud = plant
    entity = climate(hass, coordinator)
    await entity.async_set_temperature(temperature=22.5)
    guards = set(cloud.bulk_calls[0])
    assert {MODE, TEMPORARY, SEASON, CONTROL_TYPE} <= guards
    assert cloud.puts == [(TEMPORARY, 22.5)]
    assert cloud.gets[-1] == TEMPORARY
    assert coordinator.data[MANUAL].resource.value == 21.0
    assert coordinator.data[MODE].resource.value == "auto"


async def test_manual_target_writes_the_manual_setpoint(hass, plant):
    coordinator, cloud = plant
    changed(coordinator, cloud, MODE, value="manual")
    entity = climate(hass, coordinator)
    await entity.async_set_temperature(temperature=20.5)
    assert cloud.puts == [(MANUAL, 20.5)]


async def test_mode_change_sends_only_the_mode(hass, plant):
    coordinator, cloud = plant
    entity = climate(hass, coordinator)
    await entity.async_set_hvac_mode(HVACMode.HEAT)
    assert cloud.puts == [(MODE, "manual")]
    await entity.async_turn_off()
    assert cloud.puts[-1] == (MODE, "off")


async def test_mode_with_temperature_writes_the_mode_first(hass, plant):
    coordinator, cloud = plant
    entity = climate(hass, coordinator)
    await entity.async_set_temperature(temperature=22, hvac_mode=HVACMode.HEAT)
    assert cloud.puts == [(MODE, "manual"), (MANUAL, 22)]


async def test_failed_mode_sends_no_temperature(hass, plant):
    coordinator, cloud = plant
    entity = climate(hass, coordinator)

    async def ignored(gateway, path, value):
        cloud.puts.append((path, value))  # the gateway keeps the old mode

    coordinator.client.put_resource_value.side_effect = ignored
    with pytest.raises(HomeAssistantError):
        await entity.async_set_temperature(temperature=22, hvac_mode=HVACMode.HEAT)
    assert cloud.puts == [(MODE, "manual")]


async def test_impossible_target_in_the_requested_mode_sends_nothing(hass, plant):
    coordinator, cloud = plant
    entity = climate(hass, coordinator)
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=22, hvac_mode=HVACMode.OFF)
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=40, hvac_mode=HVACMode.HEAT)
    assert cloud.puts == []


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), 4.5, 30.5, 21.3])
async def test_invalid_targets_send_nothing(hass, plant, value):
    coordinator, cloud = plant
    entity = climate(hass, coordinator)
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=value)
    assert cloud.puts == []


async def test_unknown_service_keys_send_nothing(hass, plant):
    coordinator, cloud = plant
    entity = climate(hass, coordinator)
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=22, preset_mode="eco")
    assert cloud.puts == []


@pytest.mark.parametrize("path", [MODE, TEMPORARY, SEASON, CONTROL_TYPE])
async def test_a_change_on_the_gateway_refuses_the_write(hass, plant, path):
    coordinator, cloud = plant
    entity = climate(hass, coordinator)
    resource = cloud.state[path]
    other = {
        MODE: "manual",
        TEMPORARY: 23.0,
        SEASON: "cooling",
        CONTROL_TYPE: "room",
    }[path]
    cloud.state[path] = replace(resource, value=other)
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=22)
    assert cloud.puts == []


async def test_uncertain_put_is_read_back_never_sent_twice(hass, plant):
    coordinator, cloud = plant
    entity = climate(hass, coordinator)
    from custom_components.bosch_buderus_heating.pointt import RequestTimeout

    async def lost(gateway, path, value):
        cloud.puts.append((path, value))
        cloud.state[path] = replace(cloud.state[path], value=value)
        raise RequestTimeout("lost response")

    coordinator.client.put_resource_value.side_effect = lost
    await entity.async_set_temperature(temperature=22)
    assert cloud.puts == [(TEMPORARY, 22)]


async def test_cloud_down_is_named_before_anything_is_checked(hass, plant):
    coordinator, cloud = plant
    entity = climate(hass, coordinator)
    coordinator.control_status = lambda: ("cloud_unreachable", None)
    with pytest.raises(HomeAssistantError) as error:
        await entity.async_set_hvac_mode(HVACMode.HEAT)
    assert error.value.translation_key == "command_cloud_unreachable"
    assert cloud.puts == []


async def test_fahrenheit_target_uses_the_nearest_grid_value(hass, plant):
    coordinator, cloud = plant
    hass.config.units = US_CUSTOMARY_SYSTEM
    entity = climate(hass, coordinator)
    # Home Assistant hands over 72 °F in the entity's unit (°C).
    await entity.async_set_temperature(temperature=(72 - 32) * 5 / 9)
    assert cloud.puts == [(TEMPORARY, 22.0)]


async def test_constants_control_type_only_shows_the_zone(hass, plant):
    coordinator, cloud = plant
    changed(coordinator, cloud, CONTROL_TYPE, value="constants")
    entity = climate(hass, coordinator)
    assert entity.available
    assert entity.hvac_modes == [HVACMode.AUTO]
    assert entity.supported_features == ClimateEntityFeature(0)
    with pytest.raises(ServiceValidationError):
        await entity.async_set_hvac_mode(HVACMode.HEAT)
    assert cloud.puts == []


async def test_concurrent_commands_run_one_after_another(hass, plant):
    import asyncio

    coordinator, cloud = plant
    entity = climate(hass, coordinator)
    results = await asyncio.gather(
        entity.async_set_temperature(temperature=22),
        entity.async_set_temperature(temperature=23),
        return_exceptions=True,
    )
    # The second compares with the reading the first published.
    assert cloud.puts[0] == (TEMPORARY, 22)
    assert len(cloud.puts) == 1 or cloud.puts[1] == (TEMPORARY, 23)
    assert sum(isinstance(r, Exception) for r in results) == 2 - len(cloud.puts)


# Cooling side ------------------------------------------------------------


async def test_cooling_season_switches_to_the_cooling_side(hass, plant):
    coordinator, cloud = plant
    cooling(coordinator, cloud)
    entity = climate(hass, coordinator)
    assert entity.hvac_mode == HVACMode.COOL
    assert entity.hvac_modes == [HVACMode.OFF, HVACMode.COOL, HVACMode.AUTO]
    assert entity.native_target_temperature == 25
    await entity.async_set_temperature(temperature=24)
    assert cloud.puts == [(f"{Z1}/cooling/manualRoomSetpoint", 24)]
    assert entity.extra_state_attributes["zone_side"] == "cooling"


async def test_cooling_season_without_a_cooling_side_is_unavailable(hass, plant):
    coordinator, cloud = plant
    cooling(coordinator, cloud, readable=False)
    entity = climate(hass, coordinator)
    assert not entity.available


async def test_without_a_circuit_the_zone_stays_on_the_heating_side(hass, plant):
    coordinator, cloud = plant
    changed(coordinator, cloud, "/devices/device1/assignedHC", value="hc2")
    entity = climate(hass, coordinator)
    assert assigned_circuit(coordinator, Z1) is None
    changed(coordinator, cloud, SEASON, value="cooling")
    assert entity.hvac_mode == HVACMode.AUTO
    assert entity.extra_state_attributes["zone_side"] == "heating"
    assert "heating_circuit" not in entity.extra_state_attributes


# The zone's heating circuit ----------------------------------------------


@pytest.mark.parametrize(
    ("assigned", "expected"),
    [
        ("hc1", "/heatingCircuits/hc1"),
        ("HC2", "/heatingCircuits/hc2"),
        ("2", "/heatingCircuits/hc2"),
        ("hc3", None),  # not a circuit of this plant
        ("none", None),
        ("", None),
    ],
)
async def test_circuit_comes_from_the_zone_s_radio_devices(
    hass, plant, assigned, expected
):
    coordinator, cloud = plant
    for device in ("device1", "device2"):  # both devices of zone 1
        changed(coordinator, cloud, f"/devices/{device}/assignedHC", value=assigned)
    assert assigned_circuit(coordinator, Z1) == expected


async def test_without_devices_a_single_circuit_is_the_zone_s(hass, plant):
    coordinator, _ = plant
    for path in [p for p in coordinator.data if p.endswith("/zoneId")]:
        del coordinator.data[path]
    assert assigned_circuit(coordinator, Z1) is None  # two circuits
    del coordinator.data["/heatingCircuits/hc2/operationMode"]
    assert assigned_circuit(coordinator, Z1) == "/heatingCircuits/hc1"


# Calculated action --------------------------------------------------------


def activity(monkeypatch, value):
    monkeypatch.setattr(
        zone_climate,
        "circuit_activity",
        lambda coordinator, root: Derivation(
            value, {f"{root}/operationMode": "cloud"} if value else {}
        ),
    )


@pytest.mark.parametrize(
    ("circuit", "room", "expected"),
    [
        ("off", 20.0, HVACAction.OFF),
        ("defrosting", 20.0, HVACAction.DEFROSTING),
        ("idle", 20.0, HVACAction.IDLE),
        ("heating", 20.0, HVACAction.HEATING),  # below the setpoint of 21
        ("heating", 21.0, HVACAction.IDLE),
        ("heating", 22.0, HVACAction.IDLE),
        ("cooling", 22.0, HVACAction.COOLING),
        ("cooling", 20.0, HVACAction.IDLE),
        (None, 20.0, None),
    ],
)
async def test_action_combines_the_circuit_and_the_room(
    hass, plant, monkeypatch, circuit, room, expected
):
    coordinator, cloud = plant
    activity(monkeypatch, circuit)
    changed(coordinator, cloud, f"{Z1}/averageCurrentTemperature", value=room)
    entity = climate(hass, coordinator)
    assert entity.hvac_action == expected
    derived = entity.extra_state_attributes.get("hvac_action_derived_from")
    assert (derived is not None) == (expected is not None)
    if expected in (HVACAction.HEATING, HVACAction.COOLING, HVACAction.IDLE) and (
        circuit in ("heating", "cooling")
    ):
        assert f"{Z1}/averageCurrentTemperature" in derived


async def test_action_is_off_in_the_mode_off_and_unknown_without_inputs(
    hass, plant, monkeypatch
):
    coordinator, cloud = plant
    activity(monkeypatch, "heating")
    entity = climate(hass, coordinator)
    changed(coordinator, cloud, f"{Z1}/averageCurrentTemperature", value=None)
    assert entity.hvac_action is None
    changed(coordinator, cloud, MODE, value="off")
    assert entity.hvac_action == HVACAction.OFF
    changed(coordinator, cloud, MODE, value="auto")
    changed(coordinator, cloud, "/devices/device1/assignedHC", value="hc2")
    assert entity.hvac_action is None  # no circuit


# Clear button -------------------------------------------------------------


def button(hass, coordinator, root=Z1) -> ZoneTemporaryClearButton:
    entity = ZoneTemporaryClearButton(coordinator, root)
    entity.hass = hass
    return entity


async def test_clear_button_ends_an_active_temporary_setpoint_once(hass, plant):
    coordinator, cloud = plant
    entity = button(hass, coordinator)
    assert not entity.available  # nothing active
    with pytest.raises(ServiceValidationError) as error:
        await entity.async_press()
    assert error.value.translation_key == "zone_temporary_setpoint_not_active"
    changed(coordinator, cloud, TEMPORARY, value=22.5)
    assert entity.available
    await entity.async_press()
    assert cloud.puts == [(TEMPORARY, -1.0)]
    assert coordinator.data[TEMPORARY].resource.value == -1.0
    assert entity.unique_id == (
        "gateway:zones:zone1:heating:temporaryRoomSetpoint:clear"
    )


async def test_clear_button_is_not_offered_in_manual_or_read_only(hass, plant):
    coordinator, cloud = plant
    changed(coordinator, cloud, TEMPORARY, value=22.5)
    changed(coordinator, cloud, MODE, value="manual")
    assert not button(hass, coordinator).available
    changed(coordinator, cloud, MODE, value="auto")
    changed(coordinator, cloud, CONTROL_TYPE, value="constants")
    assert not button(hass, coordinator).available


async def test_clear_button_descriptions_follow_the_clear_rule(plant):
    coordinator, _ = plant
    resources = {path: s.resource for path, s in coordinator.data.items()}
    context = SimpleNamespace(rrc_plant=False, heat_pump=False)
    descriptions = {
        d.key: d for d in zone_clear_button_descriptions(resources, context)
    }
    # Zone 4 is read-only, zone 10 offers no setpoint; -1 lies outside 5 to 30.
    assert set(descriptions) == {f"/zones/zone{n}" for n in (1, 2, 5, 6, 7, 8)}
    assert not any(d.entity_registry_enabled_default for d in descriptions.values())
    inside = replace(
        resources[TEMPORARY],
        metadata=replace(resources[TEMPORARY].metadata, minimum=-1.0, step_size=0.5),
    )
    (only,) = zone_clear_button_descriptions(
        {TEMPORARY: inside, MODE: resources[MODE]}, context
    )
    assert only.entity_registry_enabled_default
    rrc = SimpleNamespace(rrc_plant=True, heat_pump=False)
    assert zone_clear_button_descriptions(resources, rrc) == ()


# Policy, number entities and diagnostics ----------------------------------


def test_temporary_setpoint_has_a_policy_but_no_number_entity():
    temporary = RESOURCES[TEMPORARY]
    assessment = assess_control(temporary)
    assert assessment.policy is ZONE_TEMPORARY_SETPOINT_POLICY
    assert assessment.eligible
    assert not build_number_descriptions({TEMPORARY: temporary})
    assert _control_diagnostics(temporary)["eligible"]


def test_unused_temporary_setpoint_has_no_control():
    temporary = RESOURCES[TEMPORARY]
    unavailable = replace(
        temporary, metadata=replace(temporary.metadata, available=False)
    )
    assert not assess_control(unavailable).eligible


async def test_diagnostics_show_each_zone_without_values(hass, plant):
    coordinator, _ = plant
    report = zone_climate_diagnostics(coordinator)
    assert [entry["zone"] for entry in report] == [
        f"zone{n}" for n in (1, 2, 4, 5, 6, 7, 8, 10)
    ]
    first = report[0]
    assert first["available"] and first["mode_writable"] and first["target_writable"]
    assert first["heating_circuit_assigned"] and first["rejection_reason"] is None
    zone4 = report[2]
    assert zone4["rejection_reason"] == "mode_not_writable"
    text = json.dumps(report)
    assert "Zone 1" not in text and "21.0" not in text
