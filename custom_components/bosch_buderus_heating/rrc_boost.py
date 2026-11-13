"""Boost of an RRC 2.0 room controller through ``boostShortcut`` (W2-E1).

``/heatingCircuits/hc1/boostShortcut`` holds one structure: temperature,
duration in hours, mode ``on``/``off``, the zones it heats and the zones it
may heat. It is written as a whole and only through this path: the single
leaves (``boostMode``, ``boostTemperature`` ...) stay readings (P43). The
body is ``[{"temperature", "duration", "mode", "zones"}]`` without
``allowedZones``.

A start sends mode ``on`` with the temperature, the duration and the zones
it heats; a stop sends the same structure with mode ``off``. Temperature
and duration are never sent alone (AWAY-06): while the boost is off, a new
value is kept as the preset of the next start; while it runs, a change is
refused. A start heats only the set zones that still exist and that it may
heat (``allowedZones``); without such a zone, or without ``allowedZones``,
it is refused (AWAY-08).

Limits come only from the metadata of ``boostTemperature`` and
``boostDuration`` (S3). A structure with fields not known here is shown but
never written, since writing it would drop them.

Pure: no Home Assistant import. Expected, untested on hardware.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Final, Literal
from weakref import WeakKeyDictionary

from . import rrc
from .pointt import Resource, WriteValidationError
from .pointt.models import JsonValue
from .unit_contracts import DURATION_UNITS, TEMPERATURE_UNITS
from .write_policies import ContractRejected

BOOST_PATH: Final = "/heatingCircuits/hc1/boostShortcut"
BOOST_TEMPERATURE_PATH: Final = "/heatingCircuits/hc1/boostTemperature"
BOOST_DURATION_PATH: Final = "/heatingCircuits/hc1/boostDuration"
STRUCT_TYPE: Final = "boostShortcutStruct"
MODES: Final = ("on", "off")
_FIELDS: Final = frozenset({"temperature", "duration", "mode", "zones"})
_KNOWN: Final = frozenset({*_FIELDS, "allowedZones"})
# C34: broad plausibility limits, only against corrupt metadata.
_TEMPERATURE_RANGE: Final = (-30.0, 100.0)
_DURATION_RANGE: Final = (0.0, 48.0)

NO_ZONES: Final = "rrc_boost_no_zones"
UNSUPPORTED_SHAPE: Final = "rrc_boost_unsupported_shape"
RUNNING: Final = "rrc_boost_running"

type Setting = Literal["temperature", "duration"]


@dataclass(frozen=True, slots=True)
class Boost:
    """One valid boost structure."""

    temperature: float
    duration: int
    mode: str
    zones: tuple[int, ...]
    allowed_zones: tuple[int, ...] | None
    # Fields this integration does not know; such a structure is read-only.
    extra_fields: bool = False

    @property
    def on(self) -> bool:
        return self.mode == "on"

    def body(self) -> JsonValue:
        """The PUT body; ``allowedZones`` is never sent."""
        return [
            {
                "temperature": float(self.temperature),
                "duration": int(self.duration),
                "mode": self.mode,
                "zones": list(self.zones),
            }
        ]

    def shows(self, target: Boost) -> bool:
        """Whether this reading shows every written field of ``target``."""
        return (
            math.isclose(self.temperature, target.temperature, abs_tol=1e-6)
            and self.duration == target.duration
            and self.mode == target.mode
            and self.zones == target.zones
        )


@dataclass(frozen=True, slots=True)
class Limits:
    """The range and grid of one boost setting, from its leaf's metadata."""

    minimum: float
    maximum: float
    step: float

    def check(self, value: float) -> None:
        """S3/S4: inside the range and on the grid counted from the minimum;
        never rounded."""
        if (
            not math.isfinite(value)
            or not self.minimum <= value <= self.maximum
            or not math.isclose(
                (ticks := (value - self.minimum) / self.step),
                round(ticks),
                abs_tol=1e-7,
            )
        ):
            raise WriteValidationError("The boost value is outside its limits")


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _zones(value: object) -> tuple[int, ...] | None:
    if not isinstance(value, list) or len(value) > 64:
        return None
    if any(type(item) is not int or not 0 < item <= 9999 for item in value):
        return None
    if len(set(value)) != len(value):
        return None
    return tuple(value)


