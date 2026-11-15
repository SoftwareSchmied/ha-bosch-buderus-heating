"""Single cp0 values of a wallbox: which exist, how they read, from which
firmware on the vendor app offers them.

A single value answers like any PointT resource (``value`` plus flags such
as ``writeable``, ``minValue``, ``maxValue``, ``stepSize``, ``used``,
``available``). Only the shapes listed here are accepted; anything else
stays unknown. Nothing is ever defaulted to zero or "off".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from math import isfinite
from typing import Final, Literal

from .pointt.exceptions import ProtocolError

type LeafKind = Literal["on_off", "number", "text", "meter_state", "strategy", "phases"]
type LeafValue = str | float | tuple[float, float, float] | None
type Firmware = tuple[int, int, int]

# Firmware thresholds of the vendor app (its local feature flags and fixed
# checks); compared with ``info.softwareVersion``.
FW_BOOST: Final[Firmware] = (4, 0, 0)
FW_SOLAR: Final[Firmware] = (4, 0, 0)
FW_HOME_BATTERY: Final[Firmware] = (5, 0, 0)
FW_OVERLOAD: Final[Firmware] = (5, 0, 0)
FW_FEED_IN: Final[Firmware] = (5, 0, 0)
FW_ECO: Final[Firmware] = (5, 1, 0)
FW_AUTHENTICATE: Final[Firmware] = (5, 1, 0)
FW_GRID_FRIENDLY: Final[Firmware] = (5, 1, 0)


@dataclass(frozen=True, slots=True)
class LeafSpec:
    """One single value below ``/rest/v1/cp0/``."""

    path: str
    kind: LeafKind
    firmware: Firmware | None = None


LEAVES: Final[dict[str, LeafSpec]] = {
    spec.path: spec
    for spec in (
        LeafSpec("telemetry/extmeterState", "meter_state"),
        LeafSpec("telemetry/forceActive", "on_off", FW_BOOST),
        LeafSpec("telemetry/extInputState", "on_off", FW_GRID_FRIENDLY),
        LeafSpec("info/extmeterCurrentMaximum", "number", FW_OVERLOAD),
        LeafSpec("info/brand", "text", FW_OVERLOAD),
        LeafSpec("conf/chargingStrategy", "strategy", FW_SOLAR),
        LeafSpec("conf/locked", "on_off"),
        LeafSpec("conf/label", "text"),
        LeafSpec("conf/currency", "text"),
        LeafSpec("conf/price", "number"),
        LeafSpec("conf/feedinPrice", "number", FW_FEED_IN),
        LeafSpec("conf/solarSurplus/supplementEnable", "on_off", FW_ECO),
        LeafSpec("conf/solarSurplus/supplementLimit", "number", FW_ECO),
        LeafSpec("conf/solarSurplus/supplementThreshold", "number", FW_ECO),
        LeafSpec("conf/strategy/powerOffset", "number", FW_HOME_BATTERY),
        LeafSpec("conf/strategy/filterTime", "number", FW_HOME_BATTERY),
        LeafSpec("conf/overloadProtection/currentLimiter", "on_off", FW_OVERLOAD),
        LeafSpec("conf/overloadProtection/currentLimit", "phases", FW_OVERLOAD),
        LeafSpec("conf/overloadProtection/powerLimiter", "on_off", FW_OVERLOAD),
        LeafSpec("conf/overloadProtection/powerLimit", "number", FW_OVERLOAD),
    )
}

_VERSION = re.compile(r"\s*v?(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?:\D|$)")
METER_STATES: Final = ("disconnected", "connected", "error")
STRATEGIES: Final = {"default": "manual", "solar-eco": "solar"}


def firmware_version(text: object) -> Firmware | None:
    """``5.1.0`` (also ``v5.1.0`` or ``5.1.0-rc1``) as a comparable triple."""
    if not isinstance(text, str) or len(text) > 64:
        return None
    match = _VERSION.match(text)
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def firmware_reaches(firmware: Firmware | None, threshold: Firmware | None) -> bool:
    """An unknown firmware reaches no threshold; no threshold is always met."""
    if threshold is None:
        return True
    return firmware is not None and firmware >= threshold


@dataclass(frozen=True, slots=True)
class Leaf:
    """One interpreted single value with the flags the wallbox reported."""

    value: LeafValue
    writeable: bool = False
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    used: bool | None = None
    available: bool | None = None
    received: float = 0


def _finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str) and 0 < len(value) <= 32:
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    return number if isfinite(number) else None


def _flag(data: dict[str, object], key: str) -> bool | None:
    raw = data.get(key)
    if isinstance(raw, bool):
        return raw
    if raw in ("true", "false"):
        return raw == "true"
    return None


def _value(kind: LeafKind, raw: object) -> LeafValue:
    if kind == "on_off":
        text = raw.strip().lower() if isinstance(raw, str) else None
        return text if text in ("on", "off") else None
    if kind == "number":
        return _finite(raw)
    if kind == "meter_state":
        text = raw.strip().lower() if isinstance(raw, str) else None
        return text if text in METER_STATES else None
    if kind == "strategy":
        return raw if isinstance(raw, str) and raw in STRATEGIES else None
    if kind == "phases":
        if not isinstance(raw, list) or len(raw) != 3:
            return None
        items = [_finite(item) for item in raw]
        if any(item is None or item < 0 for item in items):
            return None
        first, second, third = (float(item) for item in items if item is not None)
        return first, second, third
    if (
        isinstance(raw, str)
        and 0 < len(raw.strip()) <= 128
        and all(character.isprintable() for character in raw)
    ):
        return raw.strip()
    return None


def parse_leaf(spec: LeafSpec, payload: object, received: float) -> Leaf:
    """Interpret one answer; a foreign ``id`` or a non-object is refused."""
    if not isinstance(payload, dict):
        raise ProtocolError("Invalid wallbox value envelope")
    identifier = payload.get("id")
    if identifier is not None and (
        not isinstance(identifier, str)
        or not identifier.rstrip("/").endswith("cp0/" + spec.path)
    ):
        raise ProtocolError("Wallbox value answered for another path")
    writeable = payload.get("writeable", payload.get("writable"))
    return Leaf(
        value=_value(spec.kind, payload.get("value")),
        writeable=writeable is True or (type(writeable) is int and writeable == 1),
        minimum=_finite(payload.get("minValue")),
        maximum=_finite(payload.get("maxValue")),
        step=step if (step := _finite(payload.get("stepSize"))) and step > 0 else None,
        used=_flag(payload, "used"),
        available=_flag(payload, "available"),
        received=received,
    )
