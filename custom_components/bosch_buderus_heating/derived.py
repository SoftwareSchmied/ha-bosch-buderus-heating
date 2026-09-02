"""Pure calculations for values derived from PointT measurements."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite, log

MAGNUS_A = 17.62
MAGNUS_B_CELSIUS = 243.12

_MIN_ROOM_TEMPERATURE_CELSIUS = -45.0
_MAX_ROOM_TEMPERATURE_CELSIUS = 60.0


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


def _finite_number(value: object) -> float | None:
    """Return a finite number while rejecting booleans and PointT sentinels."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not isfinite(number) or number in {32767.0, -32768.0}:
        return None
    return number
