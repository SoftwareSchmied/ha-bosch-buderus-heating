"""Temperature number entities: device class, display step and snapping.

Every number entity that holds a temperature carries the device class
``temperature`` (a temperature difference ``temperature_delta``), so Home
Assistant shows it in the unit of the user's unit system. Home Assistant
converts the value and the limits, but not the step: a step of 0.5 °C stays
"0.5" in °F. ``TemperatureNumber`` converts the step as a temperature
difference, and moves a value set in the other unit back onto the native grid
when it only misses it by the rounding of the conversion. A value that really
lies between two steps is passed on unchanged, so the write rules still
refuse it instead of rounding it.
"""

from __future__ import annotations

import math

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.util.unit_conversion import TemperatureDeltaConverter

_TEMPERATURE_UNITS = frozenset(UnitOfTemperature)
_TEMPERATURE_CLASSES = frozenset(
    {NumberDeviceClass.TEMPERATURE, NumberDeviceClass.TEMPERATURE_DELTA}
)
# The most a converted value may miss a native step by and still count as
# that step (in the native unit): the rounding of a value shown with one
# decimal in the other unit stays below it.
_SNAP_TOLERANCE = 0.1


def temperature_device_class(
    unit: str | None, *, delta: bool = False
) -> NumberDeviceClass | None:
    """The device class of a number in ``unit`` (an HA unit), if a temperature."""
    if unit not in _TEMPERATURE_UNITS:
        return None
    if delta:
        return NumberDeviceClass.TEMPERATURE_DELTA
    return NumberDeviceClass.TEMPERATURE


class TemperatureNumber(NumberEntity):
    """A number entity whose temperature step follows the shown unit."""

    def _converted_units(self) -> tuple[str, str] | None:
        """The native and the shown unit, when Home Assistant converts."""
        if self.device_class not in _TEMPERATURE_CLASSES:
            return None
        native = self.native_unit_of_measurement
        shown = self.unit_of_measurement
        if (
            native is None
            or shown is None
            or native == shown
            or native not in _TEMPERATURE_UNITS
            or shown not in _TEMPERATURE_UNITS
        ):
            return None
        return native, shown

    def _calculate_step(self, min_value: float, max_value: float) -> float:
        step = super()._calculate_step(min_value, max_value)
        if (units := self._converted_units()) is None or self.native_step is None:
            return step
        # A step is a temperature difference: 0.5 °C is 0.9 °F, not 32.9 °F.
        return round(TemperatureDeltaConverter.convert(step, *units), 4)

    def convert_to_native_value(self, value: float) -> float:
        native = super().convert_to_native_value(value)
        if self._converted_units() is None:
            return native
        return snap_to_grid(native, self.native_min_value, self.native_step)


def snap_to_grid(value: float, minimum: float, step: float | None) -> float:
    """``value`` on the nearest step counted from ``minimum`` when it misses
    that step only by a conversion's rounding; otherwise unchanged."""
    if step is None or step <= 0 or not math.isfinite(value):
        return value
    nearest = minimum + round((value - minimum) / step) * step
    if abs(value - nearest) <= min(_SNAP_TOLERANCE, step * 0.45):
        return round(nearest, 6)
    return value
