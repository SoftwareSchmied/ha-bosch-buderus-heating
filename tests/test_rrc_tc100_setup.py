"""#113: a TC100.2-shaped RRC gateway through the real config-entry setup.

The synthetic cloud answers in the observed wire format: ``used`` as the
strings "true"/"false", ``writeable`` as 0/1, ``userMode`` without
``allowedValues`` and Base64 zone names. Values and names are constructed.
"""

from __future__ import annotations

import base64
import json
from contextlib import ExitStack
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.discovery_inventory import (
    RECOVERY_SCHEDULE,
)
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


# ------------------------------------------------- #113 RRC-08: end to end
PROGRAMS = "/programs/list"
PROGRAM_NAMES = ("Wohnen", "Arbeit", "Nacht", "Gäste")


def large_plant(*, filler: int) -> dict[str, Resource]:
    """Five TC100.2 zones in the observed and demo shapes, with named programs.

    The gateway advertises the zone leaves; ``filler`` further advertised but
    unreadable leaves follow them and use up the discovery budget, as on a
    large plant. Zone 4 runs manually: its program is sent with used "false".
    """
    tree: dict[str, Resource] = {
        "/zones": container("/zones", *(f"/zones/zn{n}" for n in range(1, 6))),
        "/programs": container("/programs", PROGRAMS),
        PROGRAMS: wire(
            PROGRAMS,
            type="programArray",
            value=[
                {"id": number, "name": b64(name)}
                for number, name in enumerate(PROGRAM_NAMES, 1)
            ],
        ),
    }
    for number in range(1, 6):
        root = f"/zones/zn{number}"
        manual = number == 4
        leaves = {
            f"{root}/name": wire(
                f"{root}/name", type="stringValue", value=b64(f"Raum {number}")
            ),
            f"{root}/userMode": wire(
                f"{root}/userMode",
                type="stringValue",
                used="true",
                writeable=1,
                value="manual" if manual else "clock",
            ),
            f"{root}/clockProgram": wire(
                f"{root}/clockProgram",
                type="floatValue",
                used="false" if manual else "true",
                writeable=1,
                unitOfMeasure="",
                minValue=0.0,
                maxValue=24.0,
                stepSize=1.0,
                value=float(number % 4 + 1),
            ),
        }
        count = filler if number == 5 else 0
        extra = [f"{root}/unread{index}" for index in range(count)]
        tree[root] = container(root, *leaves, *extra)
        tree.update(leaves)
    return tree


def program_selects(hass: HomeAssistant, entry) -> dict[str, str]:
    registry = er.async_get(hass)
    return {
        item.unique_id: item.entity_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        if item.domain == "select" and item.unique_id.endswith(":clockProgram:control")
    }


def cloud_patches(cloud: Cloud, clock: Clock, sent: list) -> ExitStack:
    async def put(gateway_id, path, value, **_kwargs):
        sent.append((path, value))
        cloud.tree[path] = replace(cloud.tree[path], value=value)

    stack = ExitStack()
    for item in (
        patch(f"{COORDINATOR}.monotonic", clock),
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
        stack.enter_context(item)
    return stack


async def test_large_tc100_plant_offers_a_named_program_select_per_zone(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    cloud = Cloud(large_plant(filler=600))
    sent: list[tuple[str, object]] = []
    with cloud_patches(cloud, Clock(), sent):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data.coordinators[0]
        coordinator._rrc_program_service.sleep = AsyncMock()
        assert coordinator.discovery_diagnostics.stop_reason == "resource_limit"

        selects = program_selects(hass, entry)
        assert sorted(selects) == [
            f"gateway-one:zones:zn{n}:clockProgram:control" for n in range(1, 6)
        ]
        names = {str(n): name for n, name in enumerate(PROGRAM_NAMES, 1)}
        for number in range(1, 6):
            state = hass.states.get(
                selects[f"gateway-one:zones:zn{number}:clockProgram:control"]
            )
            assert state.attributes["options"] == ["1", "2", "3", "4"]
            assert state.state == str(number % 4 + 1)
            assert state.attributes["program_names"] == names
            assert f"Raum {number}" in state.attributes["friendly_name"]
        manual = selects["gateway-one:zones:zn4:clockProgram:control"]
        assert hass.states.get(manual).attributes["program_name"] == "Wohnen"

        # The manual zone gets a program by its ID, confirmed by reading back.
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": manual, "option": "2"},
            blocking=True,
        )
        assert sent == [("/zones/zn4/clockProgram", 2)]
        state = hass.states.get(manual)
        assert state.state == "2" and state.attributes["program_name"] == "Arbeit"

        report = await async_get_config_entry_diagnostics(hass, entry)
        rrc_report = report["gateways"][0]["rrc_programs"]
        assert rrc_report["program_list"] == "current"
        assert rrc_report["program_selections_offered"] == 5
        assert "Wohnen" not in json.dumps(report)

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_failing_program_list_is_read_again_and_the_selects_appear(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    cloud = Cloud(large_plant(filler=0))
    cloud.down[PROGRAMS] = 503  # the bulk item and its single read fail
    clock = Clock()
    with cloud_patches(cloud, clock, []):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data.coordinators[0]
        assert program_selects(hass, entry) == {}
        # The other zone entities exist; the diagnostics say why this one not.
        registry = er.async_get(hass)
        assert registry.async_get_entity_id(
            "select", "bosch_buderus_heating", "gateway-one:zones:zn1:userMode:control"
        )
        report = await async_get_config_entry_diagnostics(hass, entry)
        gateway = report["gateways"][0]
        assert gateway["rrc_programs"]["program_list"] == "missing"
        assert {zone["selection"] for zone in gateway["rrc_programs"]["zones"]} == {
            "program_list_missing"
        }
        (path,) = [
            item for item in gateway["discovery"]["paths"] if item["path"] == PROGRAMS
        ]
        assert path["bulk_result"] == "http_503"
        assert path["fallback_result"] == "http_503"
        assert gateway["discovery_recovery"]["state"] == "pending"

        del cloud.down[PROGRAMS]
        clock.now += RECOVERY_SCHEDULE[0]
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        selects = program_selects(hass, entry)
        assert len(selects) == 5
        for entity_id in selects.values():
            assert hass.states.get(entity_id).attributes["options"] == [
                "1",
                "2",
                "3",
                "4",
            ]
        assert coordinator.discovery_recovery.state == "recovered"

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
