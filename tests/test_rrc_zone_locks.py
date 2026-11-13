"""ZONE deep review: locks, the shown target and the window of RRC zones.

Real setups and cycles against the fake cloud with the synthetic
``zone_climate.json`` and patched readings in the shapes of the vendor demo
data. Every behaviour here is expected, untested on hardware:

- ZONE-02: the active away mode locks the setpoints, not the mode.
- ZONE-05: a zone or communication error, or a zone in standby, locks the
  mode and the setpoints.
- ZONE-04, ZONE-08: the window from the zone list; the active setpoint shown.
- ZONE-01, ZONE-03, ZONE-07, ZONE-09 to ZONE-12, ZONE-15.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
from dataclasses import replace
from typing import Any

import pytest
from homeassistant.components.climate.const import ClimateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating import rrc_contract_zone
from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.pointt import Resource
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.poll_group import PollGroup
from custom_components.bosch_buderus_heating.resource_catalog import poll_group
from tests.helpers.rrc_cloud import FIXTURES, with_containers
from tests.test_rrc_zone_climate import (
    MANUAL,
    MODE,
    OVERRIDE,
    ZoneCloud,
    climate,
    entity_id,
    legacy_setup_path,  # noqa: F401  (autouse fixture)
    path,
    refresh,
    registered,
    set_mode,
    set_temperature,
    setup,
    teardown,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")
AWAY = "/system/awayMode/enabled"
ZONE_LIST = "/zones/list"
SYSTEM_WINDOW = "/system/openWindowDetection/enabled"


def tree(
    patch: dict[str, dict[str, Any]] | None = None,
    extra: list[dict[str, Any]] | None = None,
) -> dict[str, Resource]:
    data = json.loads((FIXTURES / "rrc/zone_climate.json").read_text("utf-8"))
    items = []
    for item in data["resources"]:
        if patch and item["id"] in patch:
            item = {**item, **patch[item["id"]]}
            item = {key: value for key, value in item.items() if value is not None}
        items.append(item)
    items.extend(extra or [])
    return with_containers({item["id"]: parse_resource(item) for item in items})


def cloud(patch=None, extra=None) -> ZoneCloud:
    return ZoneCloud(tree(patch, extra), forbidden=set())


def away(value: str) -> dict[str, Any]:
    return {
        "id": AWAY,
        "writeable": 1,
        "recordable": 0,
        "type": "stringValue",
        "value": value,
        "used": "true",
    }


def zone_list(functions: dict[int, str]) -> dict[str, dict[str, Any]]:
    """The fixture's zone list with ``functionIcons`` for some zones."""
    data = json.loads((FIXTURES / "rrc/zone_climate.json").read_text("utf-8"))
    (listed,) = [item for item in data["resources"] if item["id"] == ZONE_LIST]
    value = [
        {**entry, "functionIcons": functions[entry["id"]]}
        if entry["id"] in functions
        else entry
        for entry in listed["value"]
    ]
    return {ZONE_LIST: {"value": value}}


async def press(hass: HomeAssistant, button: str) -> None:
    await hass.services.async_call(
        "button", "press", {"entity_id": button}, blocking=True
    )


async def set_number(hass: HomeAssistant, target: str, value: float) -> None:
    await hass.services.async_call(
        "number", "set_value", {"entity_id": target, "value": value}, blocking=True
    )


async def select(hass: HomeAssistant, target: str, option: str) -> None:
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": target, "option": option},
        blocking=True,
    )


def clear(hass: HomeAssistant, zone: int) -> str:
    return entity_id(
        hass,
        "button",
        f"gateway-one:zones:zn{zone}:clockOverride:temperatureHeating:clear",
    )


def manual_number(hass: HomeAssistant, zone: int) -> str:
    return entity_id(
        hass, "number", f"gateway-one:zones:zn{zone}:manualTemperatureHeating:control"
    )


def mode_select(hass: HomeAssistant, zone: int) -> str:
    return entity_id(hass, "select", f"gateway-one:zones:zn{zone}:userMode:control")


# ---------------------------------------------------------------- ZONE-02


