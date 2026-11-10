"""RRC discovery, native readings and safe commands with synthetic responses."""

import base64
import json
from dataclasses import replace
from pathlib import Path
from time import monotonic
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating import rrc
from custom_components.bosch_buderus_heating.diagnostics import _capability_diagnostics
from custom_components.bosch_buderus_heating.discovery import (
    DiscoveryDiagnostics,
    async_discover_resources,
)
from custom_components.bosch_buderus_heating.discovery_inventory import (
    resource_from_data,
    resource_to_data,
)
from custom_components.bosch_buderus_heating.number import (
    BoschBuderusNumber,
    build_number_descriptions,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    BatchItemResult,
    RateLimited,
    RequestTimeout,
    Resource,
    ResourceReference,
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.pointt.redaction import (
    diagnostic_resource_path,
)
from custom_components.bosch_buderus_heating.resource_catalog import (
    PollGroup,
    poll_group,
    supports_entity,
)
from custom_components.bosch_buderus_heating.rrc_programs import (
    RrcProgramService,
    edit_point,
    parse_schedule,
    selection_options,
)
from custom_components.bosch_buderus_heating.rrc_select import (
    RrcProgramSelect,
    descriptions,
)
from custom_components.bosch_buderus_heating.select import (
    BoschBuderusSelect,
    build_select_descriptions,
)
from custom_components.bosch_buderus_heating.select import (
    async_setup_entry as setup_selects,
)
from custom_components.bosch_buderus_heating.sensor import (
    BoschBuderusSensor,
    build_sensor_descriptions,
)
from custom_components.bosch_buderus_heating.writes import (
    ControlLocked,
    assess_control,
    validate_control_value,
)
from tests import test_dynamic_controls
from tests.test_dynamic_controls import snapshots

PAYLOADS = json.loads(
    (Path(__file__).parent / "fixtures/contracts/rrc/zones-programs.json").read_text()
)["resources"]
RESOURCES = {item.path: item for item in map(parse_resource, PAYLOADS)}
TC100_PAYLOADS = json.loads(
    (
        Path(__file__).parent / "fixtures/contracts/rrc/tc100-string-flags.json"
    ).read_text()
)["resources"]
TC100_PAYLOADS_BY_ID = {item["id"]: item for item in TC100_PAYLOADS}
TC100_RESOURCES = {item.path: item for item in map(parse_resource, TC100_PAYLOADS)}
MANUAL = "/zones/zn2/manualTemperatureHeating"
MODE = "/zones/zn2/userMode"
SELECTION = "/zones/zn2/clockProgram"
WEEK = "/programs/pg3/week"
DAY = "/programs/pg3/monday"


@pytest.fixture
def environment(hass, enable_custom_integrations):
    yield from test_dynamic_controls.environment.__wrapped__(hass)


def wire(environment):
    coordinator, client, _ = environment
    state = dict(RESOURCES)
    coordinator.resources = dict(state)
    coordinator.data = snapshots(*state.values())
    coordinator._rrc_program_service = RrcProgramService(client, sleep=AsyncMock())

    async def get(gateway, path):
        return state[path]

    async def put(gateway, path, value):
        state[path] = replace(state[path], value=value)
        if path == DAY:
            others = [item for item in state[WEEK].value if item["d"] != "Mo"]
            state[WEEK] = replace(state[WEEK], value=value + others)

    client.get_resource.side_effect = get
    client.put_resource_value.side_effect = put
    return state


@pytest.mark.parametrize("parent_status", [200, 403, 404, 406])
async def test_only_reported_zone_and_program_ids_are_expanded(parent_status):
    client = AsyncMock()
    resources = {"/zones": Resource("/zones"), **RESOURCES}

    async def bulk(gateway, paths):
        return tuple(
            BatchItemResult(gateway, path, 200, resources[path])
            if path in resources
            else BatchItemResult(gateway, path, parent_status)
            for path in paths
        )

    client.get_resources_bulk.side_effect = bulk
    report = DiscoveryDiagnostics()
    discovered = await async_discover_resources(
        client, "fixture", roots=("/zones",), diagnostics=report
    )
    requested = {
        path
        for call in client.get_resources_bulk.call_args_list
        for path in call.args[1]
    }
    assert {WEEK, MANUAL, MODE, "/programs/pg7/week"} <= requested
    assert not any(
        "/zn1" in path or "/pg1" in path or "/zone2" in path for path in requested
    )
    assert not any(path.endswith("/monday") for path in requested)
    assert RESOURCES[WEEK] == discovered[WEEK]
    assert (
        max(len(call.args[1]) for call in client.get_resources_bulk.call_args_list)
        <= 30
    )
    assert report.completed
    client.get_resource.assert_not_awaited()


async def test_program_root_is_not_probed_for_connectkey():
    client = AsyncMock()

    async def bulk(gateway, paths):
        return tuple(
            BatchItemResult(
                gateway,
                path,
                200,
                Resource(path, references=(ResourceReference("/zones/zone2"),)),
            )
            if path == "/zones"
            else BatchItemResult(gateway, path, 404)
            for path in paths
        )

    client.get_resources_bulk.side_effect = bulk
    await async_discover_resources(client, "fixture", roots=("/zones",))
    assert not any(
        path.startswith("/programs")
        for call in client.get_resources_bulk.call_args_list
        for path in call.args[1]
    )


@pytest.mark.parametrize("limit,depth", [(4, 8), (512, 1)])
async def test_rrc_discovery_keeps_global_limits(limit, depth):
    client = AsyncMock()

    async def bulk(gateway, paths):
        return tuple(
            BatchItemResult(gateway, path, 200, RESOURCES.get(path, Resource(path)))
            for path in paths
        )

    client.get_resources_bulk.side_effect = bulk
    report = DiscoveryDiagnostics()
    await async_discover_resources(
        client,
        "fixture",
        roots=(rrc.ZONE_LIST,),
        maximum_resources=limit,
        maximum_depth=depth,
        diagnostics=report,
    )
    assert (
        sum(len(call.args[1]) for call in client.get_resources_bulk.call_args_list)
        <= limit
    )
    assert report.stop_reason in {"resource_limit", "depth_limit"}


@pytest.mark.parametrize(
    "value",
    [
        [{"id": True}],
        [{"id": -1}],
        [{"id": 1.5}],
        [{"id": "2"}],
        [{"id": 2}, {"id": 2}],
        [None],
        [{"id": 10000}],
        {},
    ],
)
def test_malformed_lists_do_not_supply_ids(value):
    assert rrc.listed_ids(replace(RESOURCES[rrc.ZONE_LIST], value=value)) is None


@pytest.mark.parametrize("path", [path for path in RESOURCES if rrc.scalar_field(path)])
async def test_separate_rrc_readings_and_stable_identity(environment, path):
    coordinator, _, _ = environment
    wire(environment)
    resource = RESOURCES[path]
    descriptions_ = build_sensor_descriptions({path: resource})
    assert len(descriptions_) == 1
    sensor = BoschBuderusSensor(coordinator, descriptions_[0])
    assert sensor.native_value == resource.value
    identity = sensor.unique_id
    for changed in (
        replace(resource, value=None),
        replace(resource, metadata=replace(resource.metadata, available=False)),
        replace(resource, has_values=True),
    ):
        coordinator.async_set_updated_data(snapshots(changed))
        assert not sensor.available and sensor.native_value is None
    coordinator.async_set_updated_data(snapshots(resource))
    assert sensor.available and sensor.unique_id == identity
    assert poll_group(resource) in (PollGroup.CONTROL, PollGroup.FAST)


@pytest.mark.parametrize(
    "field,value",
    [
        ("available", False),
        ("used", False),
        ("used", 1),
        ("used", "True"),
        ("available", "yes"),
    ],
)
def test_device_availability_is_not_boolean_coerced(field, value):
    payload = next(item for item in PAYLOADS if item["id"] == MANUAL)
    resource = parse_resource({**payload, field: value})
    assert not supports_entity(resource)
    assert not assess_control(resource).eligible


@pytest.mark.parametrize(
    "path",
    [
        "/zones/zn1/temperatureActual",
        "/zones/zn1/temperatureHeatingSetpoint",
        "/zones/zn1/userMode",
        "/zones/zn1/clockProgram",
    ],
)
def test_observed_string_used_flag_yields_tc100_readings(path):
    resource = TC100_RESOURCES[path]
    assert TC100_PAYLOADS_BY_ID[path]["used"] == "true"
    assert resource.metadata.used is True
    assert supports_entity(resource)
    assert rrc.scalar_value(resource) == TC100_PAYLOADS_BY_ID[path]["value"]


async def test_observed_tc100_zone_creates_reading_and_program_select(environment):
    coordinator, _, _ = environment
    coordinator.resources = dict(TC100_RESOURCES)
    coordinator.data = snapshots(*TC100_RESOURCES.values())
    reading = TC100_RESOURCES["/zones/zn1/temperatureActual"]
    sensor = BoschBuderusSensor(
        coordinator, build_sensor_descriptions({reading.path: reading})[0]
    )
    assert sensor.available
    assert sensor.native_value == TC100_PAYLOADS_BY_ID[reading.path]["value"]
    selects = [
        RrcProgramSelect(coordinator, description)
        for description in descriptions(TC100_RESOURCES)
    ]
    # #113 (RRC-03): the manual zone zn4 keeps its assigned program as well.
    assert [select.entity_description.resource_path for select in selects] == [
        "/zones/zn1/clockProgram",
        "/zones/zn4/clockProgram",
    ]
    assert all(select.available for select in selects)
    assert [select.options for select in selects] == [["1", "2", "3", "4"]] * 2
    assert [select.current_option for select in selects] == ["2", "4"]


@pytest.mark.parametrize(
    ("path", "used"),
    [("/zones/zn1/temperatureActual", True), ("/zones/zn4/clockProgram", False)],
)
def test_diagnostics_report_observed_string_flags_as_valid(path, used):
    contract = _capability_diagnostics(TC100_RESOURCES[path], None, {}, 0)[
        "rrc_contract"
    ]
    assert contract["used"] is used
    assert contract["available"] is None
    assert contract["availability_flags_valid"]
    # #113 (RRC-03): an unused program selection is still a valid reading.
    assert contract["reading_valid"]


def test_observed_unused_program_selection_keeps_its_assignment():
    """#113 (RRC-03): a manual zone keeps its program, as in the vendor app."""
    resource = TC100_RESOURCES["/zones/zn4/clockProgram"]
    assert resource.metadata.used is False
    assert rrc.scalar_value(resource) == 4
    assert supports_entity(resource)
    assert selection_options(resource, TC100_RESOURCES[rrc.PROGRAM_LIST]) == (
        1,
        2,
        3,
        4,
    )


@pytest.mark.parametrize("path", ["/zones/zn1/userMode", "/zones/zn4/userMode"])
def test_observed_mode_without_allowed_values_offers_manual_and_clock(path):
    # #113: the TC100.2 sends no allowedValues for userMode at all.
    resource = TC100_RESOURCES[path]
    assert "allowedValues" not in TC100_PAYLOADS_BY_ID[path]
    assert resource.metadata.allowed_values_missing
    assert rrc.scalar_value(resource) == TC100_PAYLOADS_BY_ID[path]["value"]
    assert rrc.control_issue(resource) is None
    assessment = assess_control(resource)
    assert assessment.eligible and assessment.current_value_issue is None
    assert assessment.options == ("manual", "clock")
    assert [item.options for item in build_select_descriptions({path: resource})] == [
        ["manual", "clock"]
    ]


@pytest.mark.parametrize("advertised", [[], None, ["manual"], ["eco", "comfort"]])
def test_advertised_mode_options_keep_the_strict_rules(advertised):
    payload = {
        **TC100_PAYLOADS_BY_ID["/zones/zn1/userMode"],
        "allowedValues": advertised,
        "value": "clock",
    }
    resource = parse_resource(payload)
    assert not resource.metadata.allowed_values_missing
    assert rrc.allowed_values(resource) == tuple(advertised or ())
    assessment = assess_control(resource)
    assert not assessment.eligible
    assert assessment.rejection_reason == "current_option_not_advertised"
    assert not build_select_descriptions({resource.path: resource})


@pytest.mark.parametrize("value", ["undefined", "eco", "", None, 1])
def test_mode_fallback_rejects_a_current_value_outside_manual_and_clock(value):
    payload = {**TC100_PAYLOADS_BY_ID["/zones/zn1/userMode"], "value": value}
    resource = parse_resource(payload)
    assert rrc.control_issue(resource) == "invalid_rrc_reading"
    assert not assess_control(resource).eligible


@pytest.mark.parametrize(
    "path",
    [
        "/heatingCircuits/hc1/operationMode",
        "/zones/zone1/heating/operationMode",
        "/zones/zn1/clockProgram",
    ],
)
def test_mode_fallback_is_limited_to_rrc_user_mode(path):
    resource = parse_resource(
        {"id": path, "type": "stringValue", "writeable": 1, "value": "manual"}
    )
    assert resource.metadata.allowed_values_missing
    assert rrc.allowed_values(resource) == ()
    assert assess_control(resource).options == ()
    assert not assess_control(resource).eligible


def locked(path="/zones/zn1/userMode", **flags):
    payload = {**TC100_PAYLOADS_BY_ID[path], "available": "false", **flags}
    return parse_resource(payload)


def test_locked_mode_keeps_its_value_and_refuses_every_write():
    resource = locked()
    assert rrc.mode_locked(resource)
    assert rrc.scalar_value(resource) == "clock"
    assert supports_entity(resource)
    assessment = assess_control(resource)
    assert assessment.eligible and assessment.options == ("manual", "clock")
    for value in ("manual", "clock"):
        with pytest.raises(ControlLocked):
            validate_control_value(resource, value)
    unlocked = locked(available="true")
    assert not rrc.mode_locked(unlocked)
    validate_control_value(unlocked, "manual")


@pytest.mark.parametrize(
    ("path", "flags"),
    [
        ("/zones/zn1/userMode", {"used": "false"}),
        ("/zones/zn1/userMode", {"used": None}),
        ("/zones/zn1/userMode", {"used": 1}),
        ("/zones/zn1/temperatureActual", {}),
        ("/zones/zn1/temperatureHeatingSetpoint", {}),
        ("/zones/zn1/clockProgram", {}),
    ],
)
def test_lock_reading_is_limited_to_a_used_user_mode(path, flags):
    payload = {**TC100_PAYLOADS_BY_ID[path], "available": "false", **flags}
    if flags.get("used", "") is None:
        del payload["used"]
    resource = parse_resource(payload)
    assert not rrc.mode_locked(resource)
    assert rrc.scalar_value(resource) is None
    assert not assess_control(resource).eligible


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("V29obnppbW1lcg==", "Wohnzimmer"),
        ("S8O8Y2hl", "Küche"),
        ("  QmFkIE9H  ", "Bad OG"),
        ("Flur", "Flur"),  # valid Base64, but not UTF-8: the raw text
        ("Bad", "Bad"),
        ("Büro", "Büro"),
        ("V29obnppbW1lcg", "V29obnppbW1lcg"),  # no padding
        ("V29obnppbW1lch==", "V29obnppbW1lch=="),  # not canonical
        ("AAEC", "AAEC"),  # decodes to control characters
        ("AEIAYQBk", "AEIAYQBk"),  # UTF-16 is not the RRC encoding
        ("IA==", "IA=="),  # decodes to a blank
        ("", None),
        ("   ", None),
        ("a\nb", None),
        ("/w==", "/w=="),  # decodes to invalid UTF-8
        (base64.b64encode(b"x" * 65).decode(), None),  # too long either way
        ("x" * 65, None),
        (None, None),
        (7, None),
    ],
)
def test_rrc_names_decode_base64_and_otherwise_keep_the_text(value, expected):
    assert rrc.display_name(value) == expected


