"""The recorded series a plant may keep, with their kind and unit.

A series is one ``/recordings/...`` path of the local data port and of the
cloud: the plant itself sums its energy, starts and working time per hour,
day and month, and records outdoor and supply temperatures as sums with a
sample count. The candidate paths come from the local read catalogue (only
paths without a placeholder); a series exists on a plant only when its path
answers (``sources``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from ..local_api._data import OPERATIONS

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
# Units each quantity accepts, with the factor to the unit kept here
# (kWh, h, a plain count and °C). Any other unit: the series is not kept.
UNITS: Final[dict[Quantity, dict[str | None, float]]] = {
    Quantity.ENERGY: {"kWh": 1.0, "Wh": 0.001, "wh": 0.001, "MWh": 1000.0},
    Quantity.COUNT: {None: 1.0, "": 1.0, "count": 1.0},
    Quantity.DURATION: {"s": 1 / 3600, "min": 1 / 60, "mins": 1 / 60, "h": 1.0},
    Quantity.TEMPERATURE: {"C": 1.0, "°C": 1.0},
}
UNIT_OF: Final[dict[Quantity, str | None]] = {
    Quantity.ENERGY: "kWh",
    Quantity.COUNT: None,
    Quantity.DURATION: "h",
    Quantity.TEMPERATURE: "°C",
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
BY_PATH: Final = {item.path: item for item in SERIES}
BY_KEY: Final = {item.key: item for item in SERIES}
LOCAL_PATHS: Final = tuple(item.path for item in SERIES)


def electricity_parts(mode: str) -> tuple[str, str]:
    """The series keys whose sum is the electricity of a mode."""
    return f"{mode}_compressor", f"{mode}_eheater"


def unit_factor(quantity: Quantity, unit: str | None) -> float | None:
    """The factor to this module's unit, or None for an unknown unit."""
    table = UNITS[quantity]
    if unit in table:
        return table[unit]
    return None
