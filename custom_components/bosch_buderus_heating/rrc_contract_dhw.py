"""RRC 2.0 contract of the hot water: mode, extra hot water, disinfection.

Owned by work package W3; see ``docs/dev/rrc-packages.md`` for what each
table does. Everything here applies only to a recognised RRC controller
(S11) and is expected from the controller's data shapes, still untested on
a real one (CT-G03, CT-D06):

- ``operationMode`` without an ``allowedValues`` key offers "Off", "high"
  and "ownprogram"; "low" and "eco" mean different things depending on the
  heat source and are offered only when the controller advertises them,
  reads one of them now (then all five, the modes of a heat pump) or its
  heat source type reads as a heat pump or hybrid plant (VER-03: always
  all five, so Eco can be chosen again after Comfort). An advertised empty
  list leaves the mode a reading. "Off" is written as
  such; a reading of "off" confirms it.
- ``temperatureLevels/high`` uses the advertised step (S4), so that a value
  the controller would round is refused rather than never confirmed.
- Extra hot water: a switch and an integer duration, each its own write.
  The switch is not turned on while the duration is 0 or unknown
  (``rrc_extra_dhw_duration_zero``); the controls dialog writes the
  duration first.
- Thermal disinfection: a switch, a weekday and a start time (integer
  minutes after midnight in five-minute steps unless the controller
  advertises a step, a time entity), disabled by default.
- The hot water program is only read (``rrc_dhw``).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from . import rrc_dhw
from .pointt import Resource
from .pointt.models import JsonScalar
from .rrc_contract_base import RrcContract
from .write_policies import EnumWritePolicy, NumberWritePolicy

_DHW: Final = rrc_dhw.CIRCUIT
MODE: Final = rf"^{_DHW}/operationMode$"
LEVEL_HIGH: Final = rf"^{_DHW}/temperatureLevels/high$"
EXTRA: Final = rf"^{_DHW}/extraDhw$"
EXTRA_DURATION: Final = rf"^{_DHW}/extraDhwDuration$"
DISINFECTION: Final = rf"^{_DHW}/thermalDisinfect/state$"
DISINFECTION_DAY: Final = rf"^{_DHW}/thermalDisinfect/weekDay$"
DISINFECTION_TIME: Final = rf"^{_DHW}/thermalDisinfect/time$"
DISINFECTION_RESULT: Final = rf"^{_DHW}/thermalDisinfect/lastResult$"
PROGRAM: Final = rrc_dhw.PROGRAM_WEEK.pattern

# The minute spellings; another time unit would change the meaning.
_MINUTES: Final = frozenset({"m", "min", "mins", "minutes"})
# Mo to Su, and "Ed" for every day.
WEEKDAYS: Final = (*rrc_dhw.DAYS, "Ed")
_WEEKDAY_KEYS: Final = MappingProxyType(
    {
        "Mo": "monday",
        "Tu": "tuesday",
        "We": "wednesday",
        "Th": "thursday",
        "Fr": "friday",
        "Sa": "saturday",
        "Su": "sunday",
        "Ed": "daily",
    }
)
TIME_OF_DAY_INVALID: Final = "rrc_time_of_day_invalid"
EXTRA_DURATION_ZERO: Final = "rrc_extra_dhw_duration_zero"
_EXTRA_LEAF: Final = "/extraDhw"
_DURATION_LEAF: Final = "/extraDhwDuration"


def _time_of_day(
    _resource: Resource, value: JsonScalar, _related: Mapping[str, Resource]
) -> str | None:
    """A start time is a minute of the day, even if the limits allow more."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not 0 <= value < rrc_dhw.MINUTES_PER_DAY
    ):
        return TIME_OF_DAY_INVALID
    return None


def duration_path(path: str) -> str:
    """The duration that belongs to an extra hot water switch."""
    return path.removesuffix(_EXTRA_LEAF) + _DURATION_LEAF


def _extra_duration_set(
    resource: Resource, value: JsonScalar, related: Mapping[str, Resource]
) -> str | None:
    """Extra hot water is turned on only with a duration above 0 minutes.

    ``related`` holds the current readings; a circuit without a current
    duration reading keeps the switch, a duration without a valid number
    refuses as well. Turning it off is never refused.
    """
    if value != "on":
        return None
    duration = related.get(duration_path(resource.path))
    if duration is None:
        return None
    minutes = duration.value if duration.has_value else None
    if (
        isinstance(minutes, bool)
        or not isinstance(minutes, int | float)
        or not math.isfinite(minutes)
        or minutes <= 0
    ):
        return EXTRA_DURATION_ZERO
    return None


