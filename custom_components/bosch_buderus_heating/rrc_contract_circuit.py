"""RRC 2.0 contract of the heating circuit: curve, summer/winter, night.

Owned by work package W4 (CT-G01, CT-G02). Every setting is written through
the generic controls with the W0 write path: metadata limits and steps,
exactly one write and a read-back. Nothing here is confirmed on a real
controller yet ("expected, untested").

- Heating curve, supply limits and room influence: numbers, configuration,
  disabled by default (W4-E1). The base point and the minimum supply
  temperature stay at or below the end point and the maximum supply
  temperature (``rrc_curve_order``); the entities read these limits freshly
  first.
- Summer/winter and night switching with their thresholds: selections and
  numbers, disabled by default. The season mode: a selection, only with at
  least two offered modes, the empty one left out. Setpoint optimization: a
  switch.
- The heating curve, the supply limits, the room influence and the
  summer/winter and night settings act only in weather-compensated control
  (``control`` "weather"); in any other control a write is refused unsent
  (``rrc_weather_control_off``).
- ``control``, ``type`` and ``typeRoomControl`` are installer settings that
  change the control strategy: shown at most, never written. The heat-up
  speed (``buildingHeatup``) is switched off in the manufacturer's own app
  and its effect is unknown: a disabled reading, never written.

See ``docs/dev/rrc-packages.md`` for what each table does.
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
from .rrc_contract_base import Category, RrcContract
from .write_policies import EnumWritePolicy, NumberWritePolicy

# A heating circuit of a room controller (``hc1``; more are only expected).
CIRCUIT: Final = r"/heatingCircuits/hc[1-9][0-9]{0,2}"
_CIRCUIT_LEAF = re.compile(rf"^({CIRCUIT})/([A-Za-z]+)$")


def leaf(name: str) -> str:
    """The pattern of one leaf below every heating circuit."""
    return rf"^{CIRCUIT}/{name}$"


# Each lower limit stays at or below each upper limit: the base point and the
# minimum supply temperature at or below the end point and the maximum supply
# temperature. The two lower limits, and the two upper ones, do not depend on
# each other.
LOWER_LIMITS: Final = ("heatCurveMin", "minSupply")
UPPER_LIMITS: Final = ("heatCurveMax", "maxSupply")
CURVE_ORDER: Final = "rrc_curve_order"
# The control strategy of a circuit; only "weather" (weather-compensated
# control) uses the heating curve and the outdoor temperature settings.
CONTROL_LEAF: Final = "control"
WEATHER_CONTROL: Final = "weather"
WEATHER_CONTROL_OFF: Final = "rrc_weather_control_off"


def limit_role(path: str) -> str | None:
    """``"lower"`` or ``"upper"`` for a heating curve point or supply limit."""
    match = _CIRCUIT_LEAF.fullmatch(path)
    if match is None:
        return None
    if match[2] in LOWER_LIMITS:
        return "lower"
    if match[2] in UPPER_LIMITS:
        return "upper"
    return None


def counterparts(path: str) -> tuple[str, ...]:
    """The limits a heating curve point or supply limit is ordered against."""
    match = _CIRCUIT_LEAF.fullmatch(path)
    role = limit_role(path)
    if match is None or role is None:
        return ()
    others = UPPER_LIMITS if role == "lower" else LOWER_LIMITS
    return tuple(f"{match[1]}/{name}" for name in others)


def control_path(path: str) -> str | None:
    """The control strategy of the circuit a setting belongs to."""
    match = _CIRCUIT_LEAF.fullmatch(path)
    return f"{match[1]}/{CONTROL_LEAF}" if match is not None else None


def _number(resource: Resource) -> float | None:
    value = resource.value
    if (
        not resource.has_value
        or isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
    ):
        return None
    return float(value)


def check_order(
    resource: Resource, value: JsonScalar, related: Mapping[str, Resource]
) -> str | None:
    """Each lower limit at or below each upper limit; equal is allowed.

    ``related`` holds the current readings, with the fresh reads of the
    entity's ``expected`` (all limits). A limit the plant does not have
    leaves only the metadata limits; one without a valid number refuses, as
    the order cannot be checked.
    """
    role = limit_role(resource.path)
    if role is None or isinstance(value, bool) or not isinstance(value, int | float):
        return None
    for other_path in counterparts(resource.path):
        other = related.get(other_path)
        if other is None:
            continue
        bound = _number(other)
        if bound is None:
            return CURVE_ORDER
        if (value > bound) if role == "lower" else (value < bound):
            return CURVE_ORDER
    return None


def check_weather_control(
    resource: Resource, _value: JsonScalar, related: Mapping[str, Resource]
) -> str | None:
    """A setting of weather-compensated control is written only while the
    circuit uses it (``control`` "weather").

    A circuit without a current ``control`` reading keeps the setting; any
    other reading, also one without a value, refuses.
    """
    path = control_path(resource.path)
    control = related.get(path) if path is not None else None
    if control is None:
        return None
    if control.has_value and control.value == WEATHER_CONTROL:
        return None
    return WEATHER_CONTROL_OFF


def weather_control_off(path: str, control: Resource | None) -> bool:
    """Whether ``path`` is a setting of weather-compensated control that its
    circuit does not use now (``control`` is the circuit's current reading)."""
    match = _CIRCUIT_LEAF.fullmatch(path)
    if match is None or match[2] not in WEATHER_SETTINGS or control is None:
        return False
    return check_weather_control(control, None, {control.path: control}) is not None


def guard_paths(path: str) -> tuple[str, ...]:
    """What a write of ``path`` is checked against and reads again first
    (S5): the limits it is ordered against and, for a setting of
    weather-compensated control, the circuit's control strategy."""
    match = _CIRCUIT_LEAF.fullmatch(path)
    if match is None or match[2] not in WEATHER_SETTINGS:
        return ()
    return (*counterparts(path), f"{match[1]}/{CONTROL_LEAF}")


def check_limit(
    resource: Resource, value: JsonScalar, related: Mapping[str, Resource]
) -> str | None:
    """Weather-compensated control first, then the order of the limits."""
    return check_weather_control(resource, value, related) or check_order(
        resource, value, related
    )


_STRING = frozenset({"stringValue"})
# The heating circuit settings do not depend on their ``used`` flag: a
# setting the controller reports as not used stays writable, a used but
# unavailable one is shown and locked (``unused_writable``).
_UNUSED_WRITABLE: Final = True
SWITCH_MODES: Final = ("off", "automatic")
SEASON_MODES: Final = ("always_heating", "always_cooling", "automatic")
# The settings of weather-compensated control (``check_weather_control``).
WEATHER_SETTINGS: Final = (
    *LOWER_LIMITS,
    *UPPER_LIMITS,
    "roomInfluence",
    "suWiSwitchMode",
    "suWiThreshold",
    "nightSwitchMode",
    "nightThreshold",
)


def _limit(name: str) -> NumberWritePolicy:
    """A heating curve or supply limit: 1 K steps, disabled by default."""
    return NumberWritePolicy(
        leaf(name),
        "C",
        1.0,
        enabled_by_default=False,
        strict_grid=True,
        create_entity=False,
        value_check=check_limit,
        unused_writable=_UNUSED_WRITABLE,
    )


def _threshold(name: str) -> NumberWritePolicy:
    """An outdoor temperature threshold: 0.5 K steps, disabled by default."""
    return NumberWritePolicy(
        leaf(name),
        "C",
        0.5,
        enabled_by_default=False,
        strict_grid=True,
        create_entity=False,
        value_check=check_weather_control,
        unused_writable=_UNUSED_WRITABLE,
    )


def _mode(name: str, values: tuple[str, ...]) -> EnumWritePolicy:
    """A switching mode of weather-compensated control, disabled by default.

    The expected shapes and #122 advertise no modes, so the known modes are
    offered for a current known value.
    """
    return EnumWritePolicy(
        leaf(name),
        _STRING,
        frozenset(values),
        known_only=True,
        enabled_by_default=False,
        fallback_options=values,
        value_check=check_weather_control,
        unused_writable=_UNUSED_WRITABLE,
    )


HEAT_CURVE_MIN_POLICY: Final = _limit("heatCurveMin")
HEAT_CURVE_MAX_POLICY: Final = _limit("heatCurveMax")
MIN_SUPPLY_POLICY: Final = _limit("minSupply")
MAX_SUPPLY_POLICY: Final = _limit("maxSupply")
# S2: a whole number (``int`` on the wire), dimensionless ("" or "other").
ROOM_INFLUENCE_POLICY: Final = NumberWritePolicy(
    leaf("roomInfluence"),
    "",
    1.0,
    enabled_by_default=False,
    wire_integer=True,
    strict_grid=True,
    accepted_units=frozenset({"", "other"}),
    ui_unit_override="",
    create_entity=False,
    value_check=check_weather_control,
    unused_writable=_UNUSED_WRITABLE,
)
SUMMER_WINTER_THRESHOLD_POLICY: Final = _threshold("suWiThreshold")
NIGHT_THRESHOLD_POLICY: Final = _threshold("nightThreshold")
NUMBER_POLICIES: Final = (
    HEAT_CURVE_MIN_POLICY,
    HEAT_CURVE_MAX_POLICY,
    MIN_SUPPLY_POLICY,
    MAX_SUPPLY_POLICY,
    ROOM_INFLUENCE_POLICY,
    SUMMER_WINTER_THRESHOLD_POLICY,
    NIGHT_THRESHOLD_POLICY,
)

SUMMER_WINTER_MODE_POLICY: Final = _mode("suWiSwitchMode", SWITCH_MODES)
NIGHT_MODE_POLICY: Final = _mode("nightSwitchMode", SWITCH_MODES)
# Firmware 4.2.0 and later advertise the modes, sometimes with an empty one;
# a single offered mode (#122) leaves the reading only.
SEASON_MODE_POLICY: Final = EnumWritePolicy(
    leaf("seasonOptMode"),
    _STRING,
    frozenset(SEASON_MODES),
    known_only=True,
    ignored_options=frozenset({""}),
    min_options=2,
    unused_writable=_UNUSED_WRITABLE,
)
SETPOINT_OPTIMIZATION_POLICY: Final = EnumWritePolicy(
    leaf("setpointOptimization"),
    _STRING,
    frozenset({"on", "off"}),
    known_only=True,
    fallback_options=("on", "off"),
    on_value="on",
    off_value="off",
    unused_writable=_UNUSED_WRITABLE,
)
ENUM_POLICIES: Final = (
    SUMMER_WINTER_MODE_POLICY,
    NIGHT_MODE_POLICY,
    SEASON_MODE_POLICY,
    SETPOINT_OPTIMIZATION_POLICY,
)

OPERATING_SEASON: Final = leaf("operatingSeason")
OPERATING_SEASONS: Final = ("heating", "cooling", "idle")
# Installer settings that change the control strategy; no policy, so they
# are never written (at most read).
INSTALLER_SETTINGS: Final = ("control", "type", "typeRoomControl")
# Switched off in the manufacturer's own app, which neither reads nor writes
# it; its effect is unknown. No policy: a disabled reading at most.
READ_ONLY_SETTINGS: Final = ("buildingHeatup",)

# Leaf -> (German, English) name, translation key, icon.
_SETTINGS: Final[Mapping[str, tuple[tuple[str, str], str, str]]] = MappingProxyType(
    {
        "heatCurveMin": (
            ("Heizkurve Fußpunkt", "Heating curve base point"),
            "rrc_heat_curve_min",
            "mdi:chart-bell-curve-cumulative",
        ),
        "heatCurveMax": (
            ("Heizkurve Endpunkt", "Heating curve end point"),
            "rrc_heat_curve_max",
            "mdi:chart-bell-curve-cumulative",
        ),
        "minSupply": (
            ("Minimale Vorlauftemperatur", "Minimum supply temperature"),
            "rrc_min_supply",
            "mdi:thermometer-chevron-down",
        ),
        "maxSupply": (
            ("Maximale Vorlauftemperatur", "Maximum supply temperature"),
            "rrc_max_supply",
            "mdi:thermometer-chevron-up",
        ),
        "roomInfluence": (
            ("Raumeinfluss", "Room influence"),
            "rrc_room_influence",
            "mdi:home-thermometer-outline",
        ),
        "suWiThreshold": (
            (
                "Heizbetrieb bis Außentemperatur",
                "Stop heating at outdoor temperature",
            ),
            "rrc_summer_winter_threshold",
            "mdi:sun-thermometer-outline",
        ),
        "nightThreshold": (
            (
                "Nachtabsenkung bis Außentemperatur",
                "Night setback down to outdoor temperature",
            ),
            "rrc_night_threshold",
            "mdi:weather-night",
        ),
        "suWiSwitchMode": (
            ("Sommer-/Winterumschaltung", "Summer/winter switching"),
            "rrc_summer_winter_mode",
            "mdi:sun-snowflake-variant",
        ),
        "nightSwitchMode": (
            (
                "Nachtabsenkung nach Außentemperatur",
                "Night setback by outdoor temperature",
            ),
            "rrc_night_mode",
            "mdi:weather-night",
        ),
        "seasonOptMode": (
            ("Saisonbetrieb", "Season mode"),
            "rrc_season_mode",
            "mdi:sun-snowflake",
        ),
        "buildingHeatup": (
            ("Aufheizgeschwindigkeit", "Heat-up speed"),
            "rrc_building_heatup",
            "mdi:speedometer",
        ),
        "setpointOptimization": (
            ("Sollwertoptimierung", "Setpoint optimization"),
            "rrc_setpoint_optimization",
            "mdi:tune-vertical",
        ),
    }
)
_PLATFORMS: Final = MappingProxyType(
    {
        **dict.fromkeys(
            (
                "heatCurveMin",
                "heatCurveMax",
                "minSupply",
                "maxSupply",
                "roomInfluence",
                "suWiThreshold",
                "nightThreshold",
            ),
            "number",
        ),
        **dict.fromkeys(
            ("suWiSwitchMode", "nightSwitchMode", "seasonOptMode"), "select"
        ),
        "setpointOptimization": "switch",
    }
)
_OPTIONS: Final = MappingProxyType(
    {
        "suWiSwitchMode": SWITCH_MODES,
        "nightSwitchMode": SWITCH_MODES,
        "seasonOptMode": SEASON_MODES,
    }
)
SETTING_NAMES: Final = tuple(_SETTINGS)
_CATEGORIES: Final[Mapping[str, Category]] = MappingProxyType(
    {leaf(name): "config" for name in _SETTINGS}
)
OPERATING_SEASON_NAMES: Final = ("Betriebssaison", "Operating season")


def _diagnostics(resources: Mapping[str, Resource]) -> Mapping[str, object]:
    """S12: counts and metadata flags per setting; no values, no names."""
    circuits: set[str] = set()
    settings: dict[str, dict[str, int]] = {}
    for path, resource in resources.items():
        match = _CIRCUIT_LEAF.fullmatch(path)
        if match is None:
            continue
        base, name = match.groups()
        circuits.add(base)
        if name not in _SETTINGS and name != "operatingSeason":
            continue
        metadata = resource.metadata
        counts = settings.setdefault(
            name, {"present": 0, "writable": 0, "unavailable": 0, "with_limits": 0}
        )
        counts["present"] += 1
        counts["writable"] += metadata.writable
        counts["unavailable"] += metadata.available is False
        counts["with_limits"] += (
            metadata.minimum is not None and metadata.maximum is not None
        )
        if name == "seasonOptMode":
            counts["offered_modes"] = len(
                {value for value in metadata.allowed_values if value in SEASON_MODES}
            )
    return {"circuits": len(circuits), "settings": dict(sorted(settings.items()))}


CONTRACT = RrcContract(
    name="circuit",
    enum_policies=ENUM_POLICIES,
    number_policies=NUMBER_POLICIES,
    poll_groups=MappingProxyType(
        {
            # Settings: the configuration cadence (5 minutes), so a change
            # made elsewhere and a late confirmation show within minutes.
            leaf(f"(?:{'|'.join(_SETTINGS)})"): PollGroup.CONTROL,
            OPERATING_SEASON: PollGroup.CONTROL,
        }
    ),
    names=MappingProxyType(
        {
            **{leaf(name): names for name, (names, _, _) in _SETTINGS.items()},
            OPERATING_SEASON: OPERATING_SEASON_NAMES,
        }
    ),
    entity_keys=MappingProxyType(
        {
            **{
                leaf(name): MappingProxyType({_PLATFORMS[name]: key})
                for name, (_, key, _) in _SETTINGS.items()
                if name in _PLATFORMS
            },
            OPERATING_SEASON: MappingProxyType({"sensor": "rrc_operating_season"}),
        }
    ),
    option_keys=MappingProxyType(
        {
            leaf(name): MappingProxyType({value: value for value in values})
            for name, values in _OPTIONS.items()
        }
    ),
    read_only_mirrors=tuple(leaf(name) for name in _SETTINGS),
    # Without a policy the heat-up speed is still a known reading: its
    # generic sensor (a disabled diagnostic mirror) stays.
    understood=tuple(leaf(name) for name in READ_ONLY_SETTINGS),
    categories=_CATEGORIES,
    icons=MappingProxyType(
        {
            **{leaf(name): icon for name, (_, _, icon) in _SETTINGS.items()},
            OPERATING_SEASON: "mdi:sun-snowflake",
        }
    ),
    diagnostics=_diagnostics,
)
