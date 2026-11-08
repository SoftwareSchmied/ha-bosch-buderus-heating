"""The setup menu and "local access only" in Chromium (P49b step 7)."""

import asyncio
import json
import os
from pathlib import Path

import pytest
from homeassistant.setup import async_setup_component

from custom_components.bosch_buderus_heating.config_flow import (
    BoschBuderusConfigFlow,
)
from custom_components.bosch_buderus_heating.const import DOMAIN


@pytest.mark.parametrize(
    ("language", "width", "choice"),
    [("de", 1100, "local_only"), ("en", 390, "local_only"), ("en", 1100, "cloud")],
)
async def test_local_first_browser(
    recorder_mock,
    hass,
    hass_client,
    hass_access_token,
    enable_custom_integrations,
    monkeypatch,
    language,
    width,
    choice,
):
    monkeypatch.setattr(BoschBuderusConfigFlow, "MINOR_VERSION", 2)
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
    client = await hass_client()
    environment = dict(
        os.environ,
        LOCAL_SETUP_URL=str(client.make_url("/")),
        LOCAL_SETUP_TOKEN=hass_access_token,
        LOCAL_SETUP_LANGUAGE=language,
        LOCAL_SETUP_WIDTH=str(width),
        LOCAL_FIRST_CHOICE=choice,
        LOCAL_FIRST_NAME=json.loads(
            (
                Path(__file__).parents[2]
                / "custom_components"
                / DOMAIN
                / "manifest.json"
            ).read_text(encoding="utf-8")
        )["name"],
    )
    process = await asyncio.create_subprocess_exec(
        "node",
        str(Path(__file__).with_name("ha-local-first.test.cjs")),
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
    entries = hass.config_entries.async_entries(DOMAIN)
    if choice == "local_only":
        (entry,) = entries
        assert entry.data["connections"] == {}
        # The local setup of the new entry is open in its options.
        assert any(
            flow["handler"] == entry.entry_id
            for flow in hass.config_entries.options.async_progress()
        )
        await hass.config_entries.async_unload(entry.entry_id)
    else:
        assert not entries
