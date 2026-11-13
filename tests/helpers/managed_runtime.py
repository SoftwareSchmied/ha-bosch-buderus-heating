"""Wait for asynchronous sources without replacing the normal HA setup path."""

import asyncio
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from custom_components.bosch_buderus_heating.managed_entry import ManagedEntry
from custom_components.bosch_buderus_heating.managed_wallbox import (
    ManagedWallboxCoordinator,
)


@contextmanager
def pointt_client(client):
    """Supply the same synthetic transport to connection and source factories."""
    with ExitStack() as stack:
        for module in ("managed_cloud", "managed_pointt"):
            stack.enter_context(
                patch(
                    f"custom_components.bosch_buderus_heating.{module}.PointTClient",
                    return_value=client,
                )
            )
        yield


async def wait_for_sources(hass, entry, count):
    assert entry.version == 4
    owner = entry.runtime_data
    assert isinstance(owner, ManagedEntry)
    ready = asyncio.Event()

    def changed():
        if (
            sum(
                owner.connections.source_data(key) is not None
                for key in owner.connections.snapshot.bindings
            )
            == count
        ):
            ready.set()

    unsubscribe = owner.connections.subscribe(changed)
    try:
        changed()
        async with asyncio.timeout(5):
            await ready.wait()
    finally:
        unsubscribe()
    await hass.async_block_till_done()


def wallbox_coordinators(entry):
    owner = entry.runtime_data
    return tuple(
        source
        for key in owner.connections.snapshot.bindings
        if isinstance(
            source := owner.connections.source_data(key), ManagedWallboxCoordinator
        )
    )
