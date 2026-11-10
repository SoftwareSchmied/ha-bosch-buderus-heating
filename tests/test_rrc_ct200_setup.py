"""C8: an RRC 2.0 room controller of the CT200 class through the real setup.

The synthetic cloud answers with ``ct200-shapes.json``: 11 zones, 12 radio
devices and 24 programs, 572 resources in all, more than the discovery
budget. Its device tree follows the anonymized TC100.2 tree of issue 122
(leaf names and types only). Missing catalog and optional paths answer
HTTP 500 as these controllers do; missing roots answer 404.
"""

from __future__ import annotations

import json
import logging
import re
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.discovery import ROOT_RESOURCE_PATHS
from custom_components.bosch_buderus_heating.discovery_inventory import (
    RECOVERY_SCHEDULE,
)
from custom_components.bosch_buderus_heating.ha_compat import device_by_identifier
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Gateway,
    PointTClient,
    Resource,
    ResourceError,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from tests.test_discovery_recovery import Clock, Cloud
from tests.test_entry import _entry

FIXTURE = Path(__file__).parent / "fixtures" / "contracts" / "rrc" / "ct200-shapes.json"
COORDINATOR = "custom_components.bosch_buderus_heating.coordinator"
PACKAGE = "custom_components.bosch_buderus_heating"
GATEWAY = Gateway("gateway-one")
DEVICE_LIST = "/devices/list"
# What must never be requested, stored, logged or exported.
PRIVATE = re.compile(
    r"^/devices/dev[0-9]+(?:/|$)|/homematicip(?:/|$)|^/gateway/(?:user|installer)"
    r"(?:/|$)|^/gateway/userPassword$|^/system/location(?:/|$)"
    r"|^/system/autoAway/users/list$"
)
MARKERS = ("MARKER", "private-marker")

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture(autouse=True)
def legacy_setup_path():
    """Reach the format 3 setup without migrating, as test_startup_inventory."""
    with patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)):
        yield


def ct200_tree() -> dict[str, Resource]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["provenance"]["physical_device_verified"] is False
    return {item["id"]: parse_resource(item) for item in data["resources"]}


class RrcCloud(Cloud):
    """A missing catalog or optional path answers HTTP 500 (P31)."""

    def __init__(self, resources: dict[str, Resource]) -> None:
        super().__init__(resources)
        self.single: list[str] = []
        self.forbidden = {"/devices/inclusionWhitelist"}

    def _missing(self, path: str) -> int:
        if path in self.forbidden:
            return 403
        return 404 if path in ROOT_RESOURCE_PATHS else 500

    async def get_resources_bulk(self, gateway_id, paths, **kwargs):
        results = await super().get_resources_bulk(gateway_id, paths, **kwargs)
        answered = []
        for result in results:
            if result.status == 404 and result.path not in self.down:
                status = self._missing(result.path)
                error = ResourceError(result.path, status)
                result = BatchItemResult(gateway_id, result.path, status, error=error)
            answered.append(result)
        return tuple(answered)

    async def get_resource(self, gateway_id, path, **kwargs):
        self.single.append(path)
        if path not in self.tree and path not in self.down:
            status = self._missing(path)
            if status >= 500:
                raise ServiceUnavailable(status)
            raise ResourceError(path, status)
        return await super().get_resource(gateway_id, path, **kwargs)

    def all_requested(self) -> set[str]:
        return {path for paths in self.requests for path in paths} | set(self.single)


