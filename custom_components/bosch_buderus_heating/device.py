"""Home Assistant device metadata derived from PointT resources."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import cast

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

from . import rrc, rrc_devices
from .configuration import ConfigurationError
from .configuration_access import EntryConfiguration
from .const import CONF_BRAND, DEFAULT_MANUFACTURER, DOMAIN
from .coordinator import BoschBuderusDataUpdateCoordinator
from .ha_compat import device_by_identifier, via_device
from .i18n import localize
from .pointt import Resource
from .resource_catalog import configured_device_name, logical_device_for_path

_UNKNOWN_HEAT_PUMP_TYPES = frozenset(
    {"", "none", "not_available", "undefined", "unknown"}
)
_GROUP_SEPARATOR = " \N{EN DASH} "
_CONTROLLER_MODEL_PATTERN = re.compile(
    r"(?<![A-Z0-9])MX(?:300|400)(?![A-Z0-9])", re.IGNORECASE
)
# C8: device names never carry the brand (the manufacturer field does).
_BRAND_WORDS = re.compile(r"\b(?:Bosch|Buderus)\b[\s/]*", re.IGNORECASE)
_GATEWAY_MODEL_PATTERN = re.compile(
    r"(?<![A-Z0-9])K(?:30|40)(?:RF)?(?:[_ -]V\d+)?(?![A-Z0-9])",
    re.IGNORECASE,
)


def device_info_for_resource(
    coordinator: BoschBuderusDataUpdateCoordinator,
    resource_path: str,
) -> DeviceInfo:
    """Build metadata for the device selected by the plant's resource binding."""
    if (
        (device_id := rrc_devices.listed_device(resource_path)) is not None
        and is_rrc_plant(coordinator)
        and (info := radio_device_info(coordinator, device_id)) is not None
    ):
        return info
    gateway = coordinator.gateway
    binding = coordinator.plant_model.bind_resource(resource_path)
    identifiers = {(DOMAIN, binding.device.identifier)}
    entry = coordinator.config_entry
    known = (
        entry is not None
        and device_by_identifier(
            dr.async_get(coordinator.hass),
            (DOMAIN, binding.device.identifier),
            entry.entry_id,
        )
        is not None
    )
    if getattr(coordinator, "read_only_view", False) is True and known:
        # P49b: the local readings lack the identity readings (model, serial
        # number, versions); a known device keeps what the registry holds.
        return DeviceInfo(identifiers=identifiers)
    manufacturer = _manufacturer(coordinator)
    system_info = _system_info_values(coordinator)
    model, model_id = _system_model(gateway.model, gateway.device_type, system_info)
    rrc_plant = is_rrc_plant(coordinator)
    if rrc_plant:
        # E05: a room controller's model only from the cloud's own data.
        model = _rrc_model(coordinator) or (
            rrc.PRODUCT_TYPE
            if model == "Heating system" or model.casefold() == rrc.GATEWAY_DEVICE_TYPE
            else model
        )
    serial_number = _clean_serial(
        _resource_string(coordinator, "/gateway/serialId")
    ) or _first_serial(system_info, "ProductSerialNumber", "ModuleSerialNumber")
    info = DeviceInfo(identifiers=identifiers, manufacturer=manufacturer)
    if not (known and _system_info_unread(coordinator, system_info)):
        # D34: a start from the stored inventory has no values of
        # ``/system/info``; a known device then keeps the model the registry
        # holds instead of a fallback from the gateway listing.
        info["model"] = model
        info["model_id"] = model_id
    sw_version = (
        _resource_string(coordinator, "/gateway/versionFirmware")
        or gateway.firmware_version
        or _first_value(system_info, "Ver", "SwIdenStr")
    )
    hw_version = _resource_string(
        coordinator, "/gateway/versionHardware"
    ) or _first_value(system_info, "HwVersion", "ModuleHwIdentStr")
    # Without a reading the key is left out for a known device, so the
    # registry keeps the last known version (as for the serial number).
    if sw_version is not None or not known:
        info["sw_version"] = sw_version
    if hw_version is not None or not known:
        info["hw_version"] = hw_version
    if serial_number:
        # D34: without a reading (the stored inventory keeps no serial
        # number) the key is left out, so the registry keeps the known one.
        info["serial_number"] = serial_number
    if not known:
        # C8: a new device gets a name without the brand (the manufacturer
        # field still names it); an existing device keeps its name.
        info["name"] = (
            rrc_devices.type_label(
                rrc_devices.CONTROLLER_TYPE, coordinator.hass.config.language
            )
            if rrc_plant
            else neutral_device_name(model)
        )
    return info


