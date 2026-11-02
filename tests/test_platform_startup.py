"""Load every HA platform for PointT devices that do not have a purifier."""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_buderus_heating.const import DOMAIN, PLATFORMS
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.pointt import Gateway, PointTClient
from custom_components.bosch_buderus_heating.runtime import BoschBuderusRuntimeData
from tests.test_heating_controls import MODE, TARGET, scalar
from tests.test_water_softener import reading


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize("include_heating", [False, True])
async def test_all_platforms_load_without_purifier(hass, include_heating):
    entry = MockConfigEntry(domain=DOMAIN, version=3)
    entry.add_to_hass(hass)
    client = PointTClient(async_get_clientsession(hass), "synthetic-access")
    devices = [
        (
            Gateway("softener", model="watersoftener"),
            [reading("sensor/saltLevel", 37, "%")],
        )
    ]
    if include_heating:
        devices.append(
            (
                Gateway("heating", model="MX400"),
                [
                    scalar(MODE, "manual", options=("off", "manual", "auto")),
                    scalar(TARGET, 19),
                ],
            )
        )
    coordinators = []
    for gateway, resources in devices:
        coordinator = BoschBuderusDataUpdateCoordinator(hass, client, gateway, entry)
        coordinator.resources = {r.path: r for r in resources}
        coordinator.data = {
            r.path: ResourceSnapshot(r, True, datetime.now(UTC)) for r in resources
        }
        coordinator.last_update_success = True
        coordinators.append(coordinator)

    async def start(hass, config):
        config.runtime_data = BoschBuderusRuntimeData(
            client, None, tuple(g for g, _ in devices), tuple(coordinators)
        )
        await hass.config_entries.async_forward_entry_setups(config, PLATFORMS)
        return True

    async def stop(hass, config):
        result = await hass.config_entries.async_unload_platforms(config, PLATFORMS)
        for coordinator in coordinators:
            await coordinator.async_shutdown()
        return result

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
        entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        states = [hass.states.get(entity.entity_id) for entity in entities]
        assert any(
            state is not None and state.state in ("37", "37.0") for state in states
        ), {state.entity_id: state.state for state in states if state is not None}
        assert not any(entity.domain == "fan" for entity in entities)
        assert any(entity.domain == "climate" for entity in entities) is include_heating
        assert await hass.config_entries.async_unload(entry.entry_id)
