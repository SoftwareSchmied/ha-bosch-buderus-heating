"""A6: "Search the system again" in the options dialog, in Chromium."""

import asyncio
import os
from pathlib import Path
from time import monotonic

import pytest
from homeassistant.setup import async_setup_component

from tests.helpers.local_runtime import running_local_owner as running_local_owner
from tests.test_local_flow import journal as journal
from tests.test_local_flow import supported as supported
from tests.test_managed_pointt import eventually, source_for


async def _browser(phase: str, environment: dict[str, str]) -> str:
    process = await asyncio.create_subprocess_exec(
        "node",
        str(Path(__file__).with_name("ha-rescan.test.cjs")),
        env={**environment, "RESCAN_PHASE": phase},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        async with asyncio.timeout(100):
            output, _ = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, output.decode()
    return output.decode()


@pytest.mark.parametrize("language", ["en", "de"])
async def test_rescan_browser(
    recorder_mock,
    hass,
    hass_client,
    hass_access_token,
    enable_custom_integrations,
    supported,
    journal,
    running_local_owner,
    language,
):
    hass.config.language = language
    entry = running_local_owner[0]
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
    await hass.async_block_till_done()
    coordinator = source_for(entry.runtime_data, "000123")
    await eventually(lambda: coordinator.rescan_state() == "ready")
    client = await hass_client()
    environment = dict(
        os.environ,
        LOCAL_SETUP_URL=str(client.make_url("/")),
        LOCAL_SETUP_TOKEN=hass_access_token,
        LOCAL_SETUP_LANGUAGE=language,
    )
    # Steps 1-3: the entry, the started search, then "already planned".
    print(await _browser("start", environment))
    assert coordinator.rescan_result == "planned"
    # Step 4: while the cloud pauses, nothing is planned.
    coordinator._rescan_due = None
    coordinator.rescan_result = None
    coordinator._cloud_backoff_until = monotonic() + 600
    print(await _browser("paused", environment))
    assert coordinator._rescan_due is None
    coordinator._cloud_backoff_until = 0.0
