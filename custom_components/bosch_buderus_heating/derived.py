"""Pure calculations for values derived from PointT measurements."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite, log

MAGNUS_A = 17.62
MAGNUS_B_CELSIUS = 243.12

_MIN_ROOM_TEMPERATURE_CELSIUS = -45.0
_MAX_ROOM_TEMPERATURE_CELSIUS = 60.0

# Estimated energy from the reported current power: two successive readings
# are integrated only when they are at most three polling intervals and at
# most 30 minutes apart. A longer gap is never bridged.
ESTIMATION_GAP_INTERVALS = 3
MAX_ESTIMATION_GAP = timedelta(minutes=30)
_POWER_UNIT_TO_KILOWATT = {"kW": 1.0, "W": 0.001}


@dataclass(frozen=True, slots=True)
class DewPointCalculation:
    """Validated inputs and result of one Magnus dew-point calculation."""

    room_temperature_celsius: float
    relative_humidity_percent: float
    dew_point_celsius: float


def calculate_dew_point(
    room_temperature: object, relative_humidity: object
) -> DewPointCalculation | None:
    """Calculate dew point from plausible indoor temperature and humidity."""
    temperature = _finite_number(room_temperature)
    humidity = _finite_number(relative_humidity)
    if temperature is None or humidity is None:
        return None
    if (
        not _MIN_ROOM_TEMPERATURE_CELSIUS
        <= temperature
        <= _MAX_ROOM_TEMPERATURE_CELSIUS
    ):
        return None
    if not 0.0 < humidity <= 100.0:
        return None

    gamma = log(humidity / 100.0) + (
        MAGNUS_A * temperature / (MAGNUS_B_CELSIUS + temperature)
    )
    denominator = MAGNUS_A - gamma
    if denominator == 0.0:
        return None
    dew_point = MAGNUS_B_CELSIUS * gamma / denominator
    if not isfinite(dew_point) or dew_point > temperature + 1e-9:
        return None

    return DewPointCalculation(
        room_temperature_celsius=temperature,
        relative_humidity_percent=humidity,
        dew_point_celsius=round(dew_point, 2),
    )


@dataclass(frozen=True, slots=True)
class PowerSample:
    """One fresh, valid power reading and where it came from."""

    observed_at: datetime
    power_kw: float
    origin: str


def power_kilowatts(
    value: object, unit: str | None, error_values: Collection[float] = ()
) -> float | None:
    """Return a valid, non-negative power in kW, or ``None``.

    Booleans, texts, non-finite numbers, PointT sentinels, declared device
    error values, negative power and unknown units never count.
    """
    scale = _POWER_UNIT_TO_KILOWATT.get(unit or "")
    number = _finite_number(value)
    if scale is None or number is None or number < 0.0:
        return None
    if number in error_values:
        return None
    return number * scale


def estimation_gap_limit(interval: timedelta | None) -> timedelta:
    """Longest gap between two readings that is still integrated."""
    if interval is None or interval <= timedelta(0):
        return MAX_ESTIMATION_GAP
    return min(interval * ESTIMATION_GAP_INTERVALS, MAX_ESTIMATION_GAP)


def estimated_energy_kwh(
    previous: PowerSample | None, current: PowerSample, max_gap: timedelta
) -> float:
    """Energy between two successive readings as a left Riemann sum.

    The earlier reading's power applies until the later reading, also across
    a change of origin: both report the same power. Nothing is counted
    without a previous reading, for a reading that is not newer, or across a
    gap longer than ``max_gap``.
    """
    if previous is None:
        return 0.0
    elapsed = current.observed_at - previous.observed_at
    if elapsed <= timedelta(0) or elapsed > max_gap:
        return 0.0
    return previous.power_kw * elapsed.total_seconds() / 3600.0


def _finite_number(value: object) -> float | None:
    """Return a finite number while rejecting booleans and PointT sentinels."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not isfinite(number) or number in {32767.0, -32768.0}:
        return None
    return number
