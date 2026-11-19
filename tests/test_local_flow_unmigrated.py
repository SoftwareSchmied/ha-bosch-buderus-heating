"""A discovered gateway next to an entry whose update failed.

An entry that stays at an older version (its migration failed) is no owner
for local access, but the system is not a new installation either: the
discovery offers no new local-only entry for it, which could set up the same
plant twice. An ignored notice still counts as no entry.
"""

from __future__ import annotations

import pytest
from homeassistant.config_entries import SOURCE_IGNORE
from homeassistant.data_entry_flow import FlowResultType

from custom_components.bosch_buderus_heating.const import DOMAIN
from tests.ha_test_helpers import MockConfigEntry
from tests.test_local_flow import info

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")


def _old_entry(hass, *, version: int = 3, source: str = "user") -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=version,
        source=source,
        data={"brand": "buderus", "gateway_ids": ["gateway"]},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.parametrize("version", [3, 5])
async def test_an_entry_of_another_version_stops_the_notice(hass, version):
    _old_entry(hass, version=version)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=info()
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "local_entry_not_updated"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_an_old_entry_added_meanwhile_stops_the_new_entry(hass):
    manager = hass.config_entries.flow
    result = await manager.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=info()
    )
    assert result["step_id"] == "local_discovered"
    _old_entry(hass)
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "local_entry_not_updated"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_an_ignored_notice_is_no_entry(hass):
    _old_entry(hass, source=SOURCE_IGNORE)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=info()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "local_discovered"


async def test_the_abort_names_the_entry_that_was_not_updated(hass):
    """The reason has its own text in every language, about the entry."""
    import json
    from pathlib import Path

    folder = Path(__file__).parents[1] / "custom_components/bosch_buderus_heating"
    files = [folder / "strings.json", *sorted((folder / "translations").glob("*.json"))]
    assert len(files) == 20
    for file in files:
        aborts = json.loads(file.read_text(encoding="utf-8"))["config"]["abort"]
        assert aborts["local_entry_not_updated"].strip(), file.name
        assert aborts["local_entry_not_updated"] != aborts["local_setup_unavailable"]
