"""Program name text control and the variable-tariff readings: failure paths.

The name control is driven through its service with the coordinator's write
replaced by the failure under test, so the outcome is the translated error the
user sees and nothing else.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating import text as text_platform
from custom_components.bosch_buderus_heating.coordinator import Freshness
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    RateLimited,
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.pointt.exceptions import ServiceUnavailable
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.variable_tariff import (
    TARIFF_ENABLE_PATH,
    TARIFF_ID_PATH,
    tariff_enabled,
    tariff_id,
)
from custom_components.bosch_buderus_heating.variable_tariff_entities import (
    TariffIdSensor,
    VariableTariffActiveBinarySensor,
)
from custom_components.bosch_buderus_heating.write_policies import ContractRejected
from custom_components.bosch_buderus_heating.writes import WriteBusy
from tests import test_dynamic_controls
from tests.circuit_control_helpers import coordinator_for, variant
from tests.test_program_names import NAME, gateway_for

ICOM = "icom/tbso.json"


@pytest.fixture
def environment(hass, enable_custom_integrations):
    yield from test_dynamic_controls.environment.__wrapped__(hass)


def _entity(environment):
    gateway = gateway_for(environment, "ascii_plain")
    (description,) = text_platform.descriptions({NAME: gateway.read(NAME)})
    entity = text_platform.ProgramNameText(gateway.coordinator, description)
    entity.hass = gateway.coordinator.hass
    return gateway, entity


# Program name -------------------------------------------------------------------------


async def test_a_name_that_is_not_current_is_unavailable_and_not_written(environment):
    gateway, entity = _entity(environment)
    coordinator = gateway.coordinator
    assert entity.available
    coordinator.data[NAME] = replace(coordinator.data[NAME], freshness=Freshness.STALE)
    assert not entity.available
    assert entity.native_value is None
    with pytest.raises(ServiceValidationError) as caught:
        await entity.async_set_value("Home")
    assert caught.value.translation_key == "program_name_unsupported"
    assert gateway.puts == 0


async def test_the_name_length_follows_the_reading_and_has_a_default(environment):
    gateway, entity = _entity(environment)
    assert entity.native_max >= 1
    gateway.coordinator.data.pop(NAME)
    assert entity.native_max == entity.entity_description.native_max
    assert entity.device_info["identifiers"]


@pytest.mark.parametrize(
    ("error", "key", "kind"),
    [
        (WriteNotConfirmed("x"), "program_name_not_confirmed", HomeAssistantError),
        (WriteBusy("x"), "write_busy", HomeAssistantError),
        (AuthenticationError("x"), "write_authentication_failed", HomeAssistantError),
        (RateLimited(5), "write_rate_limited", HomeAssistantError),
        (ServiceUnavailable(503), "write_failed", HomeAssistantError),
        (WriteValidationError("x"), "program_name_changed", ServiceValidationError),
    ],
)
async def test_failed_name_writes_become_translated_messages(
    environment, error, key, kind
):
    gateway, entity = _entity(environment)
    with (
        patch.object(
            gateway.coordinator,
            "async_write_program_name",
            AsyncMock(side_effect=error),
        ),
        pytest.raises(kind) as caught,
    ):
        await entity.async_set_value("Home")
    assert caught.value.translation_key == key
    assert gateway.puts == 0


async def test_a_refusal_while_the_cloud_went_down_names_the_cloud(environment):
    gateway, entity = _entity(environment)
    coordinator = gateway.coordinator

    async def refused(*_args):
        coordinator.control_status = lambda: ("cloud_unreachable", None)
        raise WriteValidationError("changed")

    with (
        patch.object(coordinator, "async_write_program_name", refused),
        pytest.raises(HomeAssistantError) as caught,
    ):
        await entity.async_set_value("Home")
    assert caught.value.translation_key == "command_cloud_unreachable"


async def test_a_name_is_not_written_while_the_cloud_is_down(environment):
    gateway, entity = _entity(environment)
    gateway.coordinator.control_status = lambda: ("rate_limited", None)
    with pytest.raises(HomeAssistantError) as caught:
        await entity.async_set_value("Home")
    assert caught.value.translation_key == "command_cloud_rate_limited"
    assert gateway.puts == 0


async def test_a_rejected_name_says_which_limit_applies(environment):
    gateway, entity = _entity(environment)
    error = ContractRejected("program_name_too_long")
    with (
        patch.object(
            gateway.coordinator,
            "async_write_program_name",
            AsyncMock(side_effect=error),
        ),
        pytest.raises(ServiceValidationError) as caught,
    ):
        await entity.async_set_value("x" * 80)
    assert caught.value.translation_key == "program_name_too_long"
    assert caught.value.translation_placeholders["max"].isdigit()


# Variable tariff ----------------------------------------------------------------------


def test_a_tariff_reading_without_a_value_is_unknown():
    empty = parse_resource({"id": TARIFF_ID_PATH, "type": "floatValue"})
    assert tariff_id(None) is None
    assert tariff_id(empty) is None
    assert tariff_enabled(empty) is None


async def test_tariff_entities_show_nothing_for_an_outdated_reading(hass):
    coordinator = coordinator_for(hass, variant(ICOM, "on"))
    binary = VariableTariffActiveBinarySensor(coordinator)
    sensor = TariffIdSensor(coordinator)
    assert binary.available and sensor.available
    for path in (TARIFF_ENABLE_PATH, TARIFF_ID_PATH):
        coordinator.data[path] = replace(
            coordinator.data[path], freshness=Freshness.STALE
        )
    assert not binary.available and binary.is_on is None
    assert not sensor.available and sensor.native_value is None
    assert sensor.extra_state_attributes is None


async def test_tariff_entities_belong_to_the_gateway_device(hass):
    coordinator = coordinator_for(hass, variant(ICOM, "on"))
    sensor = TariffIdSensor(coordinator)
    assert sensor.device_info["identifiers"]
    await sensor.async_update()
