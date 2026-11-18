"""Further system settings: silent mode window, low-noise operation, pool (0.9.2).

Each function below has its own policy and its own gate. A control exists
only when the gateway reports the path, as writable and with the data the
policy needs (limits for a number, a list of values for a selection);
without that path nothing is created.

- The silent mode of the heat pump besides its switch: the power reduction
  in percent and the start and end time of the window. The two times are
  numbers in the unit the gateway reports (minutes); nothing is assumed
  about the day they count from.
- The low-noise operation: its mode as a selection over the values the
  gateway advertises, and its duration as a number.
- The pool of the heat source: the target temperature
  (``/heatSources/poolSetpointTemperature``) and the mode of the auxiliary
  heater for the pool (``/pool/additionalHeater/poolMode``), as a selection
  over the advertised values.

All of them are created disabled. They follow the manufacturer's app, which
offers them; no installation we know of reports the low-noise and the pool
paths. Pure data without Home Assistant imports. Expected, untested on a
real installation.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

from .pointt import Resource
from .write_policies import EnumWritePolicy, NumberWritePolicy

# A whole number the gateway reports as an integer or as a float.
_NUMBER_TYPES: Final = frozenset({"integerValue", "floatValue"})
_STRING: Final = frozenset({"stringValue"})
_MINUTES: Final = frozenset({"mins", "min", "minutes"})
MINUTES_PER_DAY: Final = 1440.0
# The steps the app takes when the gateway reports none: 15 minutes for the
# times of the window, 10 for the power reduction.
TIME_STEP_MINUTES: Final = 15.0
POWER_REDUCTION_STEP: Final = 10.0

POWER_REDUCTION_PATTERN: Final = r"^/system/silentMode/powerReduction$"
START_TIME_PATTERN: Final = r"^/system/silentMode/startTime$"
STOP_TIME_PATTERN: Final = r"^/system/silentMode/stopTime$"
LOW_NOISE_MODE_PATTERN: Final = r"^/system/lowNoise/mode$"
LOW_NOISE_DURATION_PATTERN: Final = r"^/system/lowNoise/duration$"
POOL_SETPOINT_PATTERN: Final = r"^/heatSources/poolSetpointTemperature$"
POOL_MODE_PATTERN: Final = r"^/pool/additionalHeater/poolMode$"

# A whole percentage; both limits within 0 and 100.
POWER_REDUCTION_POLICY: Final = NumberWritePolicy(
    POWER_REDUCTION_PATTERN,
    "%",
    POWER_REDUCTION_STEP,
    enabled_by_default=False,
    strict_grid=True,
    wire_integer=True,
    accepted_units=frozenset({"%"}),
    plausible_range=(0.0, 100.0),
    resource_types=_NUMBER_TYPES,
)


def _time(pattern: str) -> NumberWritePolicy:
    """A time of the window in whole minutes, both limits within one day."""
    return NumberWritePolicy(
        pattern,
        "mins",
        TIME_STEP_MINUTES,
        enabled_by_default=False,
        strict_grid=True,
        wire_integer=True,
        accepted_units=_MINUTES,
        plausible_range=(0.0, MINUTES_PER_DAY),
        resource_types=_NUMBER_TYPES,
    )


START_TIME_POLICY: Final = _time(START_TIME_PATTERN)
STOP_TIME_POLICY: Final = _time(STOP_TIME_PATTERN)

# The duration unit comes from the gateway (seconds, minutes or hours).
LOW_NOISE_DURATION_POLICY: Final = NumberWritePolicy(
    LOW_NOISE_DURATION_PATTERN,
    "mins",
    1.0,
    enabled_by_default=False,
    strict_grid=True,
    wire_integer=True,
    resource_types=_NUMBER_TYPES,
)

# The pool target follows the advertised limits and step of the gateway.
POOL_SETPOINT_POLICY: Final = NumberWritePolicy(
    POOL_SETPOINT_PATTERN, "C", 1.0, enabled_by_default=False, live_step=True
)


def _selection(pattern: str) -> EnumWritePolicy:
    """A selection over the advertised values, whatever their codes: at
    least two of them, and the current one among them. No value is assumed."""
    return EnumWritePolicy(
        pattern,
        _STRING,
        frozenset(),
        min_options=2,
        current_required=True,
        enabled_by_default=False,
    )


LOW_NOISE_MODE_POLICY: Final = _selection(LOW_NOISE_MODE_PATTERN)
POOL_MODE_POLICY: Final = _selection(POOL_MODE_PATTERN)

ENUM_POLICIES: Final = (LOW_NOISE_MODE_POLICY, POOL_MODE_POLICY)
NUMBER_POLICIES: Final = (
    POWER_REDUCTION_POLICY,
    START_TIME_POLICY,
    STOP_TIME_POLICY,
    LOW_NOISE_DURATION_POLICY,
    POOL_SETPOINT_POLICY,
)
# The numbers whose unit is a duration (the others are not durations: the
# two times are points of a window).
DURATION_POLICIES: Final = frozenset({LOW_NOISE_DURATION_POLICY})

# Number pattern -> (translation key, German name).
_NUMBERS: Final = (
    (
        POWER_REDUCTION_PATTERN,
        "silent_mode_power_reduction",
        "Flüstermodus: Leistungsreduzierung",
    ),
    (START_TIME_PATTERN, "silent_mode_start_time", "Flüstermodus: Startzeit"),
    (STOP_TIME_PATTERN, "silent_mode_stop_time", "Flüstermodus: Endzeit"),
    (
        LOW_NOISE_DURATION_PATTERN,
        "low_noise_duration",
        "Leiser Betrieb: Dauer",
    ),
    (
        POOL_SETPOINT_PATTERN,
        "pool_heat_source_setpoint",
        "Pool-Solltemperatur (Wärmequelle)",
    ),
)
# Selection policy -> translation key.
_SELECTIONS: Final = (
    (LOW_NOISE_MODE_POLICY, "low_noise_mode"),
    (POOL_MODE_POLICY, "pool_additional_heater_mode"),
)
# The icon of each setting; the entities take the same from icons.json.
_ICONS: Final = (
    (POWER_REDUCTION_PATTERN, "mdi:volume-low"),
    (START_TIME_PATTERN, "mdi:clock-start"),
    (STOP_TIME_PATTERN, "mdi:clock-end"),
    (LOW_NOISE_DURATION_PATTERN, "mdi:timer-outline"),
    (POOL_SETPOINT_PATTERN, "mdi:pool-thermometer"),
    (LOW_NOISE_MODE_PATTERN, "mdi:volume-low"),
    (POOL_MODE_PATTERN, "mdi:pool"),
)


def number_identity(path: str) -> tuple[str, str] | None:
    """The translation key and the German name of one of the numbers."""
    for pattern, key, name in _NUMBERS:
        if re.fullmatch(pattern, path):
            return key, name
    return None


def selection_key(policy: object) -> str | None:
    """The translation key of one of the selections, else None."""
    return next((key for item, key in _SELECTIONS if item is policy), None)


def is_selection_path(path: str) -> bool:
    """Whether a path is one of the selections of this module."""
    return any(
        re.fullmatch(pattern, path)
        for pattern in (LOW_NOISE_MODE_PATTERN, POOL_MODE_PATTERN)
    )


def icon(path: str) -> str | None:
    """The icon of a setting of this module, else None."""
    return next((icon for pattern, icon in _ICONS if re.fullmatch(pattern, path)), None)


# Diagnostics: one entry per function with the pattern of its path.
_FUNCTIONS: Final = (
    ("silent_power_reduction", POWER_REDUCTION_PATTERN),
    ("silent_start_time", START_TIME_PATTERN),
    ("silent_stop_time", STOP_TIME_PATTERN),
    ("low_noise_mode", LOW_NOISE_MODE_PATTERN),
    ("low_noise_duration", LOW_NOISE_DURATION_PATTERN),
    ("pool_setpoint", POOL_SETPOINT_PATTERN),
    ("pool_mode", POOL_MODE_PATTERN),
)


def diagnostics(resources: Mapping[str, Resource]) -> dict[str, dict[str, int]]:
    """Counts only, per function: how many paths the gateway reports, how many
    are writable, and how many advertise a list or limits. No paths, no
    values."""
    result: dict[str, dict[str, int]] = {}
    for name, pattern in _FUNCTIONS:
        counts = {"present": 0, "writable": 0, "with_options": 0, "with_limits": 0}
        for path, resource in resources.items():
            if re.fullmatch(pattern, path) is None:
                continue
            metadata = resource.metadata
            counts["present"] += 1
            counts["writable"] += bool(metadata.writable)
            counts["with_options"] += bool(metadata.allowed_values)
            counts["with_limits"] += (
                metadata.minimum is not None and metadata.maximum is not None
            )
        result[name] = counts
    return result
