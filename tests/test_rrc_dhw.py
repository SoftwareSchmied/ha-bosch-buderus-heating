"""W3: hot water of RRC 2.0 room controllers.

Pure checks of the contract, the program parser and the controller clock,
then real setups of ``ct200-shapes.json`` with the synthetic hot water
circuits of ``dhw.json`` against the fake cloud of
``tests/helpers/rrc_cloud.py``: entities, exact wire values, one write each,
the read-back of "Off" as "off" and the failure paths in real cycles.
Everything is expected from data shapes and untested on a real controller.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import (
    rrc_contract_dhw,
    rrc_contracts,
    rrc_dhw,
    rrc_dhw_entities,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import Freshness
from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    Resource,
    ResourceReference,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.rrc_energy import legacy_meaning
from custom_components.bosch_buderus_heating.write_policies import (
    RRC_HEAT_PUMP_CONTEXT,
)
from custom_components.bosch_buderus_heating.writes import (
    DHW_OPERATION_MODE_POLICY,
    GENERIC_CONTEXT,
    RRC_CONTEXT,
    ContractRejected,
    ControlLocked,
    assess_control,
    validate_control_value,
)
from tests.helpers.rrc_cloud import Clock, RrcCloud, cloud_patches, load_tree
from tests.helpers.rrc_w0_state import w0_state
from tests.test_entry import _entry

PACKAGE = "custom_components.bosch_buderus_heating"
RRC_GATEWAY = Gateway("gateway-one", device_type="rrc2")
DHW1 = "/dhwCircuits/dhw1"
MODE = f"{DHW1}/operationMode"
HIGH = f"{DHW1}/temperatureLevels/high"
EXTRA = f"{DHW1}/extraDhw"
DURATION = f"{DHW1}/extraDhwDuration"
DISINFECTION = f"{DHW1}/thermalDisinfect/state"
DAY = f"{DHW1}/thermalDisinfect/weekDay"
TIME = f"{DHW1}/thermalDisinfect/time"
RESULT = f"{DHW1}/thermalDisinfect/lastResult"
PROGRAM = f"{DHW1}/programs/pg1/week"
MARKER = "PRIVATE-MARKER"

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture(autouse=True)
def legacy_setup_path():
    """Reach the format 3 setup without migrating, as test_startup_inventory."""
    with patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)):
        yield


def tree() -> dict[str, Resource]:
    """The CT200-class controller with the hot water circuits of dhw.json;
    the gateway also lists the controller clock."""
    resources = {**load_tree("rrc/ct200-shapes.json"), **load_tree("rrc/dhw.json")}
    gateway = resources["/gateway"]
    resources["/gateway"] = replace(
        gateway,
        references=(*gateway.references, ResourceReference(rrc_dhw.CLOCK_PATH)),
    )
    return resources


def item(**payload: Any) -> Resource:
    return parse_resource(payload)


def fixture(path: str) -> Resource:
    return tree()[path]


# ------------------------------------------------------------ contract


def test_contract_paths_have_no_conflicts() -> None:
    rrc_contracts.registry().check_paths(tree())
    assert rrc_contracts.contract_for(MODE) == "dhw"
    assert rrc_contracts.contract_for(TIME) == "dhw"


def test_mode_without_options_falls_back_only_on_a_room_controller() -> None:
    """S11: the RRC variant needs the RRC context; the generic policy keeps
    its strict rule (no advertised options, no control)."""
    mode = fixture(MODE)
    assert mode.metadata.allowed_values_missing
    rrc = assess_control(mode, RRC_CONTEXT)
    assert rrc.policy is rrc_contract_dhw.OPERATION_MODE_POLICY
    assert rrc.eligible and rrc.options == ("Off", "high", "ownprogram")
    generic = assess_control(mode, GENERIC_CONTEXT)
    assert generic.policy is DHW_OPERATION_MODE_POLICY
    assert generic.rejection_reason == "missing_options"


HEAT_PUMP_MODES = ("Off", "high", "ownprogram", "low", "eco")


@pytest.mark.parametrize(
    ("value", "options"),
    [
        ("off", ("Off", "high", "ownprogram")),
        ("Off", ("Off", "high", "ownprogram")),
        ("high", ("Off", "high", "ownprogram")),
        ("ownprogram", ("Off", "high", "ownprogram")),
        # CIRCUIT-02: a controller in an eco level keeps its control, with
        # the modes of a heat pump.
        ("low", HEAT_PUMP_MODES),
        ("eco", HEAT_PUMP_MODES),
    ],
)
def test_low_and_eco_are_offered_when_advertised_or_current(
    value: str, options: tuple[str, ...]
) -> None:
    """Heat pump and boiler mean different things by low and eco: without
    advertised options they are offered only while the controller reads one
    of them."""
    mode = replace(fixture(MODE), value=value)
    assessment = assess_control(mode, RRC_CONTEXT)
    assert assessment.eligible, assessment.rejection_reason
    assert assessment.current_value_issue is None
    assert assessment.options == options
    validate_control_value(mode, "high", RRC_CONTEXT)
    if "low" not in options:
        with pytest.raises(WriteValidationError):
            validate_control_value(mode, "low", RRC_CONTEXT)
    advertised = fixture("/dhwCircuits/dhw4/operationMode")
    assert set(assess_control(advertised, RRC_CONTEXT).options) == {
        "Off",
        "low",
        "high",
        "ownprogram",
        "eco",
    }


@pytest.mark.parametrize("value", ["high", "low", "Off"])
def test_an_empty_list_of_modes_leaves_a_reading(value: str) -> None:
    """An advertised empty list is no missing key: no control, nothing is
    guessed (the water heater shows the mode without a list)."""
    mode = parse_resource(
        {
            "id": MODE,
            "type": "stringValue",
            "writeable": 1,
            "value": value,
            "allowedValues": [],
        }
    )
    assert not mode.metadata.allowed_values_missing
    assessment = assess_control(mode, RRC_CONTEXT)
    assert assessment.rejection_reason == "missing_options"
    assert assessment.options == ()


def test_both_spellings_of_off_are_offered_once() -> None:
    """CIRCUIT-14: "Off" and "off" advertised together are one option."""
    from custom_components.bosch_buderus_heating.select import _option_map

    mode = parse_resource(
        {
            "id": MODE,
            "type": "stringValue",
            "writeable": 1,
            "value": "off",
            "allowedValues": ["Off", "off", "high"],
        }
    )
    options = _option_map(
        mode,
        "hot_water_operation_mode",
        rrc_contract_dhw.OPERATION_MODE_POLICY,
        RRC_CONTEXT,
    )
    assert options == {"off": "Off", "high": "high"}


def test_off_is_confirmed_by_either_spelling() -> None:
    policy = rrc_contract_dhw.OPERATION_MODE_POLICY
    assert policy.confirms("off", "Off")
    assert policy.confirms("Off", "off")
    assert not policy.confirms("high", "Off")
    assert not DHW_OPERATION_MODE_POLICY.confirms("off", "Off")


def test_unavailable_extra_hot_water_has_no_control_and_unused_one_is_locked() -> None:
    """P41 (CIRCUIT-05): not available, used or not, no control; available
    but not used, a control that refuses to write; malformed flags, none."""
    absent = fixture("/dhwCircuits/dhw2/extraDhw")
    assert assess_control(absent, RRC_CONTEXT).rejection_reason == "rrc_unavailable"
    duration = fixture("/dhwCircuits/dhw2/extraDhwDuration")
    assert assess_control(duration, RRC_CONTEXT).rejection_reason == "rrc_unavailable"
    hidden = fixture("/dhwCircuits/dhw3/extraDhw")
    assert assess_control(hidden, RRC_CONTEXT).rejection_reason == "rrc_unavailable"
    unused = replace(
        fixture(MODE),
        metadata=replace(fixture(MODE).metadata, used=False, available=True),
    )
    assert assess_control(unused, RRC_CONTEXT).eligible
    with pytest.raises(ControlLocked):
        validate_control_value(unused, "high", RRC_CONTEXT)
    malformed = item(
        id=EXTRA, type="stringValue", writeable=1, value="off", available="no"
    )
    assert assess_control(malformed, RRC_CONTEXT).rejection_reason == "rrc_unavailable"
    # A generic policy is not affected by the contract rule.
    away = item(
        id="/system/awayMode/enabled",
        type="stringValue",
        writeable=1,
        value="off",
        allowedValues=["on", "off"],
        available="false",
    )
    # Its generic policy (the absence contract, W2, owns the path now).
    with w0_state():
        assert assess_control(away, RRC_CONTEXT).eligible


def test_extra_hot_water_duration_is_a_whole_number_on_the_grid() -> None:
    duration = fixture(DURATION)
    assessment = assess_control(duration, RRC_CONTEXT)
    assert assessment.policy is rrc_contract_dhw.EXTRA_DURATION_POLICY
    assert assessment.ui_step == 15
    validate_control_value(duration, 90, RRC_CONTEXT)
    for refused in (20, 22.5, 0, 2895):
        with pytest.raises(WriteValidationError):
            validate_control_value(duration, refused, RRC_CONTEXT)
    hours = replace(duration, metadata=replace(duration.metadata, unit="h"))
    assert assess_control(hours, RRC_CONTEXT).rejection_reason == "unsupported_unit"
    unbounded = replace(duration, metadata=replace(duration.metadata, maximum=None))
    assert assess_control(unbounded, RRC_CONTEXT).rejection_reason == "missing_bounds"
    # Without an advertised step the contract's 15 minutes from minValue.
    stepless = replace(duration, metadata=replace(duration.metadata, step_size=None))
    validate_control_value(stepless, 30, RRC_CONTEXT)
    with pytest.raises(WriteValidationError):
        validate_control_value(stepless, 40, RRC_CONTEXT)
    # Elsewhere the path has no control at all.
    assert assess_control(duration, GENERIC_CONTEXT).policy is None


def test_comfort_level_follows_the_advertised_step_on_a_room_controller() -> None:
    high = fixture(HIGH)
    assert assess_control(high, RRC_CONTEXT).ui_step == 2
    validate_control_value(high, 46, RRC_CONTEXT)
    with pytest.raises(WriteValidationError):
        validate_control_value(high, 45, RRC_CONTEXT)
    # The generic policy (other gateways) keeps its 1 °C grid.
    validate_control_value(high, 45, GENERIC_CONTEXT)


def test_disinfection_time_needs_a_float_value_in_minutes() -> None:
    """P10: the demo's type "float" has no control (unsupported_resource_type)."""
    good = fixture(TIME)
    assessment = assess_control(good, RRC_CONTEXT)
    assert assessment.policy is rrc_contract_dhw.DISINFECTION_TIME_POLICY
    assert assessment.eligible and not assessment.enabled_by_default
    float_type = fixture("/dhwCircuits/dhw2/thermalDisinfect/time")
    assert float_type.metadata.resource_type == "float"
    assert (
        assess_control(float_type, RRC_CONTEXT).rejection_reason
        == "unsupported_resource_type"
    )
    # Seconds or hours would change the meaning of the value.
    for unit in ("s", "h"):
        other = replace(good, metadata=replace(good.metadata, unit=unit))
        assert assess_control(other, RRC_CONTEXT).rejection_reason == (
            "unsupported_unit"
        )
    # Limits up to the next midnight still allow no 24:00.
    wide = replace(good, metadata=replace(good.metadata, maximum=1440.0))
    with pytest.raises(ContractRejected) as refused:
        validate_control_value(wide, 1440, RRC_CONTEXT)
    assert refused.value.translation_key == "rrc_time_of_day_invalid"
    with pytest.raises(WriteValidationError):
        validate_control_value(good, 210.5, RRC_CONTEXT)
    stepped = replace(good, metadata=replace(good.metadata, step_size=15.0))
    with pytest.raises(WriteValidationError):
        validate_control_value(stepped, 220, RRC_CONTEXT)
    validate_control_value(stepped, 225, RRC_CONTEXT)
    # CIRCUIT-11: without an advertised step, five-minute steps.
    assert assessment.ui_step == 5.0
    validate_control_value(good, 215, RRC_CONTEXT)
    with pytest.raises(WriteValidationError):
        validate_control_value(good, 213, RRC_CONTEXT)


