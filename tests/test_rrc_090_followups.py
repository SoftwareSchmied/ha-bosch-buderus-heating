"""0.9.0 follow-ups of the TC100.2 diagnostics (#122).

- A start counter with its parts as a list of objects in ``value``.
- A part the first reading does not contain is created disabled.
- Week points ``{d, t, Th, name}``, day points ``{t, Th, name}``; a change
  goes out as ``{d, t, Th}`` and is compared as ``(d, t, Th)``; the
  diagnostics show the point forms (key names and types, no values).
- The confirmations, also in ``rrc_profile``.
- The last reason the boost settings had no limits, with its age.
- Why a cycle of the energy history imported nothing.

The program and counter forms are synthetic, in the shapes the diagnostics
of a TC100.2 reported (types and key names only). Writing the new program
form is expected, untested on hardware.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating import (
    rrc_boost,
    rrc_confirmations,
    start_counters,
)
from custom_components.bosch_buderus_heating.diagnostics import (
    _capability_diagnostics,
    _rrc_schedule_summary,
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.pointt import (
    Resource,
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.rrc_energy import HistoryState
from custom_components.bosch_buderus_heating.rrc_programs import (
    RrcProgramService,
    edit_point,
    edit_shows,
    parse_schedule,
    point_shapes,
    schedule_issues,
)
from custom_components.bosch_buderus_heating.sensor import build_sensor_descriptions
from tests.helpers.rrc_absence import absence_tree
from tests.helpers.rrc_absence import setup as absence_setup
from tests.helpers.rrc_absence import teardown as absence_teardown
from tests.helpers.rrc_cloud import RrcCloud
from tests.test_dynamic_controls import snapshots
from tests.test_rrc import environment as environment
from tests.test_rrc_energy_history import (
    NEW_FIRMWARE,
    NOW,
    TODAY,
    Sink,
    Source,
    cycle,
    day_record,
)
from tests.test_start_counters import coordinator_for

SOURCE = "/heatSources/hs1/numberOfStarts"
PACKAGE = "custom_components.bosch_buderus_heating"


@pytest.fixture
def legacy_setup_path():
    """The absence setup helper reads the coordinators of the legacy entry."""
    with patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)):
        yield


# ------------------------------------------------------- start counters
def counter(**fields: Any) -> Resource:
    return parse_resource({"id": SOURCE, "type": "emonValue", **fields})


def readings(resource: Resource) -> dict[str | None, start_counters.CounterReading]:
    return dict(start_counters.assess_start_counter(resource).readings)


def test_a_list_of_parts_in_value_is_read() -> None:
    """The #122 form: one object with float counts in a list in ``value``."""
    resource = counter(value=[{"ch": 812.0, "dhw": 344.0, "total": 1156.0}], values=[])
    parts = readings(resource)
    assert parts["total"].value == 1156 and type(parts["total"].value) is int
    assert parts["ch"].value == 812 and parts["dhw"].value == 344
    assert parts["cooling"].reason == "missing_value"
    assert parts["pool"].reason == "missing_value"
    assert start_counters.structure_summary(resource) is None


def test_several_parts_in_the_list_and_in_values_are_combined() -> None:
    resource = counter(value=[{"total": 12}, {"ch": 3}], values=[{"dhw": 4}])
    parts = readings(resource)
    assert (parts["total"].value, parts["ch"].value, parts["dhw"].value) == (12, 3, 4)


@pytest.mark.parametrize(
    "value",
    [
        [],
        [{"total": 1}] * (start_counters.MAX_START_COUNTER_PARTS + 1),
        [{"total": 1}, 7],
        [[{"total": 1}]],
    ],
    ids=["empty", "too_many", "not_an_object", "nested_list"],
)
def test_an_unusual_list_stays_invalid(value: list[Any]) -> None:
    resource = counter(value=value, values=[])
    assert {item.reason for item in readings(resource).values()} == {
        "invalid_structure"
    }
    summary = start_counters.structure_summary(resource)
    assert summary is not None and summary["value"].startswith(f"list[{len(value)}]")


def test_the_most_parts_are_still_read() -> None:
    value = [{"total": 5}] * start_counters.MAX_START_COUNTER_PARTS
    assert readings(counter(value=value, values=[]))["total"].value == 5


def test_parts_of_the_list_that_disagree_are_a_conflict() -> None:
    resource = counter(value=[{"total": 5.0}, {"total": 6.0}], values=[])
    assert readings(resource)["total"].reason == "conflicting_values"


