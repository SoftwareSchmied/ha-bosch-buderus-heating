"""Settings of a heating circuit besides its climate entity (0.9.2).

- Summer/winter switching of one heating circuit (``suWiSwitchMode``) and
  its heating limit (``suWiThreshold``), for gateways without a room
  controller contract (iCom/KM200). The central season switching of a K40
  (``/system/seasonOptimizer/mode``) stays as it is.
- The cooling setpoint (``cooling/roomTempSetpoint``) and the cooling
  threshold (``cooling/outdoorThreshold``) of heat pumps with cooling.
- The temperature levels of a heating circuit: the heating and setback
  levels (``temperatureLevels/comfort2`` and ``eco``, since 0.9.0), the away
  temperature (``awayTemperature``, also as a level) and the cooling level
  (``cooling/temperatureLevels/on``). Only these four level names are
  operated, as in the manufacturer's app; any other level stays a reading.

The rules follow the manufacturer's app: no change while the circuit is
run by a room controller (control type ``ISRC``); a temperature level not
either while the circuit runs on constant values (``constants``, the rule of
the climate entity). The cooling threshold exists only on a circuit whose
summer/winter switching offers cooling. A cooling setpoint of 0 means "off"
in the app and is never a target. The values lie within the limits the
gateway reports and on its step (1.0 for the thresholds and 0.5 for the
temperatures without one), counted from the minimum.

Every write reads the circuit's control type, the setting itself and the
settings it depends on again right before its one request; a change since
they were shown sends nothing (``guard_paths``). Its limits come from that
reading. The heating and setback levels limit each other (the heating
level's minimum follows the setback level and the other way round), so a
level write also reads the other level again: a change of its value sends
nothing, while new limits after a confirmed write of the other level do not
refuse it (``coupled_paths``, compared by value).

The operating mode and the manual setpoint of a circuit (the older
controls besides its climate entity) read the control type again as well
and take the climate entity's rule: nothing is changed on constant values.

Pure data and checks without Home Assistant imports. Expected, untested on
a real installation.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from typing import Final

from . import hc_controls_extra
from .pointt import Resource
from .pointt.models import JsonScalar
from .rrc import PRODUCT_TYPE_PATH, ZONE_LIST
from .write_policies import EnumWritePolicy, NumberWritePolicy

_CIRCUIT_ROOT: Final = re.compile(r"^(/heatingCircuits/[^/]+)/")

CONTROL_TYPE: Final = "controlType"
# The control type of a circuit run by a room controller.
ROOM_CONTROLLER: Final = "ISRC"
# The control type of a circuit run on constant values.
CONSTANT_CONTROL: Final = "constants"

SUMMER_WINTER_MODE: Final = "suWiSwitchMode"
# The codes of the summer/winter switching known from the manufacturer's
# app; any other advertised code is left out.
SUMMER_WINTER_VALUES: Final = ("automatic", "forced", "cooling", "off")
COOLING_OPTION: Final = "cooling"

# The level names the manufacturer's app knows.
HEATING_LEVELS: Final = ("comfort2", "eco", "awayTemperature")
COOLING_LEVELS: Final = ("on",)

# Refusal tokens; each is also the translation key of the error shown.
ROOM_CONTROLLER_REFUSAL: Final = "circuit_room_controller"
CONSTANT_CONTROL_REFUSAL: Final = "circuit_constant_control"
COOLING_NOT_OFFERED_REFUSAL: Final = "cooling_not_offered"
# A cooling setpoint of 0 or below: the general refusal text.
COOLING_OFF_VALUE_REFUSAL: Final = "write_validation_failed"

SUMMER_WINTER_MODE_PATTERN: Final = r"^/heatingCircuits/[^/]+/suWiSwitchMode$"
SUMMER_WINTER_THRESHOLD_PATTERN: Final = r"^/heatingCircuits/[^/]+/suWiThreshold$"
COOLING_SETPOINT_PATTERN: Final = r"^/heatingCircuits/[^/]+/cooling/roomTempSetpoint$"
COOLING_THRESHOLD_PATTERN: Final = r"^/heatingCircuits/[^/]+/cooling/outdoorThreshold$"
HEATING_LEVEL_PATTERN: Final = (
    r"^/heatingCircuits/[^/]+/temperatureLevels/(?:comfort2|eco)$"
)
AWAY_TEMPERATURE_PATTERN: Final = (
    r"^/heatingCircuits/[^/]+/(?:temperatureLevels/)?awayTemperature$"
)
COOLING_LEVEL_PATTERN: Final = r"^/heatingCircuits/[^/]+/cooling/temperatureLevels/on$"
_LEVEL_PATH: Final = re.compile(
    r"^/heatingCircuits/[^/]+/(cooling/)?temperatureLevels/([^/]+)$"
)


def circuit_root(path: str) -> str | None:
    """The heating circuit a path belongs to, such as ``/heatingCircuits/hc1``."""
    match = _CIRCUIT_ROOT.match(path)
    return match.group(1) if match is not None else None


def _control_type(path: str, readings: Mapping[str, Resource]) -> object:
    root = circuit_root(path)
    reading = readings.get(f"{root}/{CONTROL_TYPE}") if root is not None else None
    if reading is None or not reading.has_value:
        return None
    return reading.value


def _circuit_check(
    resource: Resource, _value: JsonScalar, readings: Mapping[str, Resource]
) -> str | None:
    """No change while a room controller runs the circuit (as the app).
    Without a reported control type the app goes on, so does this."""
    if _control_type(resource.path, readings) == ROOM_CONTROLLER:
        return ROOM_CONTROLLER_REFUSAL
    return None


def _level_check(
    resource: Resource, value: JsonScalar, readings: Mapping[str, Resource]
) -> str | None:
    """A temperature level: the room controller rule, and none on constant
    values (the rule of the circuit's climate entity)."""
    if (reason := _circuit_check(resource, value, readings)) is not None:
        return reason
    if _control_type(resource.path, readings) == CONSTANT_CONTROL:
        return CONSTANT_CONTROL_REFUSAL
    return None


def _constant_control_check(
    resource: Resource, _value: JsonScalar, readings: Mapping[str, Resource]
) -> str | None:
    """The rule of the circuit's climate entity: a circuit on constant
    values is only shown. A room controller (``ISRC``) does not lock the
    mode and the manual setpoint (as the climate entity)."""
    if _control_type(resource.path, readings) == CONSTANT_CONTROL:
        return CONSTANT_CONTROL_REFUSAL
    return None


def _cooling_setpoint_check(
    resource: Resource, value: JsonScalar, readings: Mapping[str, Resource]
) -> str | None:
    """0 switches the cooling off in the app; never a target here."""
    if (reason := _circuit_check(resource, value, readings)) is not None:
        return reason
    if isinstance(value, int | float) and not isinstance(value, bool) and value <= 0:
        return COOLING_OFF_VALUE_REFUSAL
    return None


def cooling_offered(path: str, readings: Mapping[str, Resource]) -> bool:
    """Whether the circuit's summer/winter switching offers cooling (the
    app's rule for showing the cooling threshold)."""
    root = circuit_root(path)
    mode = readings.get(f"{root}/{SUMMER_WINTER_MODE}") if root is not None else None
    return mode is not None and COOLING_OPTION in mode.metadata.allowed_values


def _cooling_threshold_check(
    resource: Resource, value: JsonScalar, readings: Mapping[str, Resource]
) -> str | None:
    if (reason := _circuit_check(resource, value, readings)) is not None:
        return reason
    if not cooling_offered(resource.path, readings):
        return COOLING_NOT_OFFERED_REFUSAL
    return None


# The operating mode and the manual setpoint of a heating circuit (before
# 0.9.2 without the control type rule).
OPERATION_MODE_POLICY: Final = EnumWritePolicy(
    r"^/heatingCircuits/[^/]+/operationMode$",
    frozenset({"stringValue"}),
    frozenset({"off", "manual", "auto"}),
    value_check=_constant_control_check,
)
MANUAL_SETPOINT_POLICY: Final = NumberWritePolicy(
    r"^/heatingCircuits/[^/]+/manualRoomSetpoint$",
    "C",
    0.5,
    write_grid=True,
    value_check=_constant_control_check,
)
# Summer/winter switching of a heating circuit: only known codes, at least
# two of them. A current code that is not known or not advertised stays
# shown as unknown; only the known advertised codes are written.
SUMMER_WINTER_MODE_POLICY: Final = EnumWritePolicy(
    SUMMER_WINTER_MODE_PATTERN,
    frozenset({"stringValue"}),
    frozenset(SUMMER_WINTER_VALUES),
    known_only=True,
    min_options=2,
    value_check=_circuit_check,
)
# The heating limit: the advertised step, else 1.0 as in the app.
SUMMER_WINTER_THRESHOLD_POLICY: Final = NumberWritePolicy(
    SUMMER_WINTER_THRESHOLD_PATTERN,
    "C",
    1.0,
    strict_grid=True,
    value_check=_circuit_check,
)
# The cooling setpoint and threshold; disabled by default (cooling only).
COOLING_SETPOINT_POLICY: Final = NumberWritePolicy(
    COOLING_SETPOINT_PATTERN,
    "C",
    0.5,
    enabled_by_default=False,
    strict_grid=True,
    value_check=_cooling_setpoint_check,
)
COOLING_THRESHOLD_POLICY: Final = NumberWritePolicy(
    COOLING_THRESHOLD_PATTERN,
    "C",
    1.0,
    enabled_by_default=False,
    strict_grid=True,
    value_check=_cooling_threshold_check,
)
# The heating and setback levels (0.9.0) with the level rules.
HEATING_LEVEL_POLICY: Final = NumberWritePolicy(
    HEATING_LEVEL_PATTERN,
    "C",
    0.5,
    write_grid=True,
    value_check=_level_check,
)
# The away temperature: the advertised step, else 0.5 as in the app.
AWAY_TEMPERATURE_POLICY: Final = NumberWritePolicy(
    AWAY_TEMPERATURE_PATTERN,
    "C",
    0.5,
    strict_grid=True,
    value_check=_level_check,
)
# The cooling level; disabled by default (cooling only).
COOLING_LEVEL_POLICY: Final = NumberWritePolicy(
    COOLING_LEVEL_PATTERN,
    "C",
    0.5,
    enabled_by_default=False,
    strict_grid=True,
    value_check=_level_check,
)

ENUM_POLICIES: Final = (SUMMER_WINTER_MODE_POLICY,)
NUMBER_POLICIES: Final = (
    SUMMER_WINTER_THRESHOLD_POLICY,
    COOLING_SETPOINT_POLICY,
    COOLING_THRESHOLD_POLICY,
    AWAY_TEMPERATURE_POLICY,
    COOLING_LEVEL_POLICY,
)
# Not on a room controller (its contracts have their own rules); with the
# further circuit settings of ``hc_controls_extra``.
GENERIC_ONLY_POLICIES: Final = frozenset(
    {*ENUM_POLICIES, *NUMBER_POLICIES, *hc_controls_extra.GENERIC_ONLY_POLICIES}
)
# The policies whose writes read their guards again (``guard_paths``).
GUARDED_POLICIES: Final = frozenset(
    {
        *ENUM_POLICIES,
        *NUMBER_POLICIES,
        HEATING_LEVEL_POLICY,
        OPERATION_MODE_POLICY,
        MANUAL_SETPOINT_POLICY,
        *hc_controls_extra.CIRCUIT_GUARDED_POLICIES,
    }
)
_COUPLED_LEVELS: Final = ("comfort2", "eco")


def guard_paths(path: str, policy: object) -> tuple[str, ...]:
    """The readings a write of ``path`` is checked against, the path itself
    first: the circuit's control type and the summer/winter switching for
    the thresholds and the other coupled level. Paths the plant does not have
    are skipped by the caller."""
    root = circuit_root(path)
    if root is None or policy not in GUARDED_POLICIES:
        return ()
    paths = [path, f"{root}/{CONTROL_TYPE}"]
    if policy in (SUMMER_WINTER_THRESHOLD_POLICY, COOLING_THRESHOLD_POLICY):
        paths.append(f"{root}/{SUMMER_WINTER_MODE}")
    paths.extend(coupled_paths(path, policy))
    return tuple(paths)


def coupled_paths(path: str, policy: object) -> tuple[str, ...]:
    """The other one of the heating and setback levels, which bound each
    other: a guard of a level write compared by its value only (its limits
    follow the written level). The away temperature bounds neither."""
    root = circuit_root(path)
    if root is None or policy is not HEATING_LEVEL_POLICY:
        return ()
    return tuple(
        item
        for item in (f"{root}/temperatureLevels/{name}" for name in _COUPLED_LEVELS)
        if item != path
    )


def offered(path: str, readings: Mapping[str, Resource]) -> bool:
    """Whether a control of ``path`` is offered with these readings: the
    cooling threshold only while cooling is offered, the away and cooling
    levels only with a value (the app drops a level without one); every
    other path."""
    if re.fullmatch(COOLING_THRESHOLD_PATTERN, path):
        return cooling_offered(path, readings)
    if re.fullmatch(AWAY_TEMPERATURE_PATTERN, path) or re.fullmatch(
        COOLING_LEVEL_PATTERN, path
    ):
        reading = readings.get(path)
        return reading is not None and reading.has_value
    return True


def display_minimum(policy: object, minimum: float, step: float | None) -> float:
    """The lowest value a number offers: for the cooling setpoint the first
    value of its grid above 0 when the gateway's minimum is 0 or below
    (0 means "off"); otherwise the minimum."""
    if policy is not COOLING_SETPOINT_POLICY or minimum > 0:
        return minimum
    grid = step if step is not None and math.isfinite(step) and step > 0 else 0.5
    return minimum + (math.floor(-minimum / grid + 1e-9) + 1) * grid


# The icon of each setting; the entities take the same from icons.json.
_ICONS: Final = (
    (SUMMER_WINTER_MODE_PATTERN, "mdi:sun-snowflake-variant"),
    (SUMMER_WINTER_THRESHOLD_PATTERN, "mdi:sun-thermometer-outline"),
    (COOLING_SETPOINT_PATTERN, "mdi:snowflake-thermometer"),
    (COOLING_THRESHOLD_PATTERN, "mdi:snowflake-thermometer"),
    (AWAY_TEMPERATURE_PATTERN, "mdi:home-export-outline"),
    (COOLING_LEVEL_PATTERN, "mdi:snowflake-thermometer"),
)


def icon(path: str) -> str | None:
    """The icon of a heating circuit setting of this module, else None."""
    return next((icon for pattern, icon in _ICONS if re.fullmatch(pattern, path)), None)


# The readings that show a room controller (``device.is_rrc_plant``).
ROOM_CONTROLLER_EVIDENCE_PATHS: Final = ("/zones", ZONE_LIST, PRODUCT_TYPE_PATH)
# Answers that do not settle whether a path exists.
_OPEN_ANSWERS: Final = frozenset({"http_401", "http_408", "http_429"})


def _evidence_settled(item: object) -> bool:
    """Whether one requested evidence path was found or got a definite
    client answer (such as 404)."""
    policy = getattr(item, "read_policy", "")
    # A 5xx taken as "missing" so that the discovery can go on is no answer.
    if getattr(item, "discovered", False) or policy not in (
        "normal",
        "optional_5xx_assumed_missing",
    ):
        return True
    answer = getattr(item, "fallback_result", None) or getattr(item, "bulk_result", "")
    return (
        isinstance(answer, str)
        and re.fullmatch(r"http_4\d\d", answer) is not None
        and answer not in _OPEN_ANSWERS
    )


def evidence_answers(report: object) -> dict[str, bool]:
    """Per evidence path a later discovery (the search for missing parts)
    requested: whether its answer settles it. A path below the zones left
    unread leaves all of them open."""
    paths = getattr(report, "paths", {})
    zones_unread = _search_left_zones_unread(report)
    return {
        path: not zones_unread and _evidence_settled(paths[path])
        for path in ROOM_CONTROLLER_EVIDENCE_PATHS
        if path in paths
    }


def _search_left_zones_unread(report: object) -> bool:
    """Whether a discovery stopped at its budget before reading a path below
    the zones (a room controller's zones are evidence too)."""
    return any(
        path.startswith("/zones/")
        and not item.discovered
        and item.read_policy == "normal"
        and item.bulk_result in ("not_attempted", "pending")
        for path, item in getattr(report, "paths", {}).items()
    )


def room_controller_check_complete(
    report: object, later: Mapping[str, bool] | None = None
) -> bool:
    """Whether a discovery report settles that evidence of a room controller
    was read: the discovery finished (or stopped at its budget with every
    path below the zones read), and each evidence path it requested was found
    or got a definite client answer (such as 404). A server error, a timeout,
    a rate limit or a malformed answer leaves it open.

    ``later``: the answers of later searches for missing parts
    (``evidence_answers``); an evidence path they requested is decided by the
    latest of them instead.
    """
    stop_reason = getattr(report, "stop_reason", None)
    if not getattr(report, "completed", False) and (
        stop_reason != "resource_limit" or _search_left_zones_unread(report)
    ):
        return False
    paths = getattr(report, "paths", {})
    for path in ROOM_CONTROLLER_EVIDENCE_PATHS:
        if later and path in later:
            if not later[path]:
                return False
            continue
        item = paths.get(path)
        if item is not None and not _evidence_settled(item):
            return False
    return True


def unknown_level_count(paths: Iterable[str]) -> int:
    """How many temperature levels of the heating circuits have a name the
    app does not know (they stay readings; the names are not reported)."""
    count = 0
    for path in paths:
        match = _LEVEL_PATH.fullmatch(path)
        if match is None:
            continue
        known = COOLING_LEVELS if match.group(1) else HEATING_LEVELS
        count += match.group(2) not in known
    return count