def _duration(value: object) -> dict[str, Resource]:
    return {DURATION: replace(fixture(DURATION), value=value)}


@pytest.mark.parametrize(
    ("related", "value", "result"),
    [
        (_duration(60), "on", None),
        (_duration(0), "on", "rrc_extra_dhw_duration_zero"),
        (_duration(-15), "on", "rrc_extra_dhw_duration_zero"),
        (_duration("60"), "on", "rrc_extra_dhw_duration_zero"),
        (_duration(True), "on", "rrc_extra_dhw_duration_zero"),
        (_duration(float("nan")), "on", "rrc_extra_dhw_duration_zero"),
        # Turning it off is never refused.
        (_duration(0), "off", None),
        # A circuit without a current duration reading keeps the switch.
        ({}, "on", None),
    ],
)
def test_extra_hot_water_needs_a_duration(
    related: dict[str, Resource], value: str, result: str | None
) -> None:
    """CIRCUIT-07: extra hot water is not turned on with a duration of 0."""
    policy = rrc_contract_dhw.EXTRA_DHW_POLICY
    assert policy.value_check is not None
    assert policy.value_check(fixture(EXTRA), value, related) == result
    assert rrc_contract_dhw.duration_path(EXTRA) == DURATION


def test_weekday_offers_the_known_days_only() -> None:
    day = fixture(DAY)
    assert assess_control(day, RRC_CONTEXT).options == rrc_contract_dhw.WEEKDAYS
    advertised = replace(
        day,
        metadata=replace(
            day.metadata,
            allowed_values=("Mo", "Su", "Holiday"),
            allowed_values_missing=False,
        ),
    )
    assert assess_control(advertised, RRC_CONTEXT).options == ("Mo", "Su")
    unknown = replace(day, value="Holiday")
    assert not assess_control(unknown, RRC_CONTEXT).eligible