def is_rrc_plant(coordinator: BoschBuderusDataUpdateCoordinator) -> bool:
    """C8: the gateway is an RRC 2.0 room controller, by its readings.

    Called for every entity's device, so the cheap evidence comes first and
    the readings are not copied: the current snapshot of a path counts
    before its discovered resource, as in ``rrc.profile_evidence``.
    """
    if rrc.profile_evidence(coordinator.gateway.device_type, {}):
        return True
    data = coordinator.data or {}
    resources = coordinator.resources
    lists: dict[str, Resource] = {}
    for path in (rrc.PRODUCT_TYPE_PATH, rrc.ZONE_LIST):
        if (snapshot := data.get(path)) is not None:
            lists[path] = snapshot.resource
        elif (resource := resources.get(path)) is not None:
            lists[path] = resource
    if rrc.profile_evidence(None, lists):
        return True
    return any(
        path.startswith("/zones/zn") and rrc.ZONE.match(path)
        for paths in (resources, data)
        for path in paths
    )


def _rrc_model(coordinator: BoschBuderusDataUpdateCoordinator) -> str | None:
    """The product type the controller reports, if it reports one."""
    value = _resource_string(coordinator, rrc.PRODUCT_TYPE_PATH)
    if value is not None and 1 <= len(value) <= 32 and value.isprintable():
        return _BRAND_WORDS.sub("", value).strip(" /-") or None
    return None


def _device_list(coordinator: BoschBuderusDataUpdateCoordinator) -> dict[str, Resource]:
    snapshot = (coordinator.data or {}).get(rrc.DEVICE_LIST)
    resource = (
        snapshot.resource
        if snapshot is not None and snapshot.resource.has_value
        else coordinator.resources.get(rrc.DEVICE_LIST)
    )
    return {rrc.DEVICE_LIST: resource} if resource is not None else {}


def radio_device_info(
    coordinator: BoschBuderusDataUpdateCoordinator, device_id: int
) -> DeviceInfo | None:
    """E07: a radio device of a room controller as its own device.

    It hangs below the controller (its via device) and is identified by the
    gateway and its list ID. The controller's own list entry returns
    ``None``: its values belong to the controller's device.
    """
    device = rrc_devices.find(_device_list(coordinator), device_id)
    if device is not None and device.is_controller:
        return None
    gateway_identifier = coordinator.plant_model.gateway_device.identifier
    identifier = (DOMAIN, rrc_devices.identifier(gateway_identifier, device_id))
    language = coordinator.hass.config.language
    info = DeviceInfo(
        identifiers={identifier},
        manufacturer=_manufacturer(coordinator),
        model=rrc_devices.type_label(device.kind if device else None, language),
    )
    entry = coordinator.config_entry
    registry = dr.async_get(coordinator.hass)
    if entry is not None:
        info.update(
            cast(
                DeviceInfo,
                via_device(registry, (DOMAIN, gateway_identifier), entry.entry_id),
            )
        )
    firmware = _resource_string(
        coordinator, f"/devices/device{device_id}/versionFirmware"
    )
    if firmware is not None and len(firmware) <= 32 and firmware.isprintable():
        info["sw_version"] = firmware
    if entry is not None and device_by_identifier(registry, identifier, entry.entry_id):
        return info  # an existing device keeps its name and area
    info["name"] = rrc_devices.device_name(
        device or rrc_devices.RadioDevice(device_id, None, None, None, None), language
    )
    if device is not None and device.zone:
        zone = _resource_string(coordinator, f"/zones/zn{device.zone}/name")
        if (area := rrc.display_name(zone)) is not None:
            info["suggested_area"] = area
    return info


def neutral_device_name(model: str) -> str:
    """The name of a new heating device: its controller model, no brand."""
    if model == "Heating system":
        return "Heating"
    return _BRAND_WORDS.sub("", model).strip(" /-") or "Heating"


def registered_device(hass: HomeAssistant, identifier: str) -> bool:
    """C8: a device of this integration with the identifier already exists.

    Its name stays as it is; only a new device gets the neutral name.
    """
    wanted = (DOMAIN, identifier)
    # A plain scan: identifiers are no longer unique across config entries,
    # so the global single-device lookup is deprecated on current releases.
    devices = dr.async_get(hass).devices
    entries: Iterable[dr.DeviceEntry] = (
        devices.values() if isinstance(devices, Mapping) else devices
    )
    return any(wanted in device.identifiers for device in entries)


