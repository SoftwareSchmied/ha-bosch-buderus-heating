"""Contracts for the connected Dry 4000i, 6000i and 8000i families."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.util.unit_conversion import TemperatureConverter

from .metadata import CELSIUS, metadata, writable
from .models import Device

FAMILIES = {
    "dry_4000i": ("Dry 4000i", "dry_4000i_v2"),
    "dry_6000i": ("Dry 6000i", "dry_6000i_v2"),
    "dry_8000i": ("Dry 8000i", "dry_8000i_v2"),
}
MODES = ("auto", "night", "laundry")
FLAGS = {
    "water_tank_full": "waterTankFull",
    "defrosting": "defrostingActive",
    "condensate_pump": "condensatePumpRunning",
    "idle": "idleState",
}
# Independent boolean settings, offered only when declared writable.
SWITCHES = {"child_lock": "childLockEnabled", "display": "displayEnabled"}
STATUSES = ("off", "running", "idle", "water_tank_full", "defrosting", "device_error")
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


def options(device: Device) -> list[str]:
    meta = metadata(device, "state", "opMode")
    offered = meta.get("enum") if meta is not None else None
    return (
        [mode for mode in MODES if mode in offered] if isinstance(offered, list) else []
    )


def boolean_control(device: Device, field: str) -> bool:
    return type(reported(device, field)) is bool and writable(
        metadata(device, "state", field)
    )


@dataclass(frozen=True, slots=True)
class HumidityRange:
    minimum: int
    maximum: int
    step: int

    def accepts(self, value: object) -> bool:
        return (
            type(value) is int
            and self.minimum <= value <= self.maximum
            and (value - self.minimum) % self.step == 0
        )


def target_range(device: Device) -> HumidityRange | None:
    meta = metadata(device, "state", "humiditySetpoint")
    # The target contract explicitly uses percentage, including omitted units.
    if meta is None or meta.get("unit") not in (None, "", "%"):
        return None
    minimum, maximum, step = meta.get("min"), meta.get("max"), meta.get("step", 1)
    if (
        type(minimum) is not int
        or type(maximum) is not int
        or type(step) is not int
        or not 0 <= minimum < maximum <= 100
        or not 1 <= step <= maximum - minimum
    ):
        return None
    return HumidityRange(minimum, maximum - (maximum - minimum) % step, step)


def target(device: Device) -> int | None:
    value = reported(device, "humiditySetpoint")
    limits = target_range(device)
    return value if limits is not None and limits.accepts(value) else None


def measurement(device: Device, field: str) -> float | None:
    meta = metadata(device, "sensor", field)
    value = device.sensor.get(field)
    if (
        meta is None
        or not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not -1000 <= value <= 1000
    ):
        return None
    minimum, maximum = meta.get("min"), meta.get("max")
    for bound in (minimum, maximum):
        if bound is not None and (
            type(bound) not in (int, float) or not -1000 <= bound <= 1000
        ):
            return None
    if (minimum is not None and value < minimum) or (
        maximum is not None and value > maximum
    ):
        return None
    unit = meta.get("unit")
    if field == "relHumidity":
        # Unlike the purifier family, Dry reports relative humidity as integer.
        return (
            float(value)
            if type(value) is int and 0 <= value <= 100 and unit == "%"
            else None
        )
    if field != "roomTemperature":
        return None
    if unit in CELSIUS:
        result = float(value)
    elif unit in FAHRENHEIT:
        result = TemperatureConverter.convert(value, "°F", "°C")
    else:
        return None
    return result if -50 <= result <= 70 else None


def flag(device: Device, key: str) -> bool | None:
    field = FLAGS[key]
    value = device.sensor.get(field)
    return (
        value
        if metadata(device, "sensor", field) is not None and type(value) is bool
        else None
    )


def operating_status(device: Device) -> str | None:
    """Interpret explicit reports, never the difference between humidity values.

    The caller must establish current state, sensor, metadata and information.
    Error and blocked states take precedence over normal idle/running reports.
    Missing observations cannot establish normal operation.
    """
    if device.error_present is True:
        return "device_error"
    tank, defrosting, idle = (
        flag(device, key) for key in ("water_tank_full", "defrosting", "idle")
    )
    if tank is True:
        return "water_tank_full"
    if defrosting is True:
        return "defrosting"
    power = reported(device, "powerEnabled")
    if power is False:
        return "off"
    if (
        power is not True
        or device.error_present is not False
        or tank is not False
        or defrosting is not False
        or reported(device, "opMode") not in MODES
    ):
        return None
    return "idle" if idle is True else "running" if idle is False else None


def command_valid(device: Device, desired: dict[str, Any], expected_model: str) -> bool:
    # The reviewed commands each change one field. No timer, pump, fan,
    # schedule or generic state-writing escape is exposed by this family.
    if family(device) is None or device.model != expected_model or len(desired) != 1:
        return False
    field, value = next(iter(desired.items()))
    if not writable(metadata(device, "state", field)):
        return False
    if field in ("powerEnabled", *SWITCHES.values()):
        if not boolean_control(device, field) or type(value) is not bool:
            return False
        return (
            field != "powerEnabled"
            or not value
            or reported(device, "opMode") in options(device)
        )
    if field == "opMode":
        return reported(device, "opMode") in MODES and value in options(device)
    if field == "humiditySetpoint":
        limits = target_range(device)
        # Laundry is a distinct program. Its stored target remains readable;
        # target editing is offered only in humidity-controlled modes.
        return (
            reported(device, "opMode") in ("auto", "night")
            and target(device) is not None
            and limits is not None
            and limits.accepts(value)
        )
    return False
