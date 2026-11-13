"""Per-device contracts for the seven connected Air purifier families."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.const import UnitOfTemperature
from homeassistant.util.unit_conversion import TemperatureConverter

from .metadata import CELSIUS, metadata, writable
from .models import Device

FAMILIES = {
    "air_1001i": ("Air 1001i", "air_1001i_v2"),
    "air_2000i": ("air_2000i",),
    "air_2001i": ("Air 2001i", "air_2001i_v2"),
    "air_4000i": ("air_4000i",),
    "air_4001i": ("Air 4001i", "air_4001i_v2"),
    "air_6000i": ("air_6000i",),
    "air_6001i": ("Air 6001i", "air_6001i_v2"),
}
MODES = ("auto", "manual", "silent")
COLORS = ("warmWhite", "neutralWhite", "coolWhite", "blue", "purple", "orange", "red")
COLOR_OPTIONS = {
    "warm_white": "warmWhite",
    "neutral_white": "neutralWhite",
    "cool_white": "coolWhite",
    "blue": "blue",
    "purple": "purple",
    "orange": "orange",
    "red": "red",
}
SWITCHES = {
    "child_lock": "childLockEnabled",
    "display": "displayEnabled",
    "light": "lightEnabled",
    "light_in_standby": "ambientLightOnInStandby",
    "light_in_sleep": "ambientLightOnInSleep",
}
NUMBERS = {"fan_speed": "fanSpeed", "light_intensity": "lightIntensity"}
SENSORS = {
    "air_quality": "airQuality",
    "pm2_5": "pm2_5",
    "tvoc": "tvoc",
    "temperature": "roomTemperature",
    "humidity": "relHumidity",
    "filter_life": "filterCapacity",
    "filter_status": "filterCapacity",
}
QUALITY = {1: "good", 2: "fair", 3: "poor", 4: "bad"}
FAHRENHEIT = ("degF", "Fahrenheit")


def family(device: Device) -> str | None:
    return next(
        (
            name
            for name, prefixes in FAMILIES.items()
            if device.model is not None and device.model.startswith(prefixes)
        ),
        None,
    )


def reported(device: Device, field: str) -> Any:
    return device.state.reported.get(field) if device.state else None


def options(device: Device, field: str, known: tuple[str, ...]) -> list[str]:
    meta = metadata(device, "state", field)
    offered = meta.get("enum") if meta is not None else None
    return [v for v in known if v in offered] if isinstance(offered, list) else []


def boolean_control(device: Device, field: str) -> bool:
    return type(reported(device, field)) is bool and writable(
        metadata(device, "state", field)
    )


@dataclass(frozen=True, slots=True)
class IntegerRange:
    minimum: int
    maximum: int
    step: int

    def accepts(self, value: object) -> bool:
        return (
            type(value) is int
            and self.minimum <= value <= self.maximum
            and (value - self.minimum) % self.step == 0
        )


def number_range(device: Device, field: str) -> IntegerRange | None:
    meta = metadata(device, "state", field)
    if meta is None or field not in NUMBERS.values():
        return None
    # Fan speed is a positive ordinal, never a percentage. Brightness is only
    # released when the device explicitly supplies a percentage unit.
    if meta.get("unit") not in ((None, "") if field == "fanSpeed" else ("%",)):
        return None
    minimum, maximum, step = meta.get("min"), meta.get("max"), meta.get("step", 1)
    lower = 1 if field == "fanSpeed" else 0
    # Local control envelope, not a claim about any model's maximum stage.
    if (
        type(minimum) is not int
        or type(maximum) is not int
        or type(step) is not int
        or not lower <= minimum < maximum <= 100
        or not 1 <= step <= maximum - minimum
    ):
        return None
    return IntegerRange(minimum, maximum - (maximum - minimum) % step, step)


def number_value(device: Device, field: str) -> int | None:
    limits = number_range(device, field)
    value = reported(device, field)
    return value if limits is not None and limits.accepts(value) else None


def control_available(device: Device, field: str) -> bool:
    if not writable(metadata(device, "state", field)):
        return False
    if field in ("powerEnabled", *SWITCHES.values()):
        return boolean_control(device, field)
    if field in NUMBERS.values():
        return number_value(device, field) is not None
    known = MODES if field == "opMode" else COLORS if field == "lightMode" else ()
    current_known = (*COLORS, "off", "auto") if field == "lightMode" else known
    return (
        bool(options(device, field, known)) and reported(device, field) in current_known
    )


def command_valid(device: Device, desired: dict[str, Any], expected_model: str) -> bool:
    if family(device) is None or device.model != expected_model or not desired:
        return False
    # Only fan.turn_on(preset_mode=...) combines independently proven fields.
    if len(desired) > 1 and (
        set(desired) != {"powerEnabled", "opMode"}
        or desired["powerEnabled"] is not True
    ):
        return False
    for field, value in desired.items():
        if not control_available(device, field):
            return False
        if field in ("powerEnabled", *SWITCHES.values()):
            if type(value) is not bool:
                return False
        elif field in NUMBERS.values():
            limits = number_range(device, field)
            if limits is None or not limits.accepts(value):
                return False
        elif value not in options(
            device, field, MODES if field == "opMode" else COLORS
        ):
            return False
    if desired.get("powerEnabled") is True:
        return desired.get("opMode", reported(device, "opMode")) in options(
            device, "opMode", MODES
        )
    return True


def sensor_metadata(device: Device, field: str) -> dict[str, Any] | None:
    primary = metadata(device, "sensor", field)
    if field != "pm2_5":
        return primary
    alias = metadata(device, "sensor", "pm2.5")
    if primary is not None and alias is not None and primary != alias:
        return None
    return primary if primary is not None else alias


def _sensor_value(device: Device, field: str) -> Any:
    value = device.sensor.get(field)
    if field == "pm2_5" and "pm2.5" in device.sensor:
        alias = device.sensor["pm2.5"]
        if field in device.sensor and (
            type(value) is not type(alias) or value != alias
        ):
            return None
        return alias
    return value


def _within_bounds(value: int | float, meta: dict[str, Any]) -> bool:
    minimum, maximum = meta.get("min"), meta.get("max")
    for bound in (minimum, maximum):
        if bound is not None and (
            type(bound) not in (int, float) or not -1e9 <= bound <= 1e9
        ):
            return False
    return (minimum is None or value >= minimum) and (
        maximum is None or value <= maximum
    )


def reading(device: Device, key: str) -> tuple[str | int | float, str | None] | None:
    field = SENSORS[key]
    meta = sensor_metadata(device, field)
    value = _sensor_value(device, field)
    if (
        meta is None
        or type(value) not in (int, float)
        or not -1e9 <= value <= 1e9
        or not _within_bounds(value, meta)
        or (key not in ("temperature", "humidity") and type(value) is not int)
    ):
        return None
    unit = meta.get("unit")
    if key == "temperature":
        if unit in CELSIUS:
            celsius = float(value)
        elif unit in FAHRENHEIT:
            celsius = TemperatureConverter.convert(value, "°F", "°C")
        else:
            return None
        # Local room-temperature plausibility envelope.
        return (celsius, UnitOfTemperature.CELSIUS) if -50 <= celsius <= 70 else None
    if key == "humidity":
        return (value, "%") if unit == "%" and 0 <= value <= 100 else None
    if key == "pm2_5":
        if value < 0:
            return None
        if unit in ("µg/m3", "µg/m³"):
            return value, "µg/m³"
        if unit in ("mg/m3", "mg/m³"):
            return value * 1000, "µg/m³"
        return None
    if key in ("filter_life", "filter_status"):
        # The filter contract is remaining percentage, not elapsed time.
        if unit not in (None, "", "%") or not 0 <= value <= 100:
            return None
        if key == "filter_life":
            return value, "%"
        if (name := family(device)) is None:
            return None
        error, warning = (0, 30) if name == "air_1001i" else (20, 50)
        return (
            "error" if value <= error else "warning" if value <= warning else "ok",
            None,
        )
    if unit not in (None, ""):
        return None
    if key == "air_quality":
        quality = QUALITY.get(value)
        return (quality, None) if quality is not None else None
    # TVOC is an unscaled numeric value. No concentration unit or AQI is inferred.
    return (value, None) if value >= 0 else None
