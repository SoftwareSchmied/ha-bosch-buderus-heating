"""Write policies of air conditioners on a PointT gateway (``airConditioning``).

Pure data without Home Assistant imports, like ``write_policies``. The
generic write registry (``writes``) lists these policies, so every write goes
through the same validated transaction: fresh reading, exactly one PUT,
read-back, no retry. The paths exist only on air-conditioner gateways
(``deviceType`` ``rac``), never on a heating gateway.

The behaviour follows the vendor app; nothing here is confirmed on a real
installation yet.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Final

from .pointt import Resource
from .pointt.models import JsonScalar
from .write_policies import EnumWritePolicy, NumberWritePolicy

BASE: Final = "/airConditioning"
AC_CONTROL: Final = f"{BASE}/acControl"
OPERATION_MODE: Final = f"{BASE}/operationMode"
TEMPERATURE_SETPOINT: Final = f"{BASE}/temperatureSetpoint"
ROOM_TEMPERATURE: Final = f"{BASE}/roomTemperature"
FAN_SPEED: Final = f"{BASE}/fanSpeed"
AIR_FLOW_VERTICAL: Final = f"{BASE}/airFlowVertical"
AIR_FLOW_HORIZONTAL: Final = f"{BASE}/airFlowHorizontal"
QUICK_AIR_FLOWS: Final = f"{BASE}/quickAirFlows"
ECO_MODE: Final = f"{BASE}/ecoMode"
FULL_POWER_MODE: Final = f"{BASE}/fullPowerMode"
SLEEP_MODE: Final = f"{BASE}/sleepMode"
SETBACK_MODE: Final = f"{BASE}/setbackMode"
AIR_PURIFICATION_MODE: Final = f"{BASE}/airPurificationMode"
TIMER_ON: Final = f"{BASE}/timers/on"
TIMER_OFF: Final = f"{BASE}/timers/off"
SCHEDULE_BASE: Final = f"{BASE}/switchPrograms"
SCHEDULE_ENABLED: Final = f"{BASE}/switchPrograms/enabled"
ACTIVE_PROGRAM: Final = f"{BASE}/switchPrograms/activeProgram"
SCHEDULE_CONFIGURATION: Final = f"{BASE}/switchPrograms/configuration"
TEMPORARY_SETTING: Final = f"{BASE}/switchPrograms/temporarySetting/enabled"
STANDARD_FUNCTIONS: Final = f"{BASE}/standardFunctions"
ADVANCED_FUNCTIONS: Final = f"{BASE}/advancedFunctions"
PV_ENABLE: Final = "/pv/enable"

# The extra functions the app switches over PointT, one path each; the path
# is never put together from a free name (only these five).
ADVANCED_FUNCTION_PATHS: Final = (
    FULL_POWER_MODE,
    SLEEP_MODE,
    SETBACK_MODE,
    AIR_PURIFICATION_MODE,
    ECO_MODE,
)

# The functions ``standardFunctions`` lists (each also needs its own leaf).
STANDARD_FUNCTION_PATHS: Final = frozenset(
    {
        AC_CONTROL,
        OPERATION_MODE,
        TEMPERATURE_SETPOINT,
        FAN_SPEED,
        AIR_FLOW_VERTICAL,
        AIR_FLOW_HORIZONTAL,
        QUICK_AIR_FLOWS,
    }
)

STRING_TYPES: Final = frozenset({"stringValue"})
ON_OFF: Final = frozenset({"on", "off"})
OPERATION_MODES: Final = frozenset({"auto", "cool", "heat", "dry", "fanOnly"})
# The app refuses these modes while the unit runs on solar power.
PV_BLOCKED_MODES: Final = frozenset({"auto", "heat"})
FAN_SPEEDS: Final = frozenset({"auto", "quiet", "low", "mid", "high", "turbo"})
VERTICAL_AIR_FLOWS: Final = frozenset(
    {"on", "off", "auto", "swing", *(f"angle{index}" for index in range(1, 6))}
)
HORIZONTAL_AIR_FLOWS: Final = frozenset(
    {"on", "off", "auto", "swing", "left", "center", "right", "wide", "ultraWide"}
    | {"fixed"}
)
QUICK_AIR_FLOW_VALUES: Final = frozenset(
    {"on", "off"}
    | {
        f"{row}{column}"
        for row in ("top", "bottom")
        for column in ("Left", "Center", "Right")
    }
)
# The app takes the program number from the last character of its ID.
PROGRAM_IDS: Final = frozenset(f"program{index}" for index in range(1, 10))

# A room unit: a setpoint outside this range is refused even when the
# reported limits are wider (broken metadata).
PLAUSIBLE_SETPOINT_C: Final = (10.0, 40.0)
# The app sets timers in steps of 30 minutes.
TIMER_STEP_MIN: Final = 30.0
# A running timer counts down between the PUT and the read-back.
TIMER_READ_BACK_TOLERANCE_MIN: Final = 2.0

# Refusal tokens; each is the translation key of the error shown.
SWITCHED_OFF: Final = "aircon_switched_off"
PV_MODE_BLOCKED: Final = "aircon_pv_mode_blocked"
VALUE_NOT_ALLOWED: Final = "aircon_value_not_allowed"


def _reading(related: Mapping[str, Resource], path: str) -> object:
    resource = related.get(path)
    return resource.value if resource is not None and resource.has_value else None


def operation_mode_check(
    resource: Resource, value: JsonScalar, related: Mapping[str, Resource]
) -> str | None:
    """A mode change needs a switched-on unit (one path per command) and is
    refused for auto and heat while solar operation is on (as the app)."""
    if _reading(related, AC_CONTROL) != "on":
        return SWITCHED_OFF
    if value in PV_BLOCKED_MODES and _reading(related, PV_ENABLE) == "on":
        return PV_MODE_BLOCKED
    return None


def setpoint_check(
    resource: Resource, value: JsonScalar, related: Mapping[str, Resource]
) -> str | None:
    """Only a plausible room temperature in Celsius."""
    low, high = PLAUSIBLE_SETPOINT_C
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not low <= value <= high
    ):
        return VALUE_NOT_ALLOWED
    return None


def timer_check(
    resource: Resource, value: JsonScalar, related: Mapping[str, Resource]
) -> str | None:
    """0 stops a timer; any other value lies on the 30-minute grid counted
    from the reported minimum."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return VALUE_NOT_ALLOWED
    if value == 0:
        return None
    minimum = resource.metadata.minimum
    if minimum is None:
        return VALUE_NOT_ALLOWED
    ticks = (value - minimum) / TIMER_STEP_MIN
    if not math.isclose(ticks, round(ticks), abs_tol=1e-7):
        return VALUE_NOT_ALLOWED
    return None


