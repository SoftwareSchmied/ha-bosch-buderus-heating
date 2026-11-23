"""P31: ventilation mode and filter time, pool target and switch as confirmed writes."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating.number import (
    async_setup_entry as setup_numbers,
)
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata
from custom_components.bosch_buderus_heating.select import (
    async_setup_entry as setup_selects,
)
from custom_components.bosch_buderus_heating.switch import (
    async_setup_entry as setup_switches,
)
from custom_components.bosch_buderus_heating.writes import (
    FILTER_RUN_TIME_POLICY,
    POOL_ENABLED_POLICY,
    POOL_SETPOINT_POLICY,
    VENTILATION_OPERATION_MODE_POLICY,
    assess_control,
)
from tests.test_dynamic_controls import environment, snapshots  # noqa: F401

FAN = "/ventilation/zone1/exhaustFanLevel"
POOL = "/pool/setpointTemp"
POOL_ON = "/pool/enabled"
INTEGRATION = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"


def text(path, options, value, *, writable=True, kind="stringValue"):
    return Resource(
        path=path,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type=kind, writable=writable, allowed_values=options
        ),
    )


def pool(value=26.0, *, minimum=20.0, maximum=35.0, step=0.5, **metadata):
    return Resource(
        path=POOL,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type=metadata.pop("kind", "floatValue"),
            writable=metadata.pop("writable", True),
            minimum=minimum,
            maximum=maximum,
            unit=metadata.pop("unit", "C"),
            step_size=step,
            **metadata,
        ),
    )


async def added_entities(hass, entry, setup):
    added = []
    await setup(hass, entry, added.extend)
    return added


# ------------------------------------------------- ventilation operating mode

# The shape n42net's unit reports: eleven values, the fan level read-only.
MODE = "/ventilation/zone1/operationMode"
MODES = (
    "time",
    "off",
    "min",
    "red",
    "nom",
    "max",
    "dem",
    "party",
    "powerVent",
    "fireplace",
    "fallAsleep",
)


async def test_operation_mode_select_writes_and_confirms(hass, environment):  # noqa: F811
    coordinator, client, entry = environment
    current = text(MODE, MODES, "nom")
    coordinator.data = snapshots(current)
    (select,) = await added_entities(hass, entry, setup_selects)
    assert select.entity_description.write_policy is VENTILATION_OPERATION_MODE_POLICY
    assert select.translation_key == "ventilation_operation_mode"
    # Enabled by default (explicit exception for this control).
    assert select.entity_description.entity_registry_enabled_default is True
    # The usual order; HA-safe keys for the camel-case codes.
    assert select.options == [
        "time",
        "off",
        "min",
        "red",
        "nom",
        "max",
        "dem",
        "party",
        "power_vent",
        "fireplace",
        "fall_asleep",
    ]
    assert select.current_option == "nom"
    # L16: the live reading comes first, then the read-back.
    client.get_resource.side_effect = [current, replace(current, value="powerVent")]
    await select.async_select_option("power_vent")
    client.put_resource_value.assert_awaited_once_with(
        "synthetic-gateway", MODE, "powerVent"
    )
    assert select.current_option == "power_vent"


async def test_operation_mode_offers_only_what_both_the_unit_and_the_app_know(
    hass,
    environment,  # noqa: F811
):
    coordinator, client, entry = environment
    # exhaustOnly is never offered; an unknown code is never offered.
    coordinator.data = snapshots(
        text(MODE, ("off", "nom", "exhaustOnly", "vendorSpecial"), "off")
    )
    (select,) = await added_entities(hass, entry, setup_selects)
    assert select.options == ["off", "nom"]
    with pytest.raises(ServiceValidationError):
        await select.async_select_option("exhaustOnly")
    client.put_resource_value.assert_not_awaited()


async def test_operation_mode_stays_while_the_unit_runs_a_hidden_mode(
    hass,
    environment,  # noqa: F811
):
    coordinator, _, entry = environment
    coordinator.data = snapshots(
        text(MODE, ("off", "nom", "exhaustOnly"), "exhaustOnly")
    )
    (select,) = await added_entities(hass, entry, setup_selects)
    assert select.options == ["off", "nom"]
    assert select.current_option is None


@pytest.mark.parametrize(
    "resource",
    [
        text(MODE, ("auto", "manual"), "auto"),
        text(MODE, ("exhaustOnly",), "exhaustOnly"),
        text(MODE, MODES, "nom", writable=False),
        text(MODE, (), "nom"),
        text(MODE, MODES, "nom", kind="floatValue"),
    ],
    ids=[
        "no_known_value",
        "only_hidden_value",
        "read_only",
        "no_options",
        "wrong_type",
    ],
)
async def test_operation_mode_select_needs_a_writable_known_option(
    hass,
    environment,  # noqa: F811
    resource,
):
    coordinator, client, entry = environment
    coordinator.data = snapshots(resource)
    assert await added_entities(hass, entry, setup_selects) == []
    assert not assess_control(resource).eligible
    client.put_resource_value.assert_not_awaited()


async def test_operation_mode_write_without_read_back_is_not_confirmed(
    hass,
    environment,  # noqa: F811
):
    coordinator, client, entry = environment
    current = text(MODE, MODES, "nom")
    coordinator.data = snapshots(current)
    (select,) = await added_entities(hass, entry, setup_selects)
    client.get_resource.return_value = current
    with pytest.raises(HomeAssistantError):
        await select.async_select_option("off")
    # One request, no retry; the state stays what the unit still reports.
    assert client.put_resource_value.await_count == 1
    assert select.current_option == "nom"


async def test_operation_mode_refuses_a_mode_the_unit_stopped_offering(
    hass,
    environment,  # noqa: F811
):
    coordinator, client, entry = environment
    coordinator.data = snapshots(text(MODE, MODES, "nom"))
    (select,) = await added_entities(hass, entry, setup_selects)
    # The live reading no longer lists "fireplace": nothing is sent.
    client.get_resource.side_effect = [text(MODE, ("off", "nom"), "nom")]
    with pytest.raises(HomeAssistantError):
        await select.async_select_option("fireplace")
    client.put_resource_value.assert_not_awaited()


async def test_operation_mode_is_unavailable_when_the_unit_locks_it(
    hass,
    environment,  # noqa: F811
):
    coordinator, client, entry = environment
    coordinator.data = snapshots(text(MODE, MODES, "nom"))
    (select,) = await added_entities(hass, entry, setup_selects)
    coordinator.async_set_updated_data(
        snapshots(text(MODE, MODES, "nom", writable=False))
    )
    assert not select.available
    client.put_resource_value.assert_not_awaited()


# ------------------------------------------------ exhaust fan level (sensor)


async def test_exhaust_fan_level_is_no_control_any_more(
    hass,
    environment,  # noqa: F811
):
    coordinator, _, entry = environment
    # Even when a device reported it writable, the level is changed through
    # the operating mode; there is no select on it.
    resource = text(FAN, ("off", "nom"), "off")
    coordinator.data = snapshots(resource)
    assert await added_entities(hass, entry, setup_selects) == []
    assert assess_control(resource).policy is None


# ------------------------------------------------------ filter run time

FILTER = "/ventilation/zone1/filter/maxRunTime"


def filter_time(value=6.0, *, minimum=1.0, maximum=12.0, unit="month", **metadata):
    return Resource(
        path=FILTER,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type=metadata.pop("kind", "floatValue"),
            writable=metadata.pop("writable", True),
            minimum=minimum,
            maximum=maximum,
            unit=unit,
            **metadata,
        ),
    )


async def test_filter_run_time_number_is_disabled_and_writes_once(
    hass,
    environment,  # noqa: F811
):
    coordinator, client, entry = environment
    current = filter_time()
    coordinator.data = snapshots(current)
    (number,) = await added_entities(hass, entry, setup_numbers)
    assert number.entity_description.write_policy is FILTER_RUN_TIME_POLICY
    assert number.entity_description.entity_registry_enabled_default is False
    assert number.translation_key == "filter_maximum_run_time"
    assert (number.native_min_value, number.native_max_value) == (1.0, 12.0)
    assert number.native_unit_of_measurement == "mo"
    client.get_resource.side_effect = [current, replace(current, value=9.0)]
    await number.async_set_native_value(9.0)
    client.put_resource_value.assert_awaited_once_with("synthetic-gateway", FILTER, 9.0)
    assert number.native_value == 9.0


@pytest.mark.parametrize("value", [0.0, 13.0, float("nan")])
async def test_filter_run_time_rejects_values_outside_the_limits(
    hass,
    environment,  # noqa: F811
    value,
):
    coordinator, client, entry = environment
    coordinator.data = snapshots(filter_time())
    (number,) = await added_entities(hass, entry, setup_numbers)
    with pytest.raises(ServiceValidationError):
        await number.async_set_native_value(value)
    client.put_resource_value.assert_not_awaited()


@pytest.mark.parametrize(
    "resource",
    [
        filter_time(writable=False),
        filter_time(minimum=None),
        filter_time(maximum=None),
        filter_time(unit="mins"),
        filter_time(unit=None),
        filter_time(kind="stringValue"),
    ],
    ids=[
        "read_only",
        "no_minimum",
        "no_maximum",
        "wrong_unit",
        "no_unit",
        "wrong_type",
    ],
)
async def test_filter_run_time_needs_limits_unit_and_write_permission(
    hass,
    environment,  # noqa: F811
    resource,
):
    coordinator, client, entry = environment
    coordinator.data = snapshots(resource)
    assert await added_entities(hass, entry, setup_numbers) == []
    client.put_resource_value.assert_not_awaited()


def test_there_is_no_filter_reset_control():
    path = "/ventilation/zone1/filter/resetRunTime"
    for options in (("reset",), ("on", "off")):
        assert assess_control(text(path, options, options[0])).policy is None


# ---------------------------------------------------------- pool target


async def test_pool_target_uses_live_limits_and_step(hass, environment):  # noqa: F811
    coordinator, client, entry = environment
    current = pool()
    coordinator.data = snapshots(current)
    (number,) = await added_entities(hass, entry, setup_numbers)
    assert number.entity_description.write_policy is POOL_SETPOINT_POLICY
    assert number.translation_key == "pool_setpoint_temperature"
    assert (number.native_min_value, number.native_max_value) == (20.0, 35.0)
    assert number.native_step == 0.5
    assert number.native_unit_of_measurement == "°C"
    assert number.native_value == 26.0
    client.get_resource.side_effect = [current, replace(current, value=27.5)]
    await number.async_set_native_value(27.5)
    client.put_resource_value.assert_awaited_once_with("synthetic-gateway", POOL, 27.5)
    assert number.native_value == 27.5
    # The gateway changes its limits and step: the control follows.
    coordinator.async_set_updated_data(
        snapshots(pool(27.5, minimum=22.0, maximum=30.0, step=1.0))
    )
    assert (number.native_min_value, number.native_max_value) == (22.0, 30.0)
    assert number.native_step == 1.0


@pytest.mark.parametrize("value", [27.3, 19.5, 35.5, float("nan")])
async def test_pool_target_rejects_off_grid_and_out_of_range_values(
    hass,
    environment,  # noqa: F811
    value,
):
    coordinator, client, entry = environment
    coordinator.data = snapshots(pool())
    (number,) = await added_entities(hass, entry, setup_numbers)
    with pytest.raises(ServiceValidationError):
        await number.async_set_native_value(value)
    client.put_resource_value.assert_not_awaited()


async def test_pool_target_without_step_uses_whole_degrees(
    hass,
    environment,  # noqa: F811
):
    coordinator, client, entry = environment
    current = pool(step=None)
    coordinator.data = snapshots(current)
    (number,) = await added_entities(hass, entry, setup_numbers)
    assert number.native_step == 1.0
    client.get_resource.side_effect = [current, replace(current, value=27.5)]
    # Without an advertised step only the limits apply.
    await number.async_set_native_value(27.5)
    client.put_resource_value.assert_awaited_once_with("synthetic-gateway", POOL, 27.5)


@pytest.mark.parametrize(
    "resource",
    [
        pool(writable=False),
        pool(unit="%"),
        pool(kind="stringValue"),
        pool(minimum=None),
        pool(step=0.0),
        pool(step=None, step_size_valid=False),
    ],
    ids=["read_only", "unit", "type", "no_limit", "zero_step", "invalid_step"],
)
async def test_pool_target_needs_a_writable_temperature_contract(
    hass,
    environment,  # noqa: F811
    resource,
):
    coordinator, _, entry = environment
    coordinator.data = snapshots(resource)
    assert await added_entities(hass, entry, setup_numbers) == []


async def test_pool_target_read_back_must_confirm(hass, environment):  # noqa: F811
    coordinator, client, entry = environment
    current = pool()
    coordinator.data = snapshots(current)
    (number,) = await added_entities(hass, entry, setup_numbers)
    client.get_resource.return_value = current
    with pytest.raises(HomeAssistantError):
        await number.async_set_native_value(30.0)
    assert number.native_value == 26.0


# ---------------------------------------------------------- pool switch


async def test_pool_switch_writes_and_confirms(hass, environment):  # noqa: F811
    coordinator, client, entry = environment
    current = text(POOL_ON, ("off", "on"), "off")
    coordinator.data = snapshots(current)
    (switch,) = await added_entities(hass, entry, setup_switches)
    assert await added_entities(hass, entry, setup_selects) == []
    assert switch.entity_description.write_policy is POOL_ENABLED_POLICY
    assert switch.translation_key == "pool_enabled"
    assert switch.is_on is False
    client.get_resource.side_effect = [current, replace(current, value="on")]
    await switch.async_turn_on()
    client.put_resource_value.assert_awaited_once_with(
        "synthetic-gateway", POOL_ON, "on"
    )
    assert switch.is_on is True
    client.get_resource.side_effect = None
    client.get_resource.return_value = replace(current, value="on")
    with pytest.raises(HomeAssistantError):
        await switch.async_turn_off()
    assert switch.is_on is True


@pytest.mark.parametrize(
    "resource",
    [
        text(POOL_ON, ("on", "off", "auto"), "on"),
        text(POOL_ON, ("on",), "on"),
        text(POOL_ON, ("true", "false"), "true"),
        text(POOL_ON, ("on", "off"), "on", writable=False),
    ],
    ids=["extra_value", "missing_value", "other_values", "read_only"],
)
async def test_pool_switch_needs_exactly_on_and_off(
    hass,
    environment,  # noqa: F811
    resource,
):
    coordinator, _, entry = environment
    coordinator.data = snapshots(resource)
    assert await added_entities(hass, entry, setup_switches) == []
    assert await added_entities(hass, entry, setup_selects) == []


def test_no_summer_bypass_control():
    # The pool mode of the auxiliary heater is a selection over the values the
    # gateway advertises since 0.9.2 (``system_controls_extra``).
    for path, options in (("/ventilation/zone1/summerBypass/enable", ("on", "off")),):
        assert assess_control(text(path, options, options[0])).policy is None
    # A ventilation mode list without any known mode gives no control.
    assert not assess_control(
        text("/ventilation/zone1/operationMode", ("auto", "manual"), "auto")
    ).eligible


def test_new_control_translations_exist_in_every_language():
    for name in ("strings.json", "translations/en.json", "translations/de.json"):
        entity = json.loads((INTEGRATION / name).read_text(encoding="utf-8"))["entity"]
        states = entity["select"]["ventilation_operation_mode"]["state"]
        assert set(states) == {
            "time",
            "off",
            "min",
            "red",
            "nom",
            "max",
            "dem",
            "party",
            "power_vent",
            "fireplace",
            "fall_asleep",
            "freefct",
        }
        assert "ventilation_exhaust_fan_level" not in entity["select"]
        assert entity["number"]["filter_maximum_run_time"]["name"]
        assert entity["number"]["pool_setpoint_temperature"]["name"]
        assert entity["switch"]["pool_enabled"]["name"]