@pytest.mark.parametrize(
    "value",
    [True, None, "21", {}, [], float("nan"), float("inf"), 32767, -32768, 4, 31],
)
def test_invalid_rrc_temperature_is_unavailable(value):
    assert rrc.scalar_value(replace(RESOURCES[MANUAL], value=value)) is None


@pytest.mark.parametrize("unit", ["F", "K", None, "unknown"])
def test_rrc_does_not_inherit_connectkey_unit_assumptions(unit):
    resource = replace(
        RESOURCES[MANUAL], metadata=replace(RESOURCES[MANUAL].metadata, unit=unit)
    )
    assert not supports_entity(resource) and not assess_control(resource).eligible


@pytest.mark.parametrize(
    "path,target",
    [
        (MANUAL, 21.5),
        ("/zones/zn5/manualTemperatureHeating", 20.25),
        (MODE, "manual"),
        ("/zones/zn5/userMode", "clock"),
    ],
)
async def test_each_control_revalidates_then_writes_only_its_zone(
    environment, path, target
):
    coordinator, client, _ = environment
    state = wire(environment)
    resource = state[path]
    cls, builder = (
        (BoschBuderusSelect, build_select_descriptions)
        if path.endswith("/userMode")
        else (BoschBuderusNumber, build_number_descriptions)
    )
    entity = cls(coordinator, builder({path: resource})[0])
    if isinstance(target, str):
        assert entity.options == ["manual", "clock"]
        await entity.async_select_option(target)
    else:
        assert entity.native_step == resource.metadata.step_size
        await entity.async_set_native_value(target)
    client.put_resource_value.assert_awaited_once_with(
        "synthetic-gateway", path, target
    )
    assert client.get_resource.await_args_list[0].args[1] == path
    assert len(client.get_resource.await_args_list) == 2
    assert state[path].value == target


