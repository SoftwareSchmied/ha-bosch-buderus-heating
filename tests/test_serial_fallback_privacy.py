"""The gateway's serial number from the module list never reaches a diagnosis.

A gateway that does not answer ``/gateway/serialId`` gets the first product
or module serial number of ``/system/info`` as the serial number of its
Home Assistant device (device.py). That number stays in the device registry
only: a real setup of a K40-class plant with the module list of
``tests/fixtures/x40-privacy-markers.json``, without ``/gateway/serialId``
and with the component devices switched on, shows it in no diagnosis of the
entry, of any device (the gateway, its component devices) and in no
anonymized inventory.
"""

from __future__ import annotations

import json
import logging
from contextlib import ExitStack
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.component_devices import OPTION
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.pointt import Gateway
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from tests.helpers.managed_runtime import wait_for_sources
from tests.helpers.privacy_markers import (
    MARKER,
    assert_no_markers,
    device_rows,
    diagnostics_channels,
    fixture,
    state_channels,
)
from tests.helpers.rrc_cloud import Clock, RrcCloud, cloud_patches
from tests.test_x40_golden import (
    FROZEN,
    GATEWAY_ID,
    PACKAGE,
    k40_tree,
    local_readings,
    managed_entry,
    source_of,
)
from tests.test_x40_privacy_markers import _local_marker_reading

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def _module_list(kind: str) -> dict[str, Any]:
    """The marked cloud module list; "module": without product serials."""
    raw = fixture()["system_info"]
    if kind == "module":
        for row in raw["values"]:
            # Without its serial number a name or version equal to it would
            # be a plain name or version: such fields are replaced too.
            if row.get("ProductName") == row.get("ProductSerialNumber"):
                row["ProductName"] = "Second module"
            if "ProductSerialNumber" in row and row.get("Ver") == row.get(
                "ProductSerialNumber"
            ):
                row["Ver"] = "1.0.0"
            row.pop("ProductSerialNumber", None)
    return raw


def _fallback_serial(raw: dict[str, Any]) -> str:
    """The serial number device.py takes without /gateway/serialId."""
    return next(
        row[key]
        for key in ("ProductSerialNumber", "ModuleSerialNumber")
        for row in raw["values"]
        if key in row
    )


@pytest.mark.parametrize("kind", ["product", "module"])
async def test_a_serial_number_from_the_module_list_reaches_no_diagnosis(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture, kind: str
) -> None:
    freezer.move_to(FROZEN)
    caplog.set_level(logging.DEBUG, logger=PACKAGE)
    raw = _module_list(kind)
    tree = k40_tree()
    del tree["/gateway/serialId"]
    tree["/system/info"] = parse_resource(raw)
    cloud = RrcCloud(tree, forbidden=set(), missing_as_500=False)
    clock = Clock()
    gateway = Gateway(GATEWAY_ID, device_type="k40")
    entry = managed_entry(hass, gateway)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, OPTION: True}
    )
    serial = _fallback_serial(raw)
    assert MARKER in serial
    with ExitStack() as stack:
        stack.enter_context(cloud_patches(cloud, clock, gateway))
        stack.enter_context(local_readings((_local_marker_reading(),)))
        assert await hass.config_entries.async_setup(entry.entry_id)
        await wait_for_sources(hass, entry, 1)
        await hass.async_block_till_done()
        clock.now += 3600
        await source_of(entry).async_refresh()
        await hass.async_block_till_done()
        assert "/system/info" in cloud.all_requested()
        devices = device_rows(hass, entry.entry_id)
        # The fallback is in effect: the gateway's device shows the number.
        shown = [row for row in devices if row["serial_number"] == serial]
        assert [row["identifiers"] for row in shown] == [[[DOMAIN, GATEWAY_ID]]]
        components = [row for row in devices if ":component:" in json.dumps(row)]
        assert len(components) >= 3
        channels = await diagnostics_channels(hass, entry)
        # One diagnosis per device: the gateway and its component devices.
        assert len(
            [name for name in channels if name.startswith("device_diagnostics_")]
        ) == len(devices)
        channels.update(state_channels(hass))
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    channels["log"] = caplog.text
    assert_no_markers(channels)
    assert not [name for name, text in channels.items() if serial in text]
