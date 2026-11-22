"""The module lists never leave identifying values anywhere (X40, I7).

A K40-class plant whose cloud (``/system/info``) and local
(``/system/basicInfo``) module lists carry the marker ``PRIVACYMARK`` in every
identifying field (``tests/fixtures/x40-privacy-markers.json``) runs through
setup and one cycle; no state, attribute, restore data, device registry entry,
diagnostics (entry and devices), anonymized inventory, log line or stored file
may contain the marker. Recorded on the state before the plant model; every
package of the plant model keeps it green.

The plant keeps the default options: the opt-in diagnostic entities of the
private paths (``/system/info``, ``/gateway/serialId``, ``/gateway/uuid``) are
created disabled and show their values only after the user enables them. The
gateway reports its own serial number and versions (``/gateway/serialId``,
``/gateway/versionFirmware``, ``/gateway/versionHardware``), as a K40 does; the
device registry shows the gateway's serial number from there.
"""

from __future__ import annotations

import json
import logging
from contextlib import ExitStack
from typing import Any

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.diagnostics import _gateway_diagnostics
from custom_components.bosch_buderus_heating.pointt import Gateway
from tests.helpers.managed_runtime import wait_for_sources
from tests.helpers.privacy_markers import (
    MARKER,
    assert_no_markers,
    basic_info,
    device_rows,
    diagnostics_channels,
    restore_channel,
    state_channels,
    storage_channel,
    system_info,
)
from tests.helpers.rrc_cloud import Clock, RrcCloud, cloud_patches
from tests.test_local_capture import CONTEXT, OBSERVED_AT, observations
from tests.test_x40_golden import (
    FROZEN,
    GATEWAY_ID,
    PACKAGE,
    close_local_view,
    k40_tree,
    local_readings,
    managed_entry,
    open_local_view,
    source_of,
)

INDOOR = "Logatherm WLW176i-12 TP70"

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def _heat_generator(hass: HomeAssistant, gateway_id: str) -> Any:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{gateway_id}:heat_generator"
    )
    assert entity_id is not None
    return hass.states.get(entity_id)


def _local_marker_reading() -> Any:
    # Newer than every reading of the capture, so it is the current list.
    return basic_info(CONTEXT, OBSERVED_AT, 10_000.0)


async def _final_write(hass: HomeAssistant) -> None:
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()


def test_the_fixture_marks_every_identifying_field() -> None:
    """The marker fixture covers every identifying field of a module list."""
    from tests.helpers.privacy_markers import fixture

    data = fixture()
    rows = data["system_info"]["values"]
    for field in (
        "No",
        "ProductSerialNumber",
        "ModuleSerialNumber",
        "ProductUuid",
        "SwIdenStr",
        "ProductTtn",
        "ModuleTtn",
    ):
        assert any(MARKER in str(row.get(field, "")) for row in rows), field
    # A product name that is the row's serial number, a version that is an
    # identifier of the same row.
    assert any(row.get("ProductName") == row.get("ProductSerialNumber") for row in rows)
    assert any(row.get("Ver") == row.get("ModuleSerialNumber") for row in rows)
    # Unsafe characters in the software and article identifiers.
    assert any(
        any(not char.isalnum() and char not in "-_.:" for char in row["SwIdenStr"])
        for row in rows
        if MARKER in row.get("SwIdenStr", "")
    )
    local = data["basic_info"]
    assert MARKER in local["gatewayId"]
    assert any(
        row.get("ProductName") == row.get("ProductSerialNumber")
        for row in local["values"]
    )
    assert any(
        row.get("Ver") == row.get("ModuleSerialNumber") for row in local["values"]
    )
    # The local decoder reads the marked fields.
    reading = _local_marker_reading()
    modules = reading.data.modules
    assert MARKER in reading.data.gateway_id
    assert any(MARKER in str(module.serial_number) for module in modules)
    assert any(MARKER in str(module.product_serial) for module in modules)


def test_a_marker_is_found_in_any_channel() -> None:
    with pytest.raises(pytest.fail.Exception, match="Privacy marker found in: log"):
        assert_no_markers({"states": "[]", "log": "x privacymark-no-1 y"})
    with pytest.raises(AssertionError):
        assert_no_markers({})
    assert_no_markers({"states": "[]"})


async def test_the_cloud_and_local_k40_shows_no_marker(
    hass: HomeAssistant,
    freezer: Any,
    caplog: pytest.LogCaptureFixture,
    hass_storage: dict[str, Any],
) -> None:
    freezer.move_to(FROZEN)
    caplog.set_level(logging.DEBUG, logger=PACKAGE)
    tree = k40_tree()
    tree["/system/info"] = system_info()
    cloud = RrcCloud(tree, forbidden=set(), missing_as_500=False)
    clock = Clock()
    gateway = Gateway(GATEWAY_ID, device_type="k40")
    entry = managed_entry(hass, gateway)
    with ExitStack() as stack:
        stack.enter_context(cloud_patches(cloud, clock, gateway))
        stack.enter_context(local_readings((_local_marker_reading(),)))
        assert await hass.config_entries.async_setup(entry.entry_id)
        await wait_for_sources(hass, entry, 1)
        await hass.async_block_till_done()
        clock.now += 3600
        await source_of(entry).async_refresh()
        await hass.async_block_till_done()
        # Both lists were read and used.
        shown = _heat_generator(hass, GATEWAY_ID)
        assert shown.state == INDOOR
        report = _gateway_diagnostics(1, source_of(entry))["heat_generator"]
        assert report["cloud_modules"] > 0 and report["local_modules"] > 0
        assert "/system/info" in cloud.all_requested()
        channels = {
            **state_channels(hass),
            "restore": await restore_channel(hass, hass_storage),
            "devices": json.dumps(device_rows(hass, entry.entry_id)),
            **await diagnostics_channels(hass, entry),
        }
        assert INDOOR in channels["attributes"] and INDOOR in channels["restore"]
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    await _final_write(hass)
    channels["storage"] = storage_channel(hass_storage)
    channels["log"] = caplog.text
    assert any(key.startswith(DOMAIN) for key in hass_storage)
    assert any(record.name.startswith(PACKAGE) for record in caplog.records)
    assert_no_markers(channels)


async def test_the_local_plant_view_shows_no_marker(
    hass: HomeAssistant,
    freezer: Any,
    caplog: pytest.LogCaptureFixture,
    hass_storage: dict[str, Any],
) -> None:
    freezer.move_to(FROZEN)
    caplog.set_level(logging.DEBUG, logger=PACKAGE)
    readings = (*observations(), _local_marker_reading())
    coordinator, runtime, platforms = await open_local_view(hass, readings)
    runtime.publish(readings)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    shown = _heat_generator(hass, coordinator.gateway.gateway_id)
    assert shown.state == INDOOR and shown.attributes["source"] == "local"
    entry = coordinator.config_entry
    channels = {
        **state_channels(hass),
        "restore": await restore_channel(hass, hass_storage),
        "devices": json.dumps(device_rows(hass, entry.entry_id)),
        "diagnostics": json.dumps(_gateway_diagnostics(1, coordinator), default=repr),
    }
    assert INDOOR in channels["restore"]
    await close_local_view(hass, coordinator, platforms)
    await _final_write(hass)
    channels["storage"] = storage_channel(hass_storage)
    channels["log"] = caplog.text
    assert_no_markers(channels)
