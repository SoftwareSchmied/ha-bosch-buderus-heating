"""Explicit RRC zone contracts, separate from ConnectKey room zones."""

from __future__ import annotations

import base64
import binascii
import math
import re
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Final

from .i18n import localize
from .pointt import Resource
from .pointt.models import JsonScalar
from .unit_contracts import canonical_unit

RRC_ID = r"(?:0|[1-9][0-9]{0,3})"
ZONE = re.compile(rf"^/zones/zn({RRC_ID})(?:/|$)")
PROGRAM = re.compile(rf"^/programs/pg({RRC_ID})(?:/|$)")
ZONE_LIST = "/zones/list"
PROGRAM_LIST = "/programs/list"
# C8 (CT-B02, CT-E01): the radio devices of an RRC controller, one list.
DEVICE_LIST = "/devices/list"
RADIO_DEVICE = re.compile(rf"^/devices/device({RRC_ID})(?:/|$)")
RADIO_FIRMWARE = re.compile(rf"^/devices/device{RRC_ID}/versionFirmware$")
# CT-A03: the vendor app accepts a controller with exactly this product type.
PRODUCT_TYPE_PATH = "/gateway/productType"
PRODUCT_TYPE = "RRC 2.0"
# The device type the cloud's gateway list reports for these controllers.
GATEWAY_DEVICE_TYPE = "rrc2"
MODE_PATTERN = rf"^/zones/zn{RRC_ID}/userMode$"
MANUAL_PATTERN = rf"^/zones/zn{RRC_ID}/manualTemperatureHeating$"
MODES = frozenset({"manual", "clock"})
# #113: the TC100.2 advertises no allowedValues for userMode; its app offers
# exactly these two modes.
FALLBACK_MODES: tuple[JsonScalar, ...] = ("manual", "clock")
TEMPERATURES = (
    "temperatureActual",
    "temperatureHeatingSetpoint",
    "manualTemperatureHeating",
    "clockOverride/temperatureHeating",
)
ZONE_FIELDS = (*TEMPERATURES, "userMode", "clockProgram")


def scalar_field(path: str) -> str | None:
    """Match only canonical numeric zone identifiers and documented leaves."""
    match = ZONE.match(path)
    field = path[match.end() :] if match else ""
    return field if field in ZONE_FIELDS else None


def program_list_expected(paths: Iterable[str]) -> bool:
    """Whether a zone selects a program, which needs the program list."""
    return any(scalar_field(path) == "clockProgram" for path in paths)


def is_control(path: str) -> bool:
    return scalar_field(path) in {"userMode", "manualTemperatureHeating"}


def available(resource: Resource) -> bool:
    """Missing optional flags are allowed; false or malformed flags are not."""
    metadata = resource.metadata
    return (
        metadata.availability_flags_valid
        and metadata.used is not False
        and metadata.available is not False
    )


def mode_locked(resource: Resource) -> bool:
    """#113: a used but unavailable mode keeps its value and is not writable.

    The app shows such a mode as locked, for example while the system cools.
    Only ``userMode`` is read this way; other fields stay unavailable.
    """
    metadata = resource.metadata
    return (
        scalar_field(resource.path) == "userMode"
        and metadata.availability_flags_valid
        and metadata.used is True
        and metadata.available is False
    )


def program_unused(resource: Resource) -> bool:
    """#113 (RRC-03): an assigned program that does not act at the moment.

    The TC100.2 sends ``clockProgram`` with ``used`` false while its zone runs
    manually. The vendor app only hides the program then and still assigns
    programs in this state, so the value, its sensor and the selection stay.
    This follows the vendor app; a hardware confirmation is still pending.
    """
    metadata = resource.metadata
    return (
        scalar_field(resource.path) == "clockProgram"
        and metadata.availability_flags_valid
        and metadata.used is False
        and metadata.available is not False
    )


def allowed_values(resource: Resource) -> tuple[JsonScalar, ...]:
    """The advertised values; a mode without any allowedValues key falls back.

    An empty or different list stays as advertised, so it keeps the strict
    rules.
    """
    metadata = resource.metadata
    if (
        scalar_field(resource.path) == "userMode"
        and metadata.allowed_values_missing
        and not metadata.allowed_values
    ):
        return FALLBACK_MODES
    return metadata.allowed_values