async def test_active_away_mode_locks_the_setpoints_and_shows_the_active_one(
    hass: HomeAssistant,
) -> None:
    fake = cloud(
        {
            path(1, "temperatureHeatingSetpoint"): {"value": 12.0},
            path(2, "temperatureHeatingSetpoint"): {"value": 12.0},
        },
        [away("true")],
    )
    entry, coordinator, _sleeps, stack, clock = await setup(hass, fake)
    try:
        manual = hass.states.get(climate(hass, 1))
        assert manual.state == "heat"
        # ZONE-08: the active setpoint, not the manual one (21 °C).
        assert manual.attributes["temperature"] == 12.0
        assert manual.attributes["active_setpoint"] == 12.0
        assert manual.attributes["away_mode"] is True
        assert manual.attributes["control_lock"] == "away"
        # The target control stays, so a change gets the message below.
        assert manual.attributes["supported_features"] & (
            ClimateEntityFeature.TARGET_TEMPERATURE
        )
        override = hass.states.get(climate(hass, 2))
        assert override.attributes["temperature"] == 12.0  # not the 23.5 override

        for zone in (1, 2, 3):
            with pytest.raises(ServiceValidationError) as refused:
                await set_temperature(hass, climate(hass, zone), temperature=22.0)
            assert refused.value.translation_key == "rrc_zone_locked_away"
        with pytest.raises(ServiceValidationError) as refused:
            await press(hass, clear(hass, 2))
        assert refused.value.translation_key == "rrc_zone_locked_away"
        # The manual setpoint's own number is locked by its write policy.
        with pytest.raises(ServiceValidationError) as refused:
            await set_number(hass, manual_number(hass, 1), 22.0)
        assert refused.value.translation_key == "rrc_zone_locked_away"
        assert not fake.writes

        # The mode is not locked by the away mode.
        await set_mode(hass, climate(hass, 1), "auto")
        assert fake.written("PUT") == [("PUT", path(1, MODE), "clock")]

        # Away off on the controller: the next cycle unlocks the zone.
        fake.tree[AWAY] = replace(fake.tree[AWAY], value="false")
        fake.writes.clear()
        await refresh(hass, coordinator, clock, 301)
        state = hass.states.get(climate(hass, 3))
        assert state.attributes["away_mode"] is False
        assert "control_lock" not in state.attributes
        await set_temperature(hass, climate(hass, 3), temperature=22.0)
        assert fake.written("PUT") == [("PUT", path(3, OVERRIDE), 22.0)]
    finally:
        await teardown(hass, entry, stack)


async def test_away_switched_on_since_the_last_poll_sends_nothing(
    hass: HomeAssistant,
) -> None:
    """S5: the away reading is read again with the mode before the PUT."""
    fake = cloud(extra=[away("false")])
    entry, coordinator, _sleeps, stack, _clock = await setup(hass, fake)
    try:
        assert hass.states.get(climate(hass, 1)).attributes["away_mode"] is False
        fake.tree[AWAY] = replace(fake.tree[AWAY], value="true")
        with pytest.raises(ServiceValidationError):
            await set_temperature(hass, climate(hass, 1), temperature=22.0)
        assert not fake.writes
        # The fresh reading is shown at once; a retry names the away mode.
        assert coordinator.data[AWAY].resource.value == "true"
        with pytest.raises(ServiceValidationError) as refused:
            await set_temperature(hass, climate(hass, 1), temperature=22.0)
        assert refused.value.translation_key == "rrc_zone_locked_away"
        assert not fake.writes
    finally:
        await teardown(hass, entry, stack)


def test_away_reading_shapes() -> None:
    def reading(value: object) -> Resource:
        return parse_resource(
            {"id": AWAY, "type": "stringValue", "writeable": 1, "value": value}
        )

    assert rrc_contract_zone.away_active(reading("true")) is True
    assert rrc_contract_zone.away_active(reading("on")) is True
    assert rrc_contract_zone.away_active(reading("false")) is False
    assert rrc_contract_zone.away_active(reading(1)) is False
    assert rrc_contract_zone.away_active(None) is None


# ---------------------------------------------------------------- ZONE-05


