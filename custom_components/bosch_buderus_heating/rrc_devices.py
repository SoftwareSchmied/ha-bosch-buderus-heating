"""C8 (CT-E01 to CT-E03, E07): the radio devices of an RRC 2.0 controller.

The controller lists its radio devices in ``/devices/list`` (``deviceArray``):
per device a numeric ID, its type, its zone, a battery state and a signal
level. That list is the only source of the devices; their IDs are never
guessed. The integration reads it only: devices are neither added, removed
nor configured.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from . import rrc
from .i18n import localize
from .pointt import Resource

# The controller lists itself with this type; it is the gateway device.
CONTROLLER_TYPE: Final = "thermostat"
# German and English functional names of the device types (E05: no model or
# brand names); other languages translate the English text.
TYPE_LABELS: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        CONTROLLER_TYPE: ("Raumregler", "Room controller"),
        "thermostat_valve": ("Heizkörperthermostat", "Radiator thermostat"),
        "room_thermostat": ("Raumthermostat", "Room thermostat"),
        "bwth": ("Batterie-Raumthermostat", "Battery room thermostat"),
        "device_underfloor_heating": (
            "Fußbodenheizungs-Aktor",
            "Underfloor heating actuator",
        ),
        "repeater": ("Funk-Repeater", "Radio repeater"),
        "appliance_link": ("Wärmeerzeuger-Anbindung", "Heat generator link"),
        "easy_receiver": ("Funkempfänger", "Radio receiver"),
    }
)
UNKNOWN_TYPE_LABEL: Final = ("Funkgerät", "Radio device")
_TYPE_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,47}")
BATTERY_STATES: Final = frozenset({"ok", "low", "unknown"})
MAX_ZONE = 9999


@dataclass(frozen=True, slots=True)
class RadioDevice:
    """One listed device; a missing or malformed field is ``None``."""

    identifier: int
    kind: str | None
    zone: int | None
    battery: str | None
    signal: float | None

    @property
    def is_controller(self) -> bool:
        return self.kind == CONTROLLER_TYPE

    @property
    def battery_low(self) -> bool | None:
        """``low`` is on, ``ok`` off; ``unknown`` or no state is unknown."""
        if self.battery == "low":
            return True
        return False if self.battery == "ok" else None


def parse_devices(resource: Resource | None) -> tuple[RadioDevice, ...] | None:
    """The devices of a valid list, ``None`` for a missing or invalid one.

    The list itself follows the rules of the zone list (type, flags, unique
    numeric IDs); a single malformed field only leaves that field unknown.
    """
    if resource is None or resource.path != rrc.DEVICE_LIST:
        return None
    identifiers = rrc.listed_ids(resource)
    if identifiers is None or not isinstance(resource.value, list):
        return None
    devices: list[RadioDevice] = []
    for identifier, item in zip(identifiers, resource.value, strict=True):
        if not isinstance(item, dict):  # checked by listed_ids already
            return None
        devices.append(
            RadioDevice(
                identifier=identifier,
                kind=_kind(item.get("type")),
                zone=_zone(item.get("zone")),
                battery=_battery(item.get("battery")),
                signal=_signal(item.get("signal")),
            )
        )
    return tuple(devices)


def field_issues(devices: tuple[RadioDevice, ...]) -> dict[str, int]:
    """Counts of fields a listed device sent malformed or not at all."""
    return {
        "type": sum(device.kind is None for device in devices),
        "zone": sum(device.zone is None for device in devices),
        "battery": sum(device.battery is None for device in devices),
        "signal": sum(device.signal is None for device in devices),
    }


def type_label(kind: str | None, language: str) -> str:
    return localize(language, *TYPE_LABELS.get(kind or "", UNKNOWN_TYPE_LABEL))


def device_name(device: RadioDevice, language: str) -> str:
    """The functional name of a new device: its type and its list ID."""
    return f"{type_label(device.kind, language)} {device.identifier}"


def identifier(gateway_identifier: str, device_id: int) -> str:
    """E07: gateway and list ID, never a radio identifier of the device."""
    return f"{gateway_identifier}:devices:device{device_id}"


def listed_device(path: str) -> int | None:
    """The list ID of a ``/devices/deviceN`` path, if it is one."""
    match = rrc.RADIO_DEVICE.match(path)
    return int(match.group(1)) if match else None


def _kind(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip().casefold()
    return token if _TYPE_TOKEN.fullmatch(token) else None


def _zone(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value != int(value):
        return None
    zone = int(value)
    return zone if 0 <= zone <= MAX_ZONE else None


def _battery(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    state = value.strip().casefold()
    return state if state in BATTERY_STATES else None


def _signal(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or not 0 <= value <= 100:
        return None
    return float(value)


def find(resources: Mapping[str, Resource], device_id: int) -> RadioDevice | None:
    for device in parse_devices(resources.get(rrc.DEVICE_LIST)) or ():
        if device.identifier == device_id:
            return device
    return None
