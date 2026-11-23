"""Hybrid plants: the control strategy and the energy prices as controls.

Pure data without Home Assistant imports, like ``write_policies``. The
generic write registry (``writes``) lists the two policies, so every write
goes through the same validated transaction: the setting is read again
right before the one request, then read back, never repeated.

What a hybrid plant reports (``/heatSources/hybrid/controlStrategy`` and
``/system/energyTariff/*``) is expected. Nothing here is confirmed on a
real installation yet; the controls are created disabled.

* The strategy is a select over the advertised values the integration knows
  (an unknown value is never offered). Without ``allowedValues``,
  or with fewer than two usable values, there is no control.
* The prices are numbers on the grid and within the limits the plant
  reports. Both limits are required. A value off the grid is refused, not
  rounded: the plant would store it rounded and the read-back would never
  confirm it. The unit is the one the plant reports, no unit when it
  reports none; nothing is converted.
* Gas and oil: the oil price only when no gas price is
  reported. The feed-in tariff is the PV price (``energyTariff/pv``).
* The bivalence point and the price reminder stay read-only.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Final

from .pointt import Resource
from .write_policies import EnumWritePolicy, NumberWritePolicy

STRATEGY_PATH: Final = "/heatSources/hybrid/controlStrategy"
ELECTRICITY_PATH: Final = "/system/energyTariff/electricity"
GAS_PATH: Final = "/system/energyTariff/gas"
OIL_PATH: Final = "/system/energyTariff/oil"
FEED_IN_PATH: Final = "/system/energyTariff/pv"
PRICE_PATHS: Final = (ELECTRICITY_PATH, GAS_PATH, OIL_PATH, FEED_IN_PATH)
CONTROL_PATHS: Final = frozenset({STRATEGY_PATH, *PRICE_PATHS})

# The wire value of each strategy and the option key Home Assistant shows
# (lowercase, as the translation files need it). The meaning of each:
# "heatPumpOnly" is not "heat pump only" (the boiler still
# helps when the heat pump does not suffice) and "boilerOnly" switches the
# heat pump off.
STRATEGY_KEYS: Final[Mapping[str, str]] = {
    "cost": "cost",
    "maxRenewableShare": "max_renewable_share",
    "heatPumpOnly": "heat_pump_preferred",
    "environment": "environment",
    "boilerOnly": "boiler_only",
    "bivalentAlternativeMode": "bivalent_alternative",
    # A second spelling of the same strategy on some controllers.
    "bivalent_alternative": "bivalent_alternative",
}
STRATEGIES: Final = tuple(STRATEGY_KEYS)

TRANSLATION_KEY: Final = "hybrid_control_strategy"
PRICE_KEYS: Final[Mapping[str, str]] = {
    ELECTRICITY_PATH: "hybrid_electricity_price",
    GAS_PATH: "hybrid_gas_price",
    OIL_PATH: "hybrid_oil_price",
    FEED_IN_PATH: "hybrid_feed_in_price",
}

# Both limits must lie in this range (a price per kWh in any currency).
PRICE_RANGE: Final = (0.0, 1000.0)
# The step of a price without an advertised one.
PRICE_STEP: Final = 0.01

STRATEGY_POLICY: Final = EnumWritePolicy(
    r"^/heatSources/hybrid/controlStrategy$",
    frozenset({"stringValue"}),
    frozenset(STRATEGIES),
    known_only=True,
    enabled_by_default=False,
    min_options=2,
)
PRICE_POLICY: Final = NumberWritePolicy(
    r"^/system/energyTariff/(?:electricity|gas|oil|pv)$",
    "",
    PRICE_STEP,
    enabled_by_default=False,
    strict_grid=True,
    free_unit=True,
    plausible_range=PRICE_RANGE,
)

_SAFE_UNIT = re.compile(r"[\w%./°€$£ -]{1,16}")
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,32}")


def is_price_path(path: str) -> bool:
    """Whether ``path`` is one of the energy price settings."""
    return path in PRICE_PATHS


def offered(path: str, readings: Mapping[str, Resource]) -> bool:
    """Whether a control of ``path`` is offered with these readings.

    The oil price only while no gas price is reported
    (a plant may report both, and only one of them is in use); every other
    path is not decided here.
    """
    return not (path == OIL_PATH and GAS_PATH in readings)


def display_unit(unit: str | None) -> str | None:
    """The unit a price shows: the reported one, none when it is empty or
    not plain text (control characters, too long)."""
    if unit is None:
        return None
    text = unit.strip()
    return text if _SAFE_UNIT.fullmatch(text) else None


def _unit_class(unit: str | None) -> str:
    if unit is None:
        return "none"
    if not unit.strip():
        return "empty"
    return "text" if display_unit(unit) is not None else "other"


def _finite(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def diagnostics(resource: Resource) -> dict[str, object]:
    """What the diagnostics add for these paths: never a price or the
    current strategy, only the reported limits, step, unit class and the
    advertised strategies as plain tokens."""
    metadata = resource.metadata
    if resource.path == STRATEGY_PATH:
        tokens = sorted(
            {
                value
                for value in metadata.allowed_values
                if isinstance(value, str) and _SAFE_TOKEN.fullmatch(value)
            }
        )
        return {
            "advertised_options": tokens[:16],
            "advertised_option_count": len(metadata.allowed_values),
            "known_strategies": sorted(STRATEGIES),
        }
    return {
        "unit_class": _unit_class(metadata.unit),
        "step_size": _finite(metadata.step_size),
        "step_size_valid": metadata.step_size_valid,
        "minimum": _finite(metadata.minimum),
        "maximum": _finite(metadata.maximum),
    }
