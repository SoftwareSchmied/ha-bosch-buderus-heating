"""Home Assistant device metadata derived from PointT resources."""

from __future__ import annotations

import re

from homeassistant.helpers.device_registry import DeviceInfo

from .const import CONF_BRAND, DOMAIN
from .coordinator import BoschBuderusDataUpdateCoordinator
from .resource_catalog import configured_device_name, logical_device_for_path

_UNKNOWN_HEAT_PUMP_TYPES = frozenset(
    {"", "none", "not_available", "undefined", "unknown"}
)
_GROUP_SEPARATOR = " \N{EN DASH} "


def device_info_for_resource(
    coordinator: BoschBuderusDataUpdateCoordinator,
    resource_path: str,
) -> DeviceInfo:
    """Build the shared brand-aware gateway device for a PointT resource."""
    gateway = coordinator.gateway
    manufacturer = _manufacturer(coordinator)
    model = _display_model(gateway.model or gateway.device_type or "PointT Gateway")
    system_info = _system_info_values(coordinator)
    return DeviceInfo(
        identifiers={(DOMAIN, gateway.gateway_id)},
        manufacturer=manufacturer,
        model=model,
        model_id=_first_value(system_info, "ProductTtn", "ModuleTtn"),
        name=f"{manufacturer} {model}",
        serial_number=_resource_string(coordinator, "/gateway/serialId")
        or _first_value(system_info, "ProductSerialNumber", "ModuleSerialNumber"),
        sw_version=_resource_string(coordinator, "/gateway/versionFirmware")
        or gateway.firmware_version
        or _first_value(system_info, "Ver", "SwIdenStr"),
        hw_version=_resource_string(coordinator, "/gateway/versionHardware")
        or _first_value(system_info, "HwVersion", "ModuleHwIdentStr"),
    )


def grouped_entity_name(
    coordinator: BoschBuderusDataUpdateCoordinator,
    resource_path: str,
    name: str,
) -> str:
    """Prefix an entity name with its dynamic logical PointT group."""
    german = coordinator.hass.config.language.casefold().startswith("de")
    logical = logical_device_for_path(resource_path)
    if logical is not None:
        group = _configured_logical_name(coordinator, resource_path, logical.logical_id)
        group = _localized_generic_name(group, logical.kind, german)
        if logical.kind == "heat_source" and _is_heat_pump(
            coordinator, resource_path, logical.logical_id
        ):
            group = group or _heat_pump_name(coordinator, logical.logical_id, german)
        group = group or _logical_name(logical.kind, logical.logical_id, german)
        return f"{group}{_GROUP_SEPARATOR}{name}"
    if resource_path == "/gateway" or resource_path.startswith("/gateway/"):
        group = "Gateway"
    elif resource_path == "/system" or resource_path.startswith("/system/"):
        group = "Anlage" if german else "System"
    elif resource_path == "/heatSources" or resource_path.startswith("/heatSources/"):
        group = _central_heat_source_name(coordinator, german)
    else:
        group = "Anlage" if german else "System"
    return f"{group}{_GROUP_SEPARATOR}{name}"


def _manufacturer(coordinator: BoschBuderusDataUpdateCoordinator) -> str:
    for path in ("/system/brand", "/gateway/brand"):
        manufacturer = _known_brand(_resource_string(coordinator, path))
        if manufacturer is not None:
            return manufacturer

    config_entry = coordinator.config_entry
    configured_brand = (
        config_entry.data.get(CONF_BRAND) if config_entry is not None else None
    )
    manufacturer = _known_brand(
        configured_brand if isinstance(configured_brand, str) else None
    )
    return manufacturer or "Bosch Thermotechnology"


