"""A slow cloud never holds the setup of the integration.

Home Assistant waits five minutes for the setup of a start. A configured entry
is always loaded without waiting for the cloud: the first discovery of a
gateway runs in its source, and its entities follow once it has answered. These
tests hold the answers of the cloud and check that the setup (the owner and
its platforms) is finished meanwhile, and that the entities follow later.
"""

import asyncio
import time

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.const import DOMAIN
from tests.test_managed_pointt import (
    Backend,
    eventually,
    running,
    source_for,
)

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
GATEWAY = "000123"


@pytest.fixture(autouse=True)
def loaded_integration(hass):
    # The transport fixture starts after the integration's normal setup phase.
    hass.config.components.add(DOMAIN)


async def test_the_setup_is_finished_while_the_cloud_does_not_answer(hass):
    backend = Backend()
    backend.gates[GATEWAY] = asyncio.Event()  # no answer for this gateway
    registry = er.async_get(hass)
    async with running(hass, backend) as (_, _, owner, _):
        started = time.monotonic()
        async with asyncio.timeout(5):
            await owner.start_platforms()
        assert time.monotonic() - started < 5
        # The discovery is under way, but nothing has been published yet.
        await eventually(lambda: GATEWAY in backend.entered, timeout=10)
        assert source_for(owner, GATEWAY) is None
        assert not [
            item
            for item in er.async_entries_for_config_entry(
                registry, owner.entry.entry_id
            )
            if item.unique_id.startswith(f"{GATEWAY}:")
        ]
        # The cloud answers at last: the entities follow.
        backend.gates[GATEWAY].set()
        await eventually(lambda: source_for(owner, GATEWAY), timeout=15)
        await eventually(
            lambda: [
                item
                for item in er.async_entries_for_config_entry(
                    registry, owner.entry.entry_id
                )
                if item.unique_id.startswith(f"{GATEWAY}:") and item.platform == DOMAIN
            ],
            timeout=15,
        )
