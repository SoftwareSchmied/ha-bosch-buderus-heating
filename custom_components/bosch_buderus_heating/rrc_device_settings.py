"""W6 (CT-E04, CT-E05, CT-E09): settings and valve details of radio devices.

Pure: no Home Assistant import, and no import of ``rrc`` or
``rrc_contracts`` (their contract module imports this one). The room
controller keeps the settings of a listed radio device below
``/devices/deviceN/<subfolder>/``; the subfolder follows the device type of
``/devices/list``. Only these leaves are read, and only for listed devices.

Evidence: path, type and write flag of every leaf were seen on a TC100.2
(diagnostics of issue 122, without values); the value formats come from the
manufacturer's demo shapes. Everything here is expected, untested on real
hardware. Not offered at all: the valve calibration (a trigger without a
readable state, so no read-back), the repeater switch and the radio keys.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from .pointt import Resource
from .rrc_contract_base import RRC_ID

# Device type of the device list -> subfolder of its settings.
SUBFOLDERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "thermostat_valve": "etrv",
        "room_thermostat": "wth",
        "device_underfloor_heating": "bwth",
        # The room controller itself; its settings sit on its own device.
        "thermostat": "thermostat",
    }
)
VALVE = "etrv"

CHILD_LOCK: Final = "childLock/enabled"
OFFSET: Final = "offset"
SENSITIVITY: Final = "openWindowDetection/sensitivity"
STATUS: Final = "status"
VALVE_POSITION: Final = "valvePosition"
MAX_VALVE_POSITION: Final = "maxValvePosition"
VALVE_TEMPERATURE: Final = "temperatureActual"
VALVE_STATE: Final = "valveState"
WHISPER_MODE: Final = "whisperMode/enabled"

# Every type with a subfolder has a child lock and a temperature offset.
SETTING_LEAVES: Final = (CHILD_LOCK, OFFSET)
# A radiator thermostat also reports its valve. The order is the reading
# order: with many devices the discovery cap (``MAX_RRC_DEVICE_SETTING_PROBES``)
# cuts the last leaves first, so the valve problem and the settings come
# before the plain readings.
VALVE_LEAVES: Final = (
    STATUS,
    CHILD_LOCK,
    OFFSET,
    SENSITIVITY,
    VALVE_POSITION,
    VALVE_TEMPERATURE,
    VALVE_STATE,
    MAX_VALVE_POSITION,
    WHISPER_MODE,
)


def type_leaves(kind: str) -> tuple[str, ...]:
    """The leaves below ``/devices/deviceN/`` read for one device type."""
    subfolder = SUBFOLDERS[kind]
    leaves = VALVE_LEAVES if subfolder == VALVE else SETTING_LEAVES
    return tuple(f"{subfolder}/{leaf}" for leaf in leaves)


DEVICE_LEAVES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {kind: type_leaves(kind) for kind in SUBFOLDERS}
)

_SUBFOLDER = "|".join(re.escape(item) for item in SUBFOLDERS.values())
SETTING_PATH: Final = re.compile(
    rf"^/devices/device({RRC_ID})/({_SUBFOLDER})/([A-Za-z]+(?:/[A-Za-z]+)?)$"
)


def leaf_pattern(leaf: str, *, valve_only: bool = False) -> str:
    """The path pattern of one leaf below the subfolders that have it."""
    subfolders = re.escape(VALVE) if valve_only else f"(?:{_SUBFOLDER})"
    return rf"^/devices/device{RRC_ID}/{subfolders}/{re.escape(leaf)}$"


@dataclass(frozen=True, slots=True)
class SettingPath:
    """A concrete setting path, split into device, subfolder and leaf."""

    device_id: int
    subfolder: str
    leaf: str


def split(path: str) -> SettingPath | None:
    """The parts of a leaf this package reads, ``None`` for any other path."""
    match = SETTING_PATH.fullmatch(path)
    if match is None:
        return None
    device_id, subfolder, leaf = int(match.group(1)), match.group(2), match.group(3)
    allowed = VALVE_LEAVES if subfolder == VALVE else SETTING_LEAVES
    return SettingPath(device_id, subfolder, leaf) if leaf in allowed else None


def offered(resource: Resource) -> bool:
    """Whether a setting may have an entity at all.

    ``used`` false hides the setting (the temperature offset of a device
    that does not use one). A missing flag counts as used: the TC100.2 sends
    none for these leaves. ``available`` false without ``used`` true gives no
    entity (P41); with ``used`` true the setting is shown and locked (S8).
    """
    metadata = resource.metadata
    return (
        metadata.availability_flags_valid
        and metadata.used is not False
        and not (metadata.available is False and metadata.used is not True)
    )


def unused_check(resource: Resource, _value: object, _related: object) -> str | None:
    """S1/S8 before sending: a setting the device does not use is refused."""
    return None if offered(resource) else "rrc_device_setting_unused"


# Open window detection: the level names and the exact values written.
SENSITIVITY_LEVELS: Final[Mapping[str, float]] = MappingProxyType(
    {"high": 0.5, "medium": 1.5, "low": 2.5}
)


# SYSTEM-10: fewer offered levels than this give no selection at all.
MIN_SENSITIVITY_OPTIONS: Final = 2
# The contract's step when the reading advertises none (``SENSITIVITY_POLICY``).
DEFAULT_SENSITIVITY_STEP: Final = 0.5


def sensitivity_options(
    minimum: float | None, maximum: float | None, step: float | None
) -> tuple[str, ...]:
    """SYSTEM-10: the levels whose value the limits and the step allow.

    The same rules as before sending: within ``minValue`` and ``maxValue``,
    and on the step counted from ``minValue``. Without both limits no level
    is offered.
    """
    if (
        minimum is None
        or maximum is None
        or not math.isfinite(minimum)
        or not math.isfinite(maximum)
    ):
        return ()
    grid = step if step is not None and math.isfinite(step) and step > 0 else None
    options: list[str] = []
    for name, value in SENSITIVITY_LEVELS.items():
        if not minimum <= value <= maximum:
            continue
        if grid is not None:
            ticks = (value - minimum) / grid
            if not math.isclose(ticks, round(ticks), abs_tol=1e-7):
                continue
        options.append(name)
    return tuple(options)


def resource_sensitivity_options(resource: Resource) -> tuple[str, ...]:
    """The offered levels of one sensitivity reading, by its metadata."""
    metadata = resource.metadata
    step = metadata.step_size
    if step is None or not math.isfinite(step) or step <= 0:
        step = DEFAULT_SENSITIVITY_STEP
    return sensitivity_options(metadata.minimum, metadata.maximum, step)


def sensitivity_level(resource: Resource | None) -> str | None:
    """The level of a reading: up to 1 high, up to 2 medium, above low.

    Readings between the written values (another client may write any
    value) take the nearest level by these thresholds.
    """
    value = _number(resource)
    if value is None:
        return None
    if value <= 1:
        return "high"
    return "medium" if value <= 2 else "low"


def sensitivity_check(
    _resource: Resource, value: object, _related: object
) -> str | None:
    """Only the three level values are ever written (exact read-back)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "rrc_window_sensitivity_level"
    return (
        None
        if any(
            math.isclose(value, level, abs_tol=1e-9)
            for level in SENSITIVITY_LEVELS.values()
        )
        else "rrc_window_sensitivity_level"
    )


