"""alpha.6 follow-ups of the room controller feedback (R2, Z3).

- T2: one read of the readings a confirmed zone write affects, about five
  seconds later, one timer for a series of writes, through the regular
  cycle (lock, brake, pauses), cancelled on unload.
- Z3 (ZONE-06): "advance next switch point" per zone.
- T3, T5, T7, T8, E17: value-free diagnostics details.

Real setups and cycles against the fake cloud where it matters; every
behaviour is expected, untested on hardware.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.bosch_buderus_heating import (
    rrc_boost,
    rrc_confirmations,
    rrc_contract_zone,
    start_counters,
)
from custom_components.bosch_buderus_heating.diagnostics import (
    _capability_diagnostics,
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from tests.ha_test_helpers import async_fire_time_changed
from tests.test_rrc_zone_climate import (
    OVERRIDE,
    climate,
    entity_id,
    legacy_setup_path,  # noqa: F401  (autouse fixture)
    path,
    set_temperature,
    setup,
    teardown,
    zone_cloud,
    zone_tree,
)
from tests.test_start_counters import coordinator_for

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")
Z = rrc_contract_zone


def _later(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))


def _follow_ups(coordinator, zone: int) -> set[str]:
    return {
        item
        for item in Z.follow_up_paths(path(zone, OVERRIDE))
        if item in coordinator.resources
    }


# ------------------------------------------------------------------ T2
def test_follow_up_paths_belong_to_the_zone() -> None:
    assert Z.follow_up_paths("/zones/zn3/userMode") == (
        "/zones/zn3/temperatureHeatingSetpoint",
        "/zones/zn3/status",
        "/zones/zn3/nextSetpoint",
        "/zones/zn3/timeToNextSetpoint",
        "/zones/list",
    )
    assert Z.follow_up_paths("/dhwCircuits/dhw1/operationMode") == ()
    assert Z.follow_up_paths("/system/awayMode/enabled") == ()


async def test_a_confirmed_zone_write_reads_its_follow_ups_once(
    hass: HomeAssistant,
) -> None:
    """Two writes of a series: one timer, after about five seconds one
    request with the follow-up readings; the new active setpoint shows
    then, not a cycle later. Nothing is written again."""
    cloud = zone_cloud()
    entry, coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        follow_ups = _follow_ups(coordinator, 3)
        assert len(follow_ups) == 5
        await set_temperature(hass, climate(hass, 3), temperature=22.0)
        first = coordinator._zone_follow_up_unsub
        assert first is not None
        await set_temperature(hass, climate(hass, 3), temperature=22.5)
        # The series moves the one timer.
        assert coordinator._zone_follow_up_unsub is not None
        assert coordinator._zone_follow_up_unsub is not first
        # The controller derives the active setpoint from the change.
        cloud._apply(path(3, "temperatureHeatingSetpoint"), 22.5)
        before = len(cloud.requests)
        _later(hass, 3)
        await hass.async_block_till_done()
        assert len(cloud.requests) == before
        _later(hass, 6)
        await hass.async_block_till_done()
        new = cloud.requests[before:]
        assert len(new) == 1
        assert follow_ups <= set(new[0])
        assert coordinator._zone_follow_up_unsub is None
        state = hass.states.get(climate(hass, 3))
        assert state.attributes["active_setpoint"] == 22.5
        # Once: no further request without another write.
        _later(hass, 30)
        await hass.async_block_till_done()
        assert len(cloud.requests) == before + 1
        assert cloud.written("PUT") == [
            ("PUT", path(3, OVERRIDE), 22.0),
            ("PUT", path(3, OVERRIDE), 22.5),
        ]
    finally:
        await teardown(hass, entry, stack)


async def test_each_write_of_a_series_moves_the_one_timer(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The earlier timer is cancelled when the next write of the series is
    confirmed, so the read comes about five seconds after the last one."""
    cloud = zone_cloud()
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    timers: list[tuple[float, list[int]]] = []

    def call_later(_hass, delay, _action):
        cancelled: list[int] = []
        timers.append((delay, cancelled))
        return lambda: cancelled.append(1)

    monkeypatch.setattr(
        "custom_components.bosch_buderus_heating.coordinator.async_call_later",
        call_later,
    )
    try:
        await set_temperature(hass, climate(hass, 3), temperature=22.0)
        await set_temperature(hass, climate(hass, 3), temperature=22.5)
        assert [delay for delay, _ in timers] == [5.0, 5.0]
        assert timers[0][1] == [1]
        assert timers[1][1] == []
    finally:
        await teardown(hass, entry, stack)
    assert timers[1][1] == [1]