async def test_fresh_permissions_and_steps_block_invalid_writes(environment):
    coordinator, client, _ = environment
    state = wire(environment)
    policy = assess_control(state[MANUAL]).policy
    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(MANUAL, 20.1, policy)
    state[MANUAL] = replace(
        state[MANUAL], metadata=replace(state[MANUAL].metadata, writable=False)
    )
    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(MANUAL, 21, policy)
    client.put_resource_value.assert_not_awaited()


def test_override_and_program_arrays_are_not_scalar_controls():
    override = RESOURCES["/zones/zn2/clockOverride/temperatureHeating"]
    assert rrc.scalar_value(override) == 22
    assert (
        assess_control(override).rejection_reason == "rrc_override_lifecycle_unverified"
    )
    assert not build_number_descriptions({override.path: override})
    assert not build_sensor_descriptions({WEEK: RESOURCES[WEEK]})


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: [],
        lambda data: data[::-1],
        lambda data: [data[0], data[0], *data[1:]],
        lambda data: [{**data[0], "futureField": {"keep": [1, 2]}}, *data[1:]],
        lambda data: [{**data[0], "Th": 0}, *data[1:]],
    ],
)
def test_parser_roundtrips_even_noneditable_programs_without_data_loss(mutation):
    value = mutation(RESOURCES[WEEK].value)
    schedule = parse_schedule(replace(RESOURCES[WEEK], value=value))
    assert schedule is not None and schedule.encode() == value
    if value:
        assert not schedule.editable
        with pytest.raises(WriteValidationError):
            edit_point(schedule, 0, 5, 20)


