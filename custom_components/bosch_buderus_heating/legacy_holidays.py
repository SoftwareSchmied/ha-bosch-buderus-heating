"""Strict contracts for the separate ICOM and WDDW2 holiday resources."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from functools import lru_cache

from .pointt import Resource

ICOM_HOLIDAY_ROOT = "/system/holidayModes"
WDDW2_HOLIDAY_PATH = "/system/holidayMode"
ICOM_SLOT_PATTERN = re.compile(r"/system/holidayModes/hm([1-9][0-9]{0,2})")
ICOM_FIELDS = ("startStop", "assignedTo", "hcMode", "dhwMode", "fixTemperature")
ICOM_EMPTY_PERIODS = frozenset(
    {"2009-01-01/2009-01-01", "2016-01-01/2016-01-01", "2017-01-01/2017-01-01"}
)
ICOM_ACTIVE_PATTERN = re.compile(
    r"/(?:heatingCircuits/hc[1-9][0-9]*|dhwCircuits/dhw[1-9][0-9]*)/holidayMode/activated"
)
_CIRCUIT = re.compile(r"(?:hc|dhw)[1-9][0-9]*")
# Units an iCom ``fixTemperature`` reports for degrees Celsius (APP-12).
_CELSIUS_UNITS = frozenset({"C", "°C"})

# I2: the component list of the system. Only the component type (``Id``) and
# the token (``Tok``) are read, never a serial number, name or version.
SYSTEM_INFO_PATH = "/system/info"
# The manufacturer's app writes neither ``hcMode`` nor ``assignedTo`` of a
# holiday when the system has this controller (HMI800 HP Light). Taken from
# the app; no such system has been observed (expected, untested).
HMI800_HP_LIGHT_ID = "240"
# With token 16, the app hides the heating circuits of the tokens 24-27 from
# the holiday circuits (expected, untested).
CIRCUIT_REMOVAL_TOKEN = "16"
CIRCUIT_TOKENS: Mapping[str, str] = {
    "24": "hc1",
    "25": "hc2",
    "26": "hc3",
    "27": "hc4",
}
# Fields that are never written with a HMI800 HP Light.
HMI800_FIXED_FIELDS = ("hcMode", "assignedTo")
_COMPONENT_NUMBER = re.compile(r"[0-9]{1,6}")
_MAX_COMPONENTS = 64


@dataclass(frozen=True, slots=True)
class IcomComponents:
    """I2: the ``Id``/``Tok`` pairs of ``/system/info``, nothing else."""

    items: tuple[tuple[str, str | None], ...] = ()

    @property
    def hmi800_hp_light(self) -> bool:
        return any(item == HMI800_HP_LIGHT_ID for item, _token in self.items)

    @property
    def hidden_circuits(self) -> frozenset[str]:
        """Heating circuits the app does not offer for a holiday."""
        tokens = {token for _item, token in self.items if token is not None}
        if CIRCUIT_REMOVAL_TOKEN not in tokens:
            return frozenset()
        return frozenset(
            circuit for token, circuit in CIRCUIT_TOKENS.items() if token in tokens
        )


def _component_number(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value < 1_000_000:
        return str(value)
    if isinstance(value, str) and _COMPONENT_NUMBER.fullmatch(value.strip()):
        return value.strip()
    return None


def icom_components(resource: Resource | None) -> IcomComponents:
    """Read the bounded component numbers; anything else is ignored."""
    if resource is None or resource.path != SYSTEM_INFO_PATH:
        return IcomComponents()
    items: list[tuple[str, str | None]] = []
    for value in resource.values[:_MAX_COMPONENTS]:
        if not isinstance(value, Mapping):
            continue
        identifier = _component_number(value.get("Id"))
        if identifier is None:
            continue
        items.append((identifier, _component_number(value.get("Tok"))))
    return IcomComponents(tuple(items))


def is_icom_slot(path: str) -> bool:
    """Accept bounded numeric slot identifiers, never guessed slots."""
    return ICOM_SLOT_PATTERN.fullmatch(path) is not None


@lru_cache(maxsize=4096)
def is_legacy_holiday_path(path: str) -> bool:
    """Identify only the documented resources, including circuit activation."""
    return (
        path in (ICOM_HOLIDAY_ROOT, WDDW2_HOLIDAY_PATH)
        or is_icom_slot(path)
        or (
            is_icom_slot(path.rsplit("/", 1)[0])
            and path.rsplit("/", 1)[-1] in (*ICOM_FIELDS, "delete")
        )
        or ICOM_ACTIVE_PATTERN.fullmatch(path) is not None
    )


def icom_slots(resources: Mapping[str, Resource]) -> tuple[str, ...]:
    """Use fetched resources and advertised references, never an assumed count."""
    paths = set(resources)
    for resource in resources.values():
        paths.update(reference.path for reference in resource.references)
    slots = {
        path if is_icom_slot(path) else path.rsplit("/", 1)[0]
        for path in paths
        if is_icom_slot(path)
        or (
            is_icom_slot(path.rsplit("/", 1)[0])
            and path.rsplit("/", 1)[-1] in ICOM_FIELDS
        )
    }
    return tuple(sorted(slots, key=lambda path: int(path.rsplit("hm", 1)[-1])))


def icom_dates(resource: Resource | None) -> tuple[date, date] | None:
    """Read inclusive ISO dates; documented unused-slot markers are not events."""
    if (
        resource is None
        or resource.metadata.resource_type != "stringValue"
        or not resource.has_value
        or resource.has_values
        or not isinstance(resource.value, str)
        or resource.value in ICOM_EMPTY_PERIODS
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}/\d{4}-\d{2}-\d{2}", resource.value) is None
    ):
        return None
    try:
        start, end = (date.fromisoformat(part) for part in resource.value.split("/"))
    except ValueError:
        return None
    if not 2000 <= start.year <= end.year <= 2100 or end < start:
        return None
    return start, end


def icom_empty(resource: Resource | None) -> bool:
    """An absent or malformed date is not a free slot."""
    return (
        resource is not None
        and resource.metadata.resource_type == "stringValue"
        and resource.has_value
        and not resource.has_values
        and isinstance(resource.value, str)
        and resource.value in ICOM_EMPTY_PERIODS
    )


def icom_assignments(
    resource: Resource | None, *, allowed: bool = False
) -> tuple[str, ...] | None:
    """Validate explicit circuit lists without borrowing another slot's values."""
    if (
        resource is None
        or resource.metadata.resource_type != "arrayData"
        or resource.has_value
    ):
        return None
    values = resource.metadata.allowed_values if allowed else resource.values
    if not allowed and not resource.has_values:
        return None
    if len(values) > 64 or any(
        not isinstance(value, str) or _CIRCUIT.fullmatch(value) is None
        for value in values
    ):
        return None
    result = tuple(str(value) for value in values)
    return result if len(set(result)) == len(result) else None