def _number(resource: Resource | None) -> float | None:
    if (
        resource is None
        or resource.metadata.resource_type != "floatValue"
        or not resource.has_value
        or isinstance(resource.value, bool)
        or not isinstance(resource.value, int | float)
        or not math.isfinite(resource.value)
    ):
        return None
    return float(resource.value)


# Valve states that the controller reports as a problem with the valve,
# including a valve adaption that waits (SYSTEM-09: the valve cannot adapt,
# for example because it is not mounted correctly). The other known states
# (adaption running or done, start position, not available) are no problem;
# an unknown state is none either, but counted in diagnostics.
PROBLEM_STATES: Final = frozenset(
    {
        "VALVE_ADAPTION_IN_WAIT_STATE",
        "VALVE_TOO_TIGHT",
        "VALVE_ADJUSTMENT_RANGE_TOO_BIG",
        "VALVE_ADJUSTMENT_RANGE_TOO_SMALL",
        "VALVE_ERROR_POSITION",
    }
)
KNOWN_STATES: Final = frozenset(
    {
        *PROBLEM_STATES,
        "NOT_AVAILABLE",
        "VALVE_RUN_TO_STARTING_POSITION",
        "VALVE_ADAPTION_IN_PROGRESS",
        # Both spellings: the shorter one is the documented wire token.
        "VALVE_ADAPTION_SUCESSFUL",
        "VALVE_ADAPTION_SUCCESSFUL",
    }
)
_TOKEN = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
MAX_STATUS_ITEMS: Final = 16