@pytest.mark.parametrize(
    "item",
    [
        {"d": "Monday", "t": 0, "Th": 20},
        {"d": "Mo", "t": True, "Th": 20},
        {"d": "Mo", "t": 1440, "Th": 20},
        {"d": "Mo", "t": -1, "Th": 20},
        {"d": "Mo", "t": 0, "Th": float("nan")},
        {"d": "Mo", "t": 0, "Th": 31},
        {"d": "Mo", "t": 0, "Th": True},
        None,
    ],
)
def test_malformed_program_has_no_editable_interpretation(item):
    assert parse_schedule(replace(RESOURCES[WEEK], value=[item])) is None


async def test_day_edit_preserves_other_days_and_reconciles_week(environment):
    coordinator, client, _ = environment
    state = wire(environment)
    await coordinator.async_edit_rrc_program(state[WEEK], 1, 430, 22)
    client.put_resource_value.assert_awaited_once_with(
        "synthetic-gateway",
        DAY,
        [
            {"d": "Mo", "t": 0, "Th": 16},
            {"d": "Mo", "t": 430, "Th": 22},
            {"d": "Mo", "t": 1320, "Th": 16},
        ],
    )
    assert state[WEEK].value[3:] == RESOURCES[WEEK].value[3:]
    assert coordinator.data[WEEK].resource == state[WEEK]
    assert [call.args[1] for call in client.get_resource.await_args_list] == [
        WEEK,
        DAY,
        WEEK,
    ]


