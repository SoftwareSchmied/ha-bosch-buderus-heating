"""Z2: the options dialogs of the EMS/iCom weekly switch programs."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.translation import async_get_translations

from custom_components.bosch_buderus_heating.config_flow import BoschBuderusOptionsFlow
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import Freshness
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    RateLimited,
    RequestTimeout,
    UnexpectedHttpStatus,
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.runtime import BoschBuderusRuntimeData
from custom_components.bosch_buderus_heating.switch_program_flow import (
    SwitchProgramOptionsFlow,
)
from custom_components.bosch_buderus_heating.switch_program_service import (
    SwitchProgramService,
)
from custom_components.bosch_buderus_heating.switch_programs import SwitchPoint
from custom_components.bosch_buderus_heating.write_policies import ContractRejected
from custom_components.bosch_buderus_heating.writes import ControlUnused, WriteBusy
from tests import test_dynamic_controls
from tests.switch_program_helpers import ACTIVE, DHW, PROGRAM_A, PROGRAM_B, Gateway

STEPS = (
    "switch_programs",
    "switch_program_week",
    "switch_program_week_inactive",
    "switch_program_week_dhw",
    "switch_program_day",
    "switch_program_point",
    "switch_program_copy",
    "switch_program_confirm",
)


@pytest.fixture
def environment(hass, enable_custom_integrations):
    yield from test_dynamic_controls.environment.__wrapped__(hass)


def make_flow(hass, environment, **changes):
    coordinator, client, _ = environment
    coordinator._switch_program_service = SwitchProgramService(
        client, sleep=AsyncMock()
    )
    gateway = Gateway(coordinator, client, **changes)
    entry = coordinator.config_entry
    entry.runtime_data = BoschBuderusRuntimeData(
        client=client,
        token_manager=AsyncMock(),
        gateways=(coordinator.gateway,),
        coordinators=(coordinator,),
    )
    flow = BoschBuderusOptionsFlow(entry)
    flow.hass = hass
    return flow, gateway


def _options(result, field):
    schema = {str(key): value for key, value in result["data_schema"].schema.items()}
    return schema[field].config["options"]


async def open_program(flow, path=PROGRAM_A):
    first = await flow.async_step_switch_programs()
    keys = [option["value"] for option in _options(first, "program")]
    return await flow.async_step_switch_programs({"program": f"1:{path}"}), keys


async def test_menu_and_program_choice(hass, environment):
    flow, _ = make_flow(hass, environment)
    menu = await flow.async_step_init()
    assert menu["menu_options"] == [
        "controls",
        "notifications",
        "holidays",
        "switch_programs",
    ]
    first = await flow.async_step_switch_programs()
    labels = {item["value"]: item["label"] for item in _options(first, "program")}
    assert set(labels) == {f"1:{PROGRAM_A}", f"1:{PROGRAM_B}", f"1:{DHW}"}
    # The name the gateway reports; an empty name adds nothing.
    assert labels[f"1:{PROGRAM_A}"].endswith("(Home office)")
    assert "(" not in labels[f"1:{PROGRAM_B}"]


async def test_hints_for_an_inactive_program_and_for_hot_water(hass, environment):
    flow, _ = make_flow(hass, environment)
    week, _ = await open_program(flow, PROGRAM_A)
    assert week["step_id"] == "switch_program_week"
    week, _ = await open_program(flow, PROGRAM_B)
    assert week["step_id"] == "switch_program_week_inactive"
    week, _ = await open_program(flow, DHW)
    assert week["step_id"] == "switch_program_week_dhw"
    placeholders = week["description_placeholders"]
    assert (placeholders["per_day"], placeholders["total"]) == ("6", "42")
    assert placeholders["raster"] == "15"


async def test_move_one_point_and_save_sends_the_week_once(hass, environment):
    hass.config.language = "en"
    flow, gateway = make_flow(hass, environment)
    week, _ = await open_program(flow)
    assert (
        "**Saturday:** 08:00 Heating, 22:30 Reduced"
        in (week["description_placeholders"]["week"])
    )
    day = await flow.async_step_switch_program_week({"action": "edit", "day": "sa"})
    assert day["step_id"] == "switch_program_day"
    values = [item["value"] for item in _options(day, "point")]
    assert values == ["0", "1", "add", "done"]
    point = await flow.async_step_switch_program_day({"point": "0"})
    assert point["step_id"] == "switch_program_point"
    day = await flow.async_step_switch_program_point(
        {"time": "08:15", "level": "comfort2", "delete": False}
    )
    assert day["description_placeholders"]["points"].startswith("08:15")
    week = await flow.async_step_switch_program_day({"point": "done"})
    assert "**Saturday *:**" in week["description_placeholders"]["week"]
    assert gateway.raw_puts == []
    confirm = await flow.async_step_switch_program_week({"action": "review"})
    assert confirm["step_id"] == "switch_program_confirm"
    assert "08:00" in confirm["description_placeholders"]["before"]
    assert "08:15" in confirm["description_placeholders"]["after"]
    assert "Monday" not in confirm["description_placeholders"]["after"]
    done = await flow.async_step_switch_program_confirm({})
    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "switch_program_saved"
    assert "08:00" in done["description_placeholders"]["before"]
    ((path, body),) = gateway.raw_puts
    assert path == PROGRAM_A
    saturday = [item for item in body if item["dayOfWeek"] == "Sa"]
    assert saturday == [
        {"dayOfWeek": "Sa", "setpoint": "comfort2", "time": 495},
        {"dayOfWeek": "Sa", "setpoint": "eco", "time": 1350},
    ]
    assert len(body) == 14


async def test_add_delete_and_copy_collect_in_one_draft(hass, environment):
    flow, gateway = make_flow(hass, environment)
    await open_program(flow)
    await flow.async_step_switch_program_week({"action": "edit", "day": "mo"})
    await flow.async_step_switch_program_day({"point": "add"})
    day = await flow.async_step_switch_program_point({"time": "12:00", "level": "eco"})
    assert day["step_id"] == "switch_program_day"
    await flow.async_step_switch_program_day({"point": "2"})
    await flow.async_step_switch_program_point(
        {"time": "22:00", "level": "eco", "delete": True}
    )
    await flow.async_step_switch_program_day({"point": "done"})
    copy = await flow.async_step_switch_program_week({"action": "copy", "day": "mo"})
    assert copy["step_id"] == "switch_program_copy"
    assert [option for option in _options(copy, "days")] == [
        "tu",
        "we",
        "th",
        "fr",
        "sa",
        "su",
    ]
    await flow.async_step_switch_program_copy({"days": ["sa", "su"]})
    await flow.async_step_switch_program_week({"action": "review"})
    await flow.async_step_switch_program_confirm({})
    ((_, body),) = gateway.raw_puts
    by_day = {
        day: [item["time"] for item in body if item["dayOfWeek"] == day]
        for day in ("Mo", "Sa", "Su")
    }
    # Monday: the 22:00 point was deleted and 12:00 added.
    assert by_day == {"Mo": [390, 720], "Sa": [390, 720], "Su": [390, 720]}


@pytest.mark.parametrize(
    ("values", "field", "error"),
    [
        ({"time": "08:10", "level": "eco"}, "time", "switch_program_time"),
        ({"time": "8 Uhr", "level": "eco"}, "time", "switch_program_time"),
        ({"time": "08:00:30", "level": "eco"}, "time", "switch_program_time"),
        ({"time": "08:15", "level": "comfort1"}, "level", "switch_program_level"),
        ({"time": "22:30", "level": "eco"}, "base", "switch_program_duplicate_time"),
    ],
)
async def test_invalid_points_never_reach_the_draft(
    hass, environment, values, field, error
):
    flow, gateway = make_flow(hass, environment)
    await open_program(flow)
    await flow.async_step_switch_program_week({"action": "edit", "day": "sa"})
    await flow.async_step_switch_program_day({"point": "0"})
    result = await flow.async_step_switch_program_point(values)
    assert result["errors"] == {field: error}
    assert flow._sp_draft == flow._sp_choice.program.points
    assert gateway.raw_puts == []


@pytest.mark.parametrize(
    ("per_day", "total", "error"),
    [
        (2, 42, "switch_program_too_many_per_day"),
        (6, 14, "switch_program_too_many_in_all"),
    ],
)
async def test_the_reported_limits_stop_an_added_point(
    hass, environment, per_day, total, error
):
    flow, gateway = make_flow(hass, environment)
    snapshot = gateway.coordinator.data[PROGRAM_A]
    gateway.coordinator.data[PROGRAM_A] = replace(
        snapshot,
        resource=replace(
            snapshot.resource,
            program_fields={
                **snapshot.resource.program_fields,
                "maxNbOfSwitchPointsPerDay": per_day,
                "maxNbOfSwitchPoints": total,
            },
        ),
    )
    await open_program(flow)
    await flow.async_step_switch_program_week({"action": "edit", "day": "mo"})
    day = await flow.async_step_switch_program_day({"point": "add"})
    assert day["errors"] == {"base": error}
    assert day["description_placeholders"]["per_day"] == str(per_day)
    # Deleting a point makes room again.
    await flow.async_step_switch_program_day({"point": "0"})
    await flow.async_step_switch_program_point(
        {"time": "06:30", "level": "comfort2", "delete": True}
    )
    point = await flow.async_step_switch_program_day({"point": "add"})
    assert point["step_id"] == "switch_program_point"
    assert gateway.raw_puts == []


async def test_week_needs_a_day_and_a_change(hass, environment):
    flow, gateway = make_flow(hass, environment)
    await open_program(flow)
    result = await flow.async_step_switch_program_week({"action": "edit"})
    assert result["errors"] == {"day": "switch_program_day_required"}
    result = await flow.async_step_switch_program_week({"action": "review"})
    assert result["errors"] == {"base": "switch_program_no_changes"}
    copy = await flow.async_step_switch_program_week({"action": "copy", "day": "mo"})
    result = await flow.async_step_switch_program_copy({"days": []})
    assert result["errors"] == {"days": "switch_program_day_required"}
    assert copy["step_id"] == "switch_program_copy"
    assert gateway.raw_puts == []


async def test_a_program_that_changed_ends_the_dialog_without_sending(
    hass, environment
):
    flow, gateway = make_flow(hass, environment)
    await open_program(flow)
    coordinator = gateway.coordinator
    coordinator.data[PROGRAM_A] = replace(
        coordinator.data[PROGRAM_A], freshness=Freshness.STALE
    )
    result = await flow.async_step_switch_program_week({"action": "edit", "day": "mo"})
    assert result["reason"] == "switch_program_changed"
    assert gateway.raw_puts == []


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (RateLimited(60), "write_rate_limited"),
        (AuthenticationError("expired"), "write_authentication_failed"),
        (ControlUnused("unused"), "switch_program_unused"),
        (WriteValidationError("changed"), "switch_program_changed"),
        (WriteNotConfirmed("lost"), "switch_program_not_confirmed"),
        (WriteBusy("busy"), "write_busy"),
        (UnexpectedHttpStatus(400), "switch_program_rejected"),
        (RequestTimeout("lost"), "switch_program_failed"),
    ],
)
async def test_write_failures_have_their_own_text(hass, environment, error, key):
    flow, gateway = make_flow(hass, environment)
    await open_program(flow)
    await flow.async_step_switch_program_week({"action": "edit", "day": "mo"})
    await flow.async_step_switch_program_day({"point": "0"})
    await flow.async_step_switch_program_point({"time": "06:45", "level": "comfort2"})
    await flow.async_step_switch_program_day({"point": "done"})
    await flow.async_step_switch_program_week({"action": "review"})
    gateway.coordinator.async_write_switch_program = AsyncMock(side_effect=error)
    result = await flow.async_step_switch_program_confirm({})
    assert result["errors"] == {"base": key}
    assert result["description_placeholders"]["before"]
    gateway.coordinator.async_write_switch_program.assert_awaited_once()


async def test_programs_that_cannot_be_changed_are_not_offered(hass, environment):
    from tests.switch_program_helpers import payload

    flow, _ = make_flow(
        hass,
        environment,
        **{
            PROGRAM_A: payload(PROGRAM_A) | {"writeable": 0},
            PROGRAM_B: None,
            DHW: None,
            ACTIVE: None,
        },
    )
    assert flow._switch_program_choices() == {}
    menu = await flow.async_step_init()
    assert "switch_programs" not in menu["menu_options"]
    result = await flow.async_step_switch_programs()
    assert result["reason"] == "no_switch_programs"


async def test_an_absolute_program_type_is_never_offered(hass, environment):
    from tests.switch_program_helpers import MODE, payload

    flow, _ = make_flow(
        hass, environment, **{MODE: payload(MODE) | {"value": "absolute"}}
    )
    assert set(flow._switch_program_choices()) == {f"1:{DHW}"}


@pytest.mark.parametrize("language", ["de", "en", "cs", "sk", "fr"])
async def test_every_dialog_text_exists(hass, enable_custom_integrations, language):
    options = await async_get_translations(hass, language, "options", {DOMAIN})
    prefix = f"component.{DOMAIN}.options."
    for step in STEPS:
        assert options[prefix + f"step.{step}.title"]
    assert options[prefix + "step.init.menu_options.switch_programs"]
    for reason in ("no_switch_programs", "switch_program_changed"):
        assert options[prefix + f"abort.{reason}"]
    selectors = await async_get_translations(hass, language, "selector", {DOMAIN})
    for option in ("edit", "copy", "review"):
        assert selectors[
            f"component.{DOMAIN}.selector.switch_program_action.options.{option}"
        ]


async def test_a_program_name_cannot_add_markup_to_the_dialogs(hass, environment):
    """D43: the program name the gateway reports is shown as plain text in
    the dialog descriptions; the selection label stays plain text."""
    from tests.switch_program_helpers import HC, payload
    from tests.test_markdown_text import _rendered, _strip

    name = "![](https://t.invalid/p)[a](b)"
    item = payload(f"{HC}/switchPrograms/nameA")
    item["value"] = name
    flow, _ = make_flow(hass, environment, **{f"{HC}/switchPrograms/nameA": item})
    first = await flow.async_step_switch_programs()
    labels = {item["value"]: item["label"] for item in _options(first, "program")}
    assert labels[f"1:{PROGRAM_A}"].endswith(f"({name})")
    week, _ = await open_program(flow, PROGRAM_A)
    shown = week["description_placeholders"]["program"]
    assert _rendered(shown) == labels[f"1:{PROGRAM_A}"]
    active = _strip(shown)
    assert "![" not in active and "](" not in active and "://" not in active


# Review of alpha.6 (N1, N3, N6, N9) ----------------------------------------


async def _to_confirm(flow):
    await open_program(flow)
    await flow.async_step_switch_program_week({"action": "edit", "day": "mo"})
    await flow.async_step_switch_program_day({"point": "0"})
    await flow.async_step_switch_program_point({"time": "06:45", "level": "comfort2"})
    await flow.async_step_switch_program_day({"point": "done"})
    return await flow.async_step_switch_program_week({"action": "review"})


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (ContractRejected("circuit_breaker"), "switch_program_paused"),
        (ContractRejected("program_mode_unknown"), "switch_program_mode"),
        (ContractRejected("program_mode_not_level"), "switch_program_mode"),
        (
            ContractRejected("program_too_many_per_day"),
            "switch_program_too_many_per_day",
        ),
        (ContractRejected("program_empty_week"), "switch_program_empty_week"),
        (ContractRejected("program_level_not_allowed"), "switch_program_level"),
        (ContractRejected("program_changed"), "switch_program_changed"),
        (ContractRejected("program_not_editable"), "switch_program_changed"),
        (WriteValidationError("no current reading"), "switch_program_changed"),
        (ControlUnused("unused"), "switch_program_unused"),
    ],
)
async def test_a_refusal_before_sending_names_its_cause(hass, environment, error, key):
    """N6: the breaker, the program type and an invalid draft each have
    their own text instead of "changed in the meantime"."""
    flow, gateway = make_flow(hass, environment)
    await _to_confirm(flow)
    gateway.coordinator.async_write_switch_program = AsyncMock(side_effect=error)
    result = await flow.async_step_switch_program_confirm({})
    assert result["errors"] == {"base": key}


@pytest.mark.parametrize("language", ["en", "de", "cs", "sk", "fr", "pl"])
async def test_the_refusal_texts_exist(hass, enable_custom_integrations, language):
    options = await async_get_translations(hass, language, "options", {DOMAIN})
    prefix = f"component.{DOMAIN}.options.error."
    for key in ("switch_program_paused", "switch_program_mode"):
        assert options[prefix + key]
    entities = await async_get_translations(hass, language, "entity", {DOMAIN})
    state = f"component.{DOMAIN}.entity.sensor.dhw_switch_program.state."
    for level in ("eco_plus", "comfort", "low", "eco", "high", "off"):
        assert entities[state + level]


async def test_hot_water_level_comfort_has_a_name(hass, environment):
    """N9: a hot water level the program may use is shown translated, not
    as its key. (``eco_plus`` has a name as well; the strict program check
    accepts only level keys without an underscore.)"""
    from tests.switch_program_helpers import payload

    hass.config.language = "en"
    item = payload(DHW)
    item["switchPoints"][0]["setpoint"] = "comfort"
    level = "/dhwCircuits/dhw1/temperatureLevels/comfort"
    listed = payload("/dhwCircuits/dhw1/temperatureLevels/high") | {"id": level}
    flow, _ = make_flow(hass, environment, **{DHW: item, level: listed})
    week, _ = await open_program(flow, DHW)
    shown = week["description_placeholders"]["week"]
    assert "05:45 Comfort" in shown
    assert "comfort" not in shown


async def test_level_names_and_the_saved_program_are_plain_text(hass, environment):
    """N1/D43: a program name in the success message and a level name from
    the cloud in the dialog descriptions cannot add markup; the selection
    labels stay plain text."""
    from tests.switch_program_helpers import HC, payload
    from tests.test_markdown_text import _rendered, _strip

    name = "![](https://t.invalid/p)"
    item = payload(f"{HC}/switchPrograms/nameA")
    item["value"] = name
    flow, gateway = make_flow(hass, environment, **{f"{HC}/switchPrograms/nameA": item})
    await _to_confirm(flow)
    done = await flow.async_step_switch_program_confirm({})
    assert done["reason"] == "switch_program_saved"
    shown = done["description_placeholders"]["program"]
    assert _rendered(shown).endswith(f"({name})")
    assert "![" not in _strip(shown) and "://" not in _strip(shown)
    assert len(gateway.raw_puts) == 1
    # A level name that is markup is escaped in a description, not in a label.
    levels = {"eco": "**[x](https://t.invalid)**"}
    point = SwitchPoint(0, 360, "eco")
    described = SwitchProgramOptionsFlow._points_text((point,), levels)
    assert _rendered(described) == "06:00 **[x](https://t.invalid)**"
    assert "](" not in _strip(described)
    label = SwitchProgramOptionsFlow._points_text((point,), levels, markdown=False)
    assert label == "06:00 **[x](https://t.invalid)**"


async def test_unused_programs_and_an_unknown_program_type_are_not_offered(
    hass, environment
):
    """N2/N3 in the dialog: ``used: false`` and a program type without a
    current reading keep the heating programs out of the list."""
    from tests.switch_program_helpers import MODE, payload

    flow, _ = make_flow(
        hass, environment, **{PROGRAM_A: payload(PROGRAM_A) | {"used": "false"}}
    )
    assert f"1:{PROGRAM_A}" not in flow._switch_program_choices()
    assert f"1:{PROGRAM_B}" in flow._switch_program_choices()
    coordinator = environment[0]
    coordinator.data[MODE] = replace(coordinator.data[MODE], freshness=Freshness.STALE)
    assert set(flow._switch_program_choices()) == {f"1:{DHW}"}