async def test_the_list_form_reaches_the_sensors(hass: HomeAssistant) -> None:
    from tests.test_start_counters import sensors_for

    resource = counter(value=[{"ch": 812.0, "dhw": 344.0, "total": 1156.0}], values=[])
    _entry, coordinator = coordinator_for(hass, resource)
    sensors = sensors_for(coordinator)
    assert sensors[SOURCE, "total"].native_value == 1156
    assert sensors[SOURCE, "dhw"].native_value == 344
    assert sensors[SOURCE, "cooling"].native_value is None


# ----------------------------------------------------- disabled parts
def enabled(resource: Resource) -> dict[str | None, bool]:
    return {
        description.value_key: description.entity_registry_enabled_default
        for description in build_sensor_descriptions({resource.path: resource})
        if description.resource_path == SOURCE
    }


def test_parts_the_reading_does_not_contain_are_created_disabled() -> None:
    resource = counter(value=[{"ch": 812.0, "dhw": 344.0, "total": 1156.0}], values=[])
    assert enabled(resource) == {
        "total": True,
        "ch": True,
        "cooling": False,
        "dhw": True,
        "pool": False,
    }


def test_a_reported_part_without_a_value_stays_enabled() -> None:
    """The key decides, not its value; pool is always disabled."""
    resource = counter(
        values=[{"total": 1}, {"ch": None}, {"cooling": 0}, {"dhw": 0}, {"pool": 0}]
    )
    assert enabled(resource) == {
        "total": True,
        "ch": True,
        "cooling": True,
        "dhw": True,
        "pool": False,
    }


@pytest.mark.parametrize(
    "fields",
    [
        {"values": []},
        {"value": "777", "values": []},
        {"value": [], "values": []},
    ],
    ids=["no_data", "invalid", "empty_list"],
)
def test_without_a_usable_reading_the_defaults_stay(fields: dict[str, Any]) -> None:
    """A placeholder or an unusual form says nothing about the parts."""
    assert enabled(counter(**fields)) == {
        "total": True,
        "ch": True,
        "cooling": True,
        "dhw": True,
        "pool": False,
    }


def test_a_heat_pump_with_all_parts_is_unchanged() -> None:
    resource = counter(values=[{"ch": 1}, {"dhw": 2}, {"cooling": 3}, {"total": 6}])
    assert enabled(resource)["cooling"] is True


# ------------------------------------------------------- program form
WEEK = "/programs/pg2/week"
MONDAY = "/programs/pg2/monday"
TUESDAY = "/programs/pg2/tuesday"
# The #122 form, synthetic values: ``name`` an integer on every point.
WEEK_POINTS: list[dict[str, Any]] = [
    {"d": "Mo", "t": 360, "Th": 21.0, "name": 0},
    {"d": "Mo", "t": 1320, "Th": 17.0, "name": 0},
    {"d": "Tu", "t": 390, "Th": 21.5, "name": 0},
    {"d": "Tu", "t": 1350, "Th": 17.0, "name": 0},
]


def week(points: list[dict[str, Any]] | None = None, **fields: Any) -> Resource:
    return parse_resource(
        {
            "id": WEEK,
            "type": "weekProgram",
            "writeable": 1,
            "value": WEEK_POINTS if points is None else points,
            **fields,
        }
    )


def day_program(path: str, day: str, points: list[dict[str, Any]]) -> Resource:
    """A day as the controller reports it: no ``d``."""
    return parse_resource(
        {
            "id": path,
            "type": "dayProgram",
            "writeable": 1,
            "value": [
                {key: value for key, value in item.items() if key != "d"}
                for item in points
                if item["d"] == day
            ],
        }
    )


class Controller:
    """A fake controller: a day PUT stores the points without ``d`` and with
    ``name`` 0, and the week shows them."""

    def __init__(self) -> None:
        self.week = week()
        self.days = {
            MONDAY: day_program(MONDAY, "Mo", WEEK_POINTS),
            TUESDAY: day_program(TUESDAY, "Tu", WEEK_POINTS),
        }
        self.puts: list[tuple[str, Any]] = []
        self.stored_temperature_offset = 0.0

    async def get(self, _gateway: str, path: str) -> Resource:
        return self.week if path == WEEK else self.days[path]

    async def put(self, _gateway: str, path: str, value: Any) -> None:
        self.puts.append((path, value))
        day = "Mo" if path == MONDAY else "Tu"
        stored = [
            {
                "d": day,
                "t": item["t"],
                "Th": item["Th"] + self.stored_temperature_offset,
                "name": 0,
            }
            for item in value
        ]
        others = [item for item in self.week.value if item["d"] != day]
        self.week = replace(self.week, value=sorted_week(stored + others))
        self.days[path] = day_program(path, day, stored)


