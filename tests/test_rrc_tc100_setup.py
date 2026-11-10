"""#113: a TC100.2-shaped RRC gateway through the real config-entry setup.

The synthetic cloud answers in the observed wire format: ``used`` as the
strings "true"/"false", ``writeable`` as 0/1, ``userMode`` without
``allowedValues`` and Base64 zone names. Values and names are constructed.
"""

from __future__ import annotations

import base64
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    PointTClient,
    Resource,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from tests.test_discovery_recovery import Clock, Cloud, container
from tests.test_entry import _entry

GATEWAY = Gateway("gateway-one", model="MX400")
COORDINATOR = "custom_components.bosch_buderus_heating.coordinator"
PACKAGE = "custom_components.bosch_buderus_heating"
FREE = "/zones/zn1/userMode"
LOCKED = "/zones/zn4/userMode"

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture(autouse=True)
def legacy_setup_path():
    """Reach the format 3 setup without migrating, as test_startup_inventory."""
    with patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)):
        yield


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def wire(path: str, **fields: object) -> Resource:
    return parse_resource({"id": path, "writeable": 0, "recordable": 0, **fields})


def zone(number: int, name: str, mode: str, **mode_flags: object) -> dict:
    root = f"/zones/zn{number}"
    fields = {
        f"{root}/name": wire(f"{root}/name", type="stringValue", value=name),
        f"{root}/temperatureActual": wire(
            f"{root}/temperatureActual",
            type="floatValue",
            used="true",
            unitOfMeasure="C",
            minValue=5.0,
            maxValue=30.0,
            value=20.5,
        ),
        f"{root}/userMode": wire(
            f"{root}/userMode",
            type="stringValue",
            used="true",
            writeable=1,
            value=mode,
            **mode_flags,
        ),
    }
    return {root: container(root, *fields), **fields}


def plant() -> dict[str, Resource]:
    zones = {
        **zone(1, b64("Wohnzimmer"), "clock"),
        # Not Base64: the raw text. Locked while the system cools.
        **zone(4, "Flur", "manual", available="false"),
    }
    return {
        "/zones": container("/zones", "/zones/list", "/zones/zn1", "/zones/zn4"),
        "/zones/list": wire(
            "/zones/list",
            type="zoneArray",
            value=[{"id": 1, "name": b64("Wohnzimmer")}, {"id": 4, "name": "Flur"}],
        ),
        **zones,
    }


async def test_tc100_modes_names_and_lock_reach_home_assistant(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    cloud = Cloud(plant())
    sent: list[tuple[str, object]] = []

    async def put(gateway_id, path, value, **_kwargs):
        sent.append((path, value))
        cloud.tree[path] = replace(cloud.tree[path], value=value)

    with (
        patch(f"{COORDINATOR}.monotonic", Clock()),
        patch(f"{COORDINATOR}.fault_resource_candidates", return_value=()),
        patch.object(PointTClient, "get_gateways", AsyncMock(return_value=(GATEWAY,))),
        patch.object(
            PointTClient,
            "get_resources_bulk",
            AsyncMock(side_effect=cloud.get_resources_bulk),
        ),
        patch.object(
            PointTClient, "get_resource", AsyncMock(side_effect=cloud.get_resource)
        ),
        patch.object(PointTClient, "put_resource_value", AsyncMock(side_effect=put)),
        patch(f"{PACKAGE}.async_setup_notifications", AsyncMock(return_value=())),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        entry.runtime_data.coordinators[0]._write_service._sleep = AsyncMock()
        registry = er.async_get(hass)
        selects = {
            item.unique_id: item.entity_id
            for item in er.async_entries_for_config_entry(registry, entry.entry_id)
            if item.domain == "select"
        }
        free = selects["gateway-one:zones:zn1:userMode:control"]
        locked = selects["gateway-one:zones:zn4:userMode:control"]

        state = hass.states.get(free)
        assert state.state == "clock"
        assert state.attributes["options"] == ["manual", "clock"]
        assert "Wohnzimmer" in state.attributes["friendly_name"]
        assert b64("Wohnzimmer") not in state.attributes["friendly_name"]
        state = hass.states.get(locked)
        assert state.state == "manual"  # locked, but its mode stays shown
        assert "Flur" in state.attributes["friendly_name"]

        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": free, "option": "manual"},
            blocking=True,
        )
        assert sent == [(FREE, "manual")]
        assert hass.states.get(free).state == "manual"

        with pytest.raises(ServiceValidationError) as caught:
            await hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": locked, "option": "clock"},
                blocking=True,
            )
        assert caught.value.translation_key == "rrc_mode_locked"
        assert "locks the heating mode" in str(caught.value)
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": free, "option": "eco"},
                blocking=True,
            )
        assert sent == [(FREE, "manual")]
        assert hass.states.get(locked).state == "manual"

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
