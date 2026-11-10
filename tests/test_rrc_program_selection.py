"""#113: program selection, names, override and day programs of RRC zones.

The synthetic answers follow the expected RRC 2.0 shapes and the observed
TC100.2 wire format: the program list as ``programArray`` with
numeric IDs and Base64 names, single days as ``dayProgram``, ``used`` as the
strings "true"/"false" and a temporary setpoint of 0 without an override.
Values and names are constructed.
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating import rrc
from custom_components.bosch_buderus_heating.diagnostics import (
    _capability_diagnostics,
    _rrc_program_diagnostics,
)
from custom_components.bosch_buderus_heating.discovery import (
    MAX_DISCOVERY_RESOURCES,
    DiscoveryDiagnostics,
    async_discover_resources,
)
from custom_components.bosch_buderus_heating.discovery_inventory import (
    RECOVERY_SCHEDULE,
)
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Resource,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.resource_catalog import supports_entity
from custom_components.bosch_buderus_heating.rrc_programs import (
    RrcProgramService,
    parse_schedule,
    program_names,
    selection_issue,
    selection_options,
)
from custom_components.bosch_buderus_heating.rrc_select import (
    RrcProgramSelect,
    descriptions,
)
from custom_components.bosch_buderus_heating.sensor import (
    BoschBuderusSensor,
    build_sensor_descriptions,
)
from tests import test_dynamic_controls
from tests.test_discovery_recovery import (
    COORDINATOR,
    Clock,
    Cloud,
    _coordinator,
    _inventory,
    container,
)
from tests.test_dynamic_controls import snapshots

PROGRAMS = rrc.PROGRAM_LIST
CLOCK = "/zones/zn1/clockProgram"
MANUAL_CLOCK = "/zones/zn4/clockProgram"
OVERRIDE = "/zones/zn3/clockOverride/temperatureHeating"
WEEK = "/programs/pg1/week"
MONDAY = "/programs/pg1/monday"


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def wire(path: str, **fields: object) -> Resource:
    return parse_resource({"id": path, "writeable": 0, "recordable": 0, **fields})


def program_list(**fields: object) -> Resource:
    return wire(
        PROGRAMS,
        **{
            "type": "programArray",
            "value": [
                {"id": 1, "name": b64("Wohnen")},
                {"id": 2, "name": b64("Arbeit")},
                {"id": 3, "name": "Bad"},  # plain text
                {"id": 4, "name": ""},
            ],
            **fields,
        },
    )


def selection(path: str = CLOCK, value: float = 2.0, **fields: object) -> Resource:
    return wire(
        path,
        **{
            "type": "floatValue",
            "used": "true",
            "writeable": 1,
            "unitOfMeasure": "",
            "minValue": 0.0,
            "maxValue": 24.0,
            "stepSize": 1.0,
            "value": value,
            **fields,
        },
    )


def manual_selection(**fields: object) -> Resource:
    """The TC100.2 shape of a zone in manual mode (fixture zone 4)."""
    return selection(MANUAL_CLOCK, 4.0, used="false", **fields)


def day(path: str = MONDAY, **fields: object) -> Resource:
    return wire(
        path,
        **{
            "type": "dayProgram",
            "writeable": 1,
            "value": [
                {"d": "Mo", "t": 420, "Th": 19},
                {"d": "Mo", "t": 1380, "Th": 15},
            ],
            **fields,
        },
    )


def week(**fields: object) -> Resource:
    return wire(
        WEEK,
        **{
            "type": "weekProgram",
            "writeable": 1,
            "value": [
                {"d": "Mo", "t": 420, "Th": 19},
                {"d": "Mo", "t": 1380, "Th": 15},
                {"d": "Tu", "t": 420, "Th": 19},
            ],
            **fields,
        },
    )


@pytest.fixture
def environment(hass, enable_custom_integrations):
    yield from test_dynamic_controls.environment.__wrapped__(hass)


@pytest.fixture
def clock():
    clock = Clock()
    with (
        patch(f"{COORDINATOR}.monotonic", clock),
        patch(f"{COORDINATOR}.fault_resource_candidates", return_value=()),
    ):
        yield clock


def wire_coordinator(environment, *resources: Resource) -> dict[str, Resource]:
    coordinator, client, _ = environment
    state = {resource.path: resource for resource in resources}
    coordinator.resources = dict(state)
    coordinator.data = snapshots(*state.values())
    coordinator._rrc_program_service = RrcProgramService(client, sleep=AsyncMock())

    async def get(gateway, path):
        return state[path]

    async def put(gateway, path, value):
        state[path] = replace(state[path], value=value)
        if path == MONDAY:
            others = [item for item in state[WEEK].value if item["d"] != "Mo"]
            state[WEEK] = replace(state[WEEK], value=value + others)

    client.get_resource.side_effect = get
    client.put_resource_value.side_effect = put
    return state


# ------------------------------------------------------------- RRC-03


async def test_manual_zone_shows_selects_and_writes_its_program(environment):
    """#113 (RRC-03): used:"false" keeps the assignment."""
    coordinator, client, _ = environment
    state = wire_coordinator(environment, program_list(), manual_selection())
    sensor = BoschBuderusSensor(
        coordinator, build_sensor_descriptions({MANUAL_CLOCK: state[MANUAL_CLOCK]})[0]
    )
    assert sensor.available and sensor.native_value == 4
    (description,) = descriptions(state)
    entity = RrcProgramSelect(coordinator, description)
    assert entity.available
    assert entity.options == ["1", "2", "3", "4"] and entity.current_option == "4"

    await entity.async_select_option("2")
    client.put_resource_value.assert_awaited_once_with(
        "synthetic-gateway", MANUAL_CLOCK, 2
    )
    # Read back before it counts as done; the zone stays unused (manual).
    assert state[MANUAL_CLOCK].metadata.used is False
    assert entity.current_option == "2"


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("de", "Zeitprogramm wählen"),
        ("de-AT", "Zeitprogramm wählen"),
        ("en", "Select heating schedule"),
        ("eo", "Select heating schedule"),
        ("sv", "Välj värmeprogram"),
        ("nb", "Velg tidsprogram for oppvarming"),
        ("fi", "Valitse lämmitysaikaohjelma"),
        ("da", "Vælg varmeprogram"),
    ],
)
async def test_program_selection_is_named_in_the_system_language(
    environment, language, expected
):
    coordinator, _, _ = environment
    state = wire_coordinator(environment, program_list(), manual_selection())
    (description,) = descriptions(state)
    coordinator.hass.config.language = language
    entity = RrcProgramSelect(coordinator, description)
    assert entity.name.endswith(expected)
    assert entity.name != expected  # grouped under the zone


