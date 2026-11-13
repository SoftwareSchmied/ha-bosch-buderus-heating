"""#122: the RRC entity modules load in the executor, never in the event loop.

``rrc_platforms.trackers`` imports the six package modules late. Both setup
paths load them before the platforms start, so a platform's ``track`` only
reads the cache.
"""

from __future__ import annotations

import importlib
import threading
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import rrc_platforms
from tests.test_discovery_recovery import Clock, Cloud
from tests.test_entry import _entry
from tests.test_managed_pointt import Backend, eventually, running
from tests.test_rrc_tc100_setup import PACKAGE, cloud_patches, plant

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")


class Imports:
    """The entity module imports of ``trackers`` and the forwards seen."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calls: list[tuple[str, bool]] = []
        self.cached_at_forward: list[bool] = []

    def import_module(self, name: str, package: str | None = None) -> object:
        on_loop = threading.get_ident() == self.hass.loop_thread_id
        self.calls.append((name.rsplit(".", 1)[-1], on_loop))
        return importlib.import_module(name, package)

    def record_forward(self, forward):
        async def forwarded(*args, **kwargs):
            self.cached_at_forward.append(
                rrc_platforms.trackers.cache_info().currsize == 1
            )
            return await forward(*args, **kwargs)

        return forwarded

    def assert_loaded_off_the_loop(self) -> None:
        assert [name for name, _ in self.calls] == list(rrc_platforms.ENTITY_MODULES)
        assert not [name for name, on_loop in self.calls if on_loop]
        assert self.cached_at_forward == [True]


@pytest.fixture
def imports(hass: HomeAssistant) -> Iterator[Imports]:
    probe = Imports(hass)
    rrc_platforms.trackers.cache_clear()
    with (
        patch.object(
            rrc_platforms,
            "importlib",
            SimpleNamespace(import_module=probe.import_module),
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            probe.record_forward(hass.config_entries.async_forward_entry_setups),
        ),
    ):
        yield probe
    rrc_platforms.trackers.cache_clear()


async def test_entry_setup_loads_the_entity_modules_before_the_platforms(
    hass: HomeAssistant, imports: Imports
) -> None:
    entry = _entry(hass)
    with (
        patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)),
        cloud_patches(Cloud(plant()), Clock(), []),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        # The trackers ran: the RRC zones have their climate entities.
        climates = [
            item.unique_id
            for item in er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
            if item.domain == "climate"
        ]
        assert any("zones:zn1" in unique_id for unique_id in climates)
        assert await hass.config_entries.async_unload(entry.entry_id)
    imports.assert_loaded_off_the_loop()


async def test_managed_sources_load_the_entity_modules_before_the_platforms(
    hass: HomeAssistant, imports: Imports
) -> None:
    hass.config.components.add("bosch_buderus_heating")
    registry = er.async_get(hass)
    async with running(hass, Backend()) as (entry, _cloud, owner, _):
        await owner.start_platforms()
        await eventually(
            lambda: bool(er.async_entries_for_config_entry(registry, entry.entry_id))
        )
    imports.assert_loaded_off_the_loop()
