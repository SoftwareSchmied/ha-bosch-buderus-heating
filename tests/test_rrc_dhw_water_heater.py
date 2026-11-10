"""W3: the PointT water heater on an RRC 2.0 room controller.

The same entity serves every gateway. Only a recognised room controller
(S11) reads "off" as off and uses the hot water contract's mode policy;
other gateways keep their exact spelling and the generic policy (the
regression tests in ``test_pointt_water_heater*.py`` stay as they are).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating.coordinator import ResourceSnapshot
from custom_components.bosch_buderus_heating.rrc_contract_dhw import (
    OPERATION_MODE_POLICY,
)
from custom_components.bosch_buderus_heating.writes import (
    DHW_OPERATION_MODE_POLICY,
    GENERIC_CONTEXT,
    RRC_CONTEXT,
)
from tests.test_pointt_water_heater import setup

# The controller of test_pointt_water_heater, but named like an RRC circuit.
MODE = "/dhwCircuits/dhw2/operationMode"


def _room_controller(hass, *, mode: str, options=(), missing: bool = True):
    entry, coordinator, heater, plant = setup(hass, mode=mode, options=options)
    resource = plant[MODE]
    plant[MODE] = replace(
        resource,
        metadata=replace(
            resource.metadata,
            allowed_values=tuple(options),
            allowed_values_missing=missing,
        ),
    )
    coordinator.data[MODE] = ResourceSnapshot(plant[MODE], True, datetime.now(UTC))
    coordinator.control_context = lambda: RRC_CONTEXT
    return entry, coordinator, heater, plant


def _refresh(coordinator, plant) -> None:
    coordinator.data[MODE] = ResourceSnapshot(plant[MODE], True, datetime.now(UTC))


def test_room_controller_reads_off_and_offers_the_fallback_modes(hass) -> None:
    _, _coordinator, heater, _ = _room_controller(hass, mode="off")
    assert heater.available
    assert heater.current_operation == "off"
    assert heater.operation_list == ["off", "high", "ownprogram"]


def test_other_gateways_keep_their_exact_spelling(hass) -> None:
    """Regression: "off" is no EMS code, and without options no control."""
    _, coordinator, heater, _ = _room_controller(hass, mode="off")
    coordinator.control_context = lambda: GENERIC_CONTEXT
    assert not heater.available
    assert heater.operation_list == []
    _, coordinator, heater, _ = _room_controller(hass, mode="high")
    coordinator.control_context = lambda: GENERIC_CONTEXT
    assert heater.current_operation == "high"
    assert heater.operation_list == []


async def test_off_is_written_as_off_and_confirmed_by_off(hass) -> None:
    _, coordinator, heater, plant = _room_controller(hass, mode="high")
    calls = []

    async def write(_gateway, path, value):
        calls.append((path, value))
        # The controller stores "Off" and reads it back as "off".
        plant[path] = replace(plant[path], value="off" if value == "Off" else value)

    coordinator.client.put_resource_value.side_effect = write
    policies = []
    original = coordinator.async_write_control

    async def spy(path, value, policy, **kwargs):
        policies.append(policy)
        return await original(path, value, policy, **kwargs)

    coordinator.async_write_control = spy
    await heater.async_set_operation_mode("off")
    assert calls == [(MODE, "Off")]
    assert policies == [OPERATION_MODE_POLICY]
    _refresh(coordinator, plant)
    assert heater.current_operation == "off"
    await heater.async_set_operation_mode("ownprogram")
    assert calls[-1] == (MODE, "ownprogram")
    with pytest.raises(ServiceValidationError):
        await heater.async_set_operation_mode("eco")
    assert len(calls) == 2


async def test_advertised_codes_are_written_as_advertised(hass) -> None:
    """Both spellings advertised: one "off" operation, written as "Off";
    only "off" advertised: written as "off"."""
    _, coordinator, heater, _plant = _room_controller(
        hass, mode="high", options=("high", "off", "Off"), missing=False
    )
    assert heater.operation_list == ["off", "high"]
    await heater.async_set_operation_mode("off")
    assert coordinator.client.put_resource_value.await_args.args[1:] == (MODE, "Off")
    _, coordinator, heater, _plant = _room_controller(
        hass, mode="high", options=("high", "off"), missing=False
    )
    await heater.async_set_operation_mode("off")
    assert coordinator.client.put_resource_value.await_args.args[1:] == (MODE, "off")


async def test_unconfirmed_mode_is_reported_and_not_sent_again(hass) -> None:
    _, coordinator, heater, _plant = _room_controller(hass, mode="high")

    async def ignore(_gateway, _path, _value):
        return None

    coordinator.client.put_resource_value.side_effect = ignore
    with pytest.raises(HomeAssistantError) as unconfirmed:
        await heater.async_set_operation_mode("ownprogram")
    assert unconfirmed.value.translation_key == "rrc_write_pending_confirmation"
    assert coordinator.client.put_resource_value.await_count == 1


def test_low_without_options_is_shown_but_not_offered(hass) -> None:
    _, _coordinator, heater, _ = _room_controller(hass, mode="low")
    assert heater.current_operation == "low"
    assert heater.operation_list == []
    assert DHW_OPERATION_MODE_POLICY is not OPERATION_MODE_POLICY