OPERATION_MODE_POLICY: Final = EnumWritePolicy(
    MODE,
    frozenset({"stringValue"}),
    frozenset({"Off", "off", "low", "high", "ownprogram", "eco"}),
    fallback_options=("Off", "high", "ownprogram"),
    # The eco levels of a heat pump, while the controller reads one of them,
    # and always on a heat pump or hybrid plant (VER-03).
    fallback_current_options=("low", "eco"),
    heat_pump_options=("low", "eco"),
    equivalent_reads=MappingProxyType(
        {"Off": frozenset({"off"}), "off": frozenset({"Off"})}
    ),
)
EXTRA_DHW_POLICY: Final = EnumWritePolicy(
    EXTRA,
    frozenset({"stringValue"}),
    frozenset({"on", "off"}),
    known_only=True,
    fallback_options=("on", "off"),
    on_value="on",
    off_value="off",
    value_check=_extra_duration_set,
)
DISINFECTION_POLICY: Final = EnumWritePolicy(
    DISINFECTION,
    frozenset({"stringValue"}),
    frozenset({"on", "off"}),
    known_only=True,
    enabled_by_default=False,
    fallback_options=("on", "off"),
    on_value="on",
    off_value="off",
)
DISINFECTION_DAY_POLICY: Final = EnumWritePolicy(
    DISINFECTION_DAY,
    frozenset({"stringValue"}),
    frozenset(WEEKDAYS),
    known_only=True,
    enabled_by_default=False,
    fallback_options=WEEKDAYS,
)
LEVEL_HIGH_POLICY: Final = NumberWritePolicy(LEVEL_HIGH, "C", 1.0, strict_grid=True)
EXTRA_DURATION_POLICY: Final = NumberWritePolicy(
    EXTRA_DURATION,
    "mins",
    15.0,
    wire_integer=True,
    strict_grid=True,
    accepted_units=_MINUTES,
)
# Written by the time entity of ``rrc_dhw_entities``, not as a number; five
# minute steps unless the controller advertises another step.
DISINFECTION_TIME_POLICY: Final = NumberWritePolicy(
    DISINFECTION_TIME,
    "mins",
    5.0,
    enabled_by_default=False,
    wire_integer=True,
    strict_grid=True,
    accepted_units=_MINUTES,
    create_entity=False,
    value_check=_time_of_day,
)

NAMES: Final = MappingProxyType(
    {
        # The level "high" is the hot water temperature of the controller,
        # not a start temperature as on other gateways.
        LEVEL_HIGH: ("Warmwassertemperatur", "Hot water temperature"),
        EXTRA: ("Extra-Warmwasser", "Extra hot water"),
        EXTRA_DURATION: ("Dauer Extra-Warmwasser", "Extra hot water duration"),
        DISINFECTION: ("Thermische Desinfektion", "Thermal disinfection"),
        DISINFECTION_DAY: (
            "Tag der thermischen Desinfektion",
            "Thermal disinfection day",
        ),
        DISINFECTION_TIME: (
            "Startzeit der thermischen Desinfektion",
            "Thermal disinfection start time",
        ),
        DISINFECTION_RESULT: (
            "Ergebnis der thermischen Desinfektion",
            "Thermal disinfection result",
        ),
        PROGRAM: (
            "Warmwasser-Programmstufe jetzt",
            "Current hot water schedule level",
        ),
    }
)

CONTRACT = RrcContract(
    name="dhw",
    enum_policies=(
        OPERATION_MODE_POLICY,
        EXTRA_DHW_POLICY,
        DISINFECTION_POLICY,
        DISINFECTION_DAY_POLICY,
    ),
    number_policies=(
        LEVEL_HIGH_POLICY,
        EXTRA_DURATION_POLICY,
        DISINFECTION_TIME_POLICY,
    ),
    names=NAMES,
    entity_keys=MappingProxyType(
        {
            MODE: MappingProxyType({"select": "hot_water_operation_mode"}),
            LEVEL_HIGH: MappingProxyType({"number": "rrc_hot_water_temperature"}),
            EXTRA: MappingProxyType({"switch": "rrc_extra_dhw"}),
            EXTRA_DURATION: MappingProxyType({"number": "rrc_extra_dhw_duration"}),
            DISINFECTION: MappingProxyType({"switch": "rrc_thermal_disinfection"}),
            DISINFECTION_DAY: MappingProxyType(
                {"select": "rrc_thermal_disinfection_weekday"}
            ),
            DISINFECTION_TIME: MappingProxyType(
                {"time": "rrc_thermal_disinfection_time"}
            ),
            DISINFECTION_RESULT: MappingProxyType(
                {"sensor": "rrc_thermal_disinfection_result"}
            ),
            PROGRAM: MappingProxyType({"sensor": "rrc_dhw_program_level"}),
        }
    ),
    option_keys=MappingProxyType(
        {
            # The controller may read "Off" back as "off": both are "off".
            MODE: MappingProxyType({"Off": "off", "off": "off"}),
            DISINFECTION_DAY: _WEEKDAY_KEYS,
        }
    ),
    categories=MappingProxyType(
        {
            EXTRA_DURATION: "config",
            DISINFECTION: "config",
            DISINFECTION_DAY: "config",
            DISINFECTION_TIME: "config",
        }
    ),
    icons=MappingProxyType(
        {
            EXTRA: "mdi:water-boiler",
            EXTRA_DURATION: "mdi:timer-outline",
            DISINFECTION: "mdi:bacteria-outline",
            DISINFECTION_DAY: "mdi:calendar-week",
            DISINFECTION_TIME: "mdi:clock-outline",
        }
    ),
    diagnostics=rrc_dhw.diagnostics,
)