# ------------------------------------------------------------- program


def week(*points: dict[str, Any], **flags: Any) -> Resource:
    return item(id=PROGRAM, type="dhwProgram", writeable=1, value=list(points), **flags)


def test_program_is_read_losslessly_with_further_fields() -> None:
    program = rrc_dhw.parse_program(fixture("/dhwCircuits/dhw2/programs/pg1/week"))
    assert program is not None
    assert program.unknown_fields and program.ordered
    assert len(program.points) == 28
    assert program.encode() == fixture("/dhwCircuits/dhw2/programs/pg1/week").value
    assert program.points[0] == rrc_dhw.DhwSwitchPoint("Su", 390, "high")


@pytest.mark.parametrize(
    "value",
    [
        [{"d": "Xx", "t": 390, "dhw": "high"}],
        [{"d": "Mo", "t": 1440, "dhw": "high"}],
        [{"d": "Mo", "t": 390.0, "dhw": "high"}],
        [{"d": "Mo", "t": True, "dhw": "high"}],
        [{"d": "Mo", "t": 390}],
        [{"d": "Mo", "t": 390, "dhw": "high level"}],
        [{"d": "Mo", "t": 390, "Th": 21}],
        ["Mo"],
    ],
)
def test_malformed_points_reject_the_whole_program(value) -> None:
    assert rrc_dhw.parse_program(week(*value)) is None


def test_program_needs_its_type_flags_and_a_bounded_list() -> None:
    point = {"d": "Mo", "t": 390, "dhw": "high"}
    assert rrc_dhw.parse_program(week(point)) is not None
    assert rrc_dhw.parse_program(week(point, used="false")) is None
    assert rrc_dhw.parse_program(week(point, available="false")) is None
    assert rrc_dhw.parse_program(week(point, used="maybe")) is None
    wrong = item(id=PROGRAM, type="weekProgram", writeable=1, value=[point])
    assert rrc_dhw.parse_program(wrong) is None
    other = item(id="/programs/pg1/week", type="dhwProgram", value=[point])
    assert rrc_dhw.parse_program(other) is None
    too_long = [{"d": "Mo", "t": minute, "dhw": "high"} for minute in range(513)]
    assert rrc_dhw.parse_program(week(*too_long)) is None


def test_current_point_looks_back_over_the_week() -> None:
    program = rrc_dhw.parse_program(fixture(PROGRAM))
    assert program is not None
    tuesday = 1
    assert rrc_dhw.current_point(program, tuesday, 390).level == "high"
    assert rrc_dhw.current_point(program, tuesday, 389).level == "off"
    assert rrc_dhw.current_point(program, tuesday, 1000).level == "high"
    assert rrc_dhw.current_point(program, tuesday, 1439).level == "off"
    sparse = rrc_dhw.parse_program(
        week({"d": "Fr", "t": 600, "dhw": "high"}, {"d": "Fr", "t": 1200, "dhw": "off"})
    )
    assert sparse is not None
    # Monday morning: Friday's last point still applies.
    assert rrc_dhw.current_point(sparse, 0, 0).level == "off"
    # Friday before its first point: last week's Friday evening.
    assert rrc_dhw.current_point(sparse, 4, 599).level == "off"
    assert rrc_dhw.current_point(sparse, 4, 600).level == "high"
    unordered = rrc_dhw.parse_program(
        week({"d": "Mo", "t": 600, "dhw": "high"}, {"d": "Mo", "t": 500, "dhw": "off"})
    )
    assert unordered is not None and not unordered.ordered
    assert rrc_dhw.current_point(unordered, 0, 700) is None
    empty = rrc_dhw.parse_program(week())
    assert empty is not None and rrc_dhw.current_point(empty, 0, 0) is None
    assert rrc_dhw.current_point(program, 7, 0) is None
    assert rrc_dhw.current_point(program, 0, 1440) is None


def test_level_options() -> None:
    assert [rrc_dhw.level_option(raw) for raw in ("Off", "off", "high", "low")] == [
        "off",
        "off",
        "high",
        "low",
    ]
    assert rrc_dhw.level_option("ownprogram") is None


def clock(value: object, **extra: Any) -> Resource:
    return item(id=rrc_dhw.CLOCK_PATH, type="stringValue", value=value, **extra)


def test_controller_clock_moves_on_from_its_reading() -> None:
    read_at = datetime(2026, 11, 10, 5, 30, tzinfo=UTC)
    reading = clock("2026-11-10T06:30:00+01:00 Tu")
    now = rrc_dhw.controller_now(reading, read_at, read_at + timedelta(minutes=4))
    assert now is not None
    assert (now.weekday(), now.hour, now.minute) == (1, 6, 34)
    assert rrc_dhw.controller_now(clock("2026-11-10T06:30:00"), read_at, read_at)
    assert rrc_dhw.controller_now(clock("2026-11-10T06:30Z Tu"), read_at, read_at)
    for wrong in (
        "2026-11-10T06:30:00+01:00 Mo",
        "2026-13-10T06:30:00",
        "yesterday",
        "2026-11-10 06:30:00",
        "",
        42,
    ):
        assert rrc_dhw.controller_now(clock(wrong), read_at, read_at) is None
    assert rrc_dhw.controller_now(reading, read_at, read_at - timedelta(1)) is None
    assert (
        rrc_dhw.controller_now(reading, read_at, read_at + timedelta(hours=2)) is None
    )
    other = item(id="/gateway/dateTime", type="stringValue", value=reading.value)
    assert rrc_dhw.controller_now(other, read_at, read_at) is None