@pytest.mark.parametrize(
    "index,minute,temp",
    [
        (1, 0, 20),
        (1, 426, 20),
        (1, 1440, 20),
        (1, 430, 20.1),
        (1, 430, 4),
        (-1, 430, 20),
        (100, 430, 20),
    ],
)
async def test_invalid_edits_make_no_cloud_calls(environment, index, minute, temp):
    _, client, _ = environment
    wire(environment)
    service = RrcProgramService(client)
    with pytest.raises(WriteValidationError):
        await service.edit("fixture", RESOURCES[WEEK], index, minute, temp)
    client.get_resource.assert_not_awaited()
    client.put_resource_value.assert_not_awaited()


@pytest.mark.parametrize("changed_path", [WEEK, DAY])
async def test_app_change_before_write_is_not_overwritten(environment, changed_path):
    coordinator, client, _ = environment
    state = wire(environment)
    state[changed_path] = replace(
        state[changed_path],
        value=[*state[changed_path].value, {"d": "Mo", "t": 1435, "Th": 18}],
    )
    with pytest.raises(WriteValidationError):
        await coordinator.async_edit_rrc_program(RESOURCES[WEEK], 1, 430, 22)
    client.put_resource_value.assert_not_awaited()
    # C33: a refused preflight sends nothing; the next poll reads the change.
    assert coordinator.data[WEEK].available


