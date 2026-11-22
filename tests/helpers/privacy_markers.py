"""Privacy markers for the plant model (X40, invariant I7).

``tests/fixtures/x40-privacy-markers.json`` holds the module list of a K40-class
gateway as the cloud (``/system/info``) and the local access
(``/system/basicInfo``) report it, with the marker ``PRIVACYMARK`` in every
identifying field: serial and module numbers (``No``, ``ProductSerialNumber``,
``ModuleSerialNumber``), the product UUID, the gateway ID of the local answer,
a product name that is the row's own serial number, a version that is an
identifier of the same row, and software and article identifiers
(``SwIdenStr``, ``ProductTtn``, ``ModuleTtn``) with unsafe characters.

``gather_channels`` collects every place a value of the plant can reach in
Home Assistant: states, attributes, restore data, the device registry, the
diagnostics of the entry and of each device, the anonymized inventory, the log
and the stored files. ``assert_no_markers`` fails with the names of the
channels that contain a marker (never with the values).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.bosch_buderus_heating.local_api.catalog import request_for_path
from custom_components.bosch_buderus_heating.local_api.decoder import decode_resource
from custom_components.bosch_buderus_heating.local_api.models import LocalObservation
from custom_components.bosch_buderus_heating.pointt import Resource
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource

FIXTURE = Path(__file__).parents[1] / "fixtures" / "x40-privacy-markers.json"
MARKER = "PRIVACYMARK"


def fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def system_info() -> Resource:
    """The cloud module list with the markers."""
    return parse_resource(fixture()["system_info"])


def basic_info(
    context: Any, observed_at: datetime, observed_monotonic: float
) -> LocalObservation:
    """The local module list with the markers, decoded like a local read."""
    body = fixture()["basic_info"]
    return decode_resource(
        request_for_path("/system/basicInfo"),
        json.dumps(body).encode(),
        context=context,
        observed_at=observed_at,
        observed_monotonic=observed_monotonic,
    )


def _text(value: Any) -> str:
    return json.dumps(value, default=repr, sort_keys=True, ensure_ascii=False)


def device_rows(hass: HomeAssistant, entry_id: str) -> list[dict[str, Any]]:
    """Every field of the entry's devices that a user or an export can see."""
    return [
        {
            "identifiers": sorted(map(list, device.identifiers)),
            "connections": sorted(map(list, device.connections)),
            "name": device.name,
            "name_by_user": device.name_by_user,
            "model": device.model,
            "model_id": device.model_id,
            "manufacturer": device.manufacturer,
            "sw_version": device.sw_version,
            "hw_version": device.hw_version,
            "serial_number": device.serial_number,
            "configuration_url": device.configuration_url,
            "area_id": device.area_id,
        }
        for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry_id)
    ]


def state_channels(hass: HomeAssistant) -> dict[str, str]:
    states = hass.states.async_all()
    return {
        "states": _text(sorted((item.entity_id, item.state) for item in states)),
        "attributes": _text(
            sorted((item.entity_id, dict(item.attributes)) for item in states)
        ),
    }


async def restore_channel(hass: HomeAssistant, storage: Mapping[str, Any]) -> str:
    """The restore data of every entity, as Home Assistant stores it."""
    from homeassistant.helpers import restore_state

    data = restore_state.async_get(hass)
    await data.async_dump_states()
    return _text(storage.get(restore_state.STORAGE_KEY))


async def diagnostics_channels(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, str]:
    """The diagnostics of the entry and of each device, and the anonymized
    inventory built from the entry's diagnostics."""
    from custom_components.bosch_buderus_heating.diagnostics import (
        async_get_config_entry_diagnostics,
        async_get_device_diagnostics,
    )
    from custom_components.bosch_buderus_heating.inventory import (
        build_anonymized_inventory,
    )

    report = await async_get_config_entry_diagnostics(hass, entry)
    channels = {
        "diagnostics": _text(report),
        "anonymized_inventory": _text(build_anonymized_inventory(report)),
    }
    for index, device in enumerate(
        dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    ):
        channels[f"device_diagnostics_{index}"] = _text(
            await async_get_device_diagnostics(hass, entry, device)
        )
    return channels


def storage_channel(storage: Mapping[str, Any]) -> str:
    """Every stored file (the calculated values, the discovery inventory, ...)."""
    return _text(dict(storage))


def assert_no_markers(channels: Mapping[str, str], marker: str = MARKER) -> None:
    """No channel contains the marker; the failure names channels only."""
    assert channels, "no channel was gathered"
    found = sorted(
        name for name, text in channels.items() if marker.casefold() in text.casefold()
    )
    if found:
        pytest.fail(f"Privacy marker found in: {', '.join(found)}")
