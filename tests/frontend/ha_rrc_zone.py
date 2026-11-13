"""W1: the zone climate of an RRC room controller in the real frontend.

A real setup against the synthetic cloud (``zone_climate.json``): the
more-info dialog of a zone shows its modes and target, the mode and the
target change through the dialog, and the clear button ends a temporary
setpoint. The browser part is ``ha-rrc-zone.test.cjs``.
"""

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.bosch_buderus_heating.const import DOMAIN
from tests.helpers.rrc_cloud import Clock, cloud_patches
from tests.test_entry import _entry
from tests.test_rrc_zone_climate import GATEWAY, OVERRIDE, path, zone_cloud

PACKAGE = "custom_components.bosch_buderus_heating"

# The browser part runs up to 120 s on its own; with the Home Assistant start
# and the frontend this test needs more than the suite's 120 s limit (D49).
pytestmark = pytest.mark.timeout(300)


async def test_zone_climate_frontend(
    recorder_mock,
    hass,
    hass_client,
    hass_access_token,
    enable_custom_integrations,
    tmp_path,
):
    hass.config.language = "de"
    dashboard = tmp_path / "zones.yaml"
    dashboard.write_text('{"views": []}', encoding="utf-8")
    frontend_config = {
        "lovelace": {
            "dashboards": {
                "test-zones": {
                    "mode": "yaml",
                    "filename": str(dashboard),
                    "title": "Zones",
                }
            }
        }
    }
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
    cloud = zone_cloud()
    # Zone 2 with a closed window: with an open one the target shown is the
    # active setpoint (ZONE-08), not the temporary setpoint set here.
    window = path(2, "openWindowDetection/status")
    cloud.tree[window] = replace(cloud.tree[window], value="closed")
    entry = _entry(hass)
    with (
        patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)),
        cloud_patches(cloud, Clock(), GATEWAY),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data.coordinators[0]

        async def no_wait(_delay: float) -> None:
            return None

        coordinator._write_service._sleep = no_wait
        registry = er.async_get(hass)
        schedule = registry.async_get_entity_id(
            "climate", DOMAIN, "gateway-one:zones:zn2:climate"
        )
        manual = registry.async_get_entity_id(
            "climate", DOMAIN, "gateway-one:zones:zn1:climate"
        )
        clear = registry.async_get_entity_id(
            "button",
            DOMAIN,
            "gateway-one:zones:zn2:clockOverride:temperatureHeating:clear",
        )
        assert schedule and manual and clear
        dashboard.write_text(
            json.dumps(
                {
                    "views": [
                        {
                            "title": "Zones",
                            "cards": [
                                {"type": "tile", "entity": manual},
                                {"type": "tile", "entity": schedule},
                                {"type": "entities", "entities": [clear]},
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
            ZONE_URL=str(client.make_url("/")),
            ZONE_TOKEN=hass_access_token,
            ZONE_MANUAL=manual,
            ZONE_SCHEDULE=schedule,
            ZONE_CLEAR=clear,
        )
        process = await asyncio.create_subprocess_exec(
            "node",
            str(Path(__file__).with_name("ha-rrc-zone.test.cjs")),
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output = []
        try:
            async with asyncio.timeout(120):
                async for line in process.stdout:
                    output.append(line.decode().rstrip())
                await process.wait()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()
        assert process.returncode == 0, "\n".join(output)
        puts = cloud.written("PUT")
        # The mode of zone 1 through the dialog, its manual target with the
        # plus button, the clear button of zone 2 (0.0) and, once zone 2
        # follows its program again, its new temporary setpoint.
        assert puts[0] == ("PUT", path(1, "userMode"), "clock")
        assert ("PUT", path(2, OVERRIDE), 0.0) in puts
        assert puts.index(("PUT", path(2, OVERRIDE), 0.0)) < len(puts) - 1
        assert puts[-1][1] == path(2, OVERRIDE)
        assert puts[-1][2] == 19.5
