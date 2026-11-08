"""Contract for the Bacon Midea Climate Series, independent of PointT cooling."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.util.unit_conversion import TemperatureConverter

from .metadata import metadata, writable
from .models import Device

PREFIXES = (
    "Climate Series",
    "ac_186i3",
    "ac_midea_unknown",
    "ae_700",
    "ae_midea_unknown",
    "cl_7000i",
    "cl_7100i",
    "cl_midea_unknown",
    "ui_midea_unknown",
    "rac_midea_unknown_v2",
    "climate_series",
)
MODES = {
    HVACMode.AUTO: "auto",
    HVACMode.HEAT: "heat",
    HVACMode.COOL: "cool",
    HVACMode.DRY: "dry",
    HVACMode.FAN_ONLY: "fan",
}
FANS = ("auto", "quiet", "low", "medium", "high", "turbo")
TEMPERATURE_MODES = ("auto", "heat", "cool")
SWINGS = ("vSwingEnabled", "hSwingEnabled")
# Eco is a separate boolean; it never changes the fan speed named "turbo".
ECO = "ecoEnabled"
PRESET_NONE = "none"
PRESET_ECO = "eco"
CELSIUS = ("C", "degC", "Celsius", "°C")
FAHRENHEIT = ("degF", "Fahrenheit")


def supported(device: Device) -> bool:
    return device.model is not None and device.model.startswith(PREFIXES)


def reported(device: Device, field: str) -> Any:
    return device.state.reported.get(field) if device.state else None


def options(device: Device, field: str, known: tuple[str, ...]) -> list[str]:
    meta = metadata(device, "state", field)
    offered = meta.get("enum") if meta is not None else None
    return (
        [value for value in known if value in offered]
        if isinstance(offered, list)
        else []
    )


def boolean_control(device: Device, field: str) -> bool:
    return type(reported(device, field)) is bool and writable(
        metadata(device, "state", field)
    )


def mode(device: Device) -> HVACMode | None:
    power = reported(device, "powerEnabled")
    if power is False:
        return HVACMode.OFF
    if power is not True:
        return None
    return next(
        (ha for ha, wire in MODES.items() if wire == reported(device, "opMode")), None
    )


def modes(device: Device) -> list[HVACMode]:
    if not boolean_control(device, "powerEnabled"):
        return []
    result = [HVACMode.OFF]
    if writable(metadata(device, "state", "opMode")):
        offered = options(device, "opMode", tuple(MODES.values()))
        result.extend(ha for ha, wire in MODES.items() if wire in offered)
    return result


def current_temperature(device: Device) -> float | None:
    value = device.sensor.get("roomTemperature")
    meta = metadata(device, "sensor", "roomTemperature")
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or meta is None
        or not -1000 <= value <= 1000
    ):
        return None
    unit = meta.get("unit")
    if unit in CELSIUS:
        celsius = float(value)
    elif unit in FAHRENHEIT:
        celsius = TemperatureConverter.convert(
            value, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS
        )
    else:
        return None
    # Local room-temperature plausibility guard, not a manufacturer limit.
    if not -50 <= celsius <= 70:
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
    return celsius


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
    if meta is None or meta.get("unit") not in CELSIUS:
        return None
    minimum, maximum, step = meta.get("min"), meta.get("max"), meta.get("step", 1)
    if (
        type(minimum) is not int
        or type(maximum) is not int
        or type(step) is not int
        or not 0 <= minimum < maximum <= 50
        or not 1 <= step <= maximum - minimum
    ):
        return None
    maximum -= (maximum - minimum) % step
    return TemperatureRange(minimum, maximum, step)


def target(device: Device) -> int | None:
    limits = target_range(device)
    value = reported(device, "tempSetpoint")
    return value if limits is not None and limits.accepts(value) else None


def command_valid(device: Device, desired: dict[str, Any], expected_model: str) -> bool:
    """Validate every changed field against fresh state and metadata.

    Only narrow patches are sent. No complete shadow or unrelated app setting
    is copied into a command. Combined HA mode/target requests use the same
    independently documented fields and must pass all checks before publishing.
    """
    if not supported(device) or device.model != expected_model or device.state is None:
        return False
    keys = set(desired)
    if not keys or keys - {
        "powerEnabled",
        "opMode",
        "tempSetpoint",
        "fanSpeed",
        *SWINGS,
        ECO,
    }:
        return False
    for field in keys:
        if not writable(metadata(device, "state", field)):
            return False
    if keys & {"fanSpeed", *SWINGS, ECO}:
        if len(keys) != 1:
            return False
        if "fanSpeed" in keys:
            return isinstance(reported(device, "fanSpeed"), str) and desired[
                "fanSpeed"
            ] in options(device, "fanSpeed", FANS)
        field = next(iter(keys))
        return boolean_control(device, field) and type(desired[field]) is bool
    if "powerEnabled" in keys:
        if (
            not boolean_control(device, "powerEnabled")
            or type(desired["powerEnabled"]) is not bool
        ):
            return False
        # Turning on preserves a known, currently offered stored mode.
        if (
            desired["powerEnabled"]
            and "opMode" not in keys
            and reported(device, "opMode")
            not in options(device, "opMode", tuple(MODES.values()))
        ):
            return False
    if "opMode" in keys and (
        desired.get("powerEnabled") is not True
        or desired["opMode"] not in options(device, "opMode", tuple(MODES.values()))
    ):
        return False
    if "tempSetpoint" in keys:
        limits = target_range(device)
        effective_mode = desired.get("opMode", reported(device, "opMode"))
        # No thermostat setpoint control for fan-only or dehumidification.
        if (
            effective_mode not in TEMPERATURE_MODES
            or target(device) is None
            or limits is None
            or not limits.accepts(desired["tempSetpoint"])
        ):
            return False
    return True