def parse(resource: Resource | None) -> Boost | None:
    """The boost structure of a reading, or ``None`` if it is not one.

    P41: an unavailable or unused structure is none; a used but unavailable
    one is read (it is locked, see ``locked``).
    """
    if (
        resource is None
        or resource.path != BOOST_PATH
        or resource.metadata.resource_type != STRUCT_TYPE
        or not resource.metadata.availability_flags_valid
        or resource.metadata.used is False
        or (resource.metadata.available is False and resource.metadata.used is not True)
        or not resource.has_value
        or not isinstance(resource.value, list)
        or len(resource.value) != 1
        or not isinstance(item := resource.value[0], dict)
        or not set(item) >= _FIELDS
    ):
        return None
    temperature = _number(item["temperature"])
    duration = _number(item["duration"])
    zones = _zones(item["zones"])
    allowed = _zones(item["allowedZones"]) if "allowedZones" in item else None
    if (
        temperature is None
        or duration is None
        or duration != int(duration)
        or item["mode"] not in MODES
        or zones is None
        or ("allowedZones" in item and allowed is None)
    ):
        return None
    return Boost(
        temperature,
        int(duration),
        str(item["mode"]),
        zones,
        allowed,
        extra_fields=not set(item) <= _KNOWN,
    )


def locked(resource: Resource) -> bool:
    """S8: used but unavailable: shown, not written."""
    metadata = resource.metadata
    return (
        metadata.availability_flags_valid
        and metadata.used is True
        and metadata.available is False
    )


def write_issue(resource: Resource | None) -> str | None:
    """Why the structure may not be written now (S1, S8), or ``None``."""
    boost = parse(resource)
    if resource is None or boost is None:
        return UNSUPPORTED_SHAPE
    if boost.extra_fields:
        return UNSUPPORTED_SHAPE
    if not resource.metadata.writable:
        return "not_writable"
    if locked(resource):
        return "locked"
    return None


def limits(resource: Resource | None, setting: Setting) -> Limits | None:
    """S3: the limits of a setting from its leaf, or ``None`` without
    valid ones (then the setting has no control)."""
    return _limits(resource, setting)[0]


def limits_issue(resource: Resource | None, setting: Setting) -> str | None:
    """T5: why a setting has no valid limits, a token for the diagnostics
    (``missing``, ``type``, ``flags``, ``unit``, ``range``, ``step``), or
    ``None`` with valid ones. The check is the one of ``limits``."""
    return _limits(resource, setting)[1]


def _limits(
    resource: Resource | None, setting: Setting
) -> tuple[Limits | None, str | None]:
    path = BOOST_TEMPERATURE_PATH if setting == "temperature" else BOOST_DURATION_PATH
    if resource is None or resource.path != path:
        return None, "missing"
    if resource.metadata.resource_type != "floatValue":
        return None, "type"
    if not rrc.available(resource):
        return None, "flags"
    metadata = resource.metadata
    if setting == "temperature":
        if TEMPERATURE_UNITS.get(metadata.unit or "") != "°C":
            return None, "unit"
        low, high = _TEMPERATURE_RANGE
    else:
        if DURATION_UNITS.get(metadata.unit or "") != "h":
            return None, "unit"
        low, high = _DURATION_RANGE
    minimum, maximum = metadata.minimum, metadata.maximum
    if (
        minimum is None
        or maximum is None
        or not math.isfinite(minimum)
        or not math.isfinite(maximum)
        or not low <= minimum < maximum <= high
    ):
        return None, "range"
    if not metadata.step_size_valid:
        return None, "step"
    step = metadata.step_size
    if step is None:
        step = 0.5 if setting == "temperature" else 1.0
    if isinstance(step, bool) or not math.isfinite(step) or step <= 0:
        return None, "step"
    if step > maximum - minimum:
        return None, "step"
    # The duration is an integer of hours on the wire.
    if setting == "duration" and step != int(step):
        return None, "step"
    if setting == "duration" and (minimum != int(minimum) or maximum != int(maximum)):
        return None, "range"
    return Limits(minimum, maximum, float(step)), None


