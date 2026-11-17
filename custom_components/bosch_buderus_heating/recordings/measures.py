"""What the period entities show, and their names.

A measure is one recorded series, or the electricity of a mode as the sum
of its compressor and heater series (the plant records no electricity of
its own). Each measure has five periods. Its value comes from the plant's
recording first (local, then cloud) and from the integration's own counters
when no recording is there; only the heat per mode (heating, hot water)
comes from the counters first, because the plant counts the hours in which
it switches between the modes for both of them. Energy measures with a
counter have a second, disabled entity with the other method to compare.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from ..i18n import localize
from .periods import Period
from .series import BY_KEY, MODES, Quantity, Series

RECORDING: Final = "recording"
CALCULATED: Final = "calculated"
_COUNTER_PATH: Final = {
    "total": "totalConsumption",
    "ch": "chConsumption",
    "dhw": "dhwConsumption",
    "cooling": "coolingConsumption",
    "pool": "poolConsumption",
}
_LEGACY: Final = {
    ("total", "heat"): "gateway:total_heat_produced",
    ("ch", "heat"): "gateway:heating_heat_produced",
    ("dhw", "heat"): "gateway:hot_water_heat_produced",
    ("total", "electricity"): "gateway:total_electricity",
    ("ch", "electricity"): "gateway:heating_electricity",
    ("dhw", "electricity"): "gateway:hot_water_electricity",
}
# The device and name group of an entity, as for the matching counter.
ANCHOR_ENERGY: Final = "/heatSources/emon/totalConsumption"
ANCHOR_OUTDOOR: Final = "/system/sensors/temperatures/outdoor_t1"
ANCHOR_SUPPLY: Final = "/heatSources/actualSupplyTemperature"
ANCHOR_STARTS: Final = "/heatSources/numberOfStarts"
ANCHOR_WORKING: Final = "/heatSources/workingTime/totalSystem"


class Statistic(StrEnum):
    CHANGE = "change"
    MEAN = "mean"


@dataclass(frozen=True, slots=True)
class Counter:
    """The integration's own entity a value can be calculated from:
    unique ID suffixes after ``<gateway>:`` (the first registered one is
    used) and the statistic taken over the period."""

    suffixes: tuple[str, ...]
    statistic: Statistic


@dataclass(frozen=True, slots=True)
class Measure:
    key: str
    mode: str | None
    kind: str
    quantity: Quantity
    parts: tuple[str, ...]
    path_id: str
    counter: Counter | None
    primary: str

    @property
    def mean(self) -> bool:
        return self.quantity is Quantity.TEMPERATURE

    @property
    def anchor(self) -> str:
        if self.kind == "outdoor":
            return ANCHOR_OUTDOOR
        if self.kind == "supply":
            return ANCHOR_SUPPLY
        if self.kind == "starts":
            return ANCHOR_STARTS
        if self.kind == "working_time":
            return ANCHOR_WORKING
        return ANCHOR_ENERGY

    def derived_from(self) -> list[str]:
        return [BY_KEY[part].path for part in self.parts if part in BY_KEY]

    @property
    def methods(self) -> tuple[str, ...]:
        """The primary method first; the comparison one for energy counters."""
        if self.counter is None or self.quantity is not Quantity.ENERGY:
            return (self.primary,)
        other = CALCULATED if self.primary == RECORDING else RECORDING
        return (self.primary, other)


@dataclass(frozen=True, slots=True)
class EntitySpec:
    """One period entity: its measure, period and the method in its name."""

    measure: Measure
    period: Period
    method: str

    @property
    def key(self) -> str:
        suffix = ":calculated" if self.method == CALCULATED else ""
        return f"{self.measure.path_id}:{self.period.value}{suffix}"

    @property
    def primary(self) -> bool:
        return self.method == self.measure.primary

    @property
    def enabled_default(self) -> bool:
        return self.primary and self.measure.key in ("total_heat", "total_electricity")


def _kind_slug(series: Series) -> str:
    if series.mode:
        return series.key.removeprefix(f"{series.mode}_")
    return series.kind if series.kind in ("outdoor", "supply") else series.key


def _counter(mode: str | None, kind: str) -> Counter | None:
    if kind == "outdoor":
        return Counter(("gateway:outdoor_temperature",), Statistic.MEAN)
    if kind == "supply":
        return Counter(("gateway:supply_temperature",), Statistic.MEAN)
    if mode is None:
        return None
    if kind == "starts":
        suffixes = [f"heatSources:numberOfStarts:{mode}"]
        if mode == "total":
            suffixes.append("heatSources:numberOfStarts")
        return Counter(tuple(suffixes), Statistic.CHANGE)
    if kind == "working_time":
        if mode != "total":
            return None
        return Counter(("heatSources:workingTime:totalSystem",), Statistic.CHANGE)
    path = _COUNTER_PATH.get(mode)
    if path is None or kind not in ("heat", "compressor", "eheater", "electricity"):
        return None
    legacy = _LEGACY.get((mode, kind))
    if legacy is not None:
        return Counter((legacy,), Statistic.CHANGE)
    part = {"heat": "outputProduced", "electricity": "total_electricity"}.get(
        kind, kind
    )
    return Counter((f"heatSources:emon:{path}:{part}",), Statistic.CHANGE)


def _path_id(path: str) -> str:
    return path.removeprefix("/recordings/").replace("/", ":")


def measures(available: Iterable[str]) -> tuple[Measure, ...]:
    """The measures of the available series keys."""
    keys = set(available)
    found: list[Measure] = []
    for key in sorted(keys):
        series = BY_KEY.get(key)
        if series is None:
            continue
        kind = _kind_slug(series)
        primary = (
            CALCULATED if kind == "heat" and series.mode in ("ch", "dhw") else RECORDING
        )
        found.append(
            Measure(
                key,
                series.mode,
                kind,
                series.quantity,
                (key,),
                _path_id(series.path),
                _counter(series.mode, kind),
                primary,
            )
        )
    for mode in MODES:
        if f"{mode}_electricity" in keys:
            continue
        parts = tuple(
            part for part in (f"{mode}_compressor", f"{mode}_eheater") if part in keys
        )
        if not parts:
            continue
        found.append(
            Measure(
                f"{mode}_electricity",
                mode,
                "electricity",
                Quantity.ENERGY,
                parts,
                f"heatSources:emon:{mode}:electricity",
                _counter(mode, "electricity"),
                RECORDING,
            )
        )
    return tuple(found)


def entity_specs(items: Iterable[Measure]) -> tuple[EntitySpec, ...]:
    return tuple(
        EntitySpec(measure, period, method)
        for measure in items
        for period in Period
        for method in measure.methods
    )


# ------------------------------------------------------------------ names
def _mode_name(language: str, mode: str) -> str:
    if mode == "total":
        return localize(language, "Gesamt", "Total")
    if mode == "ch":
        return localize(language, "Heizung", "Heating")
    if mode == "dhw":
        return localize(language, "Warmwasser", "Hot water")
    if mode == "cooling":
        return localize(language, "Kühlung", "Cooling")
    if mode == "pool":
        return localize(language, "Pool", "Pool")
    return localize(language, "Lüftung", "Ventilation")


def _kind_name(language: str, measure: Measure) -> str:
    kind = measure.kind
    if kind == "heat" and measure.mode == "cooling":
        return localize(language, "Erzeugte Kühlenergie", "Cooling energy produced")
    if kind == "heat":
        return localize(language, "Erzeugte Wärme", "Heat produced")
    if kind == "compressor":
        return localize(language, "Strom Verdichter", "Compressor electricity")
    if kind == "eheater":
        return localize(language, "Strom Heizstab", "Electric heater electricity")
    if kind == "electricity":
        return localize(language, "Strom", "Electricity")
    if kind == "burner":
        return localize(language, "Brennerenergie", "Burner energy")
    if kind == "solar":
        return localize(language, "Solarertrag", "Solar yield")
    if kind == "ventilation":
        return localize(language, "Energie Lüftung", "Ventilation energy")
    if kind == "heat_recovered":
        return localize(language, "Rückgewonnene Wärme", "Heat recovered")
    if kind == "starts":
        return localize(language, "Starts", "Starts")
    if kind == "working_time":
        return localize(language, "Laufzeit", "Working time")
    if kind == "outdoor":
        return localize(
            language, "Außentemperatur (Mittel)", "Outdoor temperature (mean)"
        )
    return localize(language, "Vorlauftemperatur (Mittel)", "Supply temperature (mean)")


def measure_name(language: str, measure: Measure) -> str:
    kind = _kind_name(language, measure)
    if measure.mode is None or measure.kind in ("solar", "ventilation"):
        return kind
    return f"{_mode_name(language, measure.mode)} \N{EN DASH} {kind}"


def _recorded(language: str, period: Period) -> str:
    if period is Period.YESTERDAY:
        return localize(
            language,
            "{name} gestern (Anlagenaufzeichnung)",
            "{name} yesterday (system recording)",
        )
    if period is Period.PREVIOUS_MONTH:
        return localize(
            language,
            "{name} Vormonat (Anlagenaufzeichnung)",
            "{name} previous month (system recording)",
        )
    if period is Period.PREVIOUS_YEAR:
        return localize(
            language,
            "{name} Vorjahr (Anlagenaufzeichnung)",
            "{name} previous year (system recording)",
        )
    if period is Period.CURRENT_MONTH:
        return localize(
            language,
            "{name} aktueller Monat (Anlagenaufzeichnung)",
            "{name} this month (system recording)",
        )
    return localize(
        language,
        "{name} aktuelles Jahr (Anlagenaufzeichnung)",
        "{name} this year (system recording)",
    )


def _calculated(language: str, period: Period) -> str:
    if period is Period.YESTERDAY:
        return localize(
            language, "{name} gestern (berechnet)", "{name} yesterday (calculated)"
        )
    if period is Period.PREVIOUS_MONTH:
        return localize(
            language,
            "{name} Vormonat (berechnet)",
            "{name} previous month (calculated)",
        )
    if period is Period.PREVIOUS_YEAR:
        return localize(
            language, "{name} Vorjahr (berechnet)", "{name} previous year (calculated)"
        )
    if period is Period.CURRENT_MONTH:
        return localize(
            language,
            "{name} aktueller Monat (berechnet)",
            "{name} this month (calculated)",
        )
    return localize(
        language,
        "{name} aktuelles Jahr (berechnet)",
        "{name} this year (calculated)",
    )


def entity_name(language: str, spec: EntitySpec) -> str:
    template = (
        _calculated(language, spec.period)
        if spec.method == CALCULATED
        else _recorded(language, spec.period)
    )
    return template.format(name=measure_name(language, spec.measure))


def statistic_name(language: str, series: Series) -> str:
    """The name of a series' statistic in the statistics and energy views."""
    measure = measures((series.key,))[0]
    return localize(
        language, "{name} (Anlagenaufzeichnung)", "{name} (system recording)"
    ).format(name=measure_name(language, measure))