def grouped_entity_name(
    coordinator: BoschBuderusDataUpdateCoordinator,
    resource_path: str,
    name: str,
) -> str:
    """Prefix an entity name with its dynamic logical PointT group."""
    language = coordinator.hass.config.language
    if (
        device_id := rrc_devices.listed_device(resource_path)
    ) is not None and is_rrc_plant(coordinator):
        # E07: a radio device is its own device and needs no prefix; the
        # controller's own entry is named after the controller.
        device = rrc_devices.find(_device_list(coordinator), device_id)
        if device is None or not device.is_controller:
            return name
        label = rrc_devices.type_label(rrc_devices.CONTROLLER_TYPE, language)
        return f"{label}{_GROUP_SEPARATOR}{name}"
    logical = coordinator.plant_model.bind_resource(resource_path).group
    if logical is not None:
        group = _configured_logical_name(coordinator, resource_path, logical.logical_id)
        group = _localized_generic_name(group, logical.kind, language)
        if logical.kind == "heat_source" and _is_heat_pump(
            coordinator, resource_path, logical.logical_id
        ):
            group = group or _heat_pump_name(coordinator, logical.logical_id, language)
        group = group or _logical_name(logical.kind, logical.logical_id, language)
        return f"{group}{_GROUP_SEPARATOR}{name}"
    if resource_path == "/gateway" or resource_path.startswith("/gateway/"):
        group = localize(language, "Gateway", "Gateway")
    elif resource_path == "/system" or resource_path.startswith("/system/"):
        group = localize(language, "Anlage", "System")
    elif resource_path == "/heatSources" or resource_path.startswith("/heatSources/"):
        group = _central_heat_source_name(coordinator, language)
    elif resource_path == "/solarCircuits" or resource_path.startswith(
        "/solarCircuits/"
    ):
        group = localize(language, "Solar", "Solar")
    elif resource_path == "/pool" or resource_path.startswith("/pool/"):
        group = localize(language, "Pool", "Pool")
    elif resource_path == "/ventilation" or resource_path.startswith("/ventilation/"):
        group = localize(language, "Lüftung", "Ventilation")
    elif resource_path == "/zones" or resource_path.startswith("/zones/"):
        group = localize(language, "Zone", "Zone")
    elif resource_path == "/pv" or resource_path.startswith("/pv/"):
        group = localize(language, "Photovoltaik", "Photovoltaics")
    else:
        group = localize(language, "Anlage", "System")
    return f"{group}{_GROUP_SEPARATOR}{name}"


def _manufacturer(coordinator: BoschBuderusDataUpdateCoordinator) -> str:
    for path in ("/system/brand", "/gateway/brand"):
        manufacturer = _known_brand(_resource_string(coordinator, path))
        if manufacturer is not None:
            return manufacturer

    if (hint := _known_brand(getattr(coordinator, "brand_hint", None))) is not None:
        return hint  # read by the local setup of a plant without a cloud
    config_entry = coordinator.config_entry
    try:
        configured_brand = (
            EntryConfiguration(
                config_entry, getattr(coordinator, "connection_id", None)
            )
            .cloud_data()
            .get(CONF_BRAND)
            if config_entry is not None
            else None
        )
    except ConfigurationError:
        # A view without a cloud access (P49b, R1): no configured brand.
        configured_brand = None
    manufacturer = _known_brand(
        configured_brand if isinstance(configured_brand, str) else None
    )
    return manufacturer or DEFAULT_MANUFACTURER


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


def _system_model(
    gateway_model: str | None,
    gateway_type: str | None,
    system_info: tuple[dict[str, str], ...],
) -> tuple[str, str | None]:
    """Return a controller model without mistaking a K-series gateway for it."""
    controller_entries: dict[str, list[dict[str, str]]] = {}
    for item in system_info:
        for key in ("ProductName", "ModuleHwIdentStr"):
            value = item.get(key)
            if (
                value is None
                or (match := _CONTROLLER_MODEL_PATTERN.search(value)) is None
            ):
                continue
            controller_entries.setdefault(match.group(0).upper(), []).append(item)
            break

    if len(controller_entries) == 1:
        model, entries = next(iter(controller_entries.items()))
        return model, _first_value(tuple(entries), "ProductTtn", "ModuleTtn")

    for value in (gateway_model, gateway_type):
        if value is not None and (match := _CONTROLLER_MODEL_PATTERN.search(value)):
            return match.group(0).upper(), None

    for value in (gateway_model, gateway_type):
        if value is None or _GATEWAY_MODEL_PATTERN.search(value):
            continue
        return _display_model(value), None

    return "Heating system", None