@dataclass(frozen=True, slots=True)
class Preset:
    """Temperature and duration for the next start (AWAY-06); ``None`` keeps
    the controller's value."""

    temperature: float | None = None
    duration: int | None = None


def started(
    boost: Boost, existing_zones: Iterable[int] | None, preset: Preset | None = None
) -> Boost:
    """The structure of a start: mode on, the preset's temperature and
    duration, and the set zones that still exist and that the boost may
    heat (AWAY-08). Without ``allowedZones`` or without such a zone the
    start is refused."""
    if boost.allowed_zones is None or existing_zones is None:
        raise ContractRejected(NO_ZONES)
    allowed = set(boost.allowed_zones) & set(existing_zones)
    zones = tuple(zone for zone in boost.zones if zone in allowed)
    if not zones:
        raise ContractRejected(NO_ZONES)
    preset = preset or Preset()
    return replace(
        boost,
        mode="on",
        zones=zones,
        temperature=boost.temperature
        if preset.temperature is None
        else float(preset.temperature),
        duration=boost.duration if preset.duration is None else int(preset.duration),
    )


def stopped(boost: Boost) -> Boost:
    """The structure of a stop: the same with mode off."""
    return replace(boost, mode="off")


def preset_value(setting: Setting, value: float, bounds: Limits) -> float:
    """A checked value for the preset (S3/S4); the duration in whole hours."""
    bounds.check(value)
    return float(round(value)) if setting == "duration" else float(value)


def diagnostics(resources: Mapping[str, Resource]) -> dict[str, object]:
    """S12: tokens only; no temperatures, durations or zones."""
    if BOOST_PATH not in resources:
        return {"boost": "missing"}
    resource = resources[BOOST_PATH]
    return {
        "boost": write_issue(resource) or "writable",
        "boost_temperature_limits": limits(
            resources.get(BOOST_TEMPERATURE_PATH), "temperature"
        )
        is not None,
        "boost_duration_limits": limits(resources.get(BOOST_DURATION_PATH), "duration")
        is not None,
        # T5: why there are no limits; the check itself is unchanged.
        "boost_temperature_limits_issue": limits_issue(
            resources.get(BOOST_TEMPERATURE_PATH), "temperature"
        ),
        "boost_duration_limits_issue": limits_issue(
            resources.get(BOOST_DURATION_PATH), "duration"
        ),
    }


_SETTING_PATHS: Final[tuple[tuple[Setting, str], ...]] = (
    ("temperature", BOOST_TEMPERATURE_PATH),
    ("duration", BOOST_DURATION_PATH),
)


@dataclass(slots=True)
class LimitIssueWatch:
    """The last reason a boost setting had no limits, and when it was
    last seen (monotonic seconds), so that a short state between two
    downloads still shows in the diagnostics. ``missing`` is not kept: a
    leaf that is not known yet is no state of the controller."""

    last: dict[Setting, tuple[str, float]] = field(default_factory=dict)

    def observe(self, leaves: Mapping[Setting, Resource | None], now: float) -> None:
        for setting, _path in _SETTING_PATHS:
            issue = limits_issue(leaves.get(setting), setting)
            if issue is not None and issue != "missing":
                self.last[setting] = (issue, now)

    def diagnostics(self, now: float) -> dict[str, object]:
        """Tokens and whole seconds only."""
        result: dict[str, object] = {}
        for setting, _path in _SETTING_PATHS:
            seen = self.last.get(setting)
            result[f"boost_{setting}_limits_last_issue"] = seen[0] if seen else None
            result[f"boost_{setting}_limits_last_issue_age_s"] = (
                max(0, round(now - seen[1])) if seen else None
            )
        return result


_WATCHES: WeakKeyDictionary[object, LimitIssueWatch] = WeakKeyDictionary()


def limit_watch(owner: object, *, create: bool = False) -> LimitIssueWatch | None:
    """The watch of one coordinator; created on request only."""
    watch = _WATCHES.get(owner)
    if watch is None and create:
        watch = _WATCHES[owner] = LimitIssueWatch()
    return watch


def setting_leaves(
    resources: Mapping[str, Resource],
) -> dict[Setting, Resource | None]:
    """The leaves the limits come from, by setting."""
    return {setting: resources.get(path) for setting, path in _SETTING_PATHS}
