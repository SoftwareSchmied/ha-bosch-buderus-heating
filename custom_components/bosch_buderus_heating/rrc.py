"""Explicit RRC zone contracts, separate from ConnectKey room zones."""

from __future__ import annotations

import base64
import binascii
import math
import re

from .pointt import Resource
from .pointt.models import JsonScalar
from .unit_contracts import canonical_unit

RRC_ID = r"(?:0|[1-9][0-9]{0,3})"
ZONE = re.compile(rf"^/zones/zn({RRC_ID})(?:/|$)")
PROGRAM = re.compile(rf"^/programs/pg({RRC_ID})(?:/|$)")
ZONE_LIST = "/zones/list"
PROGRAM_LIST = "/programs/list"
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
        or not (available(resource) or mode_locked(resource))
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


def listed_ids(resource: Resource) -> tuple[int, ...] | None:
    """Only the RRC value array is an ID source, never a guessed ID range."""
    resource_type = {ZONE_LIST: "zoneArray", PROGRAM_LIST: "programArray"}.get(
        resource.path
    )
    if (
        resource_type is None
        or resource.metadata.resource_type != resource_type
        or not available(resource)
        or not resource.has_value
        or not isinstance(resource.value, list)
        or len(resource.value) > 128
    ):
        return None
    identifiers: list[int] = []
    for item in resource.value:
        if not isinstance(item, dict):
            return None
        identifier = item.get("id")
        if type(identifier) is not int or not 0 <= identifier <= 9999:
            return None
        if identifier in identifiers:
            return None
        identifiers.append(identifier)
    return tuple(identifiers)


def listed_paths(resource: Resource) -> tuple[str, ...]:
    identifiers = listed_ids(resource)
    if identifiers is None:
        return ()
    root = "/zones/zn" if resource.path == ZONE_LIST else "/programs/pg"
    return tuple(f"{root}{identifier}" for identifier in identifiers)


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


def name(path: str, language: str, *, control: bool = False) -> str | None:
    field = scalar_field(path)
    labels = {
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
    if field is None:
        return None
    german = language.casefold().startswith("de")
    label = labels[field][0 if german else 1]
    if control:
        label = f"{label} einstellen" if german else f"Set {label.lower()}"
    return label