async def test_numeric_program_selection_uses_current_list_and_never_guesses(
    environment,
):
    coordinator, client, _ = environment
    state = wire(environment)
    entity = RrcProgramSelect(coordinator, descriptions(state)[0])
    assert entity.options == ["3", "7"] and entity.current_option == "3"
    await entity.async_select_option("7")
    assert entity.current_option == "7"
    client.put_resource_value.assert_awaited_once_with(
        "synthetic-gateway", SELECTION, 7
    )
    with pytest.raises(HomeAssistantError):
        await entity.async_select_option("4")
    client.put_resource_value.reset_mock()
    state[rrc.PROGRAM_LIST] = replace(state[rrc.PROGRAM_LIST], value=[{"id": 7}])
    with pytest.raises(HomeAssistantError):
        await entity.async_select_option("3")
    client.put_resource_value.assert_not_awaited()


@pytest.mark.parametrize("operation", ["select", "edit"])
@pytest.mark.parametrize(
    "error", [RequestTimeout("lost"), RateLimited(90), AuthenticationError("expired")]
)
async def test_write_errors_never_retry_put(environment, operation, error):
    coordinator, client, _ = environment
    state = wire(environment)
    client.put_resource_value.side_effect = error
    with (
        patch.object(coordinator._config_entry, "async_start_reauth") as reauth,
        pytest.raises(
            WriteNotConfirmed if isinstance(error, RequestTimeout) else type(error)
        ),
    ):
        if operation == "select":
            await coordinator.async_select_rrc_program(SELECTION, 7)
        else:
            await coordinator.async_edit_rrc_program(state[WEEK], 1, 430, 22)
    assert client.put_resource_value.await_count == 1
    if isinstance(error, AuthenticationError):
        reauth.assert_called_once_with(coordinator.hass)
    else:
        reauth.assert_not_called()
    if isinstance(error, RateLimited):
        assert coordinator._cloud_backoff_until > monotonic()