@pytest.mark.parametrize(
    "flags",
    [
        {"used": "False"},  # not a valid flag value
        {"used": 0},
        {"available": "false"},
        {"available": False, "used": False},
    ],
)
def test_unusable_program_selection_flags_stay_rejected(flags):
    resource = selection(MANUAL_CLOCK, 4.0, **flags)
    assert rrc.scalar_value(resource) is None
    assert not supports_entity(resource)
    assert selection_issue(resource, program_list()) == "reading_invalid"


@pytest.mark.parametrize(
    "path",
    ["/zones/zn1/userMode", "/zones/zn1/temperatureActual"],
)
def test_only_the_program_selection_keeps_its_value_when_unused(path):
    resource = (
        wire(path, type="stringValue", used="false", writeable=1, value="clock")
        if path.endswith("userMode")
        else wire(path, type="floatValue", used="false", unitOfMeasure="C", value=20.5)
    )
    assert not rrc.program_unused(resource)
    assert rrc.scalar_value(resource) is None


# ------------------------------------------------------------- RRC-04


@pytest.mark.parametrize(
    "flags",
    [
        {"used": "false"},
        {"available": "false"},
        {"used": False, "available": False},
        {"used": "True"},  # malformed flags are ignored on the program list too
    ],
)
def test_program_list_flags_are_ignored_like_the_vendor_app(flags):
    programs = program_list(**flags)
    assert rrc.listed_ids(programs) == (1, 2, 3, 4)
    assert rrc.list_issue(programs) is None
    assert selection_options(selection(), programs) == (1, 2, 3, 4)


