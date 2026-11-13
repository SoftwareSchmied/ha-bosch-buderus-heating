"""Family contracts for Bacon Midea and RWH domestic hot water devices."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .metadata import CELSIUS
from .metadata import metadata as metadata
from .metadata import writable as writable
from .models import Device

FAMILIES = {
    "midea": ("Hot Water Series", "dhw_midea_unknown_v2"),
    "rwh": (
        "Heat Pump Water Heater",
        "dhw_heatpump",
        "hpwh_vw_v1",
        "hpwh_fs_lpp_v1",
        "hpwh_fs_mpp_v1",
    ),
}
# Stable HA states, each mapped only to its own family's wire values.
MODES = {
    "midea": {
        "electric": "eHeater",
        "economy": "economy",
        "hybrid": "hybrid",
        "smart": "smart",
    },
    "rwh": {"comfort": "comfort", "dual": "dual", "eco": "eco"},
}
STATUS_FIELDS = {
    "energy_saving": "energySavingModeEnabled",
    "standard_mode": "standardModeEnabled",
    "thermal_disinfection": "thermalDisinfectionEnabled",
    "vacation": "vacationModeEnabled",
    "top_heater": "topElectricHeatEnabled",
    "bottom_heater": "bottomElectricHeatEnabled",
}


def family(device: Device) -> str | None:
    """Use the documented appliance type prefixes, never the user title."""
    if device.model:
        for name, prefixes in FAMILIES.items():
            if device.model.startswith(prefixes):
                return name
    return None


def temperature(value: object, meta: dict[str, Any] | None) -> int | None:
    """Whole Celsius degrees; reject incompatible units and error-like values.

    The 0..100 interval is a local water-temperature guard, not a vendor limit.
    Device limits, when present, may narrow it. Missing units are not guessed.
    """
    if (
        type(value) is not int
        or not 0 <= value <= 100
        or meta is None
        or meta.get("unit") not in CELSIUS
    ):
        return None
    for key, lower in (("min", True), ("max", False)):
        bound = meta.get(key)
        if bound is not None and (
            type(bound) is not int
            or not 0 <= bound <= 100
            or (value < bound if lower else value > bound)
        ):
            return None
    return value


@dataclass(frozen=True, slots=True)
class TemperatureRange:
    minimum: int
    maximum: int
    step: int

    def accepts(self, value: object) -> bool:
        return (
            type(value) is int
            and self.minimum <= value <= self.maximum
            and (value - self.minimum) % self.step == 0
        )


def target_range(device: Device) -> TemperatureRange | None:
    meta = metadata(device, "state", "tempSetpoint")
    if meta is None:
        return None
    minimum, maximum, step = meta.get("min"), meta.get("max"), meta.get("step", 1)
    if (
        type(minimum) is not int
        or type(maximum) is not int
        or type(step) is not int
        or not 0 <= minimum < maximum <= 100
        or not 1 <= step <= maximum - minimum
        or meta.get("unit") not in CELSIUS
    ):
        return None
    ceiling = device.state.reported.get("setTempMax") if device.state else None
    if ceiling is not None:
        if type(ceiling) is not int or not minimum <= ceiling <= 100:
            return None
        maximum = min(maximum, ceiling)
    # The UI's upper endpoint must itself lie on the offered grid.
    maximum -= (maximum - minimum) % step
    return TemperatureRange(minimum, maximum, step)


def target(device: Device) -> int | None:
    value = device.state.reported.get("tempSetpoint") if device.state else None
    limits = target_range(device)
    return value if limits is not None and limits.accepts(value) else None


def offered_modes(device: Device) -> list[str]:
    name = family(device)
    meta = metadata(device, "state", "opMode")
    offered = meta.get("enum") if meta is not None else None
    if name is None or not isinstance(offered, list):
        return []
    return [key for key, wire in MODES[name].items() if wire in offered]


def operation(device: Device) -> str | None:
    name = family(device)
    if name is None or device.state is None:
        return None
    power = device.state.reported.get("powerEnabled")
    if power is False:
        return "off"
    if power is not True:
        return None
    wire = device.state.reported.get("opMode")
    return next((key for key, code in MODES[name].items() if code == wire), None)


def operation_list(device: Device) -> list[str]:
    if not writable(metadata(device, "state", "powerEnabled")):
        return []
    result = ["off"]
    if writable(metadata(device, "state", "opMode")):
        result.extend(offered_modes(device))
    return result


def command_valid(device: Device, desired: dict[str, Any], expected: str) -> bool:
    """Revalidate the complete, narrowly permitted patch after the fresh read."""
    if family(device) != expected or device.state is None:
        return False
    if set(desired) == {"tempSetpoint"}:
        limits = target_range(device)
        return (
            writable(metadata(device, "state", "tempSetpoint"))
            and target(device) is not None
            and limits is not None
            and limits.accepts(desired["tempSetpoint"])
        )
    if type(device.state.reported.get("powerEnabled")) is not bool or not writable(
        metadata(device, "state", "powerEnabled")
    ):
        return False
    if set(desired) == {"powerEnabled"}:
        return desired["powerEnabled"] is False
    if (
        set(desired) != {"powerEnabled", "opMode"}
        or desired["powerEnabled"] is not True
    ):
        return False
    return writable(metadata(device, "state", "opMode")) and any(
        MODES[expected][key] == desired["opMode"] for key in offered_modes(device)
    )


def status(device: Device, key: str) -> bool | None:
    field = STATUS_FIELDS[key]
    value = device.sensor.get(field)
    return (
        value
        if type(value) is bool and metadata(device, "sensor", field) is not None
        else None
    )
