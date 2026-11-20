"""Stopping the extra hot water: a stop the plant does not take is reported."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    Resource,
    ResourceMetadata,
    WriteNotConfirmed,
)
from custom_components.bosch_buderus_heating.switch import (
    BoschBuderusSwitch,
    build_switch_descriptions,
)
from custom_components.bosch_buderus_heating.writes import WriteService
from tests.ha_test_helpers import MockConfigEntry

PATH = "/dhwCircuits/dhw1/charge"


def _charge(value: str) -> Resource:
    return Resource(
        path=PATH,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="stringValue",
            allowed_values=("start", "stop"),
            writable=True,
        ),
    )


def _plant(hass: HomeAssistant, shown: str, device: list[str]):
    """A coordinator with the real write service; ``device`` is what the plant
    reports on every read, ``device[0]`` changes when it takes a value."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.get_resource.side_effect = lambda *_a: _charge(device[0])
    client.put_resource_value.return_value = None
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, client, Gateway("gateway-one", device_type="K40"), entry
    )
    coordinator._write_service = WriteService(client, sleep=AsyncMock())
    resource = _charge(shown)
    coordinator.resources = {PATH: resource}
    coordinator.data = {PATH: ResourceSnapshot(resource, True, datetime.now(UTC))}
    coordinator.last_update_success = True
    switch = BoschBuderusSwitch(
        coordinator, build_switch_descriptions(coordinator.resources)[0]
    )
    return coordinator, client, switch


async def test_a_stop_the_plant_ignores_is_reported_not_confirmed(
    hass: HomeAssistant,
) -> None:
    """The plant keeps the charge running (the reading stays ``start``): one
    PUT, no retry, an error for the user and no confirmed state."""
    coordinator, client, switch = _plant(hass, "start", ["start"])
    with pytest.raises(HomeAssistantError) as caught:
        await switch.async_turn_off()
    assert caught.value.translation_key == "write_not_confirmed"
    assert client.put_resource_value.await_count == 1
    client.put_resource_value.assert_awaited_with("gateway-one", PATH, "stop")
    # The unconfirmed state is not shown as confirmed.
    assert not coordinator.data[PATH].available
    assert coordinator.data[PATH].last_error_category is not None


async def test_a_stop_the_plant_takes_is_confirmed(hass: HomeAssistant) -> None:
    device = ["start"]
    coordinator, client, switch = _plant(hass, "start", device)

    async def put(*_args: object) -> None:
        device[0] = "stop"

    client.put_resource_value.side_effect = put
    await switch.async_turn_off()
    assert client.put_resource_value.await_count == 1
    assert coordinator.data[PATH].resource.value == "stop"
    assert coordinator.data[PATH].available


async def test_the_write_service_does_not_confirm_a_stop_that_reads_start() -> None:
    client = AsyncMock()
    client.get_resource.return_value = _charge("start")
    client.put_resource_value.return_value = None
    service = WriteService(client, sleep=AsyncMock())
    from custom_components.bosch_buderus_heating.writes import enum_policy_for_resource

    policy = enum_policy_for_resource(_charge("start"))
    assert policy is not None
    with pytest.raises(WriteNotConfirmed):
        await service.async_write_enum("gateway-one", _charge("start"), "stop", policy)
    assert client.put_resource_value.await_count == 1