async def test_unknown_schedule_fields_never_reach_put_or_diagnostics(environment):
    coordinator, client, _ = environment
    state = wire(environment)
    secret = "private-occupancy-note"
    resource = replace(state[WEEK], unknown_fields=frozenset({secret}))
    schedule = parse_schedule(resource)
    assert schedule is not None and not schedule.editable
    report = _capability_diagnostics(resource, None, {}, 0)
    assert secret not in json.dumps(report)
    assert "Th" not in json.dumps(report)
    assert not report["rrc_contract"]["existing_points_editable"]
    with pytest.raises(WriteValidationError):
        await coordinator.async_edit_rrc_program(resource, 1, 430, 22)
    client.put_resource_value.assert_not_awaited()
    assert (
        diagnostic_resource_path("/programs/private-name/week") == "/programs/{pg}/week"
    )
    assert diagnostic_resource_path(WEEK) == WEEK


async def test_late_rrc_program_control_recovers_without_duplicate_entity(
    hass, environment
):
    coordinator, _client, entry = environment
    state = wire(environment)
    coordinator.data = {}
    added = []
    await setup_selects(hass, entry, added.extend)
    assert added == []
    coordinator.async_set_updated_data(snapshots(*state.values()))
    controls = [entity for entity in added if isinstance(entity, RrcProgramSelect)]
    assert len(controls) == 2
    identity = controls[0].unique_id
    coordinator.async_set_updated_data({})
    assert not controls[0].available
    coordinator.async_set_updated_data(snapshots(*state.values()))
    assert controls[0].available and controls[0].unique_id == identity
    assert (
        len([entity for entity in added if isinstance(entity, RrcProgramSelect)]) == 2
    )


TC100_MODE = "/zones/zn1/userMode"
TC100_NAME = "/zones/zn1/name"
DASH = " \N{EN DASH} "


def wire_tc100(environment, **changes):
    """A TC100.2-shaped gateway whose reads return the current state."""
    coordinator, client, _ = environment
    state = {**TC100_RESOURCES, **changes}
    coordinator.resources = dict(state)
    coordinator.data = snapshots(*state.values())

    async def get(gateway, path):
        return state[path]

    async def put(gateway, path, value):
        state[path] = replace(state[path], value=value)

    client.get_resource.side_effect = get
    client.put_resource_value.side_effect = put
    return state


async def tc100_mode_select(hass, environment):
    added = []
    await setup_selects(hass, environment[2], added.extend)
    return next(
        entity
        for entity in added
        if isinstance(entity, BoschBuderusSelect)
        and entity.entity_description.resource_path == TC100_MODE
    )


async def test_tc100_mode_select_writes_only_manual_and_clock(hass, environment):
    coordinator, client, _ = environment
    state = wire_tc100(environment)
    entity = await tc100_mode_select(hass, environment)
    assert entity.available
    assert entity.options == ["manual", "clock"]
    assert entity.current_option == "clock"
    await entity.async_select_option("manual")
    client.put_resource_value.assert_awaited_once_with(
        "synthetic-gateway", TC100_MODE, "manual"
    )
    assert state[TC100_MODE].value == "manual"
    assert entity.current_option == "manual"
    client.put_resource_value.reset_mock()
    with pytest.raises(ServiceValidationError):
        await entity.async_select_option("eco")
    # The write path refuses another value as well, before sending anything.
    policy = entity.entity_description.write_policy
    for value in ("eco", "undefined", ""):
        with pytest.raises(WriteValidationError):
            await coordinator.async_write_control(TC100_MODE, value, policy)
    client.put_resource_value.assert_not_awaited()


@pytest.mark.parametrize("advertised", [[], ["eco"]])
async def test_mode_write_stops_when_the_gateway_now_advertises_options(
    hass, environment, advertised
):
    _, client, _ = environment
    state = wire_tc100(environment)
    entity = await tc100_mode_select(hass, environment)
    state[TC100_MODE] = parse_resource(
        {**TC100_PAYLOADS_BY_ID[TC100_MODE], "allowedValues": advertised}
    )
    with pytest.raises(ServiceValidationError) as caught:
        await entity.async_select_option("manual")
    assert caught.value.translation_key == "write_validation_failed"
    client.put_resource_value.assert_not_awaited()