async def test_an_unconfirmed_write_arms_no_follow_up(hass: HomeAssistant) -> None:
    """Only a confirmed write; an unconfirmed one is read with the next
    cycle as before (ZONE-03)."""
    cloud = zone_cloud()
    entry, coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        cloud.ignore_writes.add(path(3, OVERRIDE))
        with pytest.raises(HomeAssistantError):
            await set_temperature(hass, climate(hass, 3), temperature=22.0)
        assert coordinator._zone_follow_up_unsub is None
        assert coordinator._zone_follow_up == set()
    finally:
        await teardown(hass, entry, stack)


async def test_the_follow_up_waits_for_the_protection_pause(
    hass: HomeAssistant,
) -> None:
    """A pause of the cloud reads (here the breaker) holds the follow-up
    read; its readings wait for the next working cycle."""
    cloud = zone_cloud()
    entry, coordinator, _sleeps, stack, clock = await setup(hass, cloud)
    try:
        await set_temperature(hass, climate(hass, 3), temperature=22.0)
        coordinator._circuit_open_until = clock() + 600
        before = len(cloud.requests)
        _later(hass, 6)
        await hass.async_block_till_done()
        assert len(cloud.requests) == before
        assert _follow_ups(coordinator, 3) <= coordinator._urgent_reads
    finally:
        await teardown(hass, entry, stack)


async def test_the_follow_up_timer_ends_with_the_entry(hass: HomeAssistant) -> None:
    cloud = zone_cloud()
    entry, coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        await set_temperature(hass, climate(hass, 3), temperature=22.0)
        assert coordinator._zone_follow_up_unsub is not None
    finally:
        await teardown(hass, entry, stack)
    assert coordinator._zone_follow_up_unsub is None
    before = len(cloud.requests)
    _later(hass, 6)
    await hass.async_block_till_done()
    assert len(cloud.requests) == before


# --------------------------------------------------------------- Z3
def advance_button(hass: HomeAssistant, zone: int) -> str:
    return entity_id(
        hass,
        "button",
        f"gateway-one:zones:zn{zone}:clockOverride:temperatureHeating:advance",
    )


async def press(hass: HomeAssistant, button: str) -> None:
    await hass.services.async_call(
        "button", "press", {"entity_id": button}, blocking=True
    )


async def test_advance_sets_the_next_setpoint_as_temporary_setpoint(
    hass: HomeAssistant,
) -> None:
    """ZONE-06: in the mode clock the button writes the next setpoint to
    the temporary setpoint, once, and the attribute tells it."""
    cloud = zone_cloud()
    entry, coordinator, sleeps, stack, _clock = await setup(hass, cloud)
    try:
        button = advance_button(hass, 3)
        state = hass.states.get(button)
        assert state.state != "unavailable"
        assert state.attributes["next_setpoint_advanced"] is False
        await press(hass, button)
        assert cloud.written("PUT") == [("PUT", path(3, OVERRIDE), 21.0)]
        assert sleeps == [1.0]
        zone = hass.states.get(climate(hass, 3))
        assert zone.attributes["temperature"] == 21.0
        assert zone.attributes["temporary_override"] is True
        assert zone.attributes["next_setpoint_advanced"] is True
        assert hass.states.get(button).attributes["next_setpoint_advanced"] is True
        # A temporary setpoint already active is replaced as well.
        await press(hass, advance_button(hass, 2))
        assert cloud.written("PUT")[-1] == ("PUT", path(2, OVERRIDE), 18.0)
        assert coordinator.rrc_write_stats.snapshot("zone")["writes_confirmed"] == 2
    finally:
        await teardown(hass, entry, stack)


async def test_advance_is_offered_only_with_a_usable_next_setpoint(
    hass: HomeAssistant,
) -> None:
    """Unavailable and refused unsent: a valid next setpoint in the mode
    manual (zone 1), no next setpoint (999, zone 2) and a next setpoint off
    the temporary setpoint's grid (zone 3)."""
    tree = zone_tree()
    next_1 = path(1, Z.NEXT_SETPOINT)
    tree[next_1] = replace(
        tree[next_1],
        value=20.0,
        metadata=replace(tree[next_1].metadata, used=True, available=None),
    )
    next_2 = path(2, Z.NEXT_SETPOINT)
    tree[next_2] = replace(tree[next_2], value=Z.NO_NEXT_SETPOINT)
    next_3 = path(3, Z.NEXT_SETPOINT)
    tree[next_3] = replace(tree[next_3], value=21.3)
    cloud = zone_cloud(tree)
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        component = hass.data["entity_components"]["button"]
        for zone in (1, 2, 3):
            button = advance_button(hass, zone)
            assert hass.states.get(button).state == "unavailable", zone
            # HA skips the unavailable entity; its own check refuses as well.
            await press(hass, button)
            with pytest.raises(ServiceValidationError) as refused:
                await component.get_entity(button).async_press()
            assert refused.value.translation_key == "rrc_advance_not_offered"
        assert cloud.written() == []
    finally:
        await teardown(hass, entry, stack)


