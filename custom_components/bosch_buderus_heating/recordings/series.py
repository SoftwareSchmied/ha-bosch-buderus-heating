"""The recorded series a plant may keep, with their kind and unit.

A series is one ``/recordings/...`` path of the local data port and of the
cloud: the plant itself sums its energy, starts and working time per hour,
day and month, and records outdoor and supply temperatures as sums with a
sample count. The candidate paths come from the local read catalogue (only
paths without a placeholder); a series exists on a plant only when its path
answers (``sources``). The heat pump of a hybrid system keeps recordings of
its own under its heat source (``HEAT_SOURCE_SERIES``); the heat pump behind an
iCom gateway under the energy monitoring (``ICOM_HEAT_PUMP_SERIES``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from ..local_api._data import OPERATIONS
from ..unit_contracts import (
    DURATION_UNITS,
    ENERGY_UNITS,
    TEMPERATURE_UNITS,
    energy_kwh_scale,
)

PREFIX: Final = "/recordings"
_EMON: Final = "/recordings/heatSources/emon/"
OUTDOOR_PATH: Final = "/recordings/system/sensors/temperatures/outdoor_t1"
SUPPLY_PATH: Final = "/recordings/heatSources/actualSupplyTemperature"


class Quantity(StrEnum):
    """What the values of a series are; decides unit, sum or mean."""

    ENERGY = "energy"
    COUNT = "count"
    DURATION = "duration"
    TEMPERATURE = "temperature"
    VOLUME = "volume"


# Each kind of value at the end of an energy path: its slug and quantity.
_KINDS: Final[dict[str, tuple[str, Quantity]]] = {
    "outputProduced": ("heat", Quantity.ENERGY),
    "compressor": ("compressor", Quantity.ENERGY),
    "eheater": ("eheater", Quantity.ENERGY),
    "burner": ("burner", Quantity.ENERGY),
    "electricity": ("electricity", Quantity.ENERGY),
    "solar": ("solar", Quantity.ENERGY),
    "ventilation": ("ventilation", Quantity.ENERGY),
    "heatRecovered": ("heat_recovered", Quantity.ENERGY),
    "numberOfStarts": ("starts", Quantity.COUNT),
    "workingTime": ("working_time", Quantity.DURATION),
}
MODES: Final = ("total", "ch", "dhw", "cooling", "pool", "ventilation")
# The factor of each canonical duration to hours.
_HOURS: Final[dict[str, float]] = {"s": 1 / 3600, "min": 1 / 60, "h": 1.0, "d": 24.0}
# Units each quantity accepts, with the factor to the unit kept here
# (kWh, h, a plain count and °C): the spellings of the shared unit table
# (``unit_contracts``) plus MWh; temperatures in °C only (a recording in °F or
# K is not converted). Any other unit: the series is not kept.
UNITS: Final[dict[Quantity, dict[str | None, float]]] = {
    Quantity.ENERGY: {
        **{
            unit: scale
            for unit in ENERGY_UNITS
            if (scale := energy_kwh_scale(unit)) is not None
        },
        "MWh": 1000.0,
    },
    Quantity.COUNT: {None: 1.0, "": 1.0, "count": 1.0},
    Quantity.DURATION: {unit: _HOURS[name] for unit, name in DURATION_UNITS.items()},
    Quantity.TEMPERATURE: {
        unit: 1.0 for unit, name in TEMPERATURE_UNITS.items() if name == "°C"
    },
    Quantity.VOLUME: {"l": 1.0, "L": 1.0, "m3": 1000.0, "m³": 1000.0},
}
UNIT_OF: Final[dict[Quantity, str | None]] = {
    Quantity.ENERGY: "kWh",
    Quantity.COUNT: None,
    Quantity.DURATION: "h",
    Quantity.TEMPERATURE: "°C",
    Quantity.VOLUME: "L",
}
# The energy paths the cloud answers in its ``interval`` form, read hourly
# only when there is no local access: heat, compressor and heater per total,
# heating and hot water (``emon/<mode>/electricity`` answers 404).
CLOUD_PATHS: Final = tuple(
    f"{_EMON}{mode}/{kind}"
    for mode in ("total", "ch", "dhw")
    for kind in ("outputProduced", "compressor", "eheater")
)


@dataclass(frozen=True, slots=True)
class Series:
    """One recorded path."""

    key: str
    path: str
    mode: str | None
    kind: str
    quantity: Quantity
    # Read from the cloud only (no path of the local data port), and only
    # for a plant that shows ``requires`` (see ``gating``).
    cloud_only: bool = False
    requires: str | None = None
    # The cloud answers this series as samples of the mean power: ``y`` is the
    # sum of ``c`` samples (about 60 per hour, about 1,440 per day, whatever
    # the sample rate), and the energy of a slot is the mean ``y / c`` times
    # the hours of the slot (``parse.mean_form``). A unit of power (kW) is
    # therefore taken as well behind an iCom gateway (``series_unit_factor``).
    minute_sums: bool = False
    # A series of ``requires`` ``boiler`` that is asked on a heat pump behind an
    # iCom gateway as well (``gating``).
    icom_heat_pump_too: bool = False
    # The heat source (``hs2``) whose own recording this is; ``None`` for
    # the gateway's series.
    heat_source: str | None = None
    # Read for the diagnostics only (its state and unit): never a ledger, an
    # entity or a statistic.
    diagnostic: bool = False

    @property
    def mean(self) -> bool:
        """Temperatures are means (y / c); everything else is a sum."""
        return self.quantity is Quantity.TEMPERATURE

    @property
    def unit(self) -> str | None:
        return UNIT_OF[self.quantity]

    @property
    def cloud(self) -> bool:
        return self.path in CLOUD_PATHS


def _series(path: str) -> Series | None:
    if path == OUTDOOR_PATH:
        return Series(
            "outdoor_temperature", path, None, "outdoor", Quantity.TEMPERATURE
        )
    if path == SUPPLY_PATH:
        return Series("supply_temperature", path, None, "supply", Quantity.TEMPERATURE)
    if not path.startswith(_EMON):
        return None
    mode, _, kind = path.removeprefix(_EMON).partition("/")
    if mode not in MODES or kind not in _KINDS:
        return None
    slug, quantity = _KINDS[kind]
    if mode == "total" and kind in ("solar", "ventilation"):
        return Series(slug, path, mode, kind, quantity)
    return Series(f"{mode}_{slug}", path, mode, kind, quantity)


def _catalogue() -> tuple[Series, ...]:
    found = []
    for path, operation in OPERATIONS.items():
        if operation.get("group") != "recordings" or "{" in path:
            continue
        if (item := _series(path)) is not None:
            found.append(item)
    return tuple(sorted(found, key=lambda item: item.path))


SERIES: Final = _catalogue()
LOCAL_PATHS: Final = tuple(item.path for item in SERIES)

# Series the local data port does not have. Each is read from the cloud
# only when the plant shows what it belongs to (``gating``) and the path
# answers there: the daily consumption of a water softener, and the energy
# recordings of boilers and older firmware, which are kept under other paths
# than a heat pump's.
REQUIRES_SOFTENER: Final = "softener"
REQUIRES_BOILER: Final = "boiler"
# The heat pump behind an iCom gateway records its energy under other paths
# than a heat pump of a K30 or K40 gateway: heat produced, compressor and
# electric heater for the total. They are read instead of the boiler series
# for an iCom gateway whose plant kind is ``heat_pump`` (``gating``), from the
# cloud only, as samples of the mean like the energy monitoring's consumed
# energy (``minute_sums``). The consumed energy itself is asked there as well
# (``icom_heat_pump_too``): the electricity of the heat pump as the gateway
# counts it, a measure of its own that is never added to the compressor and
# the heater. The solar yield of the first solar circuit is read on such a
# plant for the diagnostics only (state and unit).
REQUIRES_ICOM_HEAT_PUMP: Final = "icom_heat_pump"
_MONITORING: Final = "/recordings/heatSources/total/energyMonitoring/"
ICOM_HEAT_PUMP_SERIES: Final = tuple(
    Series(
        f"monitoring_total_{_KINDS[kind][0]}",
        f"{_MONITORING}{kind}",
        "total",
        kind,
        Quantity.ENERGY,
        cloud_only=True,
        requires=REQUIRES_ICOM_HEAT_PUMP,
        minute_sums=True,
    )
    for kind in ("outputProduced", "compressor", "eheater")
)
SOLAR_YIELD: Final = Series(
    "sc1_solar_yield",
    "/recordings/solarCircuits/sc1/solarYield",
    None,
    "solarYield",
    Quantity.ENERGY,
    cloud_only=True,
    requires=REQUIRES_ICOM_HEAT_PUMP,
    diagnostic=True,
)
CLOUD_ONLY: Final = (
    Series(
        "water",
        "/recordings/swCircuits/sw1/sensor/water",
        None,
        "water",
        Quantity.VOLUME,
        cloud_only=True,
        requires=REQUIRES_SOFTENER,
    ),
    Series(
        "ch_actual_power",
        "/recordings/heatSources/actualCHPower",
        "ch",
        "actual_power",
        Quantity.ENERGY,
        cloud_only=True,
        requires=REQUIRES_BOILER,
    ),
    Series(
        "dhw_actual_power",
        "/recordings/heatSources/actualDHWPower",
        "dhw",
        "actual_power",
        Quantity.ENERGY,
        cloud_only=True,
        requires=REQUIRES_BOILER,
    ),
    Series(
        "total_consumed",
        "/recordings/heatSources/total/energyMonitoring/consumedEnergy",
        "total",
        "consumed",
        Quantity.ENERGY,
        cloud_only=True,
        requires=REQUIRES_BOILER,
        minute_sums=True,
        icom_heat_pump_too=True,
    ),
    *ICOM_HEAT_PUMP_SERIES,
    SOLAR_YIELD,
)
# A hybrid system's heat pump keeps recordings of its own under its heat
# source: heat and compressor per total, heating and hot water (it has no
# electric heater). The gateway's energy series then count the boiler. The
# local data port names these paths for the heat sources hs2 to hs6; they
# are read (local, else cloud) only for a hybrid system whose heat pump
# reports counters of its own under that heat source (``gating``).
REQUIRES_HEAT_PUMP: Final = "heat_pump_source"
HEAT_SOURCE_IDS: Final = ("hs2", "hs3", "hs4", "hs5", "hs6")
HEAT_SOURCE_MODES: Final = ("total", "ch", "dhw")
HEAT_SOURCE_KINDS: Final = ("outputProduced", "compressor")


def heat_source_path(source: str, mode: str, kind: str) -> str:
    return f"{PREFIX}/heatSources/{source}/emon/{mode}/{kind}"


def _heat_source_series() -> tuple[Series, ...]:
    found = []
    for source in HEAT_SOURCE_IDS:
        for mode in HEAT_SOURCE_MODES:
            for kind in HEAT_SOURCE_KINDS:
                slug, quantity = _KINDS[kind]
                found.append(
                    Series(
                        f"{source}_{mode}_{slug}",
                        heat_source_path(source, mode, kind),
                        mode,
                        kind,
                        quantity,
                        requires=REQUIRES_HEAT_PUMP,
                        heat_source=source,
                    )
                )
    return tuple(found)


HEAT_SOURCE_SERIES: Final = _heat_source_series()
ALL_SERIES: Final = (*SERIES, *CLOUD_ONLY, *HEAT_SOURCE_SERIES)
BY_PATH: Final = {item.path: item for item in ALL_SERIES}
BY_KEY: Final = {item.key: item for item in ALL_SERIES}


def electricity_parts(mode: str) -> tuple[str, str]:
    """The series keys whose sum is the electricity of a mode."""
    return f"{mode}_compressor", f"{mode}_eheater"


def kind_slug(kind: str) -> str | None:
    """The slug of a kind of energy path (``outputProduced`` is ``heat``)."""
    found = _KINDS.get(kind)
    return found[0] if found is not None else None


# kWh in any case (``KWh``, ``kwh``, ``KWH``); every other unit only as the
# unit table writes it (``mWh`` would be a thousandth of a watt-hour).
_ENERGY_ANY_CASE: Final[dict[str, float]] = {"kwh": 1.0}
# The mean of samples of power in kW times hours is energy in kWh: taken only
# for a series kept as samples of the mean (``minute_sums``) behind an iCom
# gateway, whose energy monitoring is read that way whatever its unit.
_MEAN_POWER: Final[dict[str, float]] = {"kw": 1.0}


def unit_factor(
    quantity: Quantity, unit: str | None, *, mean_power: bool = False
) -> float | None:
    """The factor to this module's unit, or None for an unknown unit.

    ``mean_power``: the series is kept as samples of the mean power behind an
    iCom gateway, so a unit of power (kW) is taken as well.
    """
    table = UNITS[quantity]
    if unit in table:
        return table[unit]
    if quantity is not Quantity.ENERGY or not isinstance(unit, str):
        return None
    text = unit.strip()
    if text in table:
        return table[text]
    folded = text.casefold()
    if folded in _ENERGY_ANY_CASE:
        return _ENERGY_ANY_CASE[folded]
    if mean_power and folded in _MEAN_POWER:
        return _MEAN_POWER[folded]
    return None


def mean_samples(series: Series, *, icom: bool = False) -> bool:
    """Whether a series' answer holds samples of the mean (``parse.mean_form``):
    a series kept that way (``minute_sums``) on every gateway, and every
    energy series behind an iCom gateway, the boiler's as well. A K30, K40 or
    other gateway keeps its energy sums as they are."""
    return series.minute_sums or (icom and series.quantity is Quantity.ENERGY)


def series_unit_factor(
    series: Series, unit: str | None, *, icom: bool = False
) -> float | None:
    """The factor of a series' answer in ``unit``, or None for a unit it
    does not take. ``icom``: the answer comes from an iCom gateway, whose
    energy series are samples of the mean power (kW is taken as well); never
    for a K30, K40 or other gateway."""
    return unit_factor(
        series.quantity, unit, mean_power=icom and mean_samples(series, icom=icom)
    )
