"""Run explicitly with pytest after installing HA's frontend and Playwright.

Use Python 3.14.7 or later for unittest.mock's deferred-annotation fix, which
Home Assistant's recorder fixtures require. CI pins the validated patch version.
The browser uses a loopback HA test server and a simulated PointT gateway.
No credentials, services or devices from a real installation are used.
"""

import asyncio
import logging
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

from homeassistant.setup import async_setup_component

from custom_components.bosch_buderus_heating.coordinator import Freshness
from custom_components.bosch_buderus_heating.holiday_ui import async_setup_controls_ui
from custom_components.bosch_buderus_heating.pointt import RequestTimeout
from custom_components.bosch_buderus_heating.pointt.models import BatchItemResult
from custom_components.bosch_buderus_heating.resource_catalog import PollGroup
from custom_components.bosch_buderus_heating.writes import WriteService
from tests.test_heating_controls import MODE, ROOT, TARGET, scalar, setup

HOT_WATER = "/dhwCircuits/dhw1/temperatureLevels/high"


async def test_real_frontend(
    recorder_mock,
    hass,
    hass_client,
    hass_access_token,
    enable_custom_integrations,
    caplog,
):
    hass.config.language = "de"
    entry, coordinator, _ = setup(hass)
    advanced = scalar(ROOT + "/maxFlowTemp", 38, minimum=25, maximum=60)
    coordinator.resources[advanced.path] = advanced
    coordinator.data[advanced.path] = replace(
        coordinator.data[TARGET], resource=advanced
    )
    # A second tab: HA recreates the form after each step.
    hot_water = scalar(HOT_WATER, 52, minimum=30, maximum=60)
    coordinator.resources[HOT_WATER] = hot_water
    coordinator.data[HOT_WATER] = replace(coordinator.data[TARGET], resource=hot_water)
    plant = dict(coordinator.resources)
    failed_readback = False

    async def read(_gateway, path):
        nonlocal failed_readback
        if path == TARGET and failed_readback:
            failed_readback = False
            raise RequestTimeout()
        return plant[path]

    async def write(_gateway, path, value):
        nonlocal failed_readback
        plant[path] = replace(plant[path], value=value)
        failed_readback = value == 23

    async def poll(gateway, paths):
        return tuple(BatchItemResult(gateway, path, 200, plant[path]) for path in paths)

    coordinator.client.get_resource.side_effect = read
    coordinator.client.put_resource_value.side_effect = write
    coordinator.client.get_resources_bulk.side_effect = poll
    coordinator._paths_by_group[PollGroup.CONTROL] = (MODE, TARGET)
    coordinator._write_service = WriteService(
        coordinator.client, sleep=AsyncMock(), read_back_attempts=1
    )
    for component in (
        "homeassistant",
        "frontend",
        "config",
        "brands",
        "labs",
        "logger",
        "persistent_notification",
    ):
        assert await async_setup_component(hass, component, {})
    hass.data["onboarding"].onboarded = True
    caplog.set_level(logging.INFO, logger="homeassistant.components.websocket_api")
    await async_setup_controls_ui(hass)
    await hass.async_block_till_done()
    client = await hass_client()
    environment = dict(
        os.environ,
        CONTROLS_URL=str(client.make_url("/")),
        CONTROLS_TOKEN=hass_access_token,
    )
    process = await asyncio.create_subprocess_exec(
        "node",
        str(Path(__file__).with_name("ha-dialog.test.cjs")),
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
                    assert coordinator.data[TARGET].freshness is Freshness.STALE
                    await coordinator.async_refresh()
                    assert coordinator.data[TARGET].freshness is Freshness.FRESH
                    process.stdin.write(b"POLL_COMPLETE\n")
                    await process.stdin.drain()
            await process.wait()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        await coordinator.async_shutdown()
    assert process.returncode == 0, "\n".join(output)
    assert coordinator.data[TARGET].resource.value == 24
    assert coordinator.data[HOT_WATER].resource.value == 55
    assert entry.options == {}
    assert hass.config_entries.options.async_progress() == []
    assert [
        call.args for call in coordinator.client.put_resource_value.await_args_list
    ] == [("gateway", TARGET, value) for value in (20.5, 22, 23, 24)] + [
        ("gateway", HOT_WATER, 55)
    ]
    # C35: each write reads its target in one bulk preflight and confirms it
    # with a single GET; the simulated poll is the only other bulk read.
    reads = [call.args for call in coordinator.client.get_resource.await_args_list]
    assert reads == [("gateway", TARGET)] * 4 + [("gateway", HOT_WATER)]
    bulk = [call.args for call in coordinator.client.get_resources_bulk.await_args_list]
    assert bulk == [("gateway", (TARGET,))] * 3 + [
        ("gateway", (MODE, TARGET)),
        ("gateway", (TARGET,)),
        ("gateway", (HOT_WATER,)),
    ]