async def test_locked_mode_stays_visible_and_refuses_with_its_reason(hass, environment):
    coordinator, client, _ = environment
    state = wire_tc100(environment, **{TC100_MODE: locked()})
    entity = await tc100_mode_select(hass, environment)
    assert entity.available and entity.current_option == "clock"
    reading = BoschBuderusSensor(
        coordinator, build_sensor_descriptions({TC100_MODE: state[TC100_MODE]})[0]
    )
    assert reading.available and reading.native_value == "clock"
    for option in ("manual", "clock"):
        with pytest.raises(ServiceValidationError) as caught:
            await entity.async_select_option(option)
        assert caught.value.translation_key == "rrc_mode_locked"
    client.put_resource_value.assert_not_awaited()
    # Each attempt checked the gateway first; nothing changed the reading.
    assert [call.args[1] for call in client.get_resource.await_args_list] == [
        TC100_MODE,
        TC100_MODE,
    ]
    assert coordinator.data[TC100_MODE].available
    # The lock ends on the gateway: the next change goes through.
    state[TC100_MODE] = locked(available="true")
    await entity.async_select_option("manual")
    client.put_resource_value.assert_awaited_once_with(
        "synthetic-gateway", TC100_MODE, "manual"
    )


async def test_mode_locked_after_it_was_shown_sends_nothing(hass, environment):
    _, client, _ = environment
    state = wire_tc100(environment)
    entity = await tc100_mode_select(hass, environment)
    state[TC100_MODE] = locked()
    with pytest.raises(ServiceValidationError) as caught:
        await entity.async_select_option("manual")
    assert caught.value.translation_key == "rrc_mode_locked"
    client.put_resource_value.assert_not_awaited()


@pytest.mark.parametrize(
    ("raw", "group"),
    [
        ("V29obnppbW1lcg==", "Wohnzimmer"),
        ("Flur", "Flur"),
        ("AAEC", "AAEC"),
        ("\u0001", "RRC zone 1"),
        ("", "RRC zone 1"),
        (None, "RRC zone 1"),
    ],
)
async def test_zone_names_decode_without_changing_identities(
    hass, environment, raw, group
):
    coordinator, _, _ = environment
    reading = TC100_RESOURCES["/zones/zn1/temperatureActual"]

    async def entities():
        sensor = BoschBuderusSensor(
            coordinator, build_sensor_descriptions({reading.path: reading})[0]
        )
        return await tc100_mode_select(hass, environment), sensor

    wire_tc100(environment)
    unnamed = [entity.unique_id for entity in await entities()]
    changes = {}
    if raw is not None:
        changes[TC100_NAME] = parse_resource(
            {"id": TC100_NAME, "type": "stringValue", "writeable": 1, "value": raw}
        )
    wire_tc100(environment, **changes)
    select, sensor = await entities()
    assert select.name == f"{group}{DASH}Set heating mode"
    assert sensor.name == f"{group}{DASH}Room temperature"
    assert [select.unique_id, sensor.unique_id] == unnamed


@pytest.mark.parametrize(
    ("extra", "missing"),
    [
        ({}, True),
        ({"allowedValues": []}, False),
        ({"allowedValues": None}, False),
        ({"allowedValues": ["manual", "clock"]}, False),
    ],
)
def test_parser_tells_a_missing_option_list_from_an_advertised_one(extra, missing):
    payload = {**TC100_PAYLOADS_BY_ID[TC100_MODE], **extra}
    resource = parse_resource(payload)
    assert resource.metadata.allowed_values_missing is missing
    assert assess_control(resource).eligible is (
        missing or bool(extra["allowedValues"])
    )


def test_stored_inventory_keeps_the_missing_option_list():
    resource = TC100_RESOURCES[TC100_MODE]
    data = resource_to_data(resource)
    assert data["metadata"]["allowed_values_missing"] is True
    restored = resource_from_data(data)
    assert restored == resource and assess_control(restored).eligible
    # Stored by alpha.3 and earlier without it: the strict reading.
    del data["metadata"]["allowed_values_missing"]
    assert not resource_from_data(data).metadata.allowed_values_missing
    assert not assess_control(resource_from_data(data)).eligible
    data["metadata"]["allowed_values_missing"] = "true"
    with pytest.raises(ValueError):
        resource_from_data(data)