def test_result_is_a_short_code_never_free_text() -> None:
    assert rrc_dhw.result_token(fixture(RESULT)) == "ok"
    leaked = fixture("/dhwCircuits/dhw3/thermalDisinfect/lastResult")
    assert MARKER in str(leaked.value)
    assert rrc_dhw.result_token(leaked) is None
    assert rrc_dhw.result_token(replace(fixture(RESULT), value=3)) is None
    assert rrc_dhw.result_token(fixture(MODE)) is None


def test_diagnostics_count_without_values() -> None:
    report = rrc_dhw.diagnostics(tree())
    assert report == {
        "circuits": 4,
        "operation_mode_options_advertised": 1,
        "operation_mode_options_empty": 0,
        "extra_dhw": {"available": 2, "locked": 1, "unavailable": 1},
        "thermal_disinfection_time_types": {"float": 1, "floatValue": 3},
        "programs": {
            "count": 4,
            "valid": 4,
            "points": 112,
            "unknown_fields": 1,
            "unknown_levels": 0,
        },
    }
    assert MARKER not in json.dumps(report)


# ------------------------------------------------------- real setups


async def _setup(hass: HomeAssistant, cloud: RrcCloud, clock_: Clock, enable=()):
    entry = _entry(hass)
    registry = er.async_get(hass)
    for domain, unique_id in enable:
        # Entities disabled by default, enabled for the test.
        registry.async_get_or_create(domain, DOMAIN, unique_id, config_entry=entry)
    stack = cloud_patches(cloud, clock_, RRC_GATEWAY)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinators[0]
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    coordinator._write_service._sleep = sleep
    return entry, coordinator, sleeps, stack


async def _teardown(hass: HomeAssistant, entry, stack) -> None:
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stack.close()


def _entity_id(hass: HomeAssistant, domain: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)


def _uid(path: str, role: str = "control") -> str:
    return (
        "gateway-one:"
        + path.strip("/").replace("/", ":")
        + (f":{role}" if role else "")
    )


LEVEL_UID = _uid(f"{DHW1}/programs/pg1", "level")
DISABLED = (
    ("switch", _uid(DISINFECTION)),
    ("select", _uid(DAY)),
    ("time", _uid(TIME)),
    ("sensor", _uid(RESULT, "result")),
    ("sensor", LEVEL_UID),
)


async def _call(hass, domain: str, service: str, entity_id: str, **data) -> None:
    await hass.services.async_call(
        domain, service, {"entity_id": entity_id, **data}, blocking=True
    )


async def test_entities_follow_the_controller_flags(hass: HomeAssistant) -> None:
    cloud, clock_ = RrcCloud(tree()), Clock()
    entry, _coordinator, _sleeps, stack = await _setup(hass, cloud, clock_)
    try:
        registry = er.async_get(hass)
        entries = {
            (item.domain, item.unique_id): item
            for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        }
        # dhw1: all controls, the disinfection ones disabled by default.
        for domain, unique_id in (
            ("switch", _uid(EXTRA)),
            ("number", _uid(DURATION)),
            ("number", _uid(HIGH)),
            ("select", _uid(MODE)),
            ("water_heater", _uid(DHW1, "water_heater")),
        ):
            assert entries[domain, unique_id].disabled_by is None, unique_id
        for domain, unique_id in DISABLED:
            assert entries[domain, unique_id].disabled_by is not None, unique_id
        time_entry = entries["time", _uid(TIME)]
        assert time_entry.translation_key == "rrc_thermal_disinfection_time"
        assert time_entry.entity_category == "config"
        level = entries["sensor", LEVEL_UID]
        assert level.capabilities == {"options": ["off", "low", "high", "eco"]}
        # The program itself (a structure) has no generic entity.
        assert not any(unique_id == _uid(PROGRAM, "") for _domain, unique_id in entries)
        # dhw2: extra hot water unavailable (P41), disinfection time "float".
        assert ("switch", _uid("/dhwCircuits/dhw2/extraDhw")) not in entries
        assert ("number", _uid("/dhwCircuits/dhw2/extraDhwDuration")) not in entries
        assert ("time", _uid("/dhwCircuits/dhw2/thermalDisinfect/time")) not in entries
        # dhw3: used but not available: no control either.
        assert ("switch", _uid("/dhwCircuits/dhw3/extraDhw")) not in entries
        heaters = {
            name: hass.states.get(
                _entity_id(
                    hass, "water_heater", _uid(f"/dhwCircuits/{name}", "water_heater")
                )
            )
            for name in ("dhw1", "dhw2", "dhw3", "dhw4")
        }
        assert {name: state.state for name, state in heaters.items()} == {
            "dhw1": "high",
            "dhw2": "off",
            "dhw3": "off",
            "dhw4": "ownprogram",
        }
        for name in ("dhw1", "dhw2", "dhw3"):
            assert heaters[name].attributes["operation_list"] == [
                "off",
                "high",
                "ownprogram",
            ]
        assert heaters["dhw4"].attributes["operation_list"] == [
            "off",
            "low",
            "high",
            "ownprogram",
            "eco",
        ]
        select = hass.states.get(_entity_id(hass, "select", _uid(MODE)))
        assert select.attributes["options"] == ["off", "high", "ownprogram"]
        dhw2 = hass.states.get(
            _entity_id(hass, "select", _uid("/dhwCircuits/dhw2/operationMode"))
        )
        assert dhw2.state == "off"
        assert not cloud.writes
    finally:
        await _teardown(hass, entry, stack)