@pytest.mark.parametrize("flags", [{"used": "false"}, {"available": False}])
def test_zone_list_keeps_its_flag_rules(flags):
    zones = wire(rrc.ZONE_LIST, type="zoneArray", value=[{"id": 1}], **flags)
    assert rrc.listed_ids(zones) is None
    assert rrc.list_issue(zones) == "flags"


# ------------------------------------------------------------- RRC-02
OTHER_LIST = wire("/programs/other", type="programArray", value=[])
DUPLICATE_IDS = program_list(value=[{"id": 1}, {"id": 1}])
USER_MODE = wire("/zones/zn1/userMode", type="stringValue", value="clock")


@pytest.mark.parametrize(
    ("current", "programs", "reason"),
    [
        (selection(), program_list(), None),
        (selection(), None, "program_list_missing"),
        (selection(), OTHER_LIST, "program_list_missing"),
        (selection(), program_list(type="stringValue"), "program_list_invalid_type"),
        (selection(), program_list(value=[{"id": "1"}]), "program_list_invalid_ids"),
        (selection(), DUPLICATE_IDS, "program_list_invalid_ids"),
        (selection(), program_list(value={"id": 1}), "program_list_invalid_value"),
        (selection(), program_list(value=[]), "program_list_empty"),
        (selection(writeable=0), program_list(), "selection_not_writable"),
        (selection(value=30.0), program_list(), "reading_invalid"),
        (USER_MODE, program_list(), "not_a_program_selection"),
    ],
)
def test_each_missing_program_selection_has_its_reason(current, programs, reason):
    assert selection_issue(current, programs) == reason
    assert bool(selection_options(current, programs)) is (reason is None)


async def test_diagnostics_name_the_reason_per_zone_without_names(environment):
    coordinator, _, _ = environment
    wire_coordinator(environment, selection(), manual_selection())
    report = _rrc_program_diagnostics(coordinator)
    assert report["program_list"] == "missing"
    assert [zone["selection"] for zone in report["zones"]] == [
        "program_list_missing"
    ] * 2
    assert report["program_selections_offered"] == 0

    wire_coordinator(
        environment, selection(), manual_selection(writeable=0), program_list()
    )
    report = _rrc_program_diagnostics(coordinator)
    assert report == {
        "program_selections": 2,
        "program_selections_offered": 1,
        "program_list": "current",
        "program_list_issue": None,
        "program_count": 4,
        "program_names_readable": 3,
        "zones": [
            {"path": CLOCK, "selection": "offered", "used": True},
            {
                "path": MANUAL_CLOCK,
                "selection": "selection_not_writable",
                "used": False,
            },
        ],
    }
    dump = json.dumps(report)
    assert "Wohnen" not in dump and b64("Wohnen") not in dump and "Bad" not in dump

    # A list that no longer answers is known, but not current.
    coordinator.data[PROGRAMS] = replace(coordinator.data[PROGRAMS], available=False)
    assert _rrc_program_diagnostics(coordinator)["program_list"] == "not_current"
    coordinator.resources[PROGRAMS] = program_list(type="stringValue")
    del coordinator.data[PROGRAMS]
    report = _rrc_program_diagnostics(coordinator)
    assert report["program_list_issue"] == "type"
    assert report["zones"][0]["selection"] == "program_list_invalid_type"