def scalar_value(resource: Resource) -> str | int | float | None:
    """Keep native precision and reject sentinels and malformed metadata."""
    field = scalar_field(resource.path)
    metadata = resource.metadata
    value = resource.value
    if (
        field is None
        or not (
            available(resource) or mode_locked(resource) or program_unused(resource)
        )
        or not resource.has_value
        or resource.has_values
        or resource.references
    ):
        return None
    if field == "userMode":
        return (
            value
            if (
                metadata.resource_type == "stringValue"
                and metadata.unit in (None, "")
                and isinstance(value, str)
                and value in MODES
            )
            else None
        )
    if (
        metadata.resource_type != "floatValue"
        or isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not metadata.numeric_error_states_valid
        or value in metadata.numeric_error_values
    ):
        return None
    for bound in (metadata.minimum, metadata.maximum):
        if bound is not None and not math.isfinite(bound):
            return None
    if (metadata.minimum is not None and value < metadata.minimum) or (
        metadata.maximum is not None and value > metadata.maximum
    ):
        return None
    if field == "clockProgram":
        return (
            int(value)
            if (
                metadata.unit in (None, "")
                and value == int(value)
                and 0 <= value <= 9999
            )
            else None
        )
    return (
        value
        if canonical_unit(metadata.unit) in {"C", "°C", "ºC"} and -50 <= value <= 100
        else None
    )


def display_value(resource: Resource) -> str | int | float | None:
    """The state shown in Home Assistant for a valid reading.

    #113 (RRC-06): a temporary setpoint of 0 means that no override is active,
    as in the vendor app. It is shown as unknown rather than as 0 °C. The
    reading itself stays valid, so its sensor exists before the first
    override. This follows the vendor app; a hardware confirmation is pending.
    """
    value = scalar_value(resource)
    if scalar_field(resource.path) == "clockOverride/temperatureHeating" and (
        value == 0
    ):
        return None
    return value


def control_issue(resource: Resource) -> str | None:
    """Share read, write and diagnostic checks without creating permissions."""
    if scalar_value(resource) is None:
        return "invalid_rrc_reading"
    metadata = resource.metadata
    if scalar_field(resource.path) == "userMode":
        if resource.value not in allowed_values(resource):
            return "current_option_not_advertised"
    elif not metadata.step_size_valid or (
        metadata.step_size is not None
        and (not math.isfinite(metadata.step_size) or metadata.step_size <= 0)
    ):
        return "invalid_step"
    return None


def list_issue(resource: Resource) -> str | None:
    """Why an RRC list supplies no IDs, or ``None`` when it does.

    An empty list is valid and supplies no IDs. Only the value array is an ID
    source, never a guessed ID range.
    """
    parsed = _parse_list(resource)
    return parsed if isinstance(parsed, str) else None


def listed_ids(resource: Resource) -> tuple[int, ...] | None:
    """Only the RRC value array is an ID source, never a guessed ID range."""
    parsed = _parse_list(resource)
    return None if isinstance(parsed, str) else parsed


def _parse_list(resource: Resource) -> tuple[int, ...] | str:
    resource_type = {
        ZONE_LIST: "zoneArray",
        PROGRAM_LIST: "programArray",
        DEVICE_LIST: "deviceArray",
    }.get(resource.path)
    if resource_type is None:
        return "not_a_list"
    if resource.metadata.resource_type != resource_type:
        return "type"
    # #113 (RRC-04): the vendor app reads only the value of the program list
    # and ignores its used and available flags. The zone list keeps the rules.
    if resource.path != PROGRAM_LIST and not available(resource):
        return "flags"
    if (
        not resource.has_value
        or not isinstance(resource.value, list)
        or len(resource.value) > 128
    ):
        return "value"
    identifiers: list[int] = []
    for item in resource.value:
        if not isinstance(item, dict):
            return "ids"
        identifier = item.get("id")
        if type(identifier) is not int or not 0 <= identifier <= 9999:
            return "ids"
        if identifier in identifiers:
            return "ids"
        identifiers.append(identifier)
    return tuple(identifiers)


def listed_paths(resource: Resource) -> tuple[str, ...]:
    """The zones and programs of a list; a device list supplies no paths."""
    if resource.path == DEVICE_LIST:
        return ()
    identifiers = listed_ids(resource)
    if identifiers is None:
        return ()
    root = "/zones/zn" if resource.path == ZONE_LIST else "/programs/pg"
    return tuple(f"{root}{identifier}" for identifier in identifiers)