async def test_writes_send_the_exact_wire_values_once(hass: HomeAssistant) -> None:
    cloud, clock_ = RrcCloud(tree()), Clock()
    entry, coordinator, sleeps, stack = await _setup(
        hass, cloud, clock_, enable=DISABLED
    )
    try:
        extra = _entity_id(hass, "switch", _uid(EXTRA))
        await _call(hass, "switch", "turn_on", extra)
        assert cloud.written("PUT") == [("PUT", EXTRA, "on")]
        assert hass.states.get(extra).state == "on"
        # The controller ends extra hot water itself: the switch follows.
        cloud._apply(EXTRA, "off")
        clock_.now += 600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(extra).state == "off"

        duration = _entity_id(hass, "number", _uid(DURATION))
        await _call(hass, "number", "set_value", duration, value=90)
        put = cloud.written("PUT")[-1]
        assert put == ("PUT", DURATION, 90) and type(put[2]) is int
        for refused in (20, 22.5):
            with pytest.raises(ServiceValidationError):
                await _call(hass, "number", "set_value", duration, value=refused)
        assert cloud.calls["PUT", DURATION] == 1

        high = _entity_id(hass, "number", _uid(HIGH))
        with pytest.raises(ServiceValidationError):
            await _call(hass, "number", "set_value", high, value=45)
        await _call(hass, "number", "set_value", high, value=46)
        assert cloud.calls["PUT", HIGH] == 1

        start = _entity_id(hass, "time", _uid(TIME))
        assert hass.states.get(start).state == "03:30:00"
        await _call(hass, "time", "set_value", start, time="04:15:00")
        put = cloud.written("PUT")[-1]
        assert put == ("PUT", TIME, 255) and type(put[2]) is int
        assert hass.states.get(start).state == "04:15:00"
        with pytest.raises(ServiceValidationError):
            await _call(hass, "time", "set_value", start, time="04:15:30")
        assert cloud.calls["PUT", TIME] == 1

        day = _entity_id(hass, "select", _uid(DAY))
        assert hass.states.get(day).state == "sunday"
        await _call(hass, "select", "select_option", day, option="daily")
        assert cloud.written("PUT")[-1] == ("PUT", DAY, "Ed")
        assert hass.states.get(day).state == "daily"

        disinfection = _entity_id(hass, "switch", _uid(DISINFECTION))
        await _call(hass, "switch", "turn_off", disinfection)
        assert cloud.written("PUT")[-1] == ("PUT", DISINFECTION, "off")

        # Not available (even though used): no control at all.
        assert _entity_id(hass, "switch", _uid("/dhwCircuits/dhw3/extraDhw")) is None
        assert cloud.calls["PUT", "/dhwCircuits/dhw3/extraDhw"] == 0
        # Each write confirmed at the first RRC read-back.
        assert set(sleeps) == {1.0}
        writes = coordinator.rrc_write_stats.snapshot("dhw")
        assert writes["writes_sent"] == writes["writes_confirmed"] == 6
        # Off the grid and a fraction: refused before sending.
        assert writes["writes_rejected"] == 3, writes
    finally:
        await _teardown(hass, entry, stack)


def _reads_off_lower_case(cloud: RrcCloud) -> None:
    """The controller stores "Off" and reads it back as "off"."""
    settle = cloud._settle

    def lowered(path: str, value: Any) -> None:
        settle(path, "off" if value == "Off" else value)

    cloud._settle = lowered  # type: ignore[method-assign]


async def test_off_is_written_as_off_and_confirmed_by_off(hass: HomeAssistant) -> None:
    cloud, clock_ = RrcCloud(tree()), Clock()
    _reads_off_lower_case(cloud)
    entry, coordinator, sleeps, stack = await _setup(hass, cloud, clock_)
    try:
        heater = _entity_id(hass, "water_heater", _uid(DHW1, "water_heater"))
        await _call(
            hass, "water_heater", "set_operation_mode", heater, operation_mode="off"
        )
        assert cloud.written("PUT") == [("PUT", MODE, "Off")]
        assert cloud.tree[MODE].value == "off"
        assert sleeps == [1.0]
        assert hass.states.get(heater).state == "off"
        select = _entity_id(hass, "select", _uid(MODE))
        assert hass.states.get(select).state == "off"
        await _call(hass, "select", "select_option", select, option="ownprogram")
        assert cloud.written("PUT")[-1] == ("PUT", MODE, "ownprogram")
        assert hass.states.get(heater).state == "ownprogram"
        # Back to off through the select: "Off" again, confirmed by "off".
        await _call(hass, "select", "select_option", select, option="off")
        assert cloud.written("PUT")[-1] == ("PUT", MODE, "Off")
        assert coordinator.rrc_write_stats.snapshot("dhw")["writes_confirmed"] == 3
        # The advertised low of dhw4 is offered and written as advertised.
        dhw4 = _entity_id(
            hass, "water_heater", _uid("/dhwCircuits/dhw4", "water_heater")
        )
        await _call(
            hass, "water_heater", "set_operation_mode", dhw4, operation_mode="low"
        )
        assert cloud.written("PUT")[-1] == (
            "PUT",
            "/dhwCircuits/dhw4/operationMode",
            "low",
        )
        with pytest.raises(ServiceValidationError):
            await _call(
                hass, "water_heater", "set_operation_mode", heater, operation_mode="eco"
            )
        assert len(cloud.written("PUT")) == 4
    finally:
        await _teardown(hass, entry, stack)