def cloud_patches(cloud: Cloud, clock: Clock, gateway: Gateway = GATEWAY) -> ExitStack:
    async def put(gateway_id, path, value, **_kwargs):
        cloud.tree[path] = replace(cloud.tree[path], value=value)

    stack = ExitStack()
    for item in (
        patch(f"{COORDINATOR}.monotonic", clock),
        patch.object(PointTClient, "get_gateways", AsyncMock(return_value=(gateway,))),
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


def entities(hass: HomeAssistant, entry, domain: str) -> dict[str, er.RegistryEntry]:
    registry = er.async_get(hass)
    return {
        item.unique_id: item
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        if item.domain == domain
    }


async def test_large_ct200_plant_reaches_every_platform_within_the_budget(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=PACKAGE)
    entry = _entry(hass)
    cloud = RrcCloud(ct200_tree())
    assert len(cloud.tree) > 512  # more than one discovery may read
    # Like the TC100.2, the controller refuses single advertised paths.
    cloud.down["/zones/zn2/icon"] = 403
    with cloud_patches(cloud, Clock()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        coordinator = entry.runtime_data.coordinators[0]
        report = coordinator.discovery_diagnostics

        # CT-B01..B03: lists first, the device tree no longer eats the budget.
        assert report.stop_reason == "complete"
        assert report.device_profile == "rrc2"
        assert {"zones", "product_type"} <= set(report.rrc_evidence)
        requested = cloud.all_requested()
        assert {"/zones/list", "/programs/list", DEVICE_LIST} <= requested
        assert {path for path in requested if path.startswith("/devices")} == {
            "/devices",
            DEVICE_LIST,
            *(f"/devices/device{n}/versionFirmware" for n in range(1, 13)),
        }
        # CT-E07, CT-F05: keys and personal data are never requested.
        assert not {path for path in requested if PRIVATE.search(path)}
        assert len(requested) < 400

        # Every zone offers its program; 24 programs are named options.
        selects = entities(hass, entry, "select")
        programs = [
            item.entity_id
            for key, item in selects.items()
            if key.endswith(":clockProgram:control")
        ]
        assert len(programs) == 11
        state = hass.states.get(programs[0])
        assert state.attributes["options"] == [str(n) for n in range(1, 25)]

        # E07: eleven radio devices below the controller (not the controller).
        devices = dr.async_get(hass)
        gateway = device_by_identifier(devices, (DOMAIN, "gateway-one"), entry.entry_id)
        assert gateway.name == "Room controller"
        assert gateway.model == "RRC 2.0"
        assert gateway.sw_version == "05.04.00"
        radios = {
            next(iter(device.identifiers))[1]: device
            for device in dr.async_entries_for_config_entry(devices, entry.entry_id)
            if device.via_device_id == gateway.id
        }
        assert sorted(radios) == sorted(
            f"gateway-one:devices:device{n}" for n in range(2, 13)
        )
        valve = radios["gateway-one:devices:device2"]
        assert valve.name == "Radiator thermostat 2"
        assert valve.model == "Radiator thermostat"
        assert valve.sw_version == "1.6.0"
        assert ar.async_get(hass).async_get_area(valve.area_id).name == "Raum 1"
        link = radios["gateway-one:devices:device12"]
        assert link.model == "Heat generator link"
        assert link.area_id is None  # zone 0 is the system

        # CT-E02: battery low, enabled; unknown stays unknown.
        batteries = entities(hass, entry, "binary_sensor")
        battery = {
            n: batteries[f"gateway-one:devices:device{n}:battery"] for n in range(2, 13)
        }
        assert "gateway-one:devices:device1:battery" not in batteries
        assert all(item.disabled_by is None for item in battery.values())
        assert battery[2].device_id == valve.id
        assert hass.states.get(battery[2].entity_id).state == "off"
        assert hass.states.get(battery[3].entity_id).state == "on"
        assert hass.states.get(battery[5].entity_id).state == "unknown"
        assert hass.states.get(battery[2].entity_id).attributes["friendly_name"] == (
            "Radiator thermostat 2 Battery low"
        )
        # CT-E03: the signal is a diagnostic, disabled by default; device 8
        # sends none and gets none.
        sensors = entities(hass, entry, "sensor")
        signals = {
            key: item
            for key, item in sensors.items()
            if re.fullmatch(r"gateway-one:devices:device\d+:signal", key)
        }
        assert len(signals) == 10
        assert "gateway-one:devices:device8:signal" not in signals
        assert all(
            item.disabled_by is er.RegistryEntryDisabler.INTEGRATION
            for item in signals.values()
        )
        # CT-E06: the firmware reading sits on its device.
        firmware = sensors["gateway-one:devices:device2:versionFirmware"]
        assert firmware.device_id == valve.id
        controller_firmware = sensors["gateway-one:devices:device1:versionFirmware"]
        assert controller_firmware.device_id == gateway.id

        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        gateway_report = diagnostics["gateways"][0]
        profile = gateway_report["rrc_profile"]
        assert gateway_report["device_profile"] == "rrc2"
        assert profile["profile"] == "rrc2"
        assert profile["confirmation"] == "unconfirmed"
        assert profile["local_access"] == "not_available"
        assert profile["product_type"] == "RRC 2.0"
        assert profile["firmware"] == "05.04.00"
        assert profile["zone_count"] == 11
        assert profile["program_count"] == 24
        assert profile["device_count"] == 12
        assert profile["device_list"] == "current"
        assert profile["devices_with_low_battery"] == 2
        assert profile["device_types"]["thermostat_valve"] == 7
        assert profile["device_fields_missing"]["signal"] == 1
        assert profile["device_firmware_read"] == 12
        assert profile["access_reason"] == "some_paths_forbidden"
        discovery = gateway_report["discovery"]
        assert discovery["paths_not_read"]["never_read_private"] >= 12
        assert discovery["paths_not_read"]["outside_rrc_device_scope"] >= 12

        # Nothing private in diagnostics, states, the stored inventory or logs.
        dumped = json.dumps(diagnostics)
        stored = json.dumps(coordinator.inventory._data())
        shown = json.dumps(
            [(state.state, dict(state.attributes)) for state in hass.states.async_all()]
        )
        for text in (dumped, stored, shown, caplog.text):
            for marker in MARKERS:
                assert marker not in text
        assert not any(PRIVATE.search(path) for path in coordinator.resources)

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_device_list_outage_and_late_list_in_real_cycles(
    hass: HomeAssistant,
) -> None:
    """Failure paths: no list at the start, then one, then an outage of it."""
    entry = _entry(hass)
    cloud = RrcCloud(ct200_tree())
    cloud.down[DEVICE_LIST] = 503  # bulk item and single read fail
    clock = Clock()
    with cloud_patches(cloud, clock, Gateway("gateway-one", device_type="rrc2")):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data.coordinators[0]
        assert not [
            key for key in entities(hass, entry, "binary_sensor") if ":battery" in key
        ]
        assert coordinator.discovery_recovery.state == "pending"
        assert DEVICE_LIST in coordinator.discovery_recovery.missing

        del cloud.down[DEVICE_LIST]
        clock.now += RECOVERY_SCHEDULE[0]
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        batteries = {
            key: item
            for key, item in entities(hass, entry, "binary_sensor").items()
            if key.endswith(":battery")
        }
        assert len(batteries) == 11
        low = batteries["gateway-one:devices:device3:battery"].entity_id
        assert hass.states.get(low).state == "on"

        # A temporary failure of the list keeps the last good state shown
        # within its validity, as for every cloud reading.
        cloud.down[DEVICE_LIST] = 503
        clock.now += 2 * 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(low).state == "on"
        # A list the controller no longer answers makes its devices
        # unavailable; no entity is removed.
        cloud.down[DEVICE_LIST] = 404
        clock.now += 2 * 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(low).state == "unavailable"
        assert len(entities(hass, entry, "binary_sensor")) >= 11

        # It answers again: the state returns, the battery is changed now.
        del cloud.down[DEVICE_LIST]
        listed = cloud.tree[DEVICE_LIST]
        items = [
            {**item, "battery": "ok"} if item["id"] == 3 else item
            for item in listed.value
        ]
        cloud.tree[DEVICE_LIST] = replace(listed, value=items)
        clock.now += 2 * 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(low).state == "off"

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_radio_devices_registered_before_their_controller_get_it(
    hass: HomeAssistant,
) -> None:
    """Review (E07): when a radio device is added before the controller's
    device exists, it has no parent at first; the setup links it."""
    entry = _entry(hass)
    cloud = RrcCloud(ct200_tree())
    with (
        cloud_patches(cloud, Clock()),
        patch(f"{PACKAGE}.device.via_device", return_value={}),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        devices = dr.async_get(hass)
        gateway = device_by_identifier(devices, (DOMAIN, "gateway-one"), entry.entry_id)
        radios = [
            device
            for device in dr.async_entries_for_config_entry(devices, entry.entry_id)
            if any(":devices:device" in item[1] for item in device.identifiers)
        ]
        assert len(radios) == 11
        assert all(device.via_device_id == gateway.id for device in radios)
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