async def test_a_next_setpoint_changed_on_the_gateway_sends_nothing(
    hass: HomeAssistant,
) -> None:
    """The next setpoint is read again with the mode before the PUT; a
    switch point reached since the last poll refuses the press."""
    cloud = zone_cloud()
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        cloud._apply(path(3, Z.NEXT_SETPOINT), 17.0)
        with pytest.raises(ServiceValidationError) as changed:
            await press(hass, advance_button(hass, 3))
        assert changed.value.translation_key == "write_validation_failed"
        assert cloud.written() == []
        await hass.async_block_till_done()
        # The new reading is taken over; the next press sends it.
        await press(hass, advance_button(hass, 3))
        assert cloud.written("PUT") == [("PUT", path(3, OVERRIDE), 17.0)]
    finally:
        await teardown(hass, entry, stack)


def test_advance_value_rules() -> None:
    override = zone_tree()[path(3, OVERRIDE)]
    assert Z.advance_value(override, 21.0, 5.0) == 21.0
    assert Z.advance_value(override, None, 5.0) is None
    assert Z.advance_value(override, 0.0, None) is None  # 0 clears
    assert Z.advance_value(override, 4.5, 5.0) is None  # below the manual minimum
    assert Z.advance_value(override, 30.5, 5.0) is None  # above the maximum
    assert Z.advance_value(override, 21.25, 5.0) is None  # off the grid
    coarse = replace(override, metadata=replace(override.metadata, step_size=1.0))
    assert Z.advance_value(coarse, 21.5, 5.0) is None
    assert Z.advance_value(coarse, 21.0, None) == 21.0
    assert Z.advanced(21.0, 21.0)
    assert not Z.advanced(0.0, 0.0)
    assert not Z.advanced(21.5, 21.0)
    assert not Z.advanced(None, 21.0)


# ------------------------------------------------------- diagnostics
def test_overrides_count_only_in_the_mode_clock() -> None:
    """T7: a temporary setpoint in the mode manual does not act."""
    tree = zone_tree()
    assert Z._diagnostics(tree)["overrides_active"] == 1
    tree[path(1, OVERRIDE)] = replace(tree[path(1, OVERRIDE)], value=22.0)
    assert tree[path(1, "userMode")].value == "manual"
    assert Z._diagnostics(tree)["overrides_active"] == 1
    tree[path(3, OVERRIDE)] = replace(tree[path(3, OVERRIDE)], value=22.0)
    assert Z._diagnostics(tree)["overrides_active"] == 2


async def test_room_controller_diagnostics_point_and_confirm(
    hass: HomeAssistant,
) -> None:
    """T8: the holiday block points to the absence report; E17: only the
    functions confirmed on a TC100.2 are named as confirmed."""
    cloud = zone_cloud()
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        gateway = diagnostics["gateways"][0]
        assert (
            gateway["holidays"]["room_controller_absence"]
            == "rrc_features.contracts.absence"
        )
        features = gateway["rrc_features"]
        assert features["confirmation"] == "partly_confirmed"
        zone = features["contracts"]["zone"]
        assert zone["confirmation"] == "partly_confirmed"
        assert zone["confirmed_functions"] == [
            "temporary_setpoint",
            "zone_window_detection",
        ]
        assert zone["confirmed_on"] == "tc100_2"
        assert features["contracts"]["circuit"]["confirmation"] == "unconfirmed"
        assert "confirmed_functions" not in features["contracts"]["circuit"]
        core = features["contracts"]["core"]
        assert core["confirmed_functions"] == [
            "manual_setpoint",
            "mode_with_target",
            "program_selection",
            "zone_mode",
        ]
    finally:
        await teardown(hass, entry, stack)


def test_confirmation_report() -> None:
    assert rrc_confirmations.report("devices") == {"confirmation": "unconfirmed"}
    assert rrc_confirmations.report("absence") == {
        "confirmation": "partly_confirmed",
        "confirmed_functions": ["absence_calendar_day", "away_mode", "boost_on_off"],
        "confirmed_on": "tc100_2",
    }
    assert rrc_confirmations.overall() == "partly_confirmed"


SOURCE = "/heatSources/hs1/numberOfStarts"