def _configured_logical_name(
    coordinator: BoschBuderusDataUpdateCoordinator,
    resource_path: str,
    logical_id: str,
) -> str | None:
    root = resource_path.strip("/").split("/", 1)[0]
    value = _resource_string(coordinator, f"/{root}/{logical_id}/name")
    if value is None:
        return None
    if rrc.ZONE.match(resource_path):
        # #113: RRC zone names are Base64-encoded UTF-8, not UTF-16.
        return rrc.display_name(value)
    return configured_device_name(value)


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
    coordinator: BoschBuderusDataUpdateCoordinator, logical_id: str, language: str
) -> str:
    heat_source_ids = {
        logical.logical_id
        for path in set(coordinator.resources) | set(coordinator.data or {})
        if (logical := logical_device_for_path(path)) is not None
        and logical.kind == "heat_source"
    }
    label = localize(language, "Wärmepumpe", "Heat pump")
    if len(heat_source_ids) <= 1:
        return label
    suffix = re.search(r"(\d+)$", logical_id)
    return f"{label} {suffix.group(1) if suffix else logical_id}"


def _central_heat_source_name(
    coordinator: BoschBuderusDataUpdateCoordinator, language: str
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
            return localize(language, "Wärmepumpe", "Heat pump")
    return localize(language, "Wärmeerzeuger", "Heat generator")


def _kind_label(kind: str, language: str) -> str:
    """Return the localized generic name of one kind of dynamic PointT group."""
    return {
        "heating_circuit": localize(language, "Heizkreis", "Heating circuit"),
        "hot_water_circuit": localize(language, "Warmwasser", "Hot water"),
        "heat_source": localize(language, "Wärmeerzeuger", "Heat generator"),
        "solar_circuit": localize(language, "Solarkreis", "Solar circuit"),
        "ventilation_zone": localize(language, "Lüftungszone", "Ventilation zone"),
        "room_zone": localize(language, "Zone", "Zone"),
        "rrc_zone": localize(language, "RRC-Zone", "RRC zone"),
        "room_device": localize(language, "Raumgerät", "Room device"),
        "water_softener": localize(language, "Enthärter", "Water softener"),
    }[kind]


def _logical_name(kind: str, logical_id: str, language: str) -> str:
    """Return a localized generic name for one dynamic PointT group."""
    label = _kind_label(kind, language)
    suffix = re.search(r"(\d+)$", logical_id)
    return f"{label} {suffix.group(1) if suffix else logical_id}"


def _localized_generic_name(
    configured_name: str | None, kind: str, language: str
) -> str | None:
    """Translate vendor default names while preserving genuine custom names."""
    if configured_name is None:
        return None
    normalized = configured_name.casefold().strip()
    generic_names = {
        "heating_circuit": {"heizkreis", "heating circuit"},
        "hot_water_circuit": {"warmwasser", "hot water"},
        "heat_source": {"wärmeerzeuger", "waermeerzeuger", "heat generator"},
        "solar_circuit": {"solarkreis", "solar circuit"},
        "ventilation_zone": {"lüftungszone", "ventilation zone"},
        "room_zone": {"zone"},
        "rrc_zone": {"rrc zone", "rrc-zone"},
        "room_device": {"raumgerät", "room device"},
        "water_softener": {"enthärter", "water softener"},
    }[kind]
    if normalized not in generic_names:
        return configured_name
    return _kind_label(kind, language)


def _resource_string(
    coordinator: BoschBuderusDataUpdateCoordinator, path: str
) -> str | None:
    snapshot = (coordinator.data or {}).get(path)
    if snapshot is None or not isinstance(snapshot.resource.value, str):
        return None
    value = snapshot.resource.value.strip()
    return value or None


def _system_info_unread(
    coordinator: BoschBuderusDataUpdateCoordinator,
    system_info: tuple[dict[str, str], ...],
) -> bool:
    """``/system/info`` is known but holds no values now (for example a start
    from the stored inventory, which keeps no values of it)."""
    path = "/system/info"
    return not system_info and (
        path in (coordinator.data or {}) or path in coordinator.resources
    )


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


# Padding some devices report instead of a serial number: 0xFF bytes and the
# replacement character of undecodable bytes.
_SERIAL_FILLER = frozenset({"\ufffd", "\xff"})


def _clean_serial(value: object) -> str | None:
    """A serial number without control or filler characters; None when
    nothing printable remains."""
    if not isinstance(value, str):
        return None
    cleaned = "".join(
        character
        for character in value
        if character.isprintable() and character not in _SERIAL_FILLER
    ).strip()
    return cleaned or None


def _first_serial(values: tuple[dict[str, str], ...], *keys: str) -> str | None:
    for key in keys:
        for item in values:
            if (serial := _clean_serial(item.get(key))) is not None:
                return serial
    return None


def _first_value(values: tuple[dict[str, str], ...], *keys: str) -> str | None:
    for key in keys:
        for item in values:
            if value := item.get(key):
                return value
    return None
