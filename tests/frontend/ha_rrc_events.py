"""Exercise the room controller's events in Home Assistant's calendar.

A real setup against the fake cloud (``tests/helpers/rrc_cloud.py``) with
the frontend loaded; ``ha-rrc-events.test.cjs`` opens the frontend in a
browser and creates, changes and deletes absences through its connection
(the calendar's websocket commands and REST reads). Expected, untested on hardware.
"""

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.setup import async_setup_component

from tests.helpers.rrc_absence import EventCloud, entity_id, setup, teardown

CALENDAR = "gateway-one:events:calendar"

# The browser part runs up to 120 s on its own; with the Home Assistant start
# and the frontend this test needs more than the suite's 120 s limit (D49).
pytestmark = pytest.mark.timeout(300)


async def test_events_through_the_frontend(
    recorder_mock,
    hass,
    hass_client,
    hass_access_token,
    enable_custom_integrations,
):
    hass.config.language = "de"
    for component in (
        "homeassistant",
        "frontend",
        "config",
        "brands",
        "labs",
        "logger",
        "persistent_notification",
        "calendar",
    ):
        assert await async_setup_component(hass, component, {})
    hass.data["onboarding"].onboarded = True
    cloud = EventCloud()
    cloud.adopt_after["/events/list1"] = 2
    with patch(
        "custom_components.bosch_buderus_heating.async_migrate_entry",
        AsyncMock(return_value=True),
    ):
        entry, coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    try:
        calendar = entity_id(hass, "calendar", CALENDAR)
        assert calendar
        client = await hass_client()
        environment = dict(
            os.environ,
            EVENTS_URL=str(client.make_url("/")),
            EVENTS_TOKEN=hass_access_token,
            EVENTS_ENTITY=calendar,
        )
        process = await asyncio.create_subprocess_exec(
            "node",
            str(Path(__file__).with_name("ha-rrc-events.test.cjs")),
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
        assert process.returncode == 0, "\n".join(output)
        methods = [(method, path) for method, path, _value in cloud.written()]
        assert methods == [
            ("POST", "/events"),
            ("PUT", "/events/ev10"),
            ("DELETE", "/events/ev10"),
        ]
        stats = coordinator.rrc_write_stats.snapshot("absence")
        assert stats["writes_sent"] == 3 and stats["writes_confirmed"] == 3
    finally:
        await teardown(hass, entry, stack)
