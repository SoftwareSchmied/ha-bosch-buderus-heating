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
  and temperature of the system as numbers. A zone switch is written only
  while the system's detection is offered (not ``available`` false).
- Room control locks (ZONE-02, ZONE-05): a zone the controller reports with
  a zone or communication error, or in standby (``functionIcons`` of the
  zone list), takes no mode or setpoint change; while the away mode is
  active, no setpoint change. The zone list's ``functionIcons`` also tells
  an open window and a boost (ZONE-04).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from contextvars import ContextVar
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
# The zone list: its entries carry ``functionIcons``, the special function a
# zone runs ("" for none). The controller's own display uses it for an open
# window, a boost and the standby of a zone (expected, untested).
ZONE_LIST: Final = "/zones/list"
FUNCTION_WINDOW_OPEN: Final = "window_open"
FUNCTION_BOOST: Final = "boost"
FUNCTION_STANDBY: Final = "standby"
# A zone status that takes no room control (ZONE-05).
UNREACHABLE_STATUS: Final = frozenset({"zone error", "comm error"})
# The away mode of the system, a string flag "true"/"false" (W2).
AWAY_ENABLED: Final = "/system/awayMode/enabled"
_ACTIVE: Final = frozenset({"true", "on"})
# The lock of a zone's room control -> the refusal token (translation key).
LOCK_KEYS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "unreachable": "rrc_zone_unreachable",
        "standby": "rrc_zone_standby",
        "away": "rrc_zone_locked_away",
    }
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

# The value of the temporary setpoint before a write, so that a reading of 0
# can tell "ended" (an active one was replaced) from "not applied yet" (none
# was active): (path, requested value, value before). ZONE-15: a context
# variable, so that the write of one gateway never sees another's; the check
# and the read-back of one write run in the same task.
_OVERRIDE_BEFORE: ContextVar[tuple[str, float, float] | None] = ContextVar(
    "rrc_override_before", default=None
)


def zone_root(path: str) -> str | None:
    """``/zones/znN`` of a zone leaf, or ``None``."""
    match = ZONE_ROOT.match(path)
    return match.group(1) if match else None


def zone_number(root: str) -> int | None:
    """The number ``N`` of a zone root ``/zones/znN``."""
    match = re.fullmatch(rf"/zones/zn({RRC_ID})", root)
    return int(match[1]) if match else None


def function_icon(zone_list: Resource | None, root: str) -> str | None:
    """The special function the zone list shows for a zone: ``""`` for none,
    ``"window_open"``, ``"boost"``, ``"standby"`` or another token; ``None``
    when the list does not say (no valid list, no entry, no field)."""
    number = zone_number(root)
    if (
        zone_list is None
        or number is None
        or zone_list.path != ZONE_LIST
        or zone_list.metadata.resource_type != "zoneArray"
        or not zone_list.metadata.availability_flags_valid
        or zone_list.metadata.used is False
        or zone_list.metadata.available is False
        or not zone_list.has_value
        or not isinstance(zone_list.value, list)
    ):
        return None
    for item in zone_list.value:
        if (
            isinstance(item, dict)
            and type(item.get("id")) is int
            and item.get("id") == number
        ):
            icon = item.get("functionIcons")
            return icon if isinstance(icon, str) and len(icon) <= 32 else None
    return None


def away_active(resource: Resource | None) -> bool | None:
    """Whether the away mode is on; ``None`` without a reading of it."""
    if resource is None or resource.path != AWAY_ENABLED or not resource.has_value:
        return None
    value = resource.value
    return isinstance(value, str) and value.strip().lower() in _ACTIVE


