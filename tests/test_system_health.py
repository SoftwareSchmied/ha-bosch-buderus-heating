"""System health: the state of the accesses without identifiers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.connection_runtime import ConnectionPhase
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.local_state import LocalCondition
from custom_components.bosch_buderus_heating.managed_entry import ManagedEntry
from custom_components.bosch_buderus_heating.system_health import (
    async_register,
    system_health_info,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.test_connection_status import LAN, Runtime, ready_local

TRANSLATIONS = "custom_components/bosch_buderus_heating/translations/en.json"


def _loaded(hass: HomeAssistant, runtime: Runtime) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            "connections": {
                "cloud": {"enabled": True},
                LAN: {"enabled": True},
            },
            "targets": {"t": {"local_values": True}},
        },
    )
    entry.add_to_hass(hass)
    owner = ManagedEntry.__new__(ManagedEntry)
    owner.connections = runtime
    entry.runtime_data = owner
    return entry


async def test_info_counts_accesses_by_state_and_gives_the_last_update(
    hass: HomeAssistant,
) -> None:
    local = ready_local()
    local.condition = LocalCondition.UNAVAILABLE
    _loaded(hass, Runtime(hass, cloud_phase=ConnectionPhase.READY, local=local))
    info = await system_health_info(hass)
    assert info["cloud_accesses"] == "connected: 1"
    assert info["local_accesses"] == "unreachable: 1"
    # The cloud coordinator of the fixture has a successful read.
    assert datetime.fromisoformat(info["last_successful_update"]).tzinfo is UTC


async def test_a_paused_cloud_is_named_and_no_identifier_is_shown(
    hass: HomeAssistant,
) -> None:
    entry = _loaded(hass, Runtime(hass, paused=600.0, local=ready_local()))
    info = await system_health_info(hass)
    assert info["cloud_accesses"] == "paused: 1"
    assert info["local_accesses"] == "connected: 1"
    text = json.dumps(info)
    assert entry.entry_id not in text
    assert "192.0.2.10" not in text


async def test_without_a_running_entry_nothing_is_reported(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    info = await system_health_info(hass)
    assert info == {
        "cloud_accesses": "none",
        "local_accesses": "none",
        "last_successful_update": "none yet",
    }


def test_the_info_keys_are_translated() -> None:
    with open(TRANSLATIONS, encoding="utf-8") as handle:
        texts = json.load(handle)["system_health"]["info"]
    assert set(texts) == {
        "cloud_accesses",
        "local_accesses",
        "last_successful_update",
    }


def test_the_info_callback_is_registered(hass: HomeAssistant) -> None:
    register = MagicMock()
    async_register(hass, register)
    register.async_register_info.assert_called_once_with(system_health_info)