# ------------------------------------------------------------- RRC-05


def test_program_names_decode_and_fall_back_to_the_name_resource():
    names = {
        4: wire("/programs/pg4/name", type="stringValue", value=b64("Gäste")),
        2: wire("/programs/pg2/name", type="stringValue", value=b64("Ignored")),
    }
    assert program_names(program_list(), names) == {
        1: "Wohnen",
        2: "Arbeit",  # the list entry comes first
        3: "Bad",
        4: "Gäste",
    }
    assert program_names(program_list()) == {1: "Wohnen", 2: "Arbeit", 3: "Bad"}
    # Duplicate names stay apart: the stable ID is the key.
    same = program_list(value=[{"id": 1, "name": "Tag"}, {"id": 2, "name": "Tag"}])
    assert program_names(same) == {1: "Tag", 2: "Tag"}
    # Never a guess: no list, a broken list, or a name for another path.
    assert program_names(None, names) == {}
    assert program_names(program_list(value=[{"id": "1"}])) == {}
    wrong = {4: wire("/programs/pg5/name", type="stringValue", value="X")}
    assert 4 not in program_names(program_list(), wrong)
    unused = {
        4: wire("/programs/pg4/name", type="stringValue", value="X", used="false")
    }
    assert 4 not in program_names(program_list(), unused)


async def test_select_keeps_ids_as_options_and_shows_names_as_attributes(environment):
    coordinator, client, _ = environment
    name = wire("/programs/pg4/name", type="stringValue", value=b64("Gäste"))
    state = wire_coordinator(environment, program_list(), selection(), name)
    (description,) = descriptions(state)
    entity = RrcProgramSelect(coordinator, description)
    assert entity.options == ["1", "2", "3", "4"]
    assert entity.extra_state_attributes == {
        "program_names": {"1": "Wohnen", "2": "Arbeit", "3": "Bad", "4": "Gäste"},
        "program_name": "Arbeit",
    }
    assert "program_names" in entity._unrecorded_attributes
    # Automations select by ID; the name is never sent.
    await entity.async_select_option("4")
    client.put_resource_value.assert_awaited_once_with("synthetic-gateway", CLOCK, 4)
    assert entity.extra_state_attributes["program_name"] == "Gäste"
    # A stale list shows no names and no options.
    coordinator.data[PROGRAMS] = replace(coordinator.data[PROGRAMS], available=False)
    assert entity.extra_state_attributes == {"program_names": {}, "program_name": None}


# ------------------------------------------------------------- RRC-06


async def test_temporary_setpoint_zero_means_no_override(environment):
    coordinator, _, _ = environment
    idle = wire(
        OVERRIDE,
        type="floatValue",
        writeable=1,
        unitOfMeasure="C",
        minValue=0,
        maxValue=30,
        stepSize=0.5,
        value=0,
    )
    # The sensor exists before the first override and stays available.
    assert supports_entity(idle)
    assert rrc.scalar_value(idle) == 0 and rrc.display_value(idle) is None
    assert _capability_diagnostics(idle, None, {}, 0)["rrc_contract"]["reading_valid"]
    wire_coordinator(environment, idle)
    sensor = BoschBuderusSensor(
        coordinator, build_sensor_descriptions({OVERRIDE: idle})[0]
    )
    assert sensor.available and sensor.native_value is None
    coordinator.async_set_updated_data(snapshots(replace(idle, value=20.5)))
    assert sensor.available and sensor.native_value == 20.5
    # Other zero temperatures stay readings.
    setpoint = replace(idle, path="/zones/zn3/manualTemperatureHeating")
    assert rrc.display_value(setpoint) == 0


# ------------------------------------------------------------- RRC-07