def sorted_week(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
    return sorted(points, key=lambda item: (order.index(item["d"]), item["t"]))


def service(controller: Controller) -> RrcProgramService:
    client = AsyncMock()
    client.get_resource.side_effect = controller.get
    client.put_resource_value.side_effect = controller.put
    return RrcProgramService(client, sleep=AsyncMock(), delays=(1.0, 2.0))


def test_the_reported_forms_are_editable() -> None:
    schedule = parse_schedule(week())
    assert schedule is not None and schedule.editable
    assert [(point.day, point.minute) for point in schedule.points][:2] == [
        ("Mo", 360),
        ("Mo", 1320),
    ]
    day = parse_schedule(day_program(TUESDAY, "Tu", WEEK_POINTS))
    assert day is not None and day.editable
    assert {point.day for point in day.points} == {"Tu"}
    assert schedule_issues(week()) == ()
    assert schedule_issues(day_program(MONDAY, "Mo", WEEK_POINTS)) == ()


def test_a_change_is_sent_without_name() -> None:
    schedule = parse_schedule(week())
    assert schedule is not None
    assert edit_point(schedule, 0, 365, 21.0) == [
        {"d": "Mo", "t": 365, "Th": 21.0},
        {"d": "Mo", "t": 1320, "Th": 17.0},
    ]


async def test_one_put_in_the_written_form_and_a_read_back() -> None:
    controller = Controller()
    result = await service(controller).edit("fixture", week(), 0, 365, 21.0)
    assert controller.puts == [
        (
            MONDAY,
            [{"d": "Mo", "t": 365, "Th": 21.0}, {"d": "Mo", "t": 1320, "Th": 17.0}],
        )
    ]
    assert result[0].path == WEEK and result[0].value[0]["t"] == 365
    # The other day is unchanged, ``name`` as before.
    assert result[0].value[2:] == WEEK_POINTS[2:]


async def test_a_changed_name_alone_does_not_stop_the_write() -> None:
    """Only ``(d, t, Th)`` is compared; ``name`` is not sent anyway."""
    controller = Controller()
    controller.week = week([{**item, "name": 3} for item in WEEK_POINTS])
    await service(controller).edit("fixture", week(), 2, 395, 21.5)
    assert controller.puts[0][0] == TUESDAY


@pytest.mark.parametrize(
    "change",
    [
        lambda c: setattr(
            c, "week", week([{**WEEK_POINTS[0], "t": 355}, *WEEK_POINTS[1:]])
        ),
        lambda c: c.days.__setitem__(
            MONDAY,
            day_program(MONDAY, "Mo", [{**WEEK_POINTS[0], "Th": 20.5}, WEEK_POINTS[1]]),
        ),
        lambda c: c.days.__setitem__(
            MONDAY,
            parse_resource(
                {
                    "id": MONDAY,
                    "type": "dayProgram",
                    "writeable": 1,
                    "value": [
                        {"t": 360, "Th": 21.0, "name": "x"},
                        {"t": 1320, "Th": 17.0, "name": 0},
                    ],
                }
            ),
        ),
        lambda c: c.days.__setitem__(
            MONDAY,
            parse_resource(
                {
                    "id": MONDAY,
                    "type": "dayProgram",
                    "writeable": 1,
                    "value": [
                        {"t": 360, "Th": 21.0, "level": 1},
                        {"t": 1320, "Th": 17.0},
                    ],
                }
            ),
        ),
    ],
    ids=["week_time", "day_temperature", "day_name_type", "day_extra_key"],
)
async def test_a_changed_or_unknown_form_sends_nothing(change: Any) -> None:
    controller = Controller()
    change(controller)
    with pytest.raises(WriteValidationError):
        await service(controller).edit("fixture", week(), 0, 365, 21.0)
    assert controller.puts == []


async def test_a_read_back_with_another_temperature_is_not_confirmed() -> None:
    controller = Controller()
    controller.stored_temperature_offset = 0.5
    with pytest.raises(WriteNotConfirmed):
        await service(controller).edit("fixture", week(), 0, 365, 21.0)
    assert len(controller.puts) == 1


def test_a_later_reading_confirms_by_projection() -> None:
    shows = edit_shows(week(), 0, 365, 21.0)
    assert shows is not None
    changed = [{**WEEK_POINTS[0], "t": 365}, *WEEK_POINTS[1:]]
    assert shows(week(changed))
    # Without ``name`` and with an integer temperature it is the same.
    plain = [
        {"d": item["d"], "t": item["t"], "Th": int(item["Th"])} for item in changed
    ]
    plain[2]["Th"] = 21.5
    assert shows(week(plain))
    assert not shows(week())


async def test_the_coordinator_writes_the_reported_form(environment) -> None:
    """Through the coordinator: fresh reads, one PUT, the week published."""
    coordinator, client, _ = environment
    controller = Controller()
    coordinator.resources = {WEEK: controller.week, **controller.days}
    coordinator.data = snapshots(controller.week, *controller.days.values())
    coordinator._rrc_program_service = service(controller)
    coordinator._rrc_program_service.client = client
    client.get_resource.side_effect = controller.get
    client.put_resource_value.side_effect = controller.put
    await coordinator.async_edit_rrc_program(controller.week, 1, 1325, 17.5)
    assert client.put_resource_value.await_count == 1
    assert controller.puts[0][1][-1] == {"d": "Mo", "t": 1325, "Th": 17.5}
    assert coordinator.data[WEEK].resource.value[1]["t"] == 1325


def test_point_shapes_name_keys_and_types_only() -> None:
    assert point_shapes(week()) == ("dict{Th:float,d:str,name:int,t:int}",)
    assert point_shapes(day_program(MONDAY, "Mo", WEEK_POINTS)) == (
        "dict{Th:float,name:int,t:int}",
    )
    many = [
        {"d": "Mo", "t": 5 * index, "Th": 20.0, f"k{index}": 1} for index in range(6)
    ]
    shapes = point_shapes(week(many))
    assert len(shapes) == 5 and shapes[-1] == "more"
    assert point_shapes(week([])) == ()
    assert point_shapes(parse_resource({"id": "/zones/zn1/clockProgram"})) == ()


def test_the_summary_shows_the_point_forms_without_values() -> None:
    private = [
        {"d": "Mo", "t": 425, "Th": 23.5, "name": 77},
        {"d": "Mo", "t": 1295, "Th": 16.5, "name": 77},
    ]
    resources = {
        WEEK: week(private),
        MONDAY: day_program(MONDAY, "Mo", private),
    }
    summary = _rrc_schedule_summary(
        SimpleNamespace(resources=resources, data={})  # type: ignore[arg-type]
    )
    assert summary["week"]["point_shapes"] == {"dict{Th:float,d:str,name:int,t:int}": 1}
    assert summary["day"]["point_shapes"] == {"dict{Th:float,name:int,t:int}": 1}
    assert summary["week"]["editable"] == 1 and summary["day"]["editable"] == 1
    text = json.dumps(summary)
    # Key names are allowed for the form, no value of a point.
    for value in ("425", "1295", "23.5", "16.5", "77", '"Mo"'):
        assert value not in text
    report = _capability_diagnostics(week(private), None, {}, 0)
    assert "Th" not in json.dumps(report) and "425" not in json.dumps(report)


# -------------------------------------------------------- confirmations
def test_advance_next_switch_point_is_confirmed() -> None:
    report = rrc_confirmations.report("zone")
    assert "advance_switch_point" in report["confirmed_functions"]
    assert report["confirmed_on"] == "tc100_2"


@pytest.mark.usefixtures("enable_custom_integrations", "legacy_setup_path")
async def test_the_profile_names_the_confirmation_of_the_functions(
    hass: HomeAssistant,
) -> None:
    cloud = RrcCloud(absence_tree())
    entry, _coordinator, _clock, _sleeps, stack = await absence_setup(hass, cloud)
    try:
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        gateway = diagnostics["gateways"][0]
        profile = gateway["rrc_profile"]
        assert profile["confirmation"] == gateway["rrc_features"]["confirmation"]
        assert profile["confirmation"] == "partly_confirmed"
        assert profile["confirmed_on"] == "tc100_2"
        assert profile["physical_device_verified"] is False
    finally:
        await absence_teardown(hass, entry, stack)


# ---------------------------------------------------------- boost limits
def _leaf(**fields: Any) -> Resource:
    return parse_resource(
        {
            "id": rrc_boost.BOOST_TEMPERATURE_PATH,
            "type": "floatValue",
            "writeable": 1,
            "unitOfMeasure": "C",
            "minValue": 5,
            "maxValue": 30,
            **fields,
        }
    )


def test_the_watch_keeps_the_last_reason_with_its_age() -> None:
    watch = rrc_boost.LimitIssueWatch()
    assert watch.diagnostics(0.0) == {
        "boost_temperature_limits_last_issue": None,
        "boost_temperature_limits_last_issue_age_s": None,
        "boost_duration_limits_last_issue": None,
        "boost_duration_limits_last_issue_age_s": None,
    }
    watch.observe({"temperature": _leaf(), "duration": None}, 100.0)
    # ``missing`` is no state of the controller.
    assert watch.diagnostics(100.0)["boost_duration_limits_last_issue"] is None
    assert watch.diagnostics(100.0)["boost_temperature_limits_last_issue"] is None
    watch.observe({"temperature": _leaf(used="false")}, 200.0)
    watch.observe({"temperature": _leaf(unitOfMeasure="K")}, 250.0)
    watch.observe({"temperature": _leaf()}, 400.0)
    report = watch.diagnostics(460.4)
    assert report["boost_temperature_limits_last_issue"] == "unit"
    assert report["boost_temperature_limits_last_issue_age_s"] == 210


@pytest.mark.usefixtures("enable_custom_integrations", "legacy_setup_path")
async def test_a_short_flag_state_stays_visible(hass: HomeAssistant) -> None:
    """Real cycles: the leaf is not available for one reading, then fine."""
    from tests.helpers.rrc_absence import _raw

    cloud = RrcCloud(absence_tree())
    entry, coordinator, clock, _sleeps, stack = await absence_setup(hass, cloud)
    path = rrc_boost.BOOST_TEMPERATURE_PATH
    good = cloud.tree[path]
    try:

        async def absence() -> dict[str, Any]:
            diagnostics = await async_get_config_entry_diagnostics(hass, entry)
            return diagnostics["gateways"][0]["rrc_features"]["contracts"]["absence"]

        first = await absence()
        assert first["boost_temperature_limits_issue"] is None
        assert first["boost_temperature_limits_last_issue"] is None
        cloud.tree[path] = parse_resource(
            {
                **_raw(good),
                "used": "false",
                "unitOfMeasure": "C",
                "minValue": 5.0,
                "maxValue": 30.0,
                "value": 20.0,
            }
        )
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.data[path].resource.metadata.used is False
        cloud.tree[path] = good
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.data[path].resource.metadata.used is not False
        report = await absence()
        assert report["boost_temperature_limits_issue"] is None
        assert report["boost_temperature_limits_last_issue"] == "flags"
        assert isinstance(report["boost_temperature_limits_last_issue_age_s"], int)
        assert report["boost_duration_limits_last_issue"] is None
    finally:
        await absence_teardown(hass, entry, stack)


# -------------------------------------------------------- energy history
async def test_a_cycle_without_records_names_it() -> None:
    state = HistoryState(gas_checked_at=NOW)
    result = await cycle(state, Source([], exact=True), Sink(), firmware=NEW_FIRMWARE)
    assert result.reason == "no_records" and state.records_seen == 0


async def test_records_of_today_only_are_no_completed_days() -> None:
    state = HistoryState(gas_checked_at=NOW)
    source = Source([day_record(TODAY)])
    result = await cycle(state, source, Sink(), firmware=NEW_FIRMWARE)
    assert result.reason == "no_completed_days"
    assert state.records_seen == 1 and state.imported_days == 0


async def test_days_with_only_zeros_are_all_series_empty() -> None:
    state = HistoryState(gas_checked_at=NOW)
    zero = {"T": 0.0, "eCh": 0.0}
    source = Source([day_record(date(2026, 10, day), **zero) for day in (17, 18, 19)])
    result = await cycle(state, source, Sink(), firmware=NEW_FIRMWARE)
    assert result.reason == "all_series_empty"
    assert state.imported_days == 3 and state.active == {}


async def test_an_import_has_no_reason_and_the_count_is_stored() -> None:
    state = HistoryState(gas_checked_at=NOW)
    source = Source([day_record(date(2026, 10, day)) for day in (17, 18, 19)])
    result = await cycle(state, source, Sink(), firmware=NEW_FIRMWARE)
    assert result.reason is None and state.active
    # The known last day is read once more: every answer counts.
    assert state.records_seen == 4
    restored = HistoryState.from_dict(state.to_dict())
    assert restored.records_seen == 4
    assert (
        HistoryState.from_dict({**state.to_dict(), "records_seen": -1}).records_seen
        == 0
    )


def test_the_diagnostics_count_records_without_values(hass: HomeAssistant) -> None:
    from custom_components.bosch_buderus_heating.rrc_energy_statistics import (
        EnergyHistory,
    )

    history = EnergyHistory.__new__(EnergyHistory)
    history.state = HistoryState(records_seen=4, reason="all_series_empty")
    history.waiting = None
    history._now = lambda: datetime(2026, 10, 20, tzinfo=UTC)
    report = EnergyHistory.diagnostics(history)
    assert report["records_seen"] == 4 and report["reason"] == "all_series_empty"
