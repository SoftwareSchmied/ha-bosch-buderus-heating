"""Further settings of a heating circuit and of the season switching (0.9.2).

Each function below has its own policy and its own gate. A control exists
only when the gateway reports the path, as writable and with the data the
policy needs; without that path nothing is created.

- Boost of a heating circuit: the switch ``boostMode`` (only with an
  advertised list of exactly the values ``on`` and ``off``) and the numbers
  ``boostDuration`` and ``boostTemperature`` (only with minimum and maximum).
  A room controller has its own boost (``rrc_boost``); there these paths stay
  readings.
- The cooling operating mode of a circuit (``cooling/operationMode``) as a
  selection, with the advertised list of the known modes.
- The heating limit of the season switching (``seasonOptimizer/
  heatingThreshold``) and, only when found, the cooling thresholds
  (``seasonOptimizer/coolingThreshold`` and ``suWiCoolingThreshold``).
- The setpoint optimization of a circuit and, only when found, its open
  window detection, as switches.

All of them are created disabled. They follow the manufacturer's app, which
offers them; a room controller's circuit keeps its contracts. Pure data
without Home Assistant imports. Expected, untested on a real installation.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

from .pointt import Resource
from .write_policies import EnumWritePolicy, NumberWritePolicy

_STRING: Final = frozenset({"stringValue"})
_ON_OFF: Final = frozenset({"on", "off"})
_CIRCUIT: Final = r"^/heatingCircuits/[^/]+/"

BOOST_MODE_PATTERN: Final = _CIRCUIT + r"boostMode$"
BOOST_DURATION_PATTERN: Final = _CIRCUIT + r"boostDuration$"
BOOST_TEMPERATURE_PATTERN: Final = _CIRCUIT + r"boostTemperature$"
COOLING_MODE_PATTERN: Final = _CIRCUIT + r"cooling/operationMode$"
SETPOINT_OPTIMIZATION_PATTERN: Final = _CIRCUIT + r"setpointOptimization$"
WINDOW_DETECTION_PATTERN: Final = _CIRCUIT + r"openWindowDetection/enabled$"
HEATING_THRESHOLD_PATTERN: Final = r"^/system/seasonOptimizer/heatingThreshold$"
COOLING_THRESHOLD_PATTERN: Final = r"^/system/seasonOptimizer/coolingThreshold$"
CIRCUIT_COOLING_THRESHOLD_PATTERN: Final = _CIRCUIT + r"suWiCoolingThreshold$"

# The modes of the cooling operation, as the circuit's heating modes.
COOLING_MODES: Final = ("off", "manual", "auto")


def _switch(pattern: str) -> EnumWritePolicy:
    """A switch over exactly ``on`` and ``off``: the gateway has to advertise
    exactly these two values, else there is no control."""
    return EnumWritePolicy(
        pattern,
        _STRING,
        _ON_OFF,
        known_only=True,
        options_rule="exact",
        on_value="on",
        off_value="off",
        enabled_by_default=False,
    )


def _temperature(pattern: str, step: float) -> NumberWritePolicy:
    return NumberWritePolicy(
        pattern, "C", step, enabled_by_default=False, strict_grid=True
    )


BOOST_MODE_POLICY: Final = _switch(BOOST_MODE_PATTERN)
SETPOINT_OPTIMIZATION_POLICY: Final = _switch(SETPOINT_OPTIMIZATION_PATTERN)
WINDOW_DETECTION_POLICY: Final = _switch(WINDOW_DETECTION_PATTERN)

# The duration unit comes from the gateway (hours on the known ones); a
# whole step counted from the minimum, as the advertised one else.
BOOST_DURATION_POLICY: Final = NumberWritePolicy(
    BOOST_DURATION_PATTERN,
    "mins",
    1.0,
    enabled_by_default=False,
    strict_grid=True,
)
BOOST_TEMPERATURE_POLICY: Final = _temperature(BOOST_TEMPERATURE_PATTERN, 0.5)
HEATING_THRESHOLD_POLICY: Final = _temperature(HEATING_THRESHOLD_PATTERN, 1.0)
COOLING_THRESHOLD_POLICY: Final = _temperature(COOLING_THRESHOLD_PATTERN, 1.0)
CIRCUIT_COOLING_THRESHOLD_POLICY: Final = _temperature(
    CIRCUIT_COOLING_THRESHOLD_PATTERN, 1.0
)

# Only the modes of the app's list; at least two of them, and the current one
# among them.
COOLING_MODE_POLICY: Final = EnumWritePolicy(
    COOLING_MODE_PATTERN,
    _STRING,
    frozenset(COOLING_MODES),
    known_only=True,
    min_options=2,
    current_required=True,
    enabled_by_default=False,
)

ENUM_POLICIES: Final = (
    BOOST_MODE_POLICY,
    SETPOINT_OPTIMIZATION_POLICY,
    WINDOW_DETECTION_POLICY,
    COOLING_MODE_POLICY,
)
NUMBER_POLICIES: Final = (
    BOOST_DURATION_POLICY,
    BOOST_TEMPERATURE_POLICY,
    HEATING_THRESHOLD_POLICY,
    COOLING_THRESHOLD_POLICY,
    CIRCUIT_COOLING_THRESHOLD_POLICY,
)
# The switches of this module (the entities of ``switch``).
SWITCH_POLICIES: Final = (
    BOOST_MODE_POLICY,
    SETPOINT_OPTIMIZATION_POLICY,
    WINDOW_DETECTION_POLICY,
)
# The circuit settings of a plant without a room controller: its circuits
# follow the contracts there (``circuit_controls.GENERIC_ONLY_POLICIES``).
GENERIC_ONLY_POLICIES: Final = frozenset(
    {
        BOOST_MODE_POLICY,
        BOOST_DURATION_POLICY,
        BOOST_TEMPERATURE_POLICY,
        SETPOINT_OPTIMIZATION_POLICY,
        WINDOW_DETECTION_POLICY,
        COOLING_MODE_POLICY,
        CIRCUIT_COOLING_THRESHOLD_POLICY,
    }
)

# Switch policy -> translation key.
_SWITCHES: Final = (
    (BOOST_MODE_POLICY, "heating_circuit_boost"),
    (SETPOINT_OPTIMIZATION_POLICY, "heating_circuit_setpoint_optimization"),
    (WINDOW_DETECTION_POLICY, "heating_circuit_window_detection"),
)
# Number pattern -> (translation key, German name).
_NUMBERS: Final = (
    (BOOST_DURATION_PATTERN, "heating_circuit_boost_duration", "Boost-Dauer"),
    (
        BOOST_TEMPERATURE_PATTERN,
        "heating_circuit_boost_temperature",
        "Boost-Temperatur",
    ),
    (
        HEATING_THRESHOLD_PATTERN,
        "season_heating_threshold",
        "Heizbetrieb bis Außentemperatur",
    ),
    (
        COOLING_THRESHOLD_PATTERN,
        "season_cooling_threshold",
        "Kühlbetrieb ab Außentemperatur",
    ),
    (
        CIRCUIT_COOLING_THRESHOLD_PATTERN,
        "circuit_cooling_threshold",
        "Kühlbetrieb ab Außentemperatur",
    ),
)


def switch_key(policy: object) -> str | None:
    """The translation key of one of the switches, else None."""
    return next((key for item, key in _SWITCHES if item is policy), None)


def number_identity(path: str) -> tuple[str, str] | None:
    """The translation key and the German name of one of the numbers."""
    for pattern, key, name in _NUMBERS:
        if re.fullmatch(pattern, path):
            return key, name
    return None


def is_generic_only(policy: object) -> bool:
    """Whether a policy belongs to the settings of this module that a room
    controller's circuits do not take."""
    return policy in GENERIC_ONLY_POLICIES