def is_product_type(value: object) -> bool:
    """CT-A03: the product type of an RRC 2.0 controller, in any case."""
    return isinstance(value, str) and value.strip().casefold() == (
        PRODUCT_TYPE.casefold()
    )


def profile_evidence(
    device_type: str | None, resources: Mapping[str, Resource]
) -> tuple[str, ...]:
    """C8: what shows that a gateway is an RRC 2.0 room controller.

    Only resources and the reported device type count, never a model name;
    the evidence selects probes and device names, it offers nothing.
    """
    evidence: list[str] = []
    if isinstance(device_type, str) and (
        device_type.strip().casefold() == GATEWAY_DEVICE_TYPE
    ):
        evidence.append("device_type")
    product = resources.get(PRODUCT_TYPE_PATH)
    if product is not None and is_product_type(product.value):
        evidence.append("product_type")
    if any(ZONE.match(path) for path in resources) or (
        (zones := resources.get(ZONE_LIST)) is not None
        and listed_ids(zones) is not None
    ):
        evidence.append("zones")
    return tuple(evidence)


def in_device_scope(path: str) -> bool:
    """C8 (CT-B02): the only ``/devices`` paths read on an RRC controller.

    The device list supplies type, zone, battery and signal of every radio
    device; per device only its firmware version is read. Everything else
    below ``/devices`` (the device keys, settings and valve details) is not
    read: it costs most of the discovery budget and feeds no entity.
    """
    return (
        path in ("/devices", DEVICE_LIST) or RADIO_FIRMWARE.fullmatch(path) is not None
    )


def device_firmware_paths(resource: Resource, limit: int) -> tuple[str, ...]:
    """The firmware leaf of each listed device, at most ``limit`` of them."""
    if resource.path != DEVICE_LIST:
        return ()
    identifiers = listed_ids(resource) or ()
    return tuple(
        f"/devices/device{identifier}/versionFirmware"
        for identifier in identifiers[:limit]
    )


def catalog_paths(path: str) -> tuple[str, ...]:
    if re.fullmatch(rf"/zones/zn{RRC_ID}", path):
        return tuple(f"{path}/{field}" for field in ZONE_FIELDS)
    if re.fullmatch(rf"/programs/pg{RRC_ID}", path):
        return (f"{path}/week",)
    return ()


def display_name(value: object) -> str | None:
    """#113: a zone or program name, which the controller sends in Base64.

    Canonical Base64 that decodes to printable UTF-8 text is shown decoded;
    any other text as it is. Empty, overlong or unprintable names are none.
    """
    if not isinstance(value, str) or not (candidate := value.strip()):
        return None
    try:
        encoded = base64.b64decode(candidate, validate=True)
        if base64.b64encode(encoded).decode("ascii") == candidate:
            decoded = encoded.decode("utf-8").strip()
            if _printable_name(decoded):
                return decoded
    except binascii.Error, ValueError:  # UnicodeDecodeError is a ValueError
        pass
    return candidate if _printable_name(candidate) else None


def _printable_name(value: str) -> bool:
    return 1 <= len(value) <= 64 and value.isprintable()


# German and English names of the zone leaves; other languages translate the
# English text in their i18n data file.
_LABELS: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        "temperatureActual": ("Raumtemperatur", "Room temperature"),
        "temperatureHeatingSetpoint": (
            "Aktiver Heizsollwert",
            "Active heating setpoint",
        ),
        "manualTemperatureHeating": (
            "Manueller Heizsollwert",
            "Manual heating setpoint",
        ),
        "clockOverride/temperatureHeating": (
            "Vorübergehender Heizsollwert",
            "Temporary heating setpoint",
        ),
        "userMode": ("Heizungsbetriebsart", "Heating mode"),
        "clockProgram": ("Gewähltes Zeitprogramm", "Selected heating schedule"),
    }
)
# The names of the controls, as whole texts so that a translation need not
# follow the German or English word order.
_CONTROL_LABELS: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        field: (f"{german} einstellen", f"Set {english.lower()}")
        for field, (german, english) in _LABELS.items()
    }
)


def name(path: str, language: str, *, control: bool = False) -> str | None:
    field = scalar_field(path)
    if field is None:
        return None
    return localize(language, *(_CONTROL_LABELS if control else _LABELS)[field])