async def test_failure_paths_in_real_cycles(hass: HomeAssistant) -> None:
    """Timeout after the PUT, late adoption, 204 without effect, 403 and 429
    with Retry-After: never a second PUT, counters without values."""
    cloud, clock_ = RrcCloud(tree()), Clock()
    entry, coordinator, sleeps, stack = await _setup(hass, cloud, clock_)
    duration = _entity_id(hass, "number", _uid(DURATION))
    heater = _entity_id(hass, "water_heater", _uid(DHW1, "water_heater"))
    try:
        # A timeout after an applied PUT: confirmed by reading alone.
        cloud.timeout_after_write[DURATION] = True
        await _call(hass, "number", "set_value", duration, value=75)
        assert cloud.calls["PUT", DURATION] == 1
        del cloud.timeout_after_write[DURATION]

        # Adopted at the third read: confirmed with the RRC patience.
        sleeps.clear()
        cloud.adopt_after[DURATION] = 3
        await _call(hass, "number", "set_value", duration, value=105)
        assert sleeps == [1.0, 2.0, 4.0]
        assert cloud.calls["PUT", DURATION] == 2

        # Adopted only at the fifth read: unconfirmed, the next poll
        # confirms it late, nothing is sent again.
        sleeps.clear()
        cloud.adopt_after[MODE] = 5
        with pytest.raises(HomeAssistantError) as late:
            await _call(
                hass, "water_heater", "set_operation_mode", heater, operation_mode="off"
            )
        assert late.value.translation_key == "rrc_write_pending_confirmation"
        assert sleeps == [1.0, 2.0, 4.0, 8.0]
        assert coordinator.data[MODE].freshness is Freshness.STALE
        clock_.now += 1200
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(heater).state == "off"
        assert cloud.calls["PUT", MODE] == 1
        del cloud.adopt_after[MODE]

        # 204 without effect: unconfirmed and never confirmed later.
        cloud.ignore_writes.add(DURATION)
        with pytest.raises(HomeAssistantError) as ignored:
            await _call(hass, "number", "set_value", duration, value=120)
        assert ignored.value.translation_key == "rrc_write_pending_confirmation"
        assert cloud.calls["PUT", DURATION] == 3
        clock_.now += 1200
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(duration).state == "105.0"
        cloud.ignore_writes.clear()

        # 403 on the PUT: refused once, never repeated.
        cloud.forbidden.add(EXTRA)
        extra = _entity_id(hass, "switch", _uid(EXTRA))
        with pytest.raises(HomeAssistantError) as refused:
            await _call(hass, "switch", "turn_on", extra)
        assert refused.value.translation_key == "write_failed"
        assert cloud.calls["PUT", EXTRA] == 1
        cloud.forbidden.discard(EXTRA)
        clock_.now += 1200
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        # 429 with Retry-After: the brake stops the next write before sending.
        cloud.rate_limit_writes[EXTRA] = 120
        with pytest.raises(HomeAssistantError) as limited:
            await _call(hass, "switch", "turn_on", extra)
        assert limited.value.translation_key == "write_rate_limited"
        del cloud.rate_limit_writes[EXTRA]
        with pytest.raises(HomeAssistantError):
            await _call(hass, "switch", "turn_on", extra)
        assert cloud.calls["PUT", EXTRA] == 2

        stats = coordinator.rrc_write_stats.snapshot("dhw")
        assert stats["writes_sent"] == 6
        assert stats["writes_confirmed"] == 2
        assert stats["writes_unconfirmed"] == 2
        assert stats["late_confirmed"] == 1
        assert stats["rejection_reasons"] == {"rate_limited": 1, "request_failed": 1}
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        dhw = diagnostics["gateways"][0]["rrc_features"]["contracts"]["dhw"]
        # E17: the hot water mode was confirmed on a TC100.2.
        assert dhw["confirmation"] == "partly_confirmed"
        assert dhw["confirmed_functions"] == ["operation_mode"]
        assert dhw["circuits"] == 4
        assert dhw["writes"]["late_confirmed"] == 1
        assert MARKER not in json.dumps(dhw)
    finally:
        await _teardown(hass, entry, stack)


async def test_malformed_metadata_leaves_no_control(hass: HomeAssistant) -> None:
    resources = tree()
    resources[DURATION] = replace(
        resources[DURATION],
        metadata=replace(resources[DURATION].metadata, minimum=None),
    )
    resources[EXTRA] = item(
        id=EXTRA, type="stringValue", writeable=1, value="off", available="sometimes"
    )
    resources[TIME] = replace(
        resources[TIME], metadata=replace(resources[TIME].metadata, writable=False)
    )
    resources[MODE] = replace(resources[MODE], value="eco")
    cloud, clock_ = RrcCloud(resources), Clock()
    entry, _coordinator, _sleeps, stack = await _setup(
        hass, cloud, clock_, enable=DISABLED
    )
    try:
        assert _entity_id(hass, "number", _uid(DURATION)) is None
        assert _entity_id(hass, "switch", _uid(EXTRA)) is None
        # Not writable: the enabled registry entry gets no entity.
        start = _entity_id(hass, "time", _uid(TIME))
        assert start is not None and hass.states.get(start) is None
        heater = hass.states.get(
            _entity_id(hass, "water_heater", _uid(DHW1, "water_heater"))
        )
        # eco without advertised options (CIRCUIT-02): shown and offered with
        # the modes of a heat pump; nothing written.
        assert heater.state == "eco"
        assert heater.attributes["operation_list"] == [
            "off",
            "low",
            "high",
            "ownprogram",
            "eco",
        ]
        assert not cloud.writes
    finally:
        await _teardown(hass, entry, stack)


async def test_result_and_program_level_sensors(hass: HomeAssistant) -> None:
    cloud, clock_ = RrcCloud(tree()), Clock()
    entry, coordinator, _sleeps, stack = await _setup(
        hass, cloud, clock_, enable=DISABLED
    )
    try:
        result = _entity_id(hass, "sensor", _uid(RESULT, "result"))
        assert hass.states.get(result).state == "ok"
        leaked = _entity_id(
            hass,
            "sensor",
            _uid("/dhwCircuits/dhw3/thermalDisinfect/lastResult", "result"),
        )
        assert leaked is not None  # disabled by default, never shows free text
        level = _entity_id(hass, "sensor", LEVEL_UID)
        snapshot = coordinator.data[rrc_dhw.CLOCK_PATH]
        read_at = snapshot.last_success
        entity = hass.data["entity_components"]["sensor"].get_entity(level)
        assert isinstance(entity, rrc_dhw_entities.RrcDhwSensor)
        with patch(
            f"{PACKAGE}.rrc_dhw_entities.dt_util.utcnow",
            return_value=read_at + timedelta(minutes=5),
        ):
            assert entity.native_value == "high"  # Tuesday 06:35
        with patch(
            f"{PACKAGE}.rrc_dhw_entities.dt_util.utcnow",
            return_value=read_at + timedelta(hours=2, minutes=1),
        ):
            assert entity.native_value is None  # the clock reading is too old
        cloud.tree[rrc_dhw.CLOCK_PATH] = replace(
            cloud.tree[rrc_dhw.CLOCK_PATH], value="2026-11-10T23:00:00+01:00 Tu"
        )
        clock_.now += 600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(level).state == "off"
        # Without the clock the level is unknown, never guessed.
        cloud.down[rrc_dhw.CLOCK_PATH] = 404
        clock_.now += 600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(level).state == "unknown"
        cloud.tree[PROGRAM] = replace(cloud.tree[PROGRAM], value=[{"d": "Mo"}])
        clock_.now += 600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(level).state == "unavailable"
        assert not cloud.writes
    finally:
        await _teardown(hass, entry, stack)


