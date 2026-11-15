"""Write guards: an echo that matches, a request for another path, and which
policies count as switches."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.bosch_buderus_heating.pointt import (
    WriteRejected,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.writes import (
    HEATING_CIRCUIT_OPERATION_MODE_POLICY,
    WDDW2_HOLIDAY_POLICY,
    WriteRequest,
    WriteService,
    _validate_number,
    is_switch_policy,
    number_policy_for_resource,
)
from tests.test_writes import _number_resource


def number_policy():
    policy = number_policy_for_resource(_number_resource())
    assert policy is not None
    return policy


async def test_an_echo_that_matches_the_request_is_accepted_and_still_read_back():
    client = AsyncMock()
    client.put_resource_value.return_value = _number_resource(20.5)
    client.get_resource.return_value = _number_resource(20.5)
    result = await WriteService(client, sleep=AsyncMock()).async_write_number(
        "gw", _number_resource(), 20.5, number_policy()
    )
    assert result.resource.value == 20.5
    client.put_resource_value.assert_awaited_once()
    client.get_resource.assert_awaited()


@pytest.mark.parametrize(
    "echo",
    [
        _number_resource(21.0),  # another value
        _number_resource(20.5, path="/heatingCircuits/hc1/other"),
    ],
)
async def test_an_echo_that_does_not_match_is_rejected_without_a_second_put(echo):
    client = AsyncMock()
    client.put_resource_value.return_value = echo
    with pytest.raises(WriteRejected):
        await WriteService(client, sleep=AsyncMock()).async_write_number(
            "gw", _number_resource(), 20.5, number_policy()
        )
    client.put_resource_value.assert_awaited_once()
    client.get_resource.assert_not_awaited()


def test_a_request_for_another_path_is_never_validated_against_a_resource():
    with pytest.raises(WriteValidationError, match="path is not approved"):
        _validate_number(
            _number_resource(),
            WriteRequest("gw", "/heatingCircuits/hc2/manualRoomSetpoint", 20.0),
            number_policy(),
        )


def test_only_policies_with_two_declared_values_are_switches():
    assert is_switch_policy(WDDW2_HOLIDAY_POLICY)
    assert not is_switch_policy(HEATING_CIRCUIT_OPERATION_MODE_POLICY)
    assert not is_switch_policy(number_policy())
    assert not is_switch_policy(None)


def test_a_number_policy_never_offers_a_switch():
    from custom_components.bosch_buderus_heating.writes import ControlAssessment

    number = ControlAssessment(number_policy(), None, options=("on", "off"))
    assert not number.supports_switch and number.platform == "number"
    assert not number.supports_select
    switch = ControlAssessment(
        WDDW2_HOLIDAY_POLICY, None, options=tuple(WDDW2_HOLIDAY_POLICY.known_values)
    )
    assert switch.supports_switch and switch.platform == "switch"
    # A switch whose controller offers a third value becomes a select.
    wider = ControlAssessment(
        WDDW2_HOLIDAY_POLICY,
        None,
        options=(*WDDW2_HOLIDAY_POLICY.known_values, "extra"),
    )
    assert wider.supports_switch and wider.supports_select