@dataclass(frozen=True, slots=True)
class ValveStatus:
    """The valve states of one ``etrv/status`` reading, without values."""

    # "strings" (a string array, as on the TC100.2), "objects" (a list of
    # objects with a ``valve`` field), "mixed" or "empty".
    shape: str
    problems: tuple[str, ...]
    unknown: int

    @property
    def problem(self) -> bool:
        return bool(self.problems)


def valve_status(resource: Resource | None) -> ValveStatus | None:
    """Read ``etrv/status`` in both known forms; ``None`` when malformed.

    A string array names the states directly; a list of objects carries
    the state in ``valve`` (beside an ID and a battery state, not used).
    An empty list reports no problem.
    """
    if (
        resource is None
        or not resource.has_value
        or resource.has_values
        or resource.references
        or not isinstance(resource.value, list)
        or len(resource.value) > MAX_STATUS_ITEMS
    ):
        return None
    shapes: set[str] = set()
    tokens: list[str] = []
    for item in resource.value:
        if isinstance(item, str):
            shapes.add("strings")
            raw: object = item
        elif isinstance(item, dict):
            shapes.add("objects")
            raw = item.get("valve")
        else:
            return None
        if not isinstance(raw, str):
            return None
        tokens.append(raw.strip().upper())
    known = [token for token in tokens if token in KNOWN_STATES]
    shape = "empty" if not shapes else shapes.pop() if len(shapes) == 1 else "mixed"
    return ValveStatus(
        shape=shape,
        problems=tuple(
            dict.fromkeys(token for token in known if token in PROBLEM_STATES)
        ),
        unknown=len(tokens) - len(known),
    )


def diagnostics(resources: Mapping[str, Resource]) -> dict[str, object]:
    """S12: what the device settings look like, counted, without values."""
    leaves: Counter[str] = Counter()
    shapes: Counter[str] = Counter()
    levels: Counter[str] = Counter()
    problems = unknown = unused = too_few = 0
    for path, resource in resources.items():
        if (parts := split(path)) is None:
            continue
        leaves[f"{parts.subfolder}/{parts.leaf}"] += 1
        if not offered(resource):
            unused += 1
        if parts.leaf == STATUS:
            status = valve_status(resource)
            shapes[status.shape if status is not None else "invalid"] += 1
            if status is not None:
                problems += status.problem
                unknown += status.unknown
        elif parts.leaf == SENSITIVITY:
            levels[sensitivity_level(resource) or "unreadable"] += 1
            # SYSTEM-10: limits that leave fewer than two levels: no selection.
            too_few += (
                len(resource_sensitivity_options(resource)) < MIN_SENSITIVITY_OPTIONS
            )
    return {
        "device_settings_read": sum(leaves.values()),
        "leaves": dict(sorted(leaves.items())),
        "settings_not_offered": unused,
        "valve_status_shapes": dict(sorted(shapes.items())),
        "valves_with_problem": problems,
        "unknown_valve_states": unknown,
        "window_sensitivity_levels": dict(sorted(levels.items())),
        "window_sensitivity_too_few_options": too_few,
    }
