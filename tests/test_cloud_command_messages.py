"""A command during a cloud pause or outage says so instead of blaming itself."""

from __future__ import annotations

import json
from pathlib import Path
from time import monotonic
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.climate.const import HVACMode
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating.pointt import WriteValidationError
from tests.test_heating_controls import setup
from tests.test_select import _select

PACKAGE = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"
CLOUD_DOWN = [
    ("auth_required", "command_cloud_auth_required"),
    ("cloud_stopped", "command_cloud_stopped"),
    ("rate_limited", "command_cloud_rate_limited"),
    ("protection_pause", "command_cloud_protection_pause"),
    ("cloud_unreachable", "command_cloud_unreachable"),
]


def _take_cloud_down(coordinator, state: str) -> None:
    if state == "auth_required":
        coordinator._cloud_auth_required = lambda: True
    elif state == "cloud_stopped":
        coordinator._cloud_source_stopped = lambda: True
    elif state == "rate_limited":
        coordinator._cloud_backoff_until = monotonic() + 600
    elif state == "protection_pause":
        coordinator._circuit_open_until = monotonic() + 600
    else:
        coordinator.last_update_success = False


@pytest.mark.parametrize(("state", "translation_key"), CLOUD_DOWN)
async def test_climate_commands_name_the_cloud_state(
    hass: HomeAssistant, state: str, translation_key: str
) -> None:
    _, coordinator, entity = setup(hass)
    _take_cloud_down(coordinator, state)
    # The values stay shown; only commands are refused, with the reason.
    for command in (
        lambda: entity.async_set_hvac_mode(HVACMode.AUTO),
        lambda: entity.async_set_temperature(temperature=20.5),
        entity.async_turn_off,
    ):
        with pytest.raises(HomeAssistantError) as raised:
            await command()
        assert raised.value.translation_key == translation_key
        until = raised.value.translation_placeholders["until"]
        assert bool(until) is (state in ("rate_limited", "protection_pause"))
    coordinator._write_service.async_write_enum.assert_not_awaited()
    coordinator._write_service.async_write_number.assert_not_awaited()
    coordinator.client.get_resource.assert_not_awaited()


async def test_a_climate_command_with_a_usable_cloud_keeps_its_own_message(
    hass: HomeAssistant,
) -> None:
    _, coordinator, entity = setup(hass, mode="auto")
    with pytest.raises(ServiceValidationError) as raised:
        await entity.async_set_temperature(temperature=22)
    assert raised.value.translation_key == "heating_climate_command"
    coordinator._write_service.async_write_number.assert_not_awaited()


@pytest.mark.parametrize(("state", "translation_key"), CLOUD_DOWN)
async def test_a_refused_control_write_names_the_cloud_state(
    hass: HomeAssistant, state: str, translation_key: str
) -> None:
    entity = _select(hass)
    _take_cloud_down(entity.coordinator, state)
    entity.coordinator.async_write_control = AsyncMock(
        side_effect=WriteValidationError("paused")
    )
    with pytest.raises(HomeAssistantError) as raised:
        await entity.async_select_option("auto")
    assert raised.value.translation_key == translation_key


async def test_the_coordinator_refuses_a_write_during_its_protection_pause(
    hass: HomeAssistant,
) -> None:
    entity = _select(hass)
    entity.coordinator._circuit_open_until = monotonic() + 600
    with pytest.raises(HomeAssistantError) as raised:
        await entity.async_select_option("auto")
    assert raised.value.translation_key == "command_cloud_protection_pause"
    assert raised.value.translation_placeholders["until"]


async def test_an_option_no_longer_offered_has_a_translated_message(
    hass: HomeAssistant,
) -> None:
    entity = _select(hass)
    with pytest.raises(ServiceValidationError) as raised:
        await entity.async_select_option("no-longer-offered")
    assert raised.value.translation_key == "select_option_unavailable"


def test_every_new_message_and_event_type_is_translated() -> None:
    strings = json.loads((PACKAGE / "strings.json").read_text("utf-8"))
    exceptions = strings["exceptions"]
    for _, key in CLOUD_DOWN:
        assert exceptions[key]["message"]
    for key in ("select_option_unavailable", "holiday_not_deletable"):
        assert exceptions[key]["message"]
    states = strings["entity"]["event"]["system_notifications"]["state_attributes"]
    assert set(states["event_type"]["state"]) == {"appeared", "resolved"}
    # Messages name the cloud access, never the internal service name.
    for section in ("config", "options"):
        for text in strings[section]["error"].values():
            assert "PointT" not in text
    for key, value in exceptions.items():
        assert "PointT" not in value["message"], key
    for issue in strings["issues"].values():
        assert "PointT" not in json.dumps(issue)
