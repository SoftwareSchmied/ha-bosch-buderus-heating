"""RRC 2.0 contract of the zones: climate, temporary setpoint, details.

Owned by work package W1. See ``docs/dev/rrc-packages.md`` for what each
table does. Everything here is "expected, untested" on a CT200; the TC100.2
confirmed only the zone readings, ``userMode`` and the manual setpoint.

- ``clockOverride/temperatureHeating`` (the temporary setpoint) is written
  only in the mode ``clock``. ``0.0`` clears it; any other value lies at or
  above the minimum of the manual setpoint. It lasts until the next switch
  point of the program (P33). A reading of 0 after a write that replaced an
  active one means that the controller ended it (``rrc_override_ended``).
- The zone details (next setpoint, status, open window, valve, optimum
  start) are read only; their entities live in ``rrc_zone_entities``.
- Open window detection: a switch per zone and for the system, the duration
  and temperature of the system as numbers.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from .pointt import Resource
from .pointt.models import JsonScalar
from .poll_group import PollGroup
from .rrc_contract_base import RRC_ID, RrcContract
from .write_policies import EnumWritePolicy, NumberWritePolicy

ZONE_ROOT: Final = re.compile(rf"^(/zones/zn{RRC_ID})/")
OVERRIDE_LEAF: Final = "clockOverride/temperatureHeating"
OVERRIDE_PATTERN: Final = rf"^/zones/zn{RRC_ID}/clockOverride/temperatureHeating$"
# The zone leaves W1 reads besides the ones of phase 1 (CT-C06, CT-G07).
NEXT_SETPOINT: Final = "nextSetpoint"
MINUTES_TO_NEXT: Final = "timeToNextSetpoint"
STATUS: Final = "status"
WINDOW_ENABLED: Final = "openWindowDetection/enabled"
WINDOW_STATUS: Final = "openWindowDetection/status"
VALVE_POSITION: Final = "actualValvePosition"
OPTIMUM_STATE: Final = "optimumStartState"
OPTIMUM_RATE: Final = "optimumStartHeatupRate"
DETAIL_FIELDS: Final = (
    NEXT_SETPOINT,
    MINUTES_TO_NEXT,
    STATUS,
    WINDOW_ENABLED,
    WINDOW_STATUS,
    VALVE_POSITION,
    OPTIMUM_STATE,
    OPTIMUM_RATE,
)
# P07: the controller sends this when no further setpoint is planned.
NO_NEXT_SETPOINT: Final = 999.0
# RoomStatus of the controller -> option key of the zone status.
ZONE_STATUS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "idle": "idle",
        "heat request": "heat_request",
        "zone error": "zone_error",
        "comm error": "comm_error",
        "comm warning": "comm_warning",
        "low battery": "low_battery",
    }
)
# Open window status -> open; other values stay unknown (expected values).
WINDOW_OPEN: Final[Mapping[str, bool]] = MappingProxyType(
    {"open": True, "closed": False}
)

SYSTEM_WINDOW_ENABLED: Final = "/system/openWindowDetection/enabled"
SYSTEM_WINDOW_DURATION: Final = "/system/openWindowDetection/duration"
SYSTEM_WINDOW_TEMPERATURE: Final = "/system/openWindowDetection/temperature"
_ZONE_WINDOW: Final = rf"^/zones/zn{RRC_ID}/openWindowDetection/enabled$"
_SYSTEM_WINDOW: Final = rf"^{SYSTEM_WINDOW_ENABLED}$"
_SYSTEM_DURATION: Final = rf"^{SYSTEM_WINDOW_DURATION}$"
_SYSTEM_TEMPERATURE: Final = rf"^{SYSTEM_WINDOW_TEMPERATURE}$"
_FAST_DETAILS: Final = (
    rf"^/zones/zn{RRC_ID}/(?:nextSetpoint|timeToNextSetpoint|status"
    r"|openWindowDetection/status)$"
)
# The valve and the optimum start change slowly (radio devices, >= 15 min).
_SLOW_DETAILS: Final = (
    rf"^/zones/zn{RRC_ID}/(?:actualValvePosition|optimumStartState"
    r"|optimumStartHeatupRate)$"
)

# The value of the temporary setpoint before each write, so that a reading of
# 0 can tell "ended" (an active one was replaced) from "not applied yet" (none
# was active). Keyed by path and requested value; bounded.
_OVERRIDE_BEFORE: dict[tuple[str, float], float] = {}
_MAX_REMEMBERED: Final = 64


def zone_root(path: str) -> str | None:
    """``/zones/znN`` of a zone leaf, or ``None``."""
    match = ZONE_ROOT.match(path)
    return match.group(1) if match else None


def _number(resource: Resource | None) -> float | None:
    if resource is None or not resource.has_value:
        return None
    value = resource.value
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
    ):
        return None
    return float(value)


def _override_value_check(
    resource: Resource, value: JsonScalar, related: Mapping[str, Resource]
) -> str | None:
    """Only in the mode ``clock``; 0 clears, others at or above the manual
    setpoint's minimum (P23, P06)."""
    root = zone_root(resource.path)
    if root is None:
        return "rrc_override_needs_clock_mode"
    mode = related.get(f"{root}/userMode")
    if mode is None or not mode.has_value or mode.value != "clock":
        return "rrc_override_needs_clock_mode"
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "rrc_override_below_minimum"
    if value != 0:
        manual = related.get(f"{root}/manualTemperatureHeating")
        minimum = manual.metadata.minimum if manual is not None else None
        if minimum is None or not math.isfinite(minimum) or value < minimum:
            return "rrc_override_below_minimum"
    before = _number(resource)
    if before is not None:
        key = (resource.path, float(value))
        _OVERRIDE_BEFORE.pop(key, None)
        _OVERRIDE_BEFORE[key] = before
        while len(_OVERRIDE_BEFORE) > _MAX_REMEMBERED:
            del _OVERRIDE_BEFORE[next(iter(_OVERRIDE_BEFORE))]
    return None


