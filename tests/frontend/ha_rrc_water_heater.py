"""W3: the native water-heater dialog of a synthetic RRC 2.0 hot water circuit.

The mode list of a room controller without advertised options (Off,
Comfort, Auto), "Off" written as such and confirmed by "off".
"""

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from homeassistant.const import Platform
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.resource_catalog import PollGroup
from tests.test_rrc_dhw_water_heater import MODE, _room_controller


async def test_room_controller_hot_water_frontend(
    recorder_mock,
    hass,
    hass_client,
    hass_access_token,
    enable_custom_integrations,
    legacy_platform_runtime,
    tmp_path,
):
    hass.config.language = "de"
    entry, coordinator, expected, plant = _room_controller(hass, mode="high")

    async def write(_gateway, path, value):
        # The controller stores "Off" and reads it back as "off".
        plant[path] = replace(plant[path], value="off" if value == "Off" else value)

    coordinator.client.put_resource_value.side_effect = write
    runtime = entry.runtime_data
    platforms = [Platform.WATER_HEATER, Platform.SELECT]
    dashboard = tmp_path / "hot-water.yaml"
    dashboard.write_text('{"views": []}', encoding="utf-8")
    frontend_config = {
        "lovelace": {
            "dashboards": {
                "test-hot-water": {
                    "mode": "yaml",
                    "filename": str(dashboard),
                    "title": "Hot water",
                }
            }
        }
    }

    async def start(hass, config):
        config.runtime_data = runtime
        await hass.config_entries.async_forward_entry_setups(config, platforms)
        return True

    async def stop(hass, config):
        result = await hass.config_entries.async_unload_platforms(config, platforms)
        await coordinator.async_shutdown()
        return result

    for component in (
        "homeassistant",
        "frontend",
        "config",
        "brands",
        "labs",
        "logger",
        "persistent_notification",
    ):
        assert await async_setup_component(hass, component, frontend_config)
    hass.data["onboarding"].onboarded = True
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
        heater_id = er.async_get(hass).async_get_entity_id(
            "water_heater", DOMAIN, expected.unique_id
        )
        assert heater_id
        dashboard.write_text(
            json.dumps(
                {
                    "views": [
                        {
                            "title": "Hot water",
                            "cards": [
                                {
                                    "type": "tile",
                                    "entity": heater_id,
                                    "state_content": [
                                        "state",
                                        "current_temperature",
                                        "temperature",
                                    ],
                                }
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        client = await hass_client()
        environment = dict(
            os.environ,
            WATER_HEATER_URL=str(client.make_url("/")),
            WATER_HEATER_TOKEN=hass_access_token,
            WATER_HEATER_ENTITY=heater_id,
        )
        process = await asyncio.create_subprocess_exec(
            "node",
            str(Path(__file__).with_name("ha-rrc-water-heater.test.cjs")),
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output = []
        try:
            async with asyncio.timeout(90):
                async for line in process.stdout:
                    text = line.decode().rstrip()
                    output.append(text)
                    if text == "POLL_SIMULATED_GATEWAY":
                        # Simulate the next due interval without delaying the browser.
                        coordinator._next_update[PollGroup.FAST] = 0
                        await coordinator.async_refresh()
                        process.stdin.write(b"POLL_COMPLETE\n")
                        await process.stdin.drain()
                await process.wait()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            assert await hass.config_entries.async_unload(entry.entry_id)
        assert process.returncode == 0, "\n".join(output)
        assert [
            call.args for call in coordinator.client.put_resource_value.await_args_list
        ] == [("gateway", MODE, "Off"), ("gateway", MODE, "ownprogram")]