async def test_zone_error_locks_mode_and_setpoint(hass: HomeAssistant) -> None:
    fake = cloud({path(1, "status"): {"value": "comm error"}})
    entry, coordinator, _sleeps, stack, clock = await setup(hass, fake)
    try:
        state = hass.states.get(climate(hass, 1))
        assert state.attributes["control_lock"] == "unreachable"
        with pytest.raises(ServiceValidationError) as refused:
            await set_temperature(hass, climate(hass, 1), temperature=22.5)
        assert refused.value.translation_key == "rrc_zone_unreachable"
        with pytest.raises(ServiceValidationError) as refused:
            await set_mode(hass, climate(hass, 1), "auto")
        assert refused.value.translation_key == "rrc_zone_unreachable"
        # The generic controls of the zone are locked by their policies.
        with pytest.raises(ServiceValidationError) as refused:
            await select(hass, mode_select(hass, 1), "clock")
        assert refused.value.translation_key == "rrc_zone_unreachable"
        with pytest.raises(ServiceValidationError):
            await set_number(hass, manual_number(hass, 1), 22.5)
        # Zone 6 (no climate) reports a zone error in the fixture.
        with pytest.raises(ServiceValidationError) as refused:
            await set_number(hass, manual_number(hass, 6), 22.5)
        assert refused.value.translation_key == "rrc_zone_unreachable"
        assert not fake.writes
        stats = coordinator.rrc_write_stats.snapshot("core")
        assert stats["rejection_reasons"]["rrc_zone_unreachable"] >= 3

        # A comm warning does not lock; the zone recovers with the next cycle.
        fake.tree[path(1, "status")] = replace(
            fake.tree[path(1, "status")], value="comm warning"
        )
        await refresh(hass, coordinator, clock, 61)
        await set_temperature(hass, climate(hass, 1), temperature=22.5)
        assert fake.written("PUT") == [("PUT", path(1, MANUAL), 22.5)]
    finally:
        await teardown(hass, entry, stack)


async def test_zone_in_standby_is_off_and_locked(hass: HomeAssistant) -> None:
    fake = cloud(zone_list({1: "standby", 3: "boost"}))
    entry, coordinator, _sleeps, stack, clock = await setup(hass, fake)
    try:
        state = hass.states.get(climate(hass, 1))
        assert state.attributes["hvac_action"] == "off"
        assert state.attributes["control_lock"] == "standby"
        with pytest.raises(ServiceValidationError) as refused:
            await set_temperature(hass, climate(hass, 1), temperature=22.5)
        assert refused.value.translation_key == "rrc_zone_standby"
        with pytest.raises(ServiceValidationError) as refused:
            await set_mode(hass, climate(hass, 1), "auto")
        assert refused.value.translation_key == "rrc_zone_standby"
        assert not fake.writes
        # A boost shows the active setpoint but locks nothing.
        boost = hass.states.get(climate(hass, 3))
        assert "control_lock" not in boost.attributes
        await set_temperature(hass, climate(hass, 3), temperature=22.0)
        assert fake.written("PUT") == [("PUT", path(3, OVERRIDE), 22.0)]

        # The zone list is read with the modes now, not hourly.
        assert coordinator._poll_group(fake.tree[ZONE_LIST]) is PollGroup.CONTROL
        listed = [
            {key: value for key, value in item.items() if key != "functionIcons"}
            | {"functionIcons": ""}
            for item in fake.tree[ZONE_LIST].value
        ]
        fake.tree[ZONE_LIST] = replace(fake.tree[ZONE_LIST], value=listed)
        await refresh(hass, coordinator, clock, 301)
        state = hass.states.get(climate(hass, 1))
        assert "control_lock" not in state.attributes
        assert state.attributes["hvac_action"] == "heating"
    finally:
        await teardown(hass, entry, stack)


def test_the_zone_list_keeps_its_hourly_group_on_other_gateways() -> None:
    listed = tree()[ZONE_LIST]
    assert poll_group(listed) is PollGroup.HOURLY
    assert poll_group(listed, rrc_plant=True) is PollGroup.CONTROL


# ---------------------------------------------------------- ZONE-04, -08


