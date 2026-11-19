"""Temperature number entities: device class, display step and snapping.

Every number entity that holds a temperature carries the device class
``temperature`` (a temperature difference ``temperature_delta``), so Home
Assistant shows it in the unit of the user's unit system. Home Assistant
converts the value and the limits, but not the step: a step of 0.5 °C stays
"0.5" in °F. ``TemperatureNumber`` converts the step as a temperature
difference, and moves a value set in the other unit onto the nearest value of
the native grid, counted from the minimum: a whole value in °F, such as
120 °F, never lies exactly on a grid in °C (as the climate entities do). A
value set in the native unit is passed on unchanged, so the write rules still
refuse one between two steps instead of rounding it.
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
    """``value`` on the nearest step counted from ``minimum``; unchanged
    without a usable step."""
    if (
        step is None
        or not math.isfinite(step)
        or step <= 0
        or not math.isfinite(value)
        or not math.isfinite(minimum)
    ):
        return value
    return round(minimum + round((value - minimum) / step) * step, 6)
