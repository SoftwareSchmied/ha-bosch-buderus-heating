"""Tab order, the open tab and rejected fields of the appliance dialog."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.bosch_buderus_heating.coordinator import ResourceSnapshot
from custom_components.bosch_buderus_heating.holiday_ui import (
    async_setup_controls_ui,
    async_setup_holiday_ui,
)
from custom_components.bosch_buderus_heating.pointt import PointTError
from tests.test_heating_controls import MODE, TARGET, change, flow, scalar, selector

HOT_WATER = "/dhwCircuits/dhw1/temperatureLevels/high"
HC2 = "heatingCircuits/hc2"
DHW1 = "dhwCircuits/dhw1"


def config(result):
    return selector(result).serialize()["selector"]["bosch-buderus-controls"]


async def hot_water_flow(hass):
    options, coordinator, entity, _ = await flow(hass)
    resource = scalar(HOT_WATER, 52, minimum=30, maximum=60)
    coordinator.resources[HOT_WATER] = resource
    coordinator.data[HOT_WATER] = ResourceSnapshot(resource, True, datetime.now(UTC))
    # The dialog shows what was loaded when it opened.
    result = await options.async_step_controls()
    assert {row["group"] for row in config(result)["rows"]} == {HC2, DHW1}
    return options, coordinator, entity, result


async def test_tabs_follow_circuits_then_hot_water_then_system(
    hass, enable_custom_integrations
):
    options, _, _, _ = await flow(hass)
    paths = [
        "/dhwCircuits/dhw1/temperatureLevels/high",
        "/heatSources/additionalHeater/operationMode",
        "/heatingCircuits/hc10/manualRoomSetpoint",
        "/heatingCircuits/hc2/operationMode",
        "/heatingCircuits/hc2/manualRoomSetpoint",
        "/system/awayMode/enabled",
        "/heatingCircuits/hc1/manualRoomSetpoint",
        "/dhwCircuits/dhw2/temperatureLevels/high",
    ]
    displayed = {path: scalar(path, 20) for path in paths}
    rows = await options._control_rows(displayed, current=displayed)
    assert list(dict.fromkeys(row["group"] for row in rows)) == [
        "heatingCircuits/hc1",
        HC2,
        "heatingCircuits/hc10",
        DHW1,
        "dhwCircuits/dhw2",
        "system",
    ]
    # Inside a tab the order stays alphabetical by path.
    assert [row["key"] for row in rows if row["group"] == HC2] == [
        "/heatingCircuits/hc2/manualRoomSetpoint",
        "/heatingCircuits/hc2/operationMode",
    ]
    assert [row["key"] for row in rows if row["group"] == "system"] == [
        "/heatSources/additionalHeater/operationMode",
        "/system/awayMode/enabled",
    ]


async def test_rejected_value_opens_its_tab_and_is_marked(
    hass, enable_custom_integrations
):
    options, coordinator, _, _ = await hot_water_flow(hass)
    result = await options.async_step_operate(
        {"controls": {"changes": {MODE: "auto", HOT_WATER: 1000}, "active": HC2}}
    )
    assert result["errors"] == {"base": "controls_invalid"}
    assert config(result)["active"] == DHW1
    assert config(result)["invalid"] == [HOT_WATER]
    # The draft comes back so the rejected value stays visible.
    assert result["data_schema"]({})["controls"]["changes"] == {
        MODE: "auto",
        HOT_WATER: 1000,
    }
    coordinator._write_service.async_write_number.assert_not_awaited()
    coordinator._write_service.async_write_enum.assert_not_awaited()


async def test_several_rejected_values_open_the_first_tab_in_order(
    hass, enable_custom_integrations
):
    options, _, _, _ = await hot_water_flow(hass)
    result = await options.async_step_operate(
        {"controls": {"changes": {HOT_WATER: 1000, TARGET: 1000}, "active": DHW1}}
    )
    assert config(result)["active"] == HC2
    assert config(result)["invalid"] == [TARGET, HOT_WATER]


async def test_changed_appliance_value_opens_its_tab_without_marking_the_entry(
    hass, enable_custom_integrations
):
    options, coordinator, _, _ = await hot_water_flow(hass)
    change(coordinator, HOT_WATER, 50)
    # The changed control decides the tab, not the first entry of the draft.
    result = await options.async_step_operate(
        {"controls": {"changes": {TARGET: 20, HOT_WATER: 55}, "active": HC2}}
    )
    assert result["errors"] == {"base": "controls_invalid"}
    assert config(result)["active"] == DHW1
    assert config(result)["invalid"] == []
    coordinator._write_service.async_write_number.assert_not_awaited()


async def test_failed_write_opens_the_tab_where_writing_stopped(
    hass, enable_custom_integrations
):
    options, coordinator, _, _ = await hot_water_flow(hass)
    write = coordinator._write_service.async_write_number.side_effect

    async def fail_target(gateway, resource, value, policy, **kwargs):
        if resource.path == TARGET:
            raise PointTError("lost")
        return await write(gateway, resource, value, policy, **kwargs)

    coordinator._write_service.async_write_number.side_effect = fail_target
    # Hot water is written first and confirmed; the setpoint then fails.
    result = await options.async_step_operate(
        {"controls": {"changes": {HOT_WATER: 55, TARGET: 20}, "active": DHW1}}
    )
    assert result["errors"] == {"base": "controls_failed"}
    assert config(result)["active"] == HC2
    assert config(result)["invalid"] == []
    assert coordinator.data[HOT_WATER].resource.value == 55
    assert result["data_schema"]({})["controls"]["changes"] == {TARGET: 20}


async def test_failed_first_write_opens_its_tab_before_the_rest_of_the_draft(
    hass, enable_custom_integrations
):
    options, coordinator, _, _ = await hot_water_flow(hass)
    coordinator._write_service.async_write_number.side_effect = PointTError("lost")
    # Hot water is written first even though the draft lists it second.
    result = await options.async_step_operate(
        {"controls": {"changes": {TARGET: 20, HOT_WATER: 55}, "active": HC2}}
    )
    assert result["errors"] == {"base": "controls_failed"}
    assert config(result)["active"] == DHW1
    coordinator._write_service.async_write_number.assert_awaited_once()


async def test_success_shows_the_last_written_change(hass, enable_custom_integrations):
    options, coordinator, _, _ = await hot_water_flow(hass)
    result = await options.async_step_operate(
        {"controls": {"changes": {HOT_WATER: 55}, "active": HC2}}
    )
    assert not result["errors"]
    assert config(result)["status"] == "saved"
    assert config(result)["active"] == DHW1
    assert coordinator.data[HOT_WATER].resource.value == 55


@pytest.mark.parametrize(
    "payload,active",
    [
        ({"action": "refresh", "active": DHW1}, DHW1),
        ({"changes": {}, "active": DHW1}, DHW1),
        ({"changes": {HOT_WATER: 52}, "active": DHW1}, DHW1),
        ({"action": "refresh", "active": "heatingCircuits/hc9"}, ""),
        ({"action": "refresh", "active": 3}, ""),
        ({"action": "refresh"}, ""),
    ],
)
async def test_without_a_write_the_open_tab_stays_open(
    hass, enable_custom_integrations, payload, active
):
    options, coordinator, _, _ = await hot_water_flow(hass)
    result = await options.async_step_operate({"controls": payload})
    assert not result["errors"]
    assert config(result)["active"] == active
    assert config(result)["invalid"] == []
    coordinator._write_service.async_write_number.assert_not_awaited()


async def test_unknown_payload_keys_are_still_rejected(
    hass, enable_custom_integrations
):
    options, coordinator, _, _ = await hot_water_flow(hass)
    result = await options.async_step_operate(
        {"controls": {"changes": {HOT_WATER: 55}, "active": DHW1, "extra": 1}}
    )
    assert result["errors"] == {"base": "controls_invalid"}
    assert config(result)["active"] == DHW1
    coordinator._write_service.async_write_number.assert_not_awaited()


async def test_dialog_sends_the_control_step(hass, enable_custom_integrations):
    _, _, _, result = await flow(hass)
    rows = {row["key"]: row for row in config(result)["rows"]}
    assert rows[TARGET]["step"] == 0.5
    assert rows[MODE]["kind"] == "select"


@pytest.mark.parametrize(
    "failure",
    [
        {"read": FileNotFoundError("missing www")},
        {"register": RuntimeError("route already registered")},
        {"add": ValueError("bad url")},
    ],
)
async def test_frontend_module_failure_does_not_stop_the_integration(
    hass, caplog, failure
):
    """A missing or colliding browser module only affects its own form."""
    hass.config.components.add("frontend")
    hass.http = SimpleNamespace(
        async_register_static_paths=AsyncMock(side_effect=failure.get("register"))
    )
    read = (
        patch("pathlib.Path.read_bytes", side_effect=failure["read"])
        if "read" in failure
        else patch("pathlib.Path.read_bytes", return_value=b"module")
    )
    with (
        read,
        patch(
            "homeassistant.components.frontend.add_extra_js_url",
            new=Mock(side_effect=failure.get("add")),
        ),
    ):
        await async_setup_controls_ui(hass)
        await async_setup_holiday_ui(hass)
    assert "bosch_buderus_heating.controls_ui" not in hass.data
    assert "bosch_buderus_heating.holiday_ui" not in hass.data
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "WARNING"
    ]
    assert len(messages) == 2
    assert "controls-dialog" in messages[0] and "holiday-dialog" in messages[1]
    # ERR-9: only the error class; its text may carry a file path.
    error = next(iter(failure.values()))
    assert all(f"({type(error).__name__})" in message for message in messages)
    assert not any(str(error) in message for message in messages)


async def test_one_failing_module_keeps_the_other(hass, caplog):
    hass.config.components.add("frontend")
    hass.http = SimpleNamespace(async_register_static_paths=AsyncMock())
    real = __import__("pathlib").Path.read_bytes

    def read(path):
        if path.name == "controls-dialog.js":
            raise PermissionError("locked")
        return real(path)

    with (
        patch("pathlib.Path.read_bytes", autospec=True, side_effect=read),
        patch("homeassistant.components.frontend.add_extra_js_url", new=Mock()) as add,
    ):
        await async_setup_controls_ui(hass)
        await async_setup_holiday_ui(hass)
    assert "bosch_buderus_heating.controls_ui" not in hass.data
    assert hass.data["bosch_buderus_heating.holiday_ui"].startswith(
        "/bosch_buderus_heating/holiday-dialog-"
    )
    add.assert_called_once()
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "(PermissionError)" in warnings[0]
    assert "locked" not in warnings[0]