def _override_read_back(resource: Resource, value: JsonScalar) -> str | None:
    """A reading of 0 after replacing an active temporary setpoint: the
    controller ended it (a switch point was reached), it will not come."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or value == 0
        or _number(resource) != 0
    ):
        return None
    before = _OVERRIDE_BEFORE.get((resource.path, float(value)))
    return "rrc_override_ended" if before not in (None, 0) else None


OVERRIDE_POLICY: Final = NumberWritePolicy(
    OVERRIDE_PATTERN,
    "C",
    0.5,
    # Written by the zone's climate entity and button, no number of its own.
    create_entity=False,
    strict_grid=True,
    value_check=_override_value_check,
    read_back_check=_override_read_back,
)
_ON_OFF: Final = frozenset({"on", "off"})
ZONE_WINDOW_POLICY: Final = EnumWritePolicy(
    _ZONE_WINDOW,
    frozenset({"stringValue"}),
    _ON_OFF,
    known_only=True,
    options_rule="subset",
    fallback_options=("on", "off"),
    on_value="on",
    off_value="off",
)
SYSTEM_WINDOW_POLICY: Final = EnumWritePolicy(
    _SYSTEM_WINDOW,
    frozenset({"stringValue"}),
    _ON_OFF,
    known_only=True,
    options_rule="subset",
    fallback_options=("on", "off"),
    on_value="on",
    off_value="off",
    enabled_by_default=False,
)
SYSTEM_DURATION_POLICY: Final = NumberWritePolicy(
    _SYSTEM_DURATION, "mins", 5.0, enabled_by_default=False, strict_grid=True
)
SYSTEM_TEMPERATURE_POLICY: Final = NumberWritePolicy(
    _SYSTEM_TEMPERATURE, "C", 0.5, enabled_by_default=False, strict_grid=True
)

# German and English names of the zone detail entities (rrc_zone_entities).
NAMES: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        NEXT_SETPOINT: ("Nächster Sollwert", "Next setpoint"),
        MINUTES_TO_NEXT: (
            "Minuten bis zum nächsten Sollwert",
            "Minutes to the next setpoint",
        ),
        STATUS: ("Zonenstatus", "Zone status"),
        WINDOW_STATUS: ("Fenster offen", "Window open"),
        VALVE_POSITION: ("Ventilöffnung", "Valve opening"),
        OPTIMUM_STATE: ("Optimierter Start", "Optimum start"),
        OPTIMUM_RATE: ("Aufheizrate optimierter Start", "Optimum start heat-up rate"),
        "clear_override": (
            "Vorübergehenden Sollwert aufheben",
            "Clear temporary setpoint",
        ),
    }
)


def _diagnostics(resources: Mapping[str, Resource]) -> Mapping[str, object]:
    """S12: counts only, never a value, name or zone number."""
    zones: set[str] = set()
    counts = dict.fromkeys(
        (
            "override_paths",
            "overrides_active",
            "next_setpoint_none",
            "status_unknown",
            "window_status_unknown",
            "window_detection_paths",
        ),
        0,
    )
    for path, resource in resources.items():
        root = zone_root(path)
        if root is None:
            continue
        zones.add(root)
        leaf = path[len(root) + 1 :]
        value = resource.value if resource.has_value else None
        if leaf == OVERRIDE_LEAF:
            counts["override_paths"] += 1
            number = _number(resource)
            counts["overrides_active"] += number is not None and number != 0
        elif leaf == NEXT_SETPOINT:
            counts["next_setpoint_none"] += _number(resource) == NO_NEXT_SETPOINT
        elif leaf == STATUS:
            counts["status_unknown"] += value not in ZONE_STATUS
        elif leaf == WINDOW_STATUS:
            counts["window_status_unknown"] += value not in WINDOW_OPEN
        elif leaf == WINDOW_ENABLED:
            counts["window_detection_paths"] += 1
    return {"zones": len(zones), **counts}


CONTRACT = RrcContract(
    name="zone",
    enum_policies=(ZONE_WINDOW_POLICY, SYSTEM_WINDOW_POLICY),
    number_policies=(
        OVERRIDE_POLICY,
        SYSTEM_DURATION_POLICY,
        SYSTEM_TEMPERATURE_POLICY,
    ),
    poll_groups=MappingProxyType(
        {
            _FAST_DETAILS: PollGroup.FAST,
            _SLOW_DETAILS: PollGroup.SLOW,
            _ZONE_WINDOW: PollGroup.SLOW,
            _SYSTEM_WINDOW: PollGroup.SLOW,
            _SYSTEM_DURATION: PollGroup.SLOW,
            _SYSTEM_TEMPERATURE: PollGroup.SLOW,
        }
    ),
    names=MappingProxyType(
        {
            _ZONE_WINDOW: ("Fenster-offen-Erkennung", "Open window detection"),
            _SYSTEM_WINDOW: ("Fenster-offen-Erkennung", "Open window detection"),
            _SYSTEM_DURATION: ("Fenster-offen-Dauer", "Open window duration"),
            _SYSTEM_TEMPERATURE: (
                "Fenster-offen-Temperatur",
                "Open window temperature",
            ),
        }
    ),
    entity_keys=MappingProxyType(
        {
            _ZONE_WINDOW: MappingProxyType({"switch": "rrc_window_detection"}),
            _SYSTEM_WINDOW: MappingProxyType({"switch": "rrc_system_window_detection"}),
            _SYSTEM_DURATION: MappingProxyType({"number": "rrc_window_duration"}),
            _SYSTEM_TEMPERATURE: MappingProxyType({"number": "rrc_window_temperature"}),
        }
    ),
    read_only_mirrors=(_SYSTEM_WINDOW, _SYSTEM_DURATION, _SYSTEM_TEMPERATURE),
    zone_fields=DETAIL_FIELDS,
    diagnostics=_diagnostics,
    understood=(_SYSTEM_WINDOW, _SYSTEM_DURATION, _SYSTEM_TEMPERATURE),
    categories=MappingProxyType(
        {
            _ZONE_WINDOW: "config",
            _SYSTEM_WINDOW: "config",
            _SYSTEM_DURATION: "config",
            _SYSTEM_TEMPERATURE: "config",
        }
    ),
    icons=MappingProxyType(
        {
            _ZONE_WINDOW: "mdi:window-open-variant",
            _SYSTEM_WINDOW: "mdi:window-open-variant",
            _SYSTEM_DURATION: "mdi:timer-outline",
            _SYSTEM_TEMPERATURE: "mdi:thermometer",
        }
    ),
    texts=tuple(NAMES.values()),
)