async def test_window_follows_the_zone_list_and_shows_the_active_setpoint(
    hass: HomeAssistant,
) -> None:
    fake = cloud(
        {
            **zone_list({1: "window_open", 2: "", 3: "boost", 4: ""}),
            path(1, "temperatureHeatingSetpoint"): {"value": 12.0},
        }
    )
    entry, coordinator, _sleeps, stack, clock = await setup(hass, fake)
    try:
        windows = registered(hass, entry, "binary_sensor")
        state = {
            n: hass.states.get(
                windows[f"gateway-one:zones:zn{n}:openWindowDetection:status"].entity_id
            ).state
            for n in (1, 2, 3, 4)
        }
        # 1: listed open (leaf closed); 2: no function (leaf "open");
        # 3: a boost, leaf "tilted" -> unknown; 4: no leaf, listed none.
        assert state == {1: "on", 2: "off", 3: "unknown", 4: "off"}
        assert "gateway-one:zones:zn5:openWindowDetection:status" not in windows

        manual = hass.states.get(climate(hass, 1))
        assert manual.attributes["temperature"] == 12.0
        assert "control_lock" not in manual.attributes
        # An open window locks nothing: the manual setpoint is written.
        await set_temperature(hass, climate(hass, 1), temperature=22.0)
        assert fake.written("PUT") == [("PUT", path(1, MANUAL), 22.0)]
        assert hass.states.get(climate(hass, 1)).attributes["temperature"] == 12.0

        # Window closed: the next cycle of the zone list shows the target.
        listed = [
            item | {"functionIcons": ""} if item["id"] == 1 else item
            for item in fake.tree[ZONE_LIST].value
        ]
        fake.tree[ZONE_LIST] = replace(fake.tree[ZONE_LIST], value=listed)
        await refresh(hass, coordinator, clock, 301)
        window = windows["gateway-one:zones:zn1:openWindowDetection:status"].entity_id
        assert hass.states.get(window).state == "off"
        assert hass.states.get(climate(hass, 1)).attributes["temperature"] == 22.0

        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        zone = diagnostics["gateways"][0]["rrc_features"]["contracts"]["zone"]
        assert zone["zones_with_function"] == 1  # the boost of zone 3
    finally:
        await teardown(hass, entry, stack)


def test_window_rule() -> None:
    rule = rrc_contract_zone.window_open
    assert rule("window_open", False) is True
    assert rule("", True) is False
    assert rule("boost", True) is True
    assert rule("boost", None) is None
    assert rule(None, False) is False
    assert rule(None, None) is None


def test_function_icon_needs_a_valid_list() -> None:
    def listed(**fields: Any) -> Resource:
        return parse_resource(
            {
                "id": ZONE_LIST,
                "type": "zoneArray",
                "writeable": 0,
                "value": [{"id": 1, "functionIcons": "standby"}, {"id": 2}],
            }
            | fields
        )

    icon = rrc_contract_zone.function_icon
    assert icon(listed(), "/zones/zn1") == "standby"
    assert icon(listed(), "/zones/zn2") is None
    assert icon(listed(), "/zones/zn3") is None
    assert icon(listed(available="false"), "/zones/zn1") is None
    assert icon(listed(type="programArray"), "/zones/zn1") is None
    boolean_id = [{"id": True, "functionIcons": "standby"}]
    assert icon(listed(value=boolean_id), "/zones/zn1") is None
    assert icon(None, "/zones/zn1") is None


# ---------------------------------------------------------------- ZONE-07


async def test_unused_room_temperature_keeps_the_climate(hass: HomeAssistant) -> None:
    fake = cloud({path(1, "temperatureActual"): {"used": "false"}})
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, fake)
    try:
        state = hass.states.get(climate(hass, 1))
        assert state.state == "heat"
        assert state.attributes["current_temperature"] is None
        assert state.attributes["temperature"] == 21.0
        await set_temperature(hass, climate(hass, 1), temperature=22.5)
        assert fake.written("PUT") == [("PUT", path(1, MANUAL), 22.5)]
    finally:
        await teardown(hass, entry, stack)


# ---------------------------------------------------------------- ZONE-03


