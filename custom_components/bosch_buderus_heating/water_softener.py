"""Reviewed water-softener capabilities, independent of Home Assistant UI.

The documented paths and meanings are not a claim of verification on
physical equipment.
"""

from __future__ import annotations

import math
import re
import struct
from collections.abc import Mapping
from dataclasses import dataclass

from .pointt import Gateway, Resource

SOFTENER_ROOT = "/swCircuits"
SOFTENER_CIRCUIT = re.compile(r"^/swCircuits/((?!list$)[^/]+)$")
PHASES = (
    "normal",
    "refill",
    "brine_preparation",
    "brine_draw",
    "backwash",
    "rinse",
    "end",
)


@dataclass(frozen=True, slots=True)
class SoftenerCapability:
    """One known scalar contract; no runtime value or device identifier."""

    suffix: str
    name_de: str
    name_en: str
    kind: str = "number"
    units: tuple[str, ...] = ()
    fallback_unit: str | None = None
    maximum: float | None = None
    poll: str = "slow"


SOFTENER_CAPABILITIES = (
    SoftenerCapability(
        "sensor/saltLevel",
        "Salzfüllstand",
        "Salt level",
        units=("%",),
        fallback_unit="%",
        maximum=100,
    ),
    SoftenerCapability(
        "sensor/saltLevelAlarm",
        "Salz-Warnschwelle",
        "Salt alert threshold",
        units=("%",),
        fallback_unit="%",
        maximum=100,
        poll="control",
    ),
    SoftenerCapability(
        "sensor/availableWater",
        "Verfügbares Weichwasser",
        "Available soft water",
        units=("l", "L", "m3", "m³"),
        fallback_unit="l",
    ),
    SoftenerCapability(
        "sensor/waterFlow",
        "Wasserdurchfluss",
        "Water flow",
        units=("l/min", "L/min"),
        fallback_unit="l/min",
        poll="fast",
    ),
    SoftenerCapability(
        "regeneration/enabled",
        "Regeneration aktiv",
        "Regeneration active",
        kind="binary",
        poll="fast",
    ),
    SoftenerCapability(
        "regeneration/phase",
        "Regenerationsphase",
        "Regeneration phase",
        kind="phase",
        poll="fast",
    ),
    SoftenerCapability(
        "regeneration/totalProgressStatus",
        "Regenerationsfortschritt",
        "Regeneration progress",
        units=("%",),
        fallback_unit="%",
        maximum=100,
        poll="fast",
    ),
    SoftenerCapability(
        "regeneration/totalRemainingTime",
        "Verbleibende Regenerationszeit",
        "Regeneration remaining time",
        units=("s", "seconds", "min", "mins", "h", "hours"),
        fallback_unit="s",
        poll="fast",
    ),
    SoftenerCapability(
        "regeneration/timeUntilNext",
        "Zeit bis zur nächsten Regeneration",
        "Time until next regeneration",
        units=("s", "seconds", "min", "mins", "h", "hours", "d", "days"),
        fallback_unit="d",
    ),
    SoftenerCapability(
        "regeneration/singleRegAppoint",
        "Geplante Regeneration",
        "Scheduled regeneration",
        units=("h", "hours", "d", "days"),
        fallback_unit="d",
        poll="control",
    ),
    SoftenerCapability(
        "holidayMode/enabled",
        "Urlaubsmodus",
        "Holiday mode",
        kind="binary",
        poll="control",
    ),
)
_BY_SUFFIX = {item.suffix: item for item in SOFTENER_CAPABILITIES}
_PROFILE_MODELS = re.compile(
    r"(?<![a-z0-9])(?:WE800i|WE500|WE400|8000iS|5000S|4000S)(?![a-z0-9])", re.IGNORECASE
)


def is_water_softener(
    gateway: Gateway | None, resources: Mapping[str, Resource]
) -> bool:
    """Use explicit model evidence; WX100 hardware alone is not sufficient."""
    values = [gateway.model, gateway.device_type] if gateway is not None else []
    appliance = resources.get("/system/appliance/model")
    if (
        appliance is not None
        and appliance.has_value
        and isinstance(appliance.value, str)
    ):
        values.append(appliance.value)
    return any(
        value is not None
        and (
            value.strip().casefold() == "watersoftener" or _PROFILE_MODELS.search(value)
        )
        for value in values
    )


def softener_capability(path: str) -> SoftenerCapability | None:
    """Return only an exact known suffix below a reported circuit."""
    parts = path.split("/", 3)
    if len(parts) != 4 or parts[1] != "swCircuits" or not parts[2]:
        return None
    return _BY_SUFFIX.get(parts[3])


def softener_unit(resource: Resource) -> str | None:
    """Accept explicit compatible units or a documented app default."""
    capability = softener_capability(resource.path)
    if capability is None or capability.kind != "number":
        return None
    unit = (resource.metadata.unit or "").strip()
    if unit in capability.units:
        return unit
    return capability.fallback_unit if unit in {"", "none"} else None


def softener_schema_issue(resource: Resource) -> str | None:
    """Explain unsupported schemas without leaking current values."""
    capability = softener_capability(resource.path)
    if capability is None:
        return "unknown_capability"
    expected = "floatValue" if capability.kind == "number" else "stringValue"
    if resource.metadata.resource_type != expected or resource.references:
        return "unsupported_resource_type"
    if resource.values or isinstance(resource.value, dict | list):
        return "unsupported_value_shape"
    if capability.kind == "number" and softener_unit(resource) is None:
        return "unconfirmed_unit"
    return None


def softener_value(resource: Resource) -> float | str | bool | None:
    """Validate each sample, including after metadata changes and failures."""
    capability = softener_capability(resource.path)
    if (
        capability is None
        or softener_schema_issue(resource) is not None
        or not resource.has_value
    ):
        return None
    value = resource.value
    if capability.kind == "binary":
        return {"on": True, "off": False}.get(value) if isinstance(value, str) else None
    if capability.kind == "phase":
        # Phases are compared case-insensitively. Keep HA's existing keys
        # and accept only casing variants of the documented ASCII codes.
        if not isinstance(value, str) or not value.isascii():
            return None
        normalized = value.lower()
        return normalized if normalized in PHASES else None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    if capability.maximum is not None and value > capability.maximum:
        return None
    if (
        not resource.metadata.numeric_error_states_valid
        or value in resource.metadata.numeric_error_values
    ):
        return None
    return float(value)


def low_salt(level: Resource, threshold: Resource | None) -> bool | None:
    """Compare fresh caller-supplied values from the same circuit only.

    Use a Float-to-integer comparison and a default for an absent threshold.
    W006 and E7 are not substituted for a numeric threshold.
    """
    if (
        threshold is not None
        and threshold.path != level.path.removesuffix("saltLevel") + "saltLevelAlarm"
    ):
        return None
    actual = softener_value(level)
    limit = 20.0 if threshold is None else softener_value(threshold)
    if type(actual) is not float or type(limit) is not float:
        return None
    # Treat both values as IEEE-754 Float before truncation.
    return int(struct.unpack("f", struct.pack("f", actual))[0]) <= int(
        struct.unpack("f", struct.pack("f", limit))[0]
    )
