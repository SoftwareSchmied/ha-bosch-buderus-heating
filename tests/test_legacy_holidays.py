"""Synthetic ICOM and WDDW2 contracts, independent of modern holiday lists."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.calendar.const import CalendarEntityFeature
from homeassistant.data_entry_flow import FlowResultType

from custom_components.bosch_buderus_heating.binary_sensor import (
    BoschBuderusHolidayActiveBinarySensor,
)
from custom_components.bosch_buderus_heating.calendar import BoschBuderusHolidayCalendar
from custom_components.bosch_buderus_heating.coordinator import Freshness
from custom_components.bosch_buderus_heating.discovery import _optional_children
from custom_components.bosch_buderus_heating.holiday_writes import (
    HolidayChangedError,
    HolidayTimeRangeError,
    create_holiday_values,
)
from custom_components.bosch_buderus_heating.holidays import (
    icom_write_configuration,
    icom_write_values,
    parse_holiday_state,
    parse_holiday_write_configuration,
)
from custom_components.bosch_buderus_heating.legacy_holidays import (
    ICOM_EMPTY_PERIODS,
    WDDW2_HOLIDAY_PATH,
    icom_slots,
    is_legacy_holiday_path,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    RateLimited,
    RequestTimeout,
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.switch import (
    BoschBuderusSwitch,
    build_switch_descriptions,
)
from custom_components.bosch_buderus_heating.writes import assess_control
from tests import test_dynamic_controls
from tests.test_config_flow import (
    _entry_with_writable_holiday,
    _holiday_form_input,
    _start_holiday_options,
)
from tests.test_dynamic_controls import snapshots

PAYLOADS = json.loads(
    (Path(__file__).parent / "fixtures/contracts/holidays/legacy.json").read_text()
)["resources"]
RESOURCES = {item.path: item for item in map(parse_resource, PAYLOADS)}
SLOT = "/system/holidayModes/hm2"
FREE = "/system/holidayModes/hm4"
START = f"{SLOT}/startStop"


@pytest.fixture
def environment(hass, enable_custom_integrations):
    yield from test_dynamic_controls.environment.__wrapped__(hass)


def wire(environment):
    coordinator, client, _ = environment
    state = dict(RESOURCES)
    coordinator.resources = dict(state)
    coordinator.data = snapshots(*state.values())

    async def get(gateway, path):
        return state[path]

    async def put(gateway, path, value):
        if path.endswith("/delete"):
            target = path.rsplit("/", 1)[0] + "/startStop"
            state[target] = replace(state[target], value="2017-01-01/2017-01-01")
        else:
            state[path] = replace(state[path], value=value)

    async def assigned(gateway, slot, values):
        path = f"/system/holidayModes/hm{slot}/assignedTo"
        state[path] = replace(state[path], values=values)

    client.get_resource.side_effect = get
    client.put_resource_value.side_effect = put
    client.put_icom_holiday_assignments.side_effect = assigned
    return state


@pytest.mark.parametrize(
    "timezone", [UTC, ZoneInfo("Europe/Berlin"), ZoneInfo("America/New_York")]
)
def test_inclusive_dates_normalize_once_and_keep_circuit_assignments(timezone):
    state = parse_holiday_state(RESOURCES, fallback_timezone=timezone)
    assert len(state.periods) == 1
    period = state.periods[0]
    assert period.start == datetime(2030, 10, 26, tzinfo=timezone)
    assert period.end == datetime(2030, 10, 29, tzinfo=timezone)
    assert period.all_day and period.source_path == SLOT
    assert period.write_values.assigned_to == ("hc2", "dhw1")
    assert period.write_values.fix_temperature == 17
    assert icom_slots(RESOURCES) == (SLOT, FREE)


@pytest.mark.parametrize("marker", sorted(ICOM_EMPTY_PERIODS))
def test_only_documented_empty_slots_can_be_created(marker):
    resources = {**RESOURCES, START: replace(RESOURCES[START], value=marker)}
    assert not parse_holiday_state(resources).periods
    assert parse_holiday_write_configuration(resources).source_path == SLOT


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        0,
        "",
        "not a date",
        "2030-10-30/2030-10-26",
        "2030-02-30/2030-03-01",
        "2030-1-1/2030-01-02",
        "1999-01-01/2030-01-01",
        "2101-01-01/2101-01-02",
    ],
)
def test_missing_or_invalid_dates_never_become_a_free_slot(bad):
    resources = {START: replace(RESOURCES[START], value=bad)}
    assert not parse_holiday_state(resources).periods
    assert parse_holiday_write_configuration(resources) is None
    assert parse_holiday_state(resources).invalid_period_count == 1


@pytest.mark.parametrize(
    "field,change",
    [
        ("startStop", {"writable": False}),
        ("assignedTo", {"writable": False}),
        ("assignedTo", {"allowed_values": ()}),
        ("hcMode", {"resource_type": "arrayData"}),
        ("hcMode", {"allowed_values": ("OFF",)}),
        ("dhwMode", {"writable": False}),
        ("fixTemperature", {"unit": "F"}),
        ("fixTemperature", {"minimum": None}),
        ("fixTemperature", {"minimum": 26}),
        ("fixTemperature", {"maximum": float("inf")}),
        ("fixTemperature", {"step_size_valid": False}),
        ("fixTemperature", {"step_size": 0}),
    ],
)
def test_invalid_slot_metadata_does_not_borrow_from_other_slots(field, change):
    path = f"{SLOT}/{field}"
    resources = {
        **RESOURCES,
        path: replace(
            RESOURCES[path], metadata=replace(RESOURCES[path].metadata, **change)
        ),
    }
    assert icom_write_configuration(resources, SLOT) is None
    assert icom_write_configuration(resources, FREE) is not None


@pytest.mark.parametrize("on", [True, False])
def test_wddw2_is_one_mode_without_calendar_dates(on):
    mode = replace(RESOURCES[WDDW2_HOLIDAY_PATH], value="on" if on else "off")
    state = parse_holiday_state({mode.path: mode})
    assert state.active is on and not state.periods
    assert parse_holiday_write_configuration({mode.path: mode}) is None


@pytest.mark.parametrize("value", [None, "unknown", True, 1, [], "ON"])
def test_unknown_wddw2_values_are_not_off_or_writable(value):
    mode = replace(RESOURCES[WDDW2_HOLIDAY_PATH], value=value)
    assert parse_holiday_state({mode.path: mode}).active is None
    assert not assess_control(mode).eligible


def test_discovery_only_expands_reported_slots():
    assert _optional_children("/system/holidayModes") == ()
    assert f"{SLOT}/assignedTo" in _optional_children(SLOT)
    assert f"{SLOT}/startStop" in _optional_children(SLOT)
    assert not _optional_children("/system/holidayModes/hm-private")
    assert not is_legacy_holiday_path("/system/holidayModes/hm-private/startStop")
    assert "/system/holidayModes" in _optional_children("/system")


@pytest.mark.parametrize("operation", ["create", "update", "delete"])
async def test_icom_wire_transaction_uses_only_selected_slot(environment, operation):
    state = wire(environment)
    coordinator, client, _ = environment
    source = FREE if operation == "create" else SLOT
    baseline = icom_write_values(state, source)
    values = replace(
        baseline,
        start_date="2030-11-01",
        end_date="2030-11-03",
        heating_mode="FIX_TEMPERATURE",
        dhw_mode="LOW",
        fix_temperature=18,
        assigned_to=("hc2",),
    )
    if operation == "create":
        await coordinator.async_create_holiday(values)
    elif operation == "update":
        await coordinator.async_update_holiday(2, values, expected=baseline)
    else:
        await coordinator.async_delete_holiday(2, expected=baseline)
    assert all(
        call.args[1].startswith(source + "/")
        for call in client.put_resource_value.await_args_list
    )
    if operation == "delete":
        client.put_resource_value.assert_awaited_once_with(
            "synthetic-gateway", f"{SLOT}/delete", "TRUE"
        )
        assert len(parse_holiday_state(coordinator.resources).periods) == 0
    else:
        assert (
            client.put_resource_value.await_args_list[-1].args[1]
            == f"{source}/startStop"
        )
        client.put_icom_holiday_assignments.assert_awaited_once_with(
            "synthetic-gateway", int(source[-1]), ("hc2",)
        )
        assert icom_write_values(coordinator.resources, source) == values
    assert coordinator.data[f"{source}/startStop"].freshness is Freshness.FRESH
    client.create_holiday_period.assert_not_awaited()
    client.update_holiday_period.assert_not_awaited()
    client.delete_holiday_period.assert_not_awaited()


@pytest.mark.parametrize(
    "failure", [RequestTimeout("timeout"), RateLimited(60), AuthenticationError("auth")]
)
async def test_failure_mid_transaction_stops_without_retry_or_rollback(
    environment, failure
):
    state = wire(environment)
    coordinator, client, _ = environment
    baseline = icom_write_values(state, SLOT)
    original = client.put_resource_value.side_effect

    async def put(gateway, path, value):
        if path.endswith("/hcMode"):
            raise failure
        await original(gateway, path, value)

    client.put_resource_value.side_effect = put
    # Starting the real reauthentication flow in the background would race the
    # test teardown; the synthetic entry intentionally has no sign-in data.
    with (
        patch.object(coordinator._config_entry, "async_start_reauth") as reauth,
        pytest.raises(type(failure)),
    ):
        await coordinator.async_update_holiday(
            2, replace(baseline, dhw_mode="LOW", heating_mode="ECO"), expected=baseline
        )
    assert reauth.call_count == int(isinstance(failure, AuthenticationError))
    assert client.put_resource_value.await_count == 2
    assert state[f"{SLOT}/dhwMode"].value == "LOW"
    assert state[START].value == RESOURCES[START].value
    assert not coordinator.data[START].available
    assert coordinator.data[START].freshness is Freshness.STALE


@pytest.mark.parametrize("when", ["before", "between", "readback"])
async def test_app_changes_abort_with_authoritative_readback(environment, when):
    state = wire(environment)
    coordinator, client, _ = environment
    baseline = icom_write_values(state, SLOT)
    calls = 0

    async def get(gateway, path):
        nonlocal calls
        if path == START:
            calls += 1
            if calls == {"before": 1, "between": 2, "readback": 3}[when]:
                state[START] = replace(state[START], value="2031-01-01/2031-01-03")
        return state[path]

    client.get_resource.side_effect = get
    with pytest.raises((HolidayChangedError, WriteNotConfirmed)):
        await coordinator.async_update_holiday(
            2, replace(baseline, dhw_mode="LOW"), expected=baseline
        )
    assert client.put_resource_value.await_count == (1 if when == "readback" else 0)
    assert coordinator.data[START].resource.value == "2031-01-01/2031-01-03"


async def test_stolen_free_slot_and_wrong_id_never_write(environment):
    state = wire(environment)
    coordinator, client, _ = environment
    values = replace(
        icom_write_values(state, FREE),
        start_date="2030-11-01",
        end_date="2030-11-03",
        assigned_to=("hc2",),
    )
    state[f"{FREE}/startStop"] = replace(
        state[f"{FREE}/startStop"], value="2032-01-01/2032-01-03"
    )
    with pytest.raises(HolidayChangedError):
        await coordinator.async_create_holiday(values)
    with pytest.raises(WriteValidationError):
        await coordinator.async_update_holiday(2, values)
    client.put_resource_value.assert_not_awaited()


async def test_wddw2_switch_confirms_value_and_has_no_calendar(environment):
    wire(environment)
    coordinator, client, _ = environment
    resource = RESOURCES[WDDW2_HOLIDAY_PATH]
    coordinator.resources = {resource.path: resource}
    coordinator.data = snapshots(resource)
    entity = BoschBuderusSwitch(
        coordinator, build_switch_descriptions(coordinator.resources)[0]
    )
    await entity.async_turn_on()
    client.put_resource_value.assert_awaited_once_with(
        "synthetic-gateway", resource.path, "on"
    )
    assert entity.is_on
    assert BoschBuderusHolidayActiveBinarySensor(coordinator).is_on
    assert not BoschBuderusHolidayCalendar(coordinator).available


@pytest.mark.parametrize("same_day", [True, False])
def test_icom_date_conversion_uses_exclusive_end_without_time_grid(same_day):
    config = icom_write_configuration(RESOURCES, FREE)
    end = date(2030, 10, 26 if same_day else 29)
    if same_day:
        with pytest.raises(HolidayTimeRangeError):
            create_holiday_values(
                date(2030, 10, 26), end, "", config, ZoneInfo("Europe/Berlin")
            )
    else:
        values = create_holiday_values(
            date(2030, 10, 26), end, "", config, ZoneInfo("Europe/Berlin")
        )
        assert values.end_date == "2030-10-28" and values.name is None
        assert values.source_path == FREE


def flow_entry(hass, resources):
    entry, coordinator = _entry_with_writable_holiday(hass)
    coordinator.resources = resources
    coordinator.data = snapshots(*resources.values())
    coordinator.async_create_holiday = AsyncMock()
    return entry, coordinator


@pytest.mark.parametrize("creating", [False, True])
async def test_legacy_native_dialog_has_dates_no_clock_or_name_and_saves(
    hass, enable_custom_integrations, creating
):
    entry, coordinator = flow_entry(hass, RESOURCES)
    result = await _start_holiday_options(hass, entry)
    options = next(iter(result["data_schema"].schema.values())).config["options"]
    selected = (
        "create"
        if creating
        else next(
            option["value"]
            for option in options
            if option["value"] not in ("create",)
            and not option["value"].startswith("mode:")
        )
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"holiday_period": selected}
    )
    assert result["step_id"] == ("new_legacy_holiday" if creating else "legacy_holiday")
    fields = {
        marker.schema: selector
        for marker, selector in result["data_schema"].schema.items()
    }
    assert "holiday_name" not in fields
    assert fields["holiday_start"].selector_type == "date"
    assert fields["holiday_end"].selector_type == "date"
    assert ("holiday_delete" in fields) is (not creating)
    draft = _holiday_form_input(result)
    draft.update(holiday_start="2030-11-03", holiday_end="2030-11-01")
    result = await hass.config_entries.options.async_configure(result["flow_id"], draft)
    assert result["errors"] == {"holiday_end": "holiday_end_not_after_start"}
    assert _holiday_form_input(result)["holiday_start"] == "2030-11-03"
    draft.update(holiday_start="2030-11-01", holiday_end="2030-11-04")
    result = await hass.config_entries.options.async_configure(result["flow_id"], draft)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == ("holiday_created" if creating else "holiday_updated")
    call = (
        coordinator.async_create_holiday
        if creating
        else coordinator.async_update_holiday
    ).await_args
    values = call.args[0 if creating else 1]
    assert (
        values.source_path == (FREE if creating else SLOT)
        and values.end_date == "2030-11-03"
    )


async def test_wddw2_options_dialog_has_only_mode(hass, enable_custom_integrations):
    entry, coordinator = flow_entry(
        hass, {WDDW2_HOLIDAY_PATH: RESOURCES[WDDW2_HOLIDAY_PATH]}
    )
    coordinator.async_write_control = AsyncMock()
    result = await _start_holiday_options(hass, entry)
    assert result["step_id"] == "holiday_mode"
    assert [m.schema for m in result["data_schema"].schema] == ["holiday_mode"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"holiday_mode": "on"}
    )
    assert result["reason"] == "holiday_updated"
    assert coordinator.async_write_control.await_args.args[:2] == (
        WDDW2_HOLIDAY_PATH,
        "on",
    )


@pytest.mark.parametrize(
    "app,submitted,expected",
    [
        (None, "on", "holiday_updated"),
        ("on", "off", "holiday_unchanged"),
        ("on", "on", "holiday_changed"),
    ],
)
async def test_wddw2_dialog_writes_only_against_the_mode_shown(
    hass, environment, app, submitted, expected
):
    """FLOW-4: a change in the app meanwhile is never overwritten."""
    from custom_components.bosch_buderus_heating.runtime import (
        BoschBuderusRuntimeData,
    )
    from tests.test_config_flow import _reconfigure_entry

    coordinator, client, _ = environment
    shown = replace(RESOURCES[WDDW2_HOLIDAY_PATH], value="off")
    gateway = {shown.path: shown}
    coordinator.resources = dict(gateway)
    coordinator.data = snapshots(shown)

    async def get(gateway_id, path):
        return gateway[path]

    async def put(gateway_id, path, value):
        gateway[path] = replace(gateway[path], value=value)

    client.get_resource.side_effect = get
    client.put_resource_value.side_effect = put
    entry = _reconfigure_entry(hass)
    entry.runtime_data = BoschBuderusRuntimeData(
        client=AsyncMock(),
        token_manager=AsyncMock(),
        gateways=(coordinator.gateway,),
        coordinators=(coordinator,),
    )
    result = await _start_holiday_options(hass, entry)
    assert result["step_id"] == "holiday_mode"
    if app is not None:
        gateway[shown.path] = replace(shown, value=app)  # switched in the app
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"holiday_mode": submitted}
    )
    if expected == "holiday_changed":
        assert result["errors"] == {"base": "holiday_changed"}
        # The form now shows the current mode; nothing was written.
        default = next(iter(result["data_schema"].schema)).default()
        assert default == "on"
        client.put_resource_value.assert_not_awaited()
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"holiday_mode": "on"}
        )
        assert result["reason"] == "holiday_unchanged"
    else:
        assert result["reason"] == expected
    if expected == "holiday_updated":
        client.put_resource_value.assert_awaited_once_with(
            coordinator.gateway.gateway_id, shown.path, "on"
        )
    else:
        client.put_resource_value.assert_not_awaited()
        assert gateway[shown.path].value == app


@pytest.mark.parametrize(
    "changes",
    [
        {"heating_mode": "unknown"},
        {"dhw_mode": "not offered"},
        {"assigned_to": ("hc1",)},
        {"assigned_to": ()},
        {"fix_temperature": 26},
        {"fix_temperature": float("nan")},
        {"fix_temperature": 18.25},
        {"fix_temperature": 18, "heating_mode": "ECO"},
        {"start_date": "2030-13-01"},
        {"end_date": "2020-01-01"},
        {"start_date": "2030-01-01T00:00:00"},
        {"name": "unsupported"},
        {"ventilation_mode": "OFF"},
        {"thermal_disinfection": "ON"},
        {"start_date": "2017-01-01", "end_date": "2017-01-01"},
    ],
)
async def test_invalid_changes_are_rejected_before_first_put(environment, changes):
    state = wire(environment)
    coordinator, client, _ = environment
    baseline = icom_write_values(state, SLOT)
    with pytest.raises(WriteValidationError):
        await coordinator.async_update_holiday(
            2, replace(baseline, **changes), expected=baseline
        )
    client.put_resource_value.assert_not_awaited()
    client.put_icom_holiday_assignments.assert_not_awaited()


@pytest.mark.parametrize("operation", ["update", "delete"])
async def test_unconfirmed_legacy_put_is_not_repeated(environment, operation):
    state = wire(environment)
    coordinator, client, _ = environment
    baseline = icom_write_values(state, SLOT)
    client.put_resource_value.side_effect = None
    with pytest.raises(WriteNotConfirmed):
        if operation == "delete":
            await coordinator.async_delete_holiday(2, expected=baseline)
        else:
            await coordinator.async_update_holiday(
                2, replace(baseline, dhw_mode="LOW"), expected=baseline
            )
    assert client.put_resource_value.await_count == 1
    assert coordinator.data[START].resource == state[START]
    assert coordinator.data[START].available


@pytest.mark.parametrize(
    "removed", ["delete", "assignedTo", "startStop", "hcMode", "fixTemperature"]
)
async def test_incomplete_or_read_only_slots_do_not_allow_delete(environment, removed):
    state = wire(environment)
    coordinator, client, _ = environment
    baseline = icom_write_values(state, SLOT)
    coordinator.data.pop(f"{SLOT}/{removed}")
    with pytest.raises(WriteValidationError):
        await coordinator.async_delete_holiday(2, expected=baseline)
    client.put_resource_value.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [
        {"writable": False},
        {"resource_type": "arrayData"},
        {"unit": "C"},
        {"allowed_values": ("FALSE",)},
    ],
)
async def test_delete_rechecks_its_own_current_contract(environment, change):
    state = wire(environment)
    coordinator, client, _ = environment
    baseline = icom_write_values(state, SLOT)
    path = f"{SLOT}/delete"
    # The dialog saw a valid contract; the fresh read must catch this change.
    state[path] = replace(state[path], metadata=replace(state[path].metadata, **change))
    with pytest.raises(WriteValidationError):
        await coordinator.async_delete_holiday(2, expected=baseline)
    client.put_resource_value.assert_not_awaited()
    assert not icom_write_configuration(coordinator.resources, SLOT).can_delete


async def test_delete_rejects_compound_response(environment):
    state = wire(environment)
    coordinator, client, _ = environment
    baseline = icom_write_values(state, SLOT)
    path = f"{SLOT}/delete"
    state[path] = replace(state[path], has_values=True, values=("TRUE",))
    with pytest.raises(WriteValidationError):
        await coordinator.async_delete_holiday(2, expected=baseline)
    client.put_resource_value.assert_not_awaited()


async def test_delete_without_optional_enum_metadata_uses_documented_command(
    environment,
):
    state = wire(environment)
    coordinator, client, _ = environment
    baseline = icom_write_values(state, SLOT)
    path = f"{SLOT}/delete"
    state[path] = replace(
        state[path], metadata=replace(state[path].metadata, allowed_values=())
    )
    await coordinator.async_delete_holiday(2, expected=baseline)
    client.put_resource_value.assert_awaited_once_with(
        "synthetic-gateway", path, "TRUE"
    )


def test_unknown_slot_ids_are_redacted_and_shared_circuit_state_does_not_duplicate():
    from custom_components.bosch_buderus_heating.pointt.redaction import (
        diagnostic_resource_path,
        resource_path_template,
    )

    assert resource_path_template(START) == "/system/holidayModes/{hm}/startStop"
    assert diagnostic_resource_path(START) == START
    assert (
        diagnostic_resource_path("/system/holidayModes/private-id/startStop")
        == "/system/holidayModes/{hm}/startStop"
    )
    resources = dict(RESOURCES)
    for path in (
        "/heatingCircuits/hc2/holidayMode/activated",
        "/dhwCircuits/dhw1/holidayMode/activated",
    ):
        resources[path] = replace(RESOURCES[WDDW2_HOLIDAY_PATH], path=path, value="hm2")
    state = parse_holiday_state(resources)
    assert state.active and len(state.periods) == 1


async def test_full_slots_have_no_create_action_but_remain_editable(
    hass, enable_custom_integrations
):
    resources = {
        path: item
        for path, item in RESOURCES.items()
        if not path.startswith(FREE) and path != WDDW2_HOLIDAY_PATH
    }
    entry, _coordinator = flow_entry(hass, resources)
    result = await _start_holiday_options(hass, entry)
    options = next(iter(result["data_schema"].schema.values())).config["options"]
    assert len(options) == 1 and options[0]["value"] != "create"


async def test_legacy_calendar_uid_capacity_and_exclusive_dates(environment):
    wire(environment)
    coordinator, _client, _ = environment
    calendar = BoschBuderusHolidayCalendar(coordinator)
    event = calendar.event
    assert (
        event.uid == "icom-hm2"
        and event.start == date(2030, 10, 26)
        and event.end == date(2030, 10, 29)
    )
    assert calendar.supported_features == (
        CalendarEntityFeature.CREATE_EVENT
        | CalendarEntityFeature.UPDATE_EVENT
        | CalendarEntityFeature.DELETE_EVENT
    )
    await calendar.async_update_event(
        "icom-hm2",
        {
            "dtstart": date(2030, 11, 1),
            "dtend": date(2030, 11, 4),
            "summary": calendar._summary_for_period(calendar._state.periods[0]),
        },
    )
    assert coordinator.data[START].resource.value == "2030-11-01/2030-11-03"
    await calendar.async_delete_event("icom-hm2")
    assert calendar.event is None


async def test_applied_timeout_recovers_on_regular_poll_without_another_put(
    environment,
):
    from custom_components.bosch_buderus_heating.pointt import BatchItemResult
    from custom_components.bosch_buderus_heating.resource_catalog import (
        PollGroup,
        poll_group,
    )

    state = wire(environment)
    coordinator, client, _ = environment
    baseline = icom_write_values(state, SLOT)
    calendar = BoschBuderusHolidayCalendar(coordinator)
    identity = calendar.unique_id
    original = client.put_resource_value.side_effect

    async def applied_timeout(gateway, path, value):
        await original(gateway, path, value)
        raise RequestTimeout("The response was lost")

    client.put_resource_value.side_effect = applied_timeout
    with pytest.raises(RequestTimeout):
        await coordinator.async_update_holiday(
            2,
            replace(baseline, start_date="2031-01-01", end_date="2031-01-03"),
            expected=baseline,
        )
    assert not coordinator.data[START].available
    assert coordinator.data[START].freshness is Freshness.STALE
    assert calendar.event is None
    assert all(poll_group(resource) is PollGroup.CONTROL for resource in state.values())
    coordinator._paths_by_group = {PollGroup.CONTROL: tuple(state)}
    coordinator._next_update = {PollGroup.CONTROL: 0}

    async def bulk(gateway, paths):
        return tuple(
            BatchItemResult(gateway, path, 200, resource=state[path]) for path in paths
        )

    client.get_resources_bulk.side_effect = bulk
    updated = await coordinator._async_update_data_locked()
    coordinator.async_set_updated_data(updated)
    assert calendar.available and calendar.unique_id == identity
    assert calendar.event.start == date(2031, 1, 1)
    assert calendar.event.end == date(2031, 1, 4)
    assert client.put_resource_value.await_count == 1
