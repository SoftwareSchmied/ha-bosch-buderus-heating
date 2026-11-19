"""Wallbox controls in a running Home Assistant: registry, names, services,
the options step. Synthetic data only; no real wallbox was involved."""

import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.data import tokens_to_data
from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.pointt import AuthTokens, Gateway
from custom_components.bosch_buderus_heating.pointt.exceptions import ResourceNotFound
from custom_components.bosch_buderus_heating.wallbox_control import (
    OPTION_AUTHENTICATE,
    OPTION_AUTHENTICATION_NAME,
)
from custom_components.bosch_buderus_heating.wallbox_entities import SPECS
from tests.ha_test_helpers import MockConfigEntry
from tests.helpers.managed_runtime import wait_for_sources, wallbox_coordinators
from tests.helpers.options_menu import menu_note, outcome

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

ROOT = "custom_components.bosch_buderus_heating"
FIXTURE = Path(__file__).parent / "fixtures/contracts/pointt-wallbox/cp0-control.json"
GW = "wallbox-A"


def entry(hass):
    config = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={
            "brand": "buderus",
            "gateway_ids": [GW],
            **tokens_to_data(AuthTokens("synthetic-access", "synthetic-refresh", 4e9)),
        },
        options={"polling_profile": "standard"},
    )
    config.add_to_hass(hass)
    return config


@pytest.fixture
def wallbox_cloud():
    data = json.loads(FIXTURE.read_text())
    calls = []

    async def section(gateway, name):
        calls.append(("GET", name))
        return copy.deepcopy(data[name])

    async def leaf(gateway, path):
        calls.append(("GET", path))
        if path not in data["leaves"]:
            raise ResourceNotFound(path, 404)
        return copy.deepcopy(data["leaves"][path])

    async def post(gateway, command, body):
        calls.append(("POST", command, body))
        if command == "limit":
            data["telemetry"]["values"]["limit"] = str(body["limit"])

    async def put(gateway, path, value):
        calls.append(("PUT", path, value))
        data["leaves"][path]["value"] = value

    with (
        patch(
            ROOT + ".PointTClient.get_gateways",
            AsyncMock(return_value=(Gateway(GW, device_type="commodule"),)),
        ),
        patch(ROOT + ".PointTClient.get_wallbox_section", side_effect=section),
        patch(ROOT + ".PointTClient.get_wallbox_leaf", side_effect=leaf),
        patch(ROOT + ".PointTClient.post_wallbox_command", side_effect=post),
        patch(ROOT + ".PointTClient.put_wallbox_setting", side_effect=put),
        patch(ROOT + "._async_load_startup_data", AsyncMock()),
        patch(ROOT + ".async_setup_notifications", AsyncMock(return_value=())),
        patch(ROOT + ".wallbox_control.READ_BACK_DELAYS", (0.0,) * 10),
    ):
        yield data, calls


async def _discover(hass, config):
    coordinator = wallbox_coordinators(config)[0]
    for _ in range(8):
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    return coordinator


async def test_controls_appear_disabled_with_names_and_work_once_enabled(
    hass, wallbox_cloud
):
    _data, calls = wallbox_cloud
    hass.config.language = "de"
    config = entry(hass)
    assert await hass.config_entries.async_setup(config.entry_id)
    await wait_for_sources(hass, config, 1)
    await hass.async_block_till_done()
    await _discover(hass, config)
    registry = er.async_get(hass)
    entries = {
        item.unique_id.rsplit(":", 1)[-1]: item
        for item in registry.entities.values()
        if item.unique_id.startswith(GW + ":wallbox:cp0:")
    }
    # Every entity of the catalog exists with this full synthetic wallbox.
    assert {spec.key for spec in SPECS} <= set(entries)
    for spec in SPECS:
        item = entries[spec.key]
        assert item.domain == spec.platform.value
        assert (item.disabled_by is None) is spec.enabled, spec.key
        assert item.translation_key == "wallbox_" + spec.key
    assert hass.states.get(entries["external_meter"].entity_id).state == "connected"
    assert hass.states.get(entries["limiter"].entity_id).state == "none"
    assert not any(call[0] in ("POST", "PUT") for call in calls)

    # Enabling the charging current control and using it sends one command.
    target = entries["charging_current_limit"].entity_id
    registry.async_update_entity(target, disabled_by=None)
    assert await hass.config_entries.async_reload(config.entry_id)
    await wait_for_sources(hass, config, 1)
    await hass.async_block_till_done()
    await _discover(hass, config)
    state = hass.states.get(target)
    assert state.state == "16.0"
    assert state.attributes["friendly_name"] == "Wallbox Ladestrom-Grenze"
    assert state.attributes["max"] == 16
    calls.clear()
    await hass.services.async_call(
        "number", "set_value", {"entity_id": target, "value": 10}, blocking=True
    )
    assert [call for call in calls if call[0] == "POST"] == [
        ("POST", "limit", {"limit": 10})
    ]
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "number", "set_value", {"entity_id": target, "value": 10.5}, blocking=True
        )
    assert sum(call[0] == "POST" for call in calls) == 1

    diagnostics = json.dumps(await async_get_config_entry_diagnostics(hass, config))
    for private in ("private-label", "Synthetic brand", "private-meter-serial", GW):
        assert private not in diagnostics
    assert await hass.config_entries.async_unload(config.entry_id)
    await hass.async_block_till_done()


async def test_wallbox_options_step(hass, wallbox_cloud):
    config = entry(hass)
    assert await hass.config_entries.async_setup(config.entry_id)
    await wait_for_sources(hass, config, 1)
    await hass.async_block_till_done()
    with patch.object(hass.config_entries, "async_schedule_reload"):
        menu = await hass.config_entries.options.async_init(config.entry_id)
        assert "wallbox" in menu["menu_options"]
        form = await hass.config_entries.options.async_configure(
            menu["flow_id"], {"next_step_id": "wallbox"}
        )
        assert form["type"] is FlowResultType.FORM
        assert form["step_id"] == "wallbox"
        invalid = await hass.config_entries.options.async_configure(
            form["flow_id"],
            {OPTION_AUTHENTICATE: True, OPTION_AUTHENTICATION_NAME: "x" * 65},
        )
        assert invalid["errors"] == {OPTION_AUTHENTICATION_NAME: "wallbox_name_invalid"}
        assert OPTION_AUTHENTICATE not in config.options
        result = await hass.config_entries.options.async_configure(
            invalid["flow_id"],
            {OPTION_AUTHENTICATE: True, OPTION_AUTHENTICATION_NAME: " Garage "},
        )
        assert await outcome(hass, result) == "wallbox_saved"
        assert "saved" in await menu_note(hass, result, "wallbox_saved")
    assert config.options[OPTION_AUTHENTICATE] is True
    assert config.options[OPTION_AUTHENTICATION_NAME] == "Garage"
    # The entry loads again with the new options, and keeps them.
    assert await hass.config_entries.async_reload(config.entry_id)
    await wait_for_sources(hass, config, 1)
    assert config.options[OPTION_AUTHENTICATE] is True
    diagnostics = json.dumps(await async_get_config_entry_diagnostics(hass, config))
    assert "Garage" not in diagnostics
    assert await hass.config_entries.async_unload(config.entry_id)
    await hass.async_block_till_done()