def timer_confirmed(read: object, requested: JsonScalar) -> bool:
    """A running timer has counted down a little by the read-back."""
    if (
        isinstance(read, bool)
        or not isinstance(read, int | float)
        or isinstance(requested, bool)
        or not isinstance(requested, int | float)
    ):
        return False
    if requested == 0:
        return bool(read == 0)
    return requested - TIMER_READ_BACK_TOLERANCE_MIN <= read <= requested


def temporary_setting_check(
    resource: Resource, value: JsonScalar, related: Mapping[str, Resource]
) -> str | None:
    """A temporary change of the schedule can only be ended (as the app)."""
    return None if value == "off" else VALUE_NOT_ALLOWED


def _switch(path: str) -> EnumWritePolicy:
    return EnumWritePolicy(
        f"^{path}$",
        STRING_TYPES,
        ON_OFF,
        known_only=True,
        enabled_by_default=False,
        on_value="on",
        off_value="off",
        min_options=2,
    )


AC_CONTROL_POLICY: Final = _switch(AC_CONTROL)
ADVANCED_FUNCTION_POLICIES: Final = {
    path: _switch(path) for path in ADVANCED_FUNCTION_PATHS
}
SCHEDULE_ENABLED_POLICY: Final = _switch(SCHEDULE_ENABLED)
OPERATION_MODE_POLICY: Final = EnumWritePolicy(
    f"^{OPERATION_MODE}$",
    STRING_TYPES,
    OPERATION_MODES,
    known_only=True,
    value_check=operation_mode_check,
)
FAN_SPEED_POLICY: Final = EnumWritePolicy(
    f"^{FAN_SPEED}$", STRING_TYPES, FAN_SPEEDS, known_only=True, min_options=2
)
AIR_FLOW_VERTICAL_POLICY: Final = EnumWritePolicy(
    f"^{AIR_FLOW_VERTICAL}$",
    STRING_TYPES,
    VERTICAL_AIR_FLOWS,
    known_only=True,
    min_options=2,
)
AIR_FLOW_HORIZONTAL_POLICY: Final = EnumWritePolicy(
    f"^{AIR_FLOW_HORIZONTAL}$",
    STRING_TYPES,
    HORIZONTAL_AIR_FLOWS,
    known_only=True,
    min_options=2,
)
QUICK_AIR_FLOWS_POLICY: Final = EnumWritePolicy(
    f"^{QUICK_AIR_FLOWS}$",
    STRING_TYPES,
    QUICK_AIR_FLOW_VALUES,
    known_only=True,
    enabled_by_default=False,
    min_options=2,
)
ACTIVE_PROGRAM_POLICY: Final = EnumWritePolicy(
    f"^{ACTIVE_PROGRAM}$",
    STRING_TYPES,
    PROGRAM_IDS,
    known_only=True,
    enabled_by_default=False,
    min_options=2,
)
TEMPORARY_SETTING_POLICY: Final = EnumWritePolicy(
    f"^{TEMPORARY_SETTING}$",
    STRING_TYPES,
    ON_OFF,
    known_only=True,
    enabled_by_default=False,
    value_check=temporary_setting_check,
)
SETPOINT_POLICY: Final = NumberWritePolicy(
    f"^{TEMPERATURE_SETPOINT}$",
    "C",
    1.0,
    strict_grid=True,
    create_entity=False,
    value_check=setpoint_check,
)
TIMER_POLICY: Final = NumberWritePolicy(
    rf"^{BASE}/timers/(?:on|off)$",
    "mins",
    TIMER_STEP_MIN,
    enabled_by_default=False,
    sentinel_value=0.0,
    value_check=timer_check,
    confirm_if=timer_confirmed,
)