@pytest.mark.parametrize(("zone", "leaf"), [(3, OVERRIDE), (1, MANUAL)])
async def test_unconfirmed_target_keeps_the_control_until_read_again(
    hass: HomeAssistant, zone: int, leaf: str
) -> None:
    fake = cloud()
    entry, coordinator, _sleeps, stack, clock = await setup(hass, fake)
    try:
        fake.ignore_writes.add(path(zone, leaf))
        with pytest.raises(HomeAssistantError) as pending:
            await set_temperature(hass, climate(hass, zone), temperature=22.5)
        assert pending.value.translation_key == "rrc_write_pending_confirmation"
        state = hass.states.get(climate(hass, zone))
        assert state.attributes["supported_features"] == (
            ClimateEntityFeature.TARGET_TEMPERATURE
        )
        assert state.attributes["min_temp"] == 5.0
        assert state.attributes["max_temp"] == 30.0
        # The active setpoint rather than no target.
        active = {1: 21.0, 3: 19.0}[zone]
        assert state.attributes["temperature"] == active
        # A change still needs the fresh reading.
        writes = len(fake.writes)
        with pytest.raises(ServiceValidationError):
            await set_temperature(hass, climate(hass, zone), temperature=23.0)
        assert len(fake.writes) == writes
        # Read again with the next cycle: current again.
        fake.ignore_writes.discard(path(zone, leaf))
        await refresh(hass, coordinator, clock, 61)
        assert coordinator.data[path(zone, leaf)].freshness.value == "fresh"
        await set_temperature(hass, climate(hass, zone), temperature=23.0)
        assert fake.written("PUT")[-1] == ("PUT", path(zone, leaf), 23.0)
    finally:
        await teardown(hass, entry, stack)


# ------------------------------------------------------------- ZONE-01


async def test_zone_window_detection_needs_the_system_one(hass: HomeAssistant) -> None:
    fake = cloud({SYSTEM_WINDOW: {"available": "false"}})
    entry, coordinator, _sleeps, stack, clock = await setup(hass, fake)
    try:
        switch = entity_id(
            hass, "switch", "gateway-one:zones:zn2:openWindowDetection:enabled:control"
        )
        with pytest.raises(ServiceValidationError) as refused:
            await hass.services.async_call(
                "switch", "turn_on", {"entity_id": switch}, blocking=True
            )
        assert refused.value.translation_key == "rrc_window_detection_unused"
        assert not fake.writes
        # Offered again: the zone switch writes.
        fake.tree[SYSTEM_WINDOW] = replace(
            fake.tree[SYSTEM_WINDOW],
            metadata=replace(fake.tree[SYSTEM_WINDOW].metadata, available=True),
        )
        await refresh(hass, coordinator, clock, 901)
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": switch}, blocking=True
        )
        assert fake.written("PUT") == [
            ("PUT", path(2, "openWindowDetection/enabled"), "on")
        ]
    finally:
        await teardown(hass, entry, stack)


# ------------------------------------------------------- ZONE-09 to -12


async def test_override_attributes_only_in_the_schedule_mode(
    hass: HomeAssistant,
) -> None:
    fake = cloud({path(1, OVERRIDE): {"value": 22.0}})
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, fake)
    try:
        manual = hass.states.get(climate(hass, 1)).attributes
        assert manual["temporary_override"] is False
        assert "override_until_next_switch_point" not in manual
        schedule = hass.states.get(climate(hass, 2)).attributes
        assert schedule["override_until_next_switch_point"] is True
    finally:
        await teardown(hass, entry, stack)


async def test_no_clear_button_when_zero_is_below_the_minimum(
    hass: HomeAssistant,
) -> None:
    fake = cloud({path(2, OVERRIDE): {"minValue": 5.0}})
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, fake)
    try:
        buttons = registered(hass, entry, "button")
        assert (
            "gateway-one:zones:zn2:clockOverride:temperatureHeating:clear"
            not in buttons
        )
        assert "gateway-one:zones:zn3:clockOverride:temperatureHeating:clear" in buttons
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        zone = diagnostics["gateways"][0]["rrc_features"]["contracts"]["zone"]
        assert zone["override_clear_unsupported"] == 1
    finally:
        await teardown(hass, entry, stack)


