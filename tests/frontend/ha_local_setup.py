"""Native local setup in Chromium with synthetic runtime and gateway evidence."""

import asyncio
import os
from pathlib import Path

import pytest
from homeassistant.setup import async_setup_component

from tests.helpers.local_runtime import running_local_owner as running_local_owner
from tests.test_local_flow import info
from tests.test_local_flow import journal as journal
from tests.test_local_flow import supported as supported
from tests.test_managed_pointt import eventually


@pytest.mark.parametrize("language,width", [("de", 1100), ("en", 390)])
async def test_local_setup_browser(
    recorder_mock,
    hass,
    hass_client,
    hass_access_token,
    enable_custom_integrations,
    supported,
    journal,
    running_local_owner,
    language,
    width,
):
    hass.config.language = "en" if language == "de" else "de"
    entry, management, _, backend, clients, probes = running_local_owner
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
    discovery = await hass.config_entries.flow.async_init(
        entry.domain, context={"source": "zeroconf"}, data=info()
    )
    assert discovery["step_id"] == "local_owner"
    client = await hass_client()
    environment = dict(
        os.environ,
        LOCAL_SETUP_URL=str(client.make_url("/")),
        LOCAL_SETUP_TOKEN=hass_access_token,
        LOCAL_SETUP_LANGUAGE=language,
        LOCAL_SETUP_WIDTH=str(width),
    )
    process = await asyncio.create_subprocess_exec(
        "node",
        str(Path(__file__).with_name("ha-local-setup.test.cjs")),
        env=environment,
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
    assert management.current().minor_version == 2
    assert len(hass.config_entries.async_entries(entry.domain)) == 1
    await eventually(
        lambda: any(
            entry.runtime_data.connections.local_state(key) is not None
            and entry.runtime_data.connections.local_state(key).current_observations
            for key, binding in management.current().bindings.items()
            if binding.adapter == "k40_local"
        )
    )
    assert backend.metadata_calls == 2
    assert len(probes) == 3 and clients and clients[-1].calls
    assert (await journal.records())[0].completion is not None
    assert not hass.config_entries.flow.async_progress_by_handler(entry.domain)