@pytest.mark.parametrize(
    ("fields", "form"),
    [
        ({"values": [42]}, {"value": "null", "values": ["int"]}),
        (
            {"value": [{"total": 12, "ch": 3}, {"dhw": 4}], "values": []},
            {"value": "list[2]<dict{ch:int,total:int}|dict{dhw:int}>", "values": []},
        ),
        (
            {"value": "777", "values": [["x", 9]]},
            {"value": "str", "values": ["list[2]<int|str>"]},
        ),
    ],
)
def test_an_invalid_counter_structure_is_described_without_values(
    hass: HomeAssistant, fields: dict, form: dict
) -> None:
    """T3: types, list lengths and key names only; no count or text."""
    resource = parse_resource(
        {"id": SOURCE, "type": "emonValue", "values": [], **fields}
    )
    summary = start_counters.structure_summary(resource)
    assert summary is not None
    assert summary["reason"] == "item_not_object"
    assert summary["value"] == form["value"]
    assert summary["values"] == form["values"]
    _entry, coordinator = coordinator_for(hass, resource)
    snapshot = coordinator.data[SOURCE]
    diagnostic = _capability_diagnostics(resource, snapshot, {}, 0)["start_counter"]
    assert diagnostic["structure"] == summary
    text = json.dumps(diagnostic)
    for value in ("777", '"x"', "12", "42"):
        assert value not in text


def test_a_valid_counter_has_no_structure_report(hass: HomeAssistant) -> None:
    resource = parse_resource(
        {"id": SOURCE, "type": "emonValue", "values": [{"total": 42}]}
    )
    assert start_counters.structure_summary(resource) is None
    _entry, coordinator = coordinator_for(hass, resource)
    diagnostic = _capability_diagnostics(resource, coordinator.data[SOURCE], {}, 0)
    assert "structure" not in diagnostic["start_counter"]


def test_unusual_keys_are_masked() -> None:
    resource = parse_resource(
        {
            "id": SOURCE,
            "type": "emonValue",
            "value": [{"1234567890": 1, "a" * 30: 2, "ok_key": 3}],
            "values": [],
        }
    )
    summary = start_counters.structure_summary(resource)
    assert summary is not None
    assert summary["value"] == "list[1]<dict{?,?,ok_key:int}>"


def _limit(name: str, **fields: object):
    return parse_resource(
        {
            "id": f"/heatingCircuits/hc1/{name}",
            "type": "floatValue",
            "writeable": 1,
            **fields,
        }
    )


@pytest.mark.parametrize(
    ("setting", "fields", "issue"),
    [
        ("temperature", {"unitOfMeasure": "C", "minValue": 15, "maxValue": 30}, None),
        ("temperature", {"unitOfMeasure": "K", "minValue": 15, "maxValue": 30}, "unit"),
        (
            "temperature",
            {"unitOfMeasure": "C", "minValue": 30, "maxValue": 15},
            "range",
        ),
        ("temperature", {"unitOfMeasure": "C", "minValue": 15}, "range"),
        (
            "temperature",
            {"unitOfMeasure": "C", "minValue": 15, "maxValue": 30, "used": "false"},
            "flags",
        ),
        (
            "temperature",
            {"unitOfMeasure": "C", "minValue": 15, "maxValue": 16, "stepSize": 5},
            "step",
        ),
        ("duration", {"unitOfMeasure": "h", "minValue": 1, "maxValue": 8}, None),
        (
            "duration",
            {"unitOfMeasure": "h", "minValue": 1, "maxValue": 8, "stepSize": 0.5},
            "step",
        ),
        ("duration", {"unitOfMeasure": "h", "minValue": 0.5, "maxValue": 8}, "range"),
        ("duration", {"unitOfMeasure": "min", "minValue": 1, "maxValue": 8}, "unit"),
    ],
)
def test_boost_limits_name_why_they_are_refused(
    setting: rrc_boost.Setting, fields: dict, issue: str | None
) -> None:
    """T5: the reason token; the check itself is the one of ``limits``."""
    name = "boostTemperature" if setting == "temperature" else "boostDuration"
    resource = _limit(name, **fields)
    assert rrc_boost.limits_issue(resource, setting) == issue
    assert (rrc_boost.limits(resource, setting) is None) is (issue is not None)


def test_boost_limits_without_a_reading_or_of_another_type() -> None:
    assert rrc_boost.limits_issue(None, "temperature") == "missing"
    other = _limit("boostTemperature", type="stringValue", value="x")
    assert rrc_boost.limits_issue(other, "temperature") == "type"
    report = rrc_boost.diagnostics(
        {
            rrc_boost.BOOST_PATH: parse_resource(
                {"id": rrc_boost.BOOST_PATH, "type": "boostShortcutStruct"}
            ),
            rrc_boost.BOOST_DURATION_PATH: _limit(
                "boostDuration", unitOfMeasure="h", minValue=1, maxValue=8
            ),
        }
    )
    assert report["boost_temperature_limits_issue"] == "missing"
    assert report["boost_duration_limits_issue"] is None
    assert report["boost_duration_limits"] is True