async def test_time_entity_without_the_room_controller_profile(
    hass: HomeAssistant,
) -> None:
    """S11: the time tracker builds nothing for a plain context."""
    resources = tree()
    assert rrc_dhw_entities._time_descriptions(resources, GENERIC_CONTEXT) == ()
    assert rrc_dhw_entities._time_descriptions(resources, RRC_CONTEXT)
    assert rrc_dhw_entities._minute_of_day(210.0) == 210
    for wrong in (210.5, -1, 1440, True, float("nan"), "210"):
        assert rrc_dhw_entities._minute_of_day(wrong) is None
    assert time(3, 30) == time(210 // 60, 210 % 60)


async def test_controls_dialog_lists_the_usable_hot_water_settings(
    hass: HomeAssistant,
) -> None:
    from custom_components.bosch_buderus_heating.controls_flow import controls

    cloud, clock_ = RrcCloud(tree()), Clock()
    entry, coordinator, _sleeps, stack = await _setup(hass, cloud, clock_)
    try:
        shown = controls(coordinator)
        for path in (MODE, HIGH, EXTRA, DURATION, DISINFECTION, DAY, TIME):
            assert path in shown, path
        for path in (
            "/dhwCircuits/dhw2/extraDhw",
            "/dhwCircuits/dhw2/thermalDisinfect/time",
            RESULT,
            PROGRAM,
        ):
            assert path not in shown, path
    finally:
        await _teardown(hass, entry, stack)


# ------------------------------------------------- deep review CIRCUIT


async def _dialog(hass: HomeAssistant, entry):
    from custom_components.bosch_buderus_heating.config_flow import (
        BoschBuderusOptionsFlow,
    )

    flow = BoschBuderusOptionsFlow(entry)
    flow.hass = hass
    opened = await flow.async_step_controls()
    assert opened["step_id"] == "operate"
    return flow


def _dialog_config(result) -> dict[str, Any]:
    schema = result["data_schema"].schema
    selector = next(iter(schema.values()))
    config: dict[str, Any] = selector.serialize()["selector"]["bosch-buderus-controls"]
    return config


async def test_eco_levels_keep_their_control_in_real_cycles(
    hass: HomeAssistant,
) -> None:
    """CIRCUIT-02: a controller switched to eco elsewhere keeps the mode
    control with the modes of a heat pump; once it leaves the eco levels,
    only off, comfort and own program are offered."""
    resources = tree()
    resources[MODE] = replace(resources[MODE], value="eco")
    cloud, clock_ = RrcCloud(resources), Clock()
    entry, coordinator, _sleeps, stack = await _setup(hass, cloud, clock_)
    try:
        heater = _entity_id(hass, "water_heater", _uid(DHW1, "water_heater"))
        select = _entity_id(hass, "select", _uid(MODE))
        state = hass.states.get(heater)
        assert state.state == "eco"
        assert state.attributes["operation_list"] == [
            "off",
            "low",
            "high",
            "ownprogram",
            "eco",
        ]
        assert hass.states.get(select).attributes["options"] == [
            "off",
            "low",
            "high",
            "ownprogram",
            "eco",
        ]
        await _call(
            hass, "water_heater", "set_operation_mode", heater, operation_mode="low"
        )
        assert cloud.written("PUT") == [("PUT", MODE, "low")]
        await _call(hass, "select", "select_option", select, option="high")
        assert cloud.written("PUT")[-1] == ("PUT", MODE, "high")
        assert hass.states.get(heater).attributes["operation_list"] == [
            "off",
            "high",
            "ownprogram",
        ]
        with pytest.raises(ServiceValidationError):
            await _call(
                hass, "water_heater", "set_operation_mode", heater, operation_mode="eco"
            )
        assert len(cloud.written("PUT")) == 2
        assert coordinator.rrc_write_stats.snapshot("dhw")["writes_confirmed"] == 2
    finally:
        await _teardown(hass, entry, stack)


async def test_a_heat_pump_always_offers_the_eco_levels(
    hass: HomeAssistant,
) -> None:
    """VER-03: on a heat pump or hybrid plant (``/heatSources/type``) Eco
    and Eco+ stay offered after Comfort, so Eco can be chosen again; the
    mode is then written once as usual. Expected, untested on hardware."""
    resources = tree()
    resources["/heatSources/type"] = item(
        id="/heatSources/type", type="stringValue", writeable=0, value="Heatpump"
    )
    sources = resources["/heatSources"]
    resources["/heatSources"] = replace(
        sources,
        references=(*sources.references, ResourceReference("/heatSources/type")),
    )
    cloud, clock_ = RrcCloud(resources), Clock()
    entry, coordinator, _sleeps, stack = await _setup(hass, cloud, clock_)
    five = ["off", "low", "high", "ownprogram", "eco"]
    try:
        assert coordinator.control_context().heat_pump
        heater = _entity_id(hass, "water_heater", _uid(DHW1, "water_heater"))
        select = _entity_id(hass, "select", _uid(MODE))
        assert hass.states.get(heater).state == "high"
        assert hass.states.get(heater).attributes["operation_list"] == five
        assert hass.states.get(select).attributes["options"] == five
        await _call(
            hass, "water_heater", "set_operation_mode", heater, operation_mode="eco"
        )
        await _call(hass, "select", "select_option", select, option="high")
        await _call(hass, "select", "select_option", select, option="low")
        assert cloud.written("PUT") == [
            ("PUT", MODE, "eco"),
            ("PUT", MODE, "high"),
            ("PUT", MODE, "low"),
        ]
        assert hass.states.get(heater).attributes["operation_list"] == five
    finally:
        await _teardown(hass, entry, stack)


@pytest.mark.parametrize(
    ("value", "heat_pump"),
    [
        ("Heatpump", True),
        ("Hybrid", True),
        ("Hybrid_Gas", True),
        ("Hybrid_Oil", True),
        ("Boiler", False),
        ("NoAppliance", False),
        (None, False),
    ],
)
def test_the_eco_levels_follow_the_heat_source_type(
    value: str | None, heat_pump: bool
) -> None:
    """VER-03: only a heat pump context adds the eco levels; a boiler or an
    unknown type keeps the rule of CIRCUIT-02."""
    mode = fixture(MODE)
    context = RRC_HEAT_PUMP_CONTEXT if heat_pump else RRC_CONTEXT
    assert legacy_meaning(value) == ("electricity" if heat_pump else "gas")
    assessment = assess_control(mode, context)
    assert assessment.eligible
    assert assessment.options == (
        ("Off", "high", "ownprogram", "low", "eco")
        if heat_pump
        else ("Off", "high", "ownprogram")
    )
    # Another policy without heat pump options is not affected.
    generic = assess_control(mode, replace(GENERIC_CONTEXT, heat_pump=True))
    assert generic.rejection_reason == "missing_options"


async def test_an_unavailable_hot_water_mode_has_no_water_heater(
    hass: HomeAssistant,
) -> None:
    """CIRCUIT-10: not available, or malformed flags: no water heater; not
    used: shown, and a change is refused unsent."""
    resources = tree()
    for name, flags in (
        ("dhw2", {"available": False}),
        ("dhw3", {"available": None}),
        ("dhw4", {"used": False}),
    ):
        path = f"/dhwCircuits/{name}/operationMode"
        payload: dict[str, Any] = {
            "id": path,
            "type": "stringValue",
            "writeable": 1,
            "value": resources[path].value,
            "used": "true",
            "available": "true",
        }
        for key, value in flags.items():
            payload[key] = "maybe" if value is None else str(value).lower()
        resources[path] = parse_resource(payload)
    cloud, clock_ = RrcCloud(resources), Clock()
    entry, _coordinator, _sleeps, stack = await _setup(hass, cloud, clock_)
    try:

        def heater(name: str) -> str | None:
            return _entity_id(
                hass, "water_heater", _uid(f"/dhwCircuits/{name}", "water_heater")
            )

        assert heater("dhw1") is not None
        assert heater("dhw2") is None
        assert heater("dhw3") is None
        unused = heater("dhw4")
        assert unused is not None
        assert hass.states.get(unused).state == "ownprogram"
        with pytest.raises(ServiceValidationError) as refused:
            await _call(
                hass,
                "water_heater",
                "set_operation_mode",
                unused,
                operation_mode="high",
            )
        assert refused.value.translation_key == "rrc_dhw_setting_unused"
        assert cloud.written() == []
    finally:
        await _teardown(hass, entry, stack)


async def test_extra_hot_water_is_not_turned_on_without_a_duration(
    hass: HomeAssistant,
) -> None:
    """CIRCUIT-07 in real cycles: a duration of 0 refuses unsent; after a
    duration is set, the switch turns on; turning off always works."""
    resources = tree()
    resources[DURATION] = parse_resource(
        {
            "id": DURATION,
            "type": "floatValue",
            "writeable": 1,
            "value": 0,
            "unitOfMeasure": "mins",
            "minValue": 0,
            "maxValue": 2880,
            "stepSize": 15,
            "used": "true",
            "available": "true",
        }
    )
    cloud, clock_ = RrcCloud(resources), Clock()
    entry, coordinator, _sleeps, stack = await _setup(hass, cloud, clock_)
    try:
        extra = _entity_id(hass, "switch", _uid(EXTRA))
        with pytest.raises(ServiceValidationError) as refused:
            await _call(hass, "switch", "turn_on", extra)
        assert refused.value.translation_key == "rrc_extra_dhw_duration_zero"
        assert cloud.written() == []
        assert coordinator.rrc_write_stats.snapshot("dhw")["rejection_reasons"] == {
            "rrc_extra_dhw_duration_zero": 1
        }
        duration = _entity_id(hass, "number", _uid(DURATION))
        await _call(hass, "number", "set_value", duration, value=30)
        await _call(hass, "switch", "turn_on", extra)
        assert cloud.written("PUT") == [("PUT", DURATION, 30), ("PUT", EXTRA, "on")]
        await _call(hass, "number", "set_value", duration, value=0)
        await _call(hass, "switch", "turn_off", extra)
        assert cloud.written("PUT")[-1] == ("PUT", EXTRA, "off")
    finally:
        await _teardown(hass, entry, stack)


async def test_dialog_writes_the_duration_before_extra_hot_water(
    hass: HomeAssistant,
) -> None:
    """CIRCUIT-07/08: in the dialog the duration goes out first; a draft
    that turns extra hot water on with 0 minutes sends nothing and says so."""
    resources = tree()
    resources[DURATION] = parse_resource(
        {
            "id": DURATION,
            "type": "floatValue",
            "writeable": 1,
            "value": 0,
            "unitOfMeasure": "mins",
            "minValue": 0,
            "maxValue": 2880,
            "stepSize": 15,
        }
    )
    cloud, clock_ = RrcCloud(resources), Clock()
    entry, _coordinator, _sleeps, stack = await _setup(hass, cloud, clock_)
    try:
        flow = await _dialog(hass, entry)
        refused = await flow.async_step_operate(
            {"controls": {"changes": {EXTRA: "on"}}}
        )
        assert refused["errors"] == {"base": "controls_extra_dhw_duration_zero"}
        assert cloud.written() == []
        flow = await _dialog(hass, entry)
        saved = await flow.async_step_operate(
            {"controls": {"changes": {EXTRA: "on", DURATION: 45}}}
        )
        assert not saved["errors"]
        assert cloud.written("PUT") == [("PUT", DURATION, 45), ("PUT", EXTRA, "on")]
    finally:
        await _teardown(hass, entry, stack)


async def test_dialog_offers_off_once_and_selects_it_for_off(
    hass: HomeAssistant,
) -> None:
    """CIRCUIT-14: both spellings of off are one option, and a reading of
    "off" selects "Off"; a setting the controller does not use is not
    offered in the dialog."""
    resources = tree()
    high = resources[HIGH]
    resources[HIGH] = replace(high, metadata=replace(high.metadata, used=False))
    resources[MODE] = parse_resource(
        {
            "id": MODE,
            "type": "stringValue",
            "writeable": 1,
            "value": "off",
            "allowedValues": ["Off", "off", "high", "ownprogram"],
        }
    )
    cloud, clock_ = RrcCloud(resources), Clock()
    entry, _coordinator, _sleeps, stack = await _setup(hass, cloud, clock_)
    try:
        flow = await _dialog(hass, entry)
        result = await flow.async_step_operate({"controls": {"action": "refresh"}})
        assert not result["errors"]
        config = _dialog_config(result)
        assert HIGH not in {row["key"] for row in config["rows"]}
        mode = next(row for row in config["rows"] if row["key"] == MODE)
        assert [option["value"] for option in mode["options"]] == [
            "Off",
            "high",
            "ownprogram",
        ]
        assert mode["value"] == "Off"
    finally:
        await _teardown(hass, entry, stack)


async def test_disinfection_time_in_five_minute_steps(hass: HomeAssistant) -> None:
    """CIRCUIT-11: a time off the five-minute grid is refused unsent with a
    message that names the step."""
    cloud, clock_ = RrcCloud(tree()), Clock()
    entry, _coordinator, _sleeps, stack = await _setup(
        hass, cloud, clock_, enable=DISABLED
    )
    try:
        start = _entity_id(hass, "time", _uid(TIME))
        with pytest.raises(ServiceValidationError) as refused:
            await _call(hass, "time", "set_value", start, time="06:03:00")
        assert refused.value.translation_key == "rrc_time_of_day_step"
        assert refused.value.translation_placeholders == {"step": "5"}
        assert cloud.written() == []
        await _call(hass, "time", "set_value", start, time="06:05:00")
        assert cloud.written("PUT") == [("PUT", TIME, 365)]
    finally:
        await _teardown(hass, entry, stack)
