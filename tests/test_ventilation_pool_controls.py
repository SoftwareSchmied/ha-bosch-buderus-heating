"""P31: exhaust fan level, pool target and pool switch as confirmed cloud writes."""

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
    POOL_ENABLED_POLICY,
    POOL_SETPOINT_POLICY,
    VENTILATION_FAN_LEVEL_POLICY,
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


# ------------------------------------------------------------ fan level


async def test_fan_level_select_writes_and_confirms(hass, environment):  # noqa: F811
    coordinator, client, entry = environment
    current = text(FAN, ("nom", "off", "max"), "off")
    coordinator.data = snapshots(current)
    (select,) = await added_entities(hass, entry, setup_selects)
    assert select.entity_description.write_policy is VENTILATION_FAN_LEVEL_POLICY
    assert select.translation_key == "ventilation_exhaust_fan_level"
    # The documented order, independently of the advertised order.
    assert select.options == ["off", "nom", "max"]
    assert select.current_option == "off"
    client.get_resource.return_value = replace(current, value="nom")
    await select.async_select_option("nom")
    client.put_resource_value.assert_awaited_once_with("synthetic-gateway", FAN, "nom")
    assert select.current_option == "nom"


@pytest.mark.parametrize(
    "resource",
    [
        text(FAN, ("off", "nom", "turbo"), "off"),
        text(FAN, ("off", "nom"), "off", writable=False),
        text(FAN, (), "off"),
        text(FAN, ("off", "nom"), "off", kind="floatValue"),
    ],
    ids=["unknown_level", "read_only", "no_options", "wrong_type"],
)
async def test_fan_level_select_needs_a_writable_documented_subset(
    hass,
    environment,  # noqa: F811
    resource,
):
    coordinator, client, entry = environment
    coordinator.data = snapshots(resource)
    assert await added_entities(hass, entry, setup_selects) == []
    assert not assess_control(resource).eligible
    client.put_resource_value.assert_not_awaited()


async def test_fan_level_select_without_read_back_is_not_confirmed(
    hass,
    environment,  # noqa: F811
):
    coordinator, client, entry = environment
    current = text(FAN, ("off", "nom"), "off")
    coordinator.data = snapshots(current)
    (select,) = await added_entities(hass, entry, setup_selects)
    client.get_resource.return_value = current
    with pytest.raises(HomeAssistantError):
        await select.async_select_option("nom")
    assert client.put_resource_value.await_count == 1
    assert select.current_option == "off"


async def test_fan_level_select_disappears_with_an_unknown_level(
    hass,
    environment,  # noqa: F811
):
    coordinator, client, entry = environment
    coordinator.data = snapshots(text(FAN, ("off", "nom"), "off"))
    (select,) = await added_entities(hass, entry, setup_selects)
    coordinator.async_set_updated_data(snapshots(text(FAN, ("off", "boost"), "off")))
    assert not select.available
    with pytest.raises(ServiceValidationError):
        await select.async_select_option("boost")
    client.put_resource_value.assert_not_awaited()


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
    client.get_resource.return_value = replace(current, value=27.5)
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
    client.get_resource.return_value = replace(current, value=27.5)
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
    client.get_resource.return_value = replace(current, value="on")
    await switch.async_turn_on()
    client.put_resource_value.assert_awaited_once_with(
        "synthetic-gateway", POOL_ON, "on"
    )
    assert switch.is_on is True
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


def test_no_summer_bypass_ventilation_mode_or_pool_mode_control():
    for path, options in (
        ("/ventilation/zone1/summerBypass/enable", ("on", "off")),
        ("/ventilation/zone1/operationMode", ("auto", "manual")),
        ("/pool/additionalHeater/poolMode", ("on", "off")),
    ):
        assert assess_control(text(path, options, options[0])).policy is None


def test_new_control_translations_exist_in_every_language():
    for name in ("strings.json", "translations/en.json", "translations/de.json"):
        entity = json.loads((INTEGRATION / name).read_text(encoding="utf-8"))["entity"]
        states = entity["select"]["ventilation_exhaust_fan_level"]["state"]
        assert set(states) == {"off", "min", "red", "nom", "max", "dem"}
        assert entity["number"]["pool_setpoint_temperature"]["name"]
        assert entity["switch"]["pool_enabled"]["name"]