def test_day_programs_are_read_like_the_vendor_app():
    schedule = parse_schedule(day())
    assert schedule is not None and schedule.editable
    assert [point.minute for point in schedule.points] == [420, 1380]
    # A week stays a week program.
    assert parse_schedule(week(type="dayProgram")) is None
    # An extra placeholder field stays readable but is not editable.
    extra = day(value=[{"d": "Mo", "t": 420, "Th": 19, "name": 0}])
    parsed = parse_schedule(extra)
    assert parsed is not None and not parsed.editable
    assert parsed.encode() == extra.value
    # Without a day key, or for another day, there is no interpretation.
    assert parse_schedule(day(value=[{"t": 420, "Th": 19}])) is None
    assert parse_schedule(day(value=[{"d": "Tu", "t": 420, "Th": 19}])) is None
    assert parse_schedule(day(type="programArray")) is None


async def test_switch_point_edit_with_a_day_program_is_read_back(environment):
    coordinator, client, _ = environment
    state = wire_coordinator(environment, week(), day())
    await coordinator.async_edit_rrc_program(state[WEEK], 0, 430, 21)
    client.put_resource_value.assert_awaited_once_with(
        "synthetic-gateway",
        MONDAY,
        [{"d": "Mo", "t": 430, "Th": 21}, {"d": "Mo", "t": 1380, "Th": 15}],
    )
    assert [call.args[1] for call in client.get_resource.await_args_list] == [
        WEEK,
        MONDAY,
        WEEK,
    ]
    assert coordinator.data[WEEK].resource.value[0] == {"d": "Mo", "t": 430, "Th": 21}


async def test_day_program_with_unknown_fields_is_not_written(environment):
    coordinator, client, _ = environment
    state = wire_coordinator(
        environment,
        week(),
        day(
            value=[
                {"d": "Mo", "t": 420, "Th": 19, "name": 0},
                {"d": "Mo", "t": 1380, "Th": 15, "name": 0},
            ]
        ),
    )
    with pytest.raises(WriteValidationError):
        await coordinator.async_edit_rrc_program(state[WEEK], 0, 430, 21)
    client.put_resource_value.assert_not_awaited()


# ------------------------------------------------------------- RRC-01


def zone_tree(*, filler: int = 0) -> dict[str, Resource]:
    """Five TC100.2 zones whose leaves the gateway advertises.

    ``filler`` advertised but unreadable leaves follow the zone leaves, as on
    a large plant; they use the discovery budget before any catalog path.
    """
    resources: dict[str, Resource] = {
        "/zones": container("/zones", *(f"/zones/zn{n}" for n in range(1, 6))),
        PROGRAMS: program_list(),
    }
    for number in range(1, 6):
        root = f"/zones/zn{number}"
        manual = number == 4
        leaves = {
            f"{root}/userMode": wire(
                f"{root}/userMode",
                type="stringValue",
                used="true",
                writeable=1,
                value="manual" if manual else "clock",
            ),
            f"{root}/clockProgram": selection(
                f"{root}/clockProgram",
                float(number % 4 + 1),
                used="false" if manual else "true",
            ),
        }
        extra = (
            tuple(f"{root}/extra{index}" for index in range(filler))
            if number == 5
            else ()
        )
        resources[root] = container(root, *leaves, *extra)
        resources.update(leaves)
    return resources


async def bulk_discovery(tree: dict[str, Resource], **kwargs):
    client = AsyncMock()
    batches: list[tuple[str, ...]] = []

    async def bulk(gateway, paths):
        batches.append(tuple(paths))
        return tuple(
            BatchItemResult(gateway, path, 200, tree[path])
            if path in tree
            else BatchItemResult(gateway, path, 404)
            for path in paths
        )

    client.get_resources_bulk.side_effect = bulk
    report = DiscoveryDiagnostics()
    found = await async_discover_resources(
        client, "fixture", diagnostics=report, **kwargs
    )
    return found, report, batches