def zone_lock(
    root: str, readings: Mapping[str, Resource], *, setpoint: bool
) -> str | None:
    """Why a zone takes no room control change now, or ``None``.

    ``unreachable`` for a zone or communication error, ``standby`` for a
    zone in standby; both lock the mode and the setpoints. ``away`` while the
    away mode is active locks the setpoints only (``setpoint``). ``readings``
    are the current readings; a missing one locks nothing. The flags of the
    readings do not matter: a lock that is shown is kept (expected,
    untested).
    """
    status = readings.get(f"{root}/{STATUS}")
    if (
        status is not None
        and status.has_value
        and isinstance(status.value, str)
        and status.value in UNREACHABLE_STATUS
    ):
        return "unreachable"
    if function_icon(readings.get(ZONE_LIST), root) == FUNCTION_STANDBY:
        return "standby"
    if setpoint and away_active(readings.get(AWAY_ENABLED)):
        return "away"
    return None


def window_open(icon: str | None, status: bool | None) -> bool | None:
    """ZONE-04: an open window from the zone list's function, which the
    controller's own display uses: ``window_open`` is open, no function is
    closed. Only when the list does not say (no field, or another function
    such as a boost) the status leaf counts, whose values are expected only;
    otherwise unknown."""
    if icon == FUNCTION_WINDOW_OPEN:
        return True
    if icon == "":
        return False
    return status


def _lock_token(resource: Resource, related: Mapping[str, Resource]) -> str | None:
    root = zone_root(resource.path)
    setpoint = not resource.path.endswith("/userMode")
    lock = zone_lock(root, related, setpoint=setpoint) if root else None
    return LOCK_KEYS[lock] if lock is not None else None


def manual_value_check(
    resource: Resource, value: JsonScalar, related: Mapping[str, Resource]
) -> str | None:
    """The manual setpoint: no change while the zone is locked (ZONE-02/05)."""
    del value
    return _lock_token(resource, related)


def mode_value_check(
    resource: Resource, value: JsonScalar, related: Mapping[str, Resource]
) -> str | None:
    """The zone mode: no change while the zone is unreachable or in standby
    (ZONE-05); the away mode does not lock it."""
    del value
    return _lock_token(resource, related)


def _zone_window_check(
    resource: Resource, value: JsonScalar, related: Mapping[str, Resource]
) -> str | None:
    """ZONE-01: a zone's open window detection only while the system offers
    its own (not ``available`` false, valid flags)."""
    del resource, value
    system = related.get(SYSTEM_WINDOW_ENABLED)
    if system is None:
        return None
    metadata = system.metadata
    if not metadata.availability_flags_valid or metadata.available is False:
        return "rrc_window_detection_unused"
    return None


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
    if (lock := _lock_token(resource, related)) is not None:
        return lock
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
    _OVERRIDE_BEFORE.set(
        (resource.path, float(value), before) if before is not None else None
    )
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
    remembered = _OVERRIDE_BEFORE.get()
    if remembered is None or remembered[:2] != (resource.path, float(value)):
        return None
    return "rrc_override_ended" if remembered[2] != 0 else None


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
    value_check=_zone_window_check,
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
            "override_clear_unsupported",
            "zones_with_function",
        ),
        0,
    )
    zone_list = resources.get(ZONE_LIST)
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
            minimum = resource.metadata.minimum
            # ZONE-09: 0 clears it; a minimum above 0 leaves no clear button.
            counts["override_clear_unsupported"] += minimum is None or minimum > 0
        elif leaf == NEXT_SETPOINT:
            counts["next_setpoint_none"] += _number(resource) == NO_NEXT_SETPOINT
        elif leaf == STATUS:
            counts["status_unknown"] += value not in ZONE_STATUS
        elif leaf == WINDOW_STATUS:
            counts["window_status_unknown"] += value not in WINDOW_OPEN
        elif leaf == WINDOW_ENABLED:
            counts["window_detection_paths"] += 1
    counts["zones_with_function"] = sum(
        function_icon(zone_list, root) not in (None, "") for root in zones
    )
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
            # ZONE-04/05: the special function of each zone (open window,
            # standby), with the modes rather than hourly.
            rf"^{ZONE_LIST}$": PollGroup.CONTROL,
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