def _known_brand(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.casefold()
    if "buderus" in normalized:
        return "Buderus"
    if "bosch" in normalized:
        return "Bosch"
    return None


def _display_model(value: str) -> str:
    model = value.strip()
    if re.fullmatch(r"(?:k|mx)\d+(?:rf)?", model, re.IGNORECASE):
        return model.upper()
    return model


def _configured_logical_name(
    coordinator: BoschBuderusDataUpdateCoordinator,
    resource_path: str,
    logical_id: str,
) -> str | None:
    root = resource_path.strip("/").split("/", 1)[0]
    value = _resource_string(coordinator, f"/{root}/{logical_id}/name")
    return configured_device_name(value) if value is not None else None


def _is_heat_pump(
    coordinator: BoschBuderusDataUpdateCoordinator,
    resource_path: str,
    logical_id: str,
) -> bool:
    root = resource_path.strip("/").split("/", 1)[0]
    heat_pump_type = _resource_string(coordinator, f"/{root}/{logical_id}/heatPumpType")
    if (
        heat_pump_type is not None
        and heat_pump_type.casefold() not in _UNKNOWN_HEAT_PUMP_TYPES
    ):
        return True
    source_type = _resource_string(coordinator, f"/{root}/{logical_id}/type")
    if source_type is None:
        return False
    normalized = re.sub(r"[^a-z]", "", source_type.casefold())
    return normalized in {"heatpump", "waermepumpe"}


def _heat_pump_name(
    coordinator: BoschBuderusDataUpdateCoordinator, logical_id: str, german: bool
) -> str:
    heat_source_ids = {
        logical.logical_id
        for path in set(coordinator.resources) | set(coordinator.data or {})
        if (logical := logical_device_for_path(path)) is not None
        and logical.kind == "heat_source"
    }
    label = "Wärmepumpe" if german else "Heat pump"
    if len(heat_source_ids) <= 1:
        return label
    suffix = re.search(r"(\d+)$", logical_id)
    return f"{label} {suffix.group(1) if suffix else logical_id}"


def _central_heat_source_name(
    coordinator: BoschBuderusDataUpdateCoordinator, german: bool
) -> str:
    heat_source_ids = {
        logical.logical_id
        for path in set(coordinator.resources) | set(coordinator.data or {})
        if (logical := logical_device_for_path(path)) is not None
        and logical.kind == "heat_source"
    }
    if len(heat_source_ids) == 1:
        logical_id = next(iter(heat_source_ids))
        if _is_heat_pump(coordinator, f"/heatSources/{logical_id}", logical_id):
            return "Wärmepumpe" if german else "Heat pump"
    return "Wärmeerzeuger" if german else "Heat generator"


def _logical_name(kind: str, logical_id: str, german: bool) -> str:
    """Return a localized generic name for one dynamic PointT group."""
    label = {
        "heating_circuit": "Heizkreis" if german else "Heating circuit",
        "hot_water_circuit": "Warmwasser" if german else "Hot water",
        "heat_source": "Wärmeerzeuger" if german else "Heat generator",
    }[kind]
    suffix = re.search(r"(\d+)$", logical_id)
    return f"{label} {suffix.group(1) if suffix else logical_id}"


def _localized_generic_name(
    configured_name: str | None, kind: str, german: bool
) -> str | None:
    """Translate vendor default names while preserving genuine custom names."""
    if configured_name is None:
        return None
    normalized = configured_name.casefold().strip()
    generic_names = {
        "heating_circuit": {"heizkreis", "heating circuit"},
        "hot_water_circuit": {"warmwasser", "hot water"},
        "heat_source": {"wärmeerzeuger", "waermeerzeuger", "heat generator"},
    }[kind]
    if normalized not in generic_names:
        return configured_name
    return {
        "heating_circuit": "Heizkreis" if german else "Heating circuit",
        "hot_water_circuit": "Warmwasser" if german else "Hot water",
        "heat_source": "Wärmeerzeuger" if german else "Heat generator",
    }[kind]


def _resource_string(
    coordinator: BoschBuderusDataUpdateCoordinator, path: str
) -> str | None:
    snapshot = (coordinator.data or {}).get(path)
    if snapshot is None or not isinstance(snapshot.resource.value, str):
        return None
    value = snapshot.resource.value.strip()
    return value or None


def _system_info_values(
    coordinator: BoschBuderusDataUpdateCoordinator,
) -> tuple[dict[str, str], ...]:
    snapshot = (coordinator.data or {}).get("/system/info")
    if snapshot is None:
        return ()
    return tuple(
        {
            key: value.strip()
            for key, value in item.items()
            if isinstance(value, str) and value.strip()
        }
        for item in snapshot.resource.values
        if isinstance(item, dict)
    )


def _first_value(values: tuple[dict[str, str], ...], *keys: str) -> str | None:
    for key in keys:
        for item in values:
            if value := item.get(key):
                return value
    return None