AIRCON_ENUM_POLICIES: Final[tuple[EnumWritePolicy, ...]] = (
    AC_CONTROL_POLICY,
    OPERATION_MODE_POLICY,
    FAN_SPEED_POLICY,
    AIR_FLOW_VERTICAL_POLICY,
    AIR_FLOW_HORIZONTAL_POLICY,
    QUICK_AIR_FLOWS_POLICY,
    *ADVANCED_FUNCTION_POLICIES.values(),
    SCHEDULE_ENABLED_POLICY,
    ACTIVE_PROGRAM_POLICY,
    TEMPORARY_SETTING_POLICY,
)
AIRCON_NUMBER_POLICIES: Final[tuple[NumberWritePolicy, ...]] = (
    SETPOINT_POLICY,
    TIMER_POLICY,
)

# What a write reads together with its own path right before the PUT.
WRITE_GUARDS: Final[Mapping[str, tuple[str, ...]]] = {
    OPERATION_MODE: (AC_CONTROL, PV_ENABLE),
}

# Read in one bulk request per cycle (the app's fixed list without the
# solar commissioning details, which are neither shown nor written).
POLL_PATHS: Final = (
    AC_CONTROL,
    OPERATION_MODE,
    TEMPERATURE_SETPOINT,
    ROOM_TEMPERATURE,
    FAN_SPEED,
    AIR_FLOW_HORIZONTAL,
    AIR_FLOW_VERTICAL,
    QUICK_AIR_FLOWS,
    *ADVANCED_FUNCTION_PATHS,
    TIMER_ON,
    TIMER_OFF,
    SCHEDULE_ENABLED,
    ACTIVE_PROGRAM,
    SCHEDULE_CONFIGURATION,
    TEMPORARY_SETTING,
    STANDARD_FUNCTIONS,
    ADVANCED_FUNCTIONS,
    PV_ENABLE,
)