# The icon of each setting; the entities take the same from icons.json.
_ICONS: Final = (
    (BOOST_MODE_PATTERN, "mdi:rocket-launch-outline"),
    (SETPOINT_OPTIMIZATION_PATTERN, "mdi:tune-vertical"),
    (WINDOW_DETECTION_PATTERN, "mdi:window-open"),
    (BOOST_DURATION_PATTERN, "mdi:timer-outline"),
    (BOOST_TEMPERATURE_PATTERN, "mdi:thermometer-high"),
    (HEATING_THRESHOLD_PATTERN, "mdi:sun-thermometer-outline"),
    (COOLING_THRESHOLD_PATTERN, "mdi:snowflake-thermometer"),
    (CIRCUIT_COOLING_THRESHOLD_PATTERN, "mdi:snowflake-thermometer"),
    (COOLING_MODE_PATTERN, "mdi:snowflake"),
)


def icon(path: str) -> str | None:
    """The icon of a setting of this module, else None."""
    return next((icon for pattern, icon in _ICONS if re.fullmatch(pattern, path)), None)


# Diagnostics: one entry per function with the pattern of its path.
_FUNCTIONS: Final = (
    ("boost_mode", BOOST_MODE_PATTERN),
    ("boost_duration", BOOST_DURATION_PATTERN),
    ("boost_temperature", BOOST_TEMPERATURE_PATTERN),
    ("cooling_mode", COOLING_MODE_PATTERN),
    ("heating_threshold", HEATING_THRESHOLD_PATTERN),
    ("cooling_threshold", COOLING_THRESHOLD_PATTERN),
    ("circuit_cooling_threshold", CIRCUIT_COOLING_THRESHOLD_PATTERN),
    ("setpoint_optimization", SETPOINT_OPTIMIZATION_PATTERN),
    ("window_detection", WINDOW_DETECTION_PATTERN),
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