async def test_clear_button_of_an_existing_entity_goes_unavailable(
    hass: HomeAssistant,
) -> None:
    fake = cloud()
    entry, coordinator, _sleeps, stack, clock = await setup(hass, fake)
    try:
        button = clear(hass, 2)
        assert hass.states.get(button).state != "unavailable"
        item = fake.tree[path(2, OVERRIDE)]
        fake.tree[path(2, OVERRIDE)] = replace(
            item, metadata=replace(item.metadata, minimum=5.0)
        )
        await refresh(hass, coordinator, clock, 301)
        assert hass.states.get(button).state == "unavailable"
        # Home Assistant skips an unavailable entity; nothing is sent.
        await press(hass, button)
        assert not fake.writes
    finally:
        await teardown(hass, entry, stack)


async def test_locked_mode_without_write_permission_names_the_lock(
    hass: HomeAssistant,
) -> None:
    fake = cloud({path(4, MODE): {"writeable": 0}})
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, fake)
    try:
        with pytest.raises(ServiceValidationError) as refused:
            await set_mode(hass, climate(hass, 4), "heat")
        assert refused.value.translation_key == "rrc_mode_locked"
        assert not fake.writes
    finally:
        await teardown(hass, entry, stack)


async def test_unused_manual_setpoint_keeps_the_temporary_one(
    hass: HomeAssistant,
) -> None:
    fake = cloud({path(3, MANUAL): {"used": "false"}})
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, fake)
    try:
        state = hass.states.get(climate(hass, 3))
        assert state.attributes["supported_features"] == (
            ClimateEntityFeature.TARGET_TEMPERATURE
        )
        assert state.attributes["min_temp"] == 5.0
        await set_temperature(hass, climate(hass, 3), temperature=22.0)
        assert fake.written("PUT") == [("PUT", path(3, OVERRIDE), 22.0)]
        # Still never below the manual minimum.
        with pytest.raises(ServiceValidationError):
            await set_temperature(hass, climate(hass, 3), temperature=4.5)
    finally:
        await teardown(hass, entry, stack)


# ---------------------------------------------------------------- ZONE-15


async def test_override_before_value_is_kept_per_write() -> None:
    """Two writes of the same path and value interleaved (two gateways):
    each read-back sees the value before its own write."""
    policy = rrc_contract_zone.OVERRIDE_POLICY
    assert policy.value_check is not None and policy.read_back_check is not None
    resources = tree()
    mode = replace(resources[path(2, MODE)], value="clock")
    related = {path(2, MODE): mode, path(2, MANUAL): resources[path(2, MANUAL)]}
    active = replace(resources[path(2, OVERRIDE)], value=23.5)
    idle = replace(active, value=0.0)
    checked = asyncio.Event()
    results: dict[str, str | None] = {}

    async def write(name: str, before: Resource, wait: bool) -> None:
        assert policy.value_check(before, 22.0, related) is None
        if wait:
            checked.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        else:
            await checked.wait()
        results[name] = policy.read_back_check(idle, 22.0)

    context = contextvars.copy_context()
    await asyncio.gather(
        asyncio.create_task(write("replaced", active, True), context=context.copy()),
        asyncio.create_task(write("none_active", idle, False), context=context.copy()),
    )
    assert results == {"replaced": "rrc_override_ended", "none_active": None}


# ---------------------------------------------------------------- ZONE-14


def test_diagnostics_judge_zone_details_by_their_own_rules() -> None:
    from custom_components.bosch_buderus_heating.diagnostics import (
        _capability_diagnostics,
    )

    resources = tree()
    status = resources[path(1, "status")]
    unknown = resources[path(3, "status")]  # "defrosting"
    nothing = resources[path(1, "nextSetpoint")]  # 999, not available
    temperature = resources[path(1, "temperatureActual")]

    def valid(resource: Resource) -> object:
        return _capability_diagnostics(resource, None, {}, 0)["rrc_contract"][
            "reading_valid"
        ]

    assert valid(status) is True
    assert valid(unknown) is False
    assert valid(nothing) is False
    assert valid(temperature) is True
