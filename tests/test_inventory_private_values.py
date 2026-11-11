"""D34: the discovery inventory keeps no private or identifier values."""

from __future__ import annotations

from datetime import UTC, datetime

from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.discovery_inventory import (
    DiscoveryInventory,
    _gateways,
    resource_to_data,
    stored_resource,
)
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    Resource,
    ResourceMetadata,
)

GATEWAY = "000123"
SERIAL = "SYNTHETIC-SERIAL-0001"
UUID = "00000000-0000-4000-8000-00000000abcd"
MODULE_SERIAL = "SYNTHETIC-MODULE-0002"
SETPOINT = "/heatingCircuits/hc1/currentRoomSetpoint"


def _text(path: str, value: str) -> Resource:
    return Resource(
        path,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(resource_type="stringValue"),
    )


def discovered() -> dict[str, Resource]:
    return {
        "/gateway/serialId": _text("/gateway/serialId", SERIAL),
        "/gateway/uuid": _text("/gateway/uuid", UUID),
        "/system/info": Resource(
            "/system/info",
            values=({"Id": 1, "ModuleSerialNumber": MODULE_SERIAL},),
            has_values=True,
            metadata=ResourceMetadata(resource_type="arrayData"),
        ),
        "/gateway/wifi/ssid": _text("/gateway/wifi/ssid", "synthetic-network"),
        "/heatSources/hs1/moduleSerialNo": _text(
            "/heatSources/hs1/moduleSerialNo", "SYNTHETIC-NO-3"
        ),
        SETPOINT: Resource(
            SETPOINT,
            value=21.5,
            has_value=True,
            metadata=ResourceMetadata(resource_type="floatValue", unit="C"),
        ),
    }


def test_private_values_are_stored_as_descriptions_only(hass: HomeAssistant) -> None:
    inventory = DiscoveryInventory(hass, "entry")
    inventory.record(Gateway(GATEWAY, model="MX400"), discovered())
    stored = inventory.resources(GATEWAY)
    # The paths stay known, so the same entities are rebuilt after a start.
    assert set(stored) == set(discovered())
    for path, resource in stored.items():
        if path == SETPOINT:
            assert resource.value == 21.5 and resource.has_value
            continue
        assert resource.value is None and not resource.has_value
        assert resource.values == () and not resource.has_values
        assert resource.metadata == discovered()[path].metadata
    saved = repr(inventory._data())
    for secret in (SERIAL, UUID, MODULE_SERIAL, "synthetic-network", "SYNTHETIC-NO"):
        assert secret not in saved
    assert "21.5" in saved


def test_values_an_earlier_release_stored_are_not_loaded(hass: HomeAssistant) -> None:
    now = datetime.now(UTC)
    older = {
        "gateways": {
            GATEWAY: {
                "gateway": {"gateway_id": GATEWAY, "model": "MX400"},
                "saved_at": now.isoformat(),
                "resources": {
                    path: {"seen": now.isoformat(), "resource": resource_to_data(item)}
                    for path, item in discovered().items()
                },
            }
        }
    }
    assert SERIAL in repr(older)
    ((_, loaded),) = _gateways(older, now)
    assert set(loaded.resources) == set(discovered())
    assert loaded.resources["/gateway/serialId"].resource.value is None
    assert loaded.resources["/system/info"].resource.values == ()
    assert loaded.resources[SETPOINT].resource.value == 21.5
    inventory = DiscoveryInventory(hass, "entry")
    inventory._gateways = {GATEWAY: loaded}
    assert SERIAL not in repr(inventory._data())


def test_ordinary_resources_are_kept_unchanged() -> None:
    resource = discovered()[SETPOINT]
    assert stored_resource(resource) is resource


async def test_an_inventory_start_keeps_the_registered_serial_number(
    hass: HomeAssistant,
) -> None:
    """Without a serial reading the key is left out, so HA keeps the old one."""
    from homeassistant.helpers import device_registry as dr

    from custom_components.bosch_buderus_heating.device import (
        device_info_for_resource,
    )
    from tests.test_device import _coordinator

    stored = stored_resource(discovered()["/gateway/serialId"])
    coordinator = _coordinator(
        hass,
        resources=(Resource("/system/brand", "Buderus", True), stored),
    )
    info = device_info_for_resource(coordinator, "/system/brand")
    assert "serial_number" not in info
    registry = dr.async_get(hass)
    entry_id = coordinator.config_entry.entry_id
    registry.async_get_or_create(
        config_entry_id=entry_id,
        identifiers=info["identifiers"],
        serial_number=SERIAL,
    )
    device = registry.async_get_or_create(config_entry_id=entry_id, **info)
    assert device.serial_number == SERIAL
    live = _coordinator(
        hass,
        resources=(
            Resource("/system/brand", "Buderus", True),
            discovered()["/gateway/serialId"],
        ),
    )
    assert device_info_for_resource(live, "/system/brand")["serial_number"] == SERIAL


async def test_an_inventory_start_keeps_the_registered_model_and_versions(
    hass: HomeAssistant,
) -> None:
    """The stored inventory keeps no values of
    ``/system/info``; a known device keeps its model and versions."""
    from homeassistant.helpers import device_registry as dr

    from custom_components.bosch_buderus_heating.device import (
        device_info_for_resource,
    )
    from tests.test_device import _coordinator

    info_values = Resource(
        "/system/info",
        values=(
            {
                "Id": 1,
                "ProductName": "MX400",
                "Ver": "05.04.00",
                "HwVersion": "02.01",
            },
        ),
        has_values=True,
        metadata=ResourceMetadata(resource_type="arrayData"),
    )
    brand = Resource("/system/brand", "Buderus", True)
    gateway = Gateway(GATEWAY, device_type="k40")
    live = _coordinator(hass, gateway=gateway, resources=(brand, info_values))
    first = device_info_for_resource(live, "/system/brand")
    assert (first["model"], first["sw_version"], first["hw_version"]) == (
        "MX400",
        "05.04.00",
        "02.01",
    )
    registry = dr.async_get(hass)
    entry_id = live.config_entry.entry_id
    registry.async_get_or_create(config_entry_id=entry_id, **first)
    start = _coordinator(
        hass, gateway=gateway, resources=(brand, stored_resource(info_values))
    )
    start.config_entry = live.config_entry
    info = device_info_for_resource(start, "/system/brand")
    for key in ("model", "model_id", "sw_version", "hw_version"):
        assert key not in info
    device = registry.async_get_or_create(config_entry_id=entry_id, **info)
    assert (device.model, device.sw_version, device.hw_version) == (
        "MX400",
        "05.04.00",
        "02.01",
    )
    # A new device still gets what can be derived.
    other = _coordinator(
        hass,
        gateway=Gateway("other-gateway", device_type="k40"),
        resources=(brand, stored_resource(info_values)),
    )
    created = device_info_for_resource(other, "/system/brand")
    assert "model" in created and "sw_version" in created