async def test_program_list_is_read_before_a_large_tree_ends_the_budget():
    found, report, batches = await bulk_discovery(zone_tree(filler=600))
    assert report.stop_reason == "resource_limit"
    assert sum(map(len, batches)) <= MAX_DISCOVERY_RESOURCES
    assert all(f"/zones/zn{n}/clockProgram" in found for n in range(1, 6))
    assert PROGRAMS in found
    assert len(descriptions(found)) == 5
    # Only the order changes: the lists join the batch of the zones.
    (batch,) = [batch for batch in batches if PROGRAMS in batch]
    assert {rrc.ZONE_LIST, "/zones/zn1", "/zones/zn5"} <= set(batch)
    assert report.paths[PROGRAMS].source.value == "catalog"


async def test_rrc_lists_cost_no_extra_request_on_a_small_tree():
    found, report, batches = await bulk_discovery(zone_tree())
    assert report.completed and PROGRAMS in found
    assert sum(path == PROGRAMS for batch in batches for path in batch) == 1
    assert sum(path == rrc.ZONE_LIST for batch in batches for path in batch) == 1
    (batch,) = [batch for batch in batches if PROGRAMS in batch]
    assert "/zones/zn1" in batch
    assert len(descriptions(found)) == 5


# ---------------------------------------------- RRC-01 recovery (real cycles)


async def test_failed_program_list_is_read_again_and_offers_the_selection(
    hass: HomeAssistant, clock: Clock
) -> None:
    cloud = Cloud(zone_tree())
    cloud.down[PROGRAMS] = 503  # the bulk item and its single read fail
    coordinator = _coordinator(hass, cloud, _inventory(hass))
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert PROGRAMS not in coordinator.resources
    assert CLOCK in coordinator.resources
    assert coordinator.discovery_recovery.missing == {PROGRAMS: "http_503"}
    assert not descriptions(coordinator.resources)

    del cloud.down[PROGRAMS]
    before = len(cloud.requests)
    clock.now += RECOVERY_SCHEDULE[0]
    await coordinator.async_refresh()
    assert coordinator.discovery_recovery.state == "recovered"
    assert coordinator.data[PROGRAMS].available
    assert len(descriptions(coordinator.resources)) == 5
    # The search asked for the list only, next to the normal poll.
    searched = [paths for paths in cloud.requests[before:] if PROGRAMS in paths]
    assert searched == [(PROGRAMS,)]
    # From now on it is polled like any other resource.
    assert any(PROGRAMS in group for group in coordinator._paths_by_group.values())


async def test_permanently_failing_program_list_is_searched_a_bounded_time(
    hass: HomeAssistant, clock: Clock
) -> None:
    cloud = Cloud(zone_tree())
    cloud.down[PROGRAMS] = 404
    coordinator = _coordinator(hass, cloud, _inventory(hass))
    await coordinator.async_refresh()
    start = clock.now
    for delay in RECOVERY_SCHEDULE:
        clock.now = start + delay
        await coordinator.async_refresh()
        assert coordinator.last_update_success
    assert coordinator.discovery_recovery.state == "given_up"
    # One discovery read plus one per search, never more.
    assert cloud.requested(PROGRAMS) == 1 + len(RECOVERY_SCHEDULE)
    clock.now += 3 * 86400
    await coordinator.async_refresh()
    assert cloud.requested(PROGRAMS) == 1 + len(RECOVERY_SCHEDULE)


async def test_zones_without_a_program_selection_do_not_search_the_list(
    hass: HomeAssistant, clock: Clock
) -> None:
    tree = zone_tree()
    for number in range(1, 6):
        root = f"/zones/zn{number}"
        del tree[f"{root}/clockProgram"]
        tree[root] = container(root, f"{root}/userMode")
    del tree[PROGRAMS]
    coordinator = _coordinator(hass, Cloud(tree), _inventory(hass))
    await coordinator.async_refresh()
    assert PROGRAMS not in coordinator.discovery_recovery.missing
    assert coordinator.discovery_recovery.state != "pending"