def icom_mode(resource: Resource | None) -> str | None:
    """Return an exact printable enum token, without case conversion."""
    if (
        resource is None
        or resource.metadata.resource_type != "stringValue"
        or not resource.has_value
        or resource.has_values
        or not isinstance(resource.value, str)
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", resource.value) is None
    ):
        return None
    return resource.value


def icom_modes(resource: Resource | None) -> tuple[str, ...]:
    """Only explicitly writable, advertised options are offered."""
    if resource is None or not resource.metadata.writable:
        return ()
    if len(resource.metadata.allowed_values) > 64 or any(
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value) is None
        for value in resource.metadata.allowed_values
    ):
        return ()
    return tuple(
        value
        for value in resource.metadata.allowed_values
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value)
    )


def icom_temperature(resource: Resource | None) -> float | None:
    """ICOM fixTemperature is already expressed in degrees Celsius.

    APP-12: ``°C`` is accepted like ``C``; a missing or other unit is not.
    """
    if (
        resource is None
        or resource.metadata.resource_type != "floatValue"
        or resource.metadata.unit not in _CELSIUS_UNITS
        or not resource.has_value
        or resource.has_values
        or isinstance(resource.value, bool)
        or not isinstance(resource.value, int | float)
        or not math.isfinite(resource.value)
    ):
        return None
    return float(resource.value)


def legacy_active(resources: Mapping[str, Resource]) -> bool | None:
    """Prefer explicit state; an unknown token is never interpreted as off."""
    states: list[bool] = []
    for path, resource in resources.items():
        token = icom_mode(resource)
        if path == WDDW2_HOLIDAY_PATH and token in ("on", "off"):
            states.append(token == "on")
        elif ICOM_ACTIVE_PATTERN.fullmatch(path):
            if token is not None and re.fullmatch(r"hm[1-9][0-9]{0,2}", token):
                states.append(True)
            elif token == "off":
                states.append(False)
    return any(states) if states else None
