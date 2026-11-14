"""A shorter local setup: buttons, refusals, the data port page and names."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.local_transport import PortRole
from custom_components.bosch_buderus_heating.plant_labels import plant_labels
from tests.test_local_enrollment import journal as journal
from tests.test_local_flow import imported, info, start, submit, supported
from tests.test_local_setup import registered

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")
PACKAGE = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"
FILES = [PACKAGE / "strings.json", *sorted((PACKAGE / "translations").glob("*.json"))]
__all__ = ["journal", "supported"]


def _options(result) -> list:
    return next(iter(result["data_schema"].schema.values())).config["options"]


async def test_the_save_page_has_no_box_and_ok_saves(hass, supported, journal):
    entry, management, _, target = registered(hass)
    manager = hass.config_entries.options
    result = await imported(hass, entry, target)
    # Intermediate pages say "Next".
    assert result["step_id"] == "local_certificate_data"
    assert result["last_step"] is False
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["step_id"] == "local_result"
    assert result["last_step"] is True
    assert not result["data_schema"].schema
    before = management.current()
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "local_setup_complete"
    assert management.current() != before


async def test_an_unconfirmed_certificate_is_explained_and_sends_nothing(
    hass, supported, journal
):
    inspections, reads = supported
    entry, _, _, target = registered(hass)
    manager = hass.config_entries.options
    result = await imported(hass, entry, target)
    assert result["step_id"] == "local_certificate_data"
    shown = len(inspections)
    result = await manager.async_configure(result["flow_id"], {"confirm": False})
    assert result["step_id"] == "local_certificate_data"
    assert result["errors"] == {"confirm": "local_certificate_unconfirmed"}
    assert len(inspections) == shown and not reads
    # Confirmed afterwards, the setup goes on.
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["step_id"] == "local_result"


async def test_the_sign_in_port_keeps_its_page(hass, supported, journal):
    inspections, _ = supported
    entry, _, _, target = registered(hass)
    manager = hass.config_entries.options
    result = await start(hass, entry, target)
    result = await submit(hass, manager, result, {"address": "gateway.example.invalid"})
    result = await submit(hass, manager, result, {"method": "label"})
    assert inspections == [PortRole.AUTHENTICATION]
    assert result["step_id"] == "local_certificate"
    assert result["last_step"] is False


@pytest.mark.parametrize("path", FILES, ids=lambda path: path.stem)
def test_both_certificate_pages_have_their_own_title(path: Path) -> None:
    data = json.loads(path.read_text("utf-8"))
    for section in ("config", "options"):
        steps = data[section]["step"]
        first, second = steps["local_certificate"], steps["local_certificate_data"]
        assert first["title"] != second["title"]
        assert first["description"] == second["description"]
        assert first["data"] == second["data"]
        assert "data" not in steps["local_result"]
        assert data[section]["error"]["local_certificate_unconfirmed"].strip()
    assert data["selector"]["local_target"]["options"]["new_plant"].strip()
    assert data["selector"]["local_owner"]["options"]["new_entry"].strip()


def _device(hass, entry, identifier, name, *, by_user=None):
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, identifier)}, name=name
    )
    if by_user is not None:
        registry.async_update_device(device.id, name_by_user=by_user)


async def test_systems_are_listed_by_name(hass, supported, journal):
    entry, management, _, target = registered(hass)
    identity = management.current().targets[target].to_data()["identities"][0]
    _device(hass, entry, identity["value"], "Heat pump", by_user="Cellar")
    manager = hass.config_entries.options
    result = await manager.async_init(entry.entry_id)
    result = await submit(hass, manager, result, {"next_step_id": "local_setup"})
    assert result["step_id"] == "local_target"
    assert result["last_step"] is False
    selector = next(iter(result["data_schema"].schema.values())).config
    assert selector["translation_key"] == "local_target"
    labels = {item["value"]: item["label"] for item in selector["options"]}
    assert labels[target] == "Cellar"
    assert identity["value"] not in labels.values()
    # The fixed choice is translated by the frontend, never built.
    assert labels["new_plant"] == "new_plant"


def test_equal_names_keep_their_identifiers(hass) -> None:
    from tests.ha_test_helpers import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, entry_id="e", version=4)
    entry.add_to_hass(hass)
    _device(hass, entry, "gw-1", "Heating")
    _device(hass, entry, "gw-2", "Heating")
    _device(hass, entry, "gw-3", "Other", by_user="Garage")
    labels = plant_labels(
        hass, "e", {"a": "gw-1", "b": "gw-2", "c": "gw-3", "d": "gw-4"}
    )
    assert labels == {
        "a": "Heating (gw-1)",
        "b": "Heating (gw-2)",
        "c": "Garage",
        # Without a device (not set up yet): its identifier.
        "d": "gw-4",
    }
    # Another entry's devices are never used.
    assert plant_labels(hass, "other", {"a": "gw-1"}) == {"a": "gw-1"}


async def test_a_found_gateway_can_start_its_own_entry(hass, supported, journal):
    entry, _, _, _ = registered(hass)
    manager = hass.config_entries.flow
    result = await manager.async_init(
        DOMAIN,
        context={"source": "zeroconf"},
        data=info(name="second", host="192.0.2.8"),
    )
    assert result["step_id"] == "local_owner"
    assert result["last_step"] is False
    selector = next(iter(result["data_schema"].schema.values())).config
    assert selector["translation_key"] == "local_owner"
    assert [item["value"] for item in selector["options"]] == [
        entry.entry_id,
        "new_entry",
    ]
    with patch(
        "custom_components.bosch_buderus_heating.async_setup_entry",
        AsyncMock(return_value=True),
    ):
        result = await manager.async_configure(
            result["flow_id"], {"owner": "new_entry"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    created = result["result"]
    assert created.entry_id != entry.entry_id
    assert created.data["connections"] == {}
    assert created.title == "Home Comfort (local)"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2
    # Its local setup offers the found address, preselected.
    step = await hass.config_entries.options.async_init(created.entry_id)
    assert step["step_id"] == "local_address"
    (key,) = step["data_schema"].schema
    assert key.default() == "192.0.2.8"


async def test_an_entry_dialog_never_offers_a_new_entry(hass, supported, journal):
    entry, _, _, _ = registered(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    handler = hass.config_entries.options._progress[flow["flow_id"]]
    handler._local_phase = "local_owner"
    result = await handler.async_step_local_owner()
    assert [item["value"] for item in _options(result)] == [entry.entry_id]
    handler._local_phase = "local_owner"
    result = await handler.async_step_local_owner({"owner": "new_entry"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "local_session_changed"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
