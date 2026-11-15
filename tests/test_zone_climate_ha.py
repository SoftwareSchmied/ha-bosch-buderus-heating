"""The zone entities through Home Assistant's own platforms and services.

Synthetic zone controller readings (the zone controller fixture); nothing
here confirms behaviour on a real device.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import Platform
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.pointt import Gateway
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.runtime import BoschBuderusRuntimeData
from tests.ha_test_helpers import MockConfigEntry
from tests.test_write_guards import Cloud

FIXTURE = json.loads(
    (
        Path(__file__).parent / "fixtures/contracts/connectkey/zone-climate.json"
    ).read_text(encoding="utf-8")
)
PLATFORMS = [Platform.CLIMATE, Platform.BUTTON, Platform.BINARY_SENSOR, Platform.SENSOR]
TEMPORARY = "/zones/zone1/heating/temporaryRoomSetpoint"


@pytest.mark.usefixtures("legacy_platform_runtime")
async def test_zone_entities_are_set_up_and_take_service_calls(
    hass, enable_custom_integrations
):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"brand": "buderus", "gateway_ids": ["gateway"]}
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, client, Gateway("gateway"), entry
    )
    resources = {item["id"]: parse_resource(item) for item in FIXTURE["resources"]}
    coordinator.data = {
        path: ResourceSnapshot(resource, True, datetime.now(UTC))
        for path, resource in resources.items()
    }
    coordinator.resources = dict(resources)
    coordinator.last_update_success = True
    cloud = Cloud(coordinator, with_context=False)
    runtime = BoschBuderusRuntimeData(
        client, AsyncMock(), (coordinator.gateway,), (coordinator,)
    )

    async def start(hass, config):
        config.runtime_data = runtime
        await hass.config_entries.async_forward_entry_setups(config, PLATFORMS)
        return True

    async def stop(hass, config):
        return await hass.config_entries.async_unload_platforms(config, PLATFORMS)

    registry = er.async_get(hass)
    with (
        patch(
            "custom_components.bosch_buderus_heating.async_setup_entry",
            side_effect=start,
        ),
        patch(
            "custom_components.bosch_buderus_heating.async_unload_entry",
            side_effect=stop,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        climate_id = registry.async_get_entity_id(
            "climate", DOMAIN, "gateway:zones:zone1:climate"
        )
        assert climate_id
        state = hass.states.get(climate_id)
        assert state.state == "auto"
        assert state.attributes["current_temperature"] == 20
        assert state.attributes["current_humidity"] == 45
        assert state.attributes["temperature"] == 21
        assert state.attributes["min_temp"] == 5
        assert state.attributes["max_temp"] == 30
        assert state.attributes["target_temp_step"] == 0.5
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": climate_id, "temperature": 22.5},
            blocking=True,
        )
        assert cloud.puts == [(TEMPORARY, 22.5)]
        assert hass.states.get(climate_id).attributes["temporary_override"] is True
        # The clear button exists, disabled: -1 lies outside the limits.
        clear = registry.async_get_entity_id(
            "button", DOMAIN, "gateway:zones:zone1:heating:temporaryRoomSetpoint:clear"
        )
        assert clear and registry.async_get(clear).disabled_by is not None
        battery = registry.async_get_entity_id(
            "binary_sensor", DOMAIN, "gateway:devices:device1:battery"
        )
        assert battery and hass.states.get(battery).state == "off"
        signal = registry.async_get_entity_id(
            "sensor", DOMAIN, "gateway:devices:device1:signal"
        )
        assert signal and registry.async_get(signal).disabled_by is not None
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
