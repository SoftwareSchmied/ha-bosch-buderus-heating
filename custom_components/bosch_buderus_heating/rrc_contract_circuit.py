"""RRC 2.0 contract of the heating circuit: curve, summer/winter, night.

Owned by work package W4 (CT-G01, CT-G02). Every setting is written through
the generic controls with the W0 write path: metadata limits and steps,
exactly one write and a read-back. Nothing here is confirmed on a real
controller yet ("expected, untested").

- Heating curve, supply limits and room influence: numbers, configuration,
  disabled by default (W4-E1). The lower limit stays below the upper one
  (``rrc_curve_order``); the entities read both limits freshly first.
- Summer/winter and night switching, season mode, heat-up speed: selections;
  the season mode only with at least two offered modes, the empty one left
  out. Setpoint optimization: a switch.
- ``control``, ``type`` and ``typeRoomControl`` are installer settings that
  change the control strategy: shown at most, never written.

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


# Lower limit -> (upper limit, whether the lower one must stay strictly below).
LIMIT_PAIRS: Final[Mapping[str, tuple[str, bool]]] = MappingProxyType(
    {"heatCurveMin": ("heatCurveMax", True), "minSupply": ("maxSupply", False)}
)
_UPPER: Final = MappingProxyType(
    {upper: (lower, strict) for lower, (upper, strict) in LIMIT_PAIRS.items()}
)
CURVE_ORDER: Final = "rrc_curve_order"


def counterpart(path: str) -> str | None:
    """The other limit of a heating curve or supply limit path."""
    match = _CIRCUIT_LEAF.fullmatch(path)
    if match is None:
        return None
    base, name = match.groups()
    if name in LIMIT_PAIRS:
        return f"{base}/{LIMIT_PAIRS[name][0]}"
    if name in _UPPER:
        return f"{base}/{_UPPER[name][0]}"
    return None


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
    """``heatCurveMin < heatCurveMax`` and ``minSupply <= maxSupply``.

    ``related`` holds the current readings, with the fresh reads of the
    entity's ``expected`` (both limits). A plant without the other limit
    keeps only the metadata limits; one whose other limit has no valid
    number refuses, as the order cannot be checked.
    """
    other_path = counterpart(resource.path)
    if (
        other_path is None
        or isinstance(value, bool)
        or not isinstance(value, int | float)
    ):
        return None
    other = related.get(other_path)
    if other is None:
        return None
    bound = _number(other)
    if bound is None:
        return CURVE_ORDER
    name = resource.path.rsplit("/", 1)[-1]
    if name in LIMIT_PAIRS:
        strict = LIMIT_PAIRS[name][1]
        ordered = value < bound if strict else value <= bound
    else:
        strict = _UPPER[name][1]
        ordered = bound < value if strict else bound <= value
    return None if ordered else CURVE_ORDER


_STRING = frozenset({"stringValue"})
SWITCH_MODES: Final = ("off", "automatic")
SEASON_MODES: Final = ("always_heating", "always_cooling", "automatic")
HEATUP_SPEEDS: Final = ("slow", "normal", "fast")


def _limit(name: str) -> NumberWritePolicy:
    """A heating curve or supply limit: 1 K steps, disabled by default."""
    return NumberWritePolicy(
        leaf(name),
        "C",
        1.0,
        enabled_by_default=False,
        strict_grid=True,
        create_entity=False,
        value_check=check_order,
    )


def _threshold(name: str) -> NumberWritePolicy:
    """An outdoor temperature threshold: 0.5 K steps."""
    return NumberWritePolicy(
        leaf(name), "C", 0.5, strict_grid=True, create_entity=False
    )


def _mode(name: str, values: tuple[str, ...]) -> EnumWritePolicy:
    """A selection of known modes; the app shapes advertise none (#122)."""
    return EnumWritePolicy(
        leaf(name),
        _STRING,
        frozenset(values),
        known_only=True,
        fallback_options=values,
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
BUILDING_HEATUP_POLICY: Final = _mode("buildingHeatup", HEATUP_SPEEDS)
# Firmware 4.2.0 and later advertise the modes, sometimes with an empty one;
# a single offered mode (#122) leaves the reading only.
SEASON_MODE_POLICY: Final = EnumWritePolicy(
    leaf("seasonOptMode"),
    _STRING,
    frozenset(SEASON_MODES),
    known_only=True,
    ignored_options=frozenset({""}),
    min_options=2,
)
SETPOINT_OPTIMIZATION_POLICY: Final = EnumWritePolicy(
    leaf("setpointOptimization"),
    _STRING,
    frozenset({"on", "off"}),
    known_only=True,
    fallback_options=("on", "off"),
    on_value="on",
    off_value="off",
)
ENUM_POLICIES: Final = (
    SUMMER_WINTER_MODE_POLICY,
    NIGHT_MODE_POLICY,
    SEASON_MODE_POLICY,
    BUILDING_HEATUP_POLICY,
    SETPOINT_OPTIMIZATION_POLICY,
)

OPERATING_SEASON: Final = leaf("operatingSeason")
OPERATING_SEASONS: Final = ("heating", "cooling", "idle")
# Installer settings that change the control strategy; no policy, so they
# are never written (at most read).
INSTALLER_SETTINGS: Final = ("control", "type", "typeRoomControl")

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
            ("suWiSwitchMode", "nightSwitchMode", "seasonOptMode", "buildingHeatup"),
            "select",
        ),
        "setpointOptimization": "switch",
    }
)
_OPTIONS: Final = MappingProxyType(
    {
        "suWiSwitchMode": SWITCH_MODES,
        "nightSwitchMode": SWITCH_MODES,
        "seasonOptMode": SEASON_MODES,
        "buildingHeatup": HEATUP_SPEEDS,
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
    categories=_CATEGORIES,
    icons=MappingProxyType(
        {
            **{leaf(name): icon for name, (_, _, icon) in _SETTINGS.items()},
            OPERATING_SEASON: "mdi:sun-snowflake",
        }
    ),
    diagnostics=_diagnostics,
)
