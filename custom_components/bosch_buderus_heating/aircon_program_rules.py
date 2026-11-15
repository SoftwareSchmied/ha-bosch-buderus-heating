"""Switch points of an air conditioner's weekly programs (pure, no HA).

A point of an air-conditioner program carries the operation mode and,
depending on the mode, a temperature and a fan speed: ``{"dayOfWeek",
"time", "parameters": {"operationMode", "temperatureSetpoint",
"fanSpeed"}}``. The program kind register (``switch_programs``) keeps such a
setting as one text, ``<mode> [<temperature>] [<fan speed>]`` (``cool 24.0
auto``, ``fanOnly low``, ``off``), so the dialog, the action and the weekly
view handle it like a level. The program is written as ``{"switchPoints":
[...]}``, the whole week in one request.

What a change may use comes from the unit only: the modes of the program
configuration (``switchPrograms/configuration``, one entry per mode with its
temperature range and fan speeds), the limits the program reports (points in
all and per day, time raster) and the write permission of both. ``off`` ends
the setting before the next point of the day, as in the app; before the
first point of a day the unit is off. Nothing is taken from the app's
fallback values: without a readable configuration and limits a program is
shown, never changed.

Expected, untested on a real installation: the shapes follow the vendor app.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from .pointt.models import JsonValue, Resource
from .unit_contracts import TEMPERATURE_UNITS

CONFIGURATION: Final = "/airConditioning/switchPrograms/configuration"
TEMPERATURE_SETPOINT: Final = "/airConditioning/temperatureSetpoint"
OFF: Final = "off"
# The operation modes the app puts into a program; ``internalCleaning`` is
# never offered (the app filters it out as well).
MODES: Final = ("auto", "cool", "heat", "dry", "fanOnly")
FAN_SPEEDS: Final = ("auto", "quiet", "low", "mid", "high", "turbo")
# A room unit: a temperature outside this range is never written, even when
# the reported range is wider (broken metadata), as for the target.
PLAUSIBLE_C: Final = (10.0, 40.0)
# Without a reported step the temperature of a point is a whole degree, the
# same rule as the unit's target temperature.
DEFAULT_STEP: Final = 1.0
_POINT_FIELDS: Final = frozenset({"dayOfWeek", "time", "parameters"})
_PARAMETER_FIELDS: Final = frozenset(
    {"operationMode", "temperatureSetpoint", "fanSpeed"}
)


@dataclass(frozen=True, slots=True)
class Setting:
    """The mode, temperature and fan speed of one switch point."""

    mode: str
    temperature: float | None = None
    fan: str | None = None

    @property
    def text(self) -> str:
        """The canonical text, e.g. ``cool 24.0 auto``."""
        parts = [self.mode]
        if self.temperature is not None:
            parts.append(number_text(self.temperature))
        if self.fan is not None:
            parts.append(self.fan)
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class ModeRule:
    """What the configuration allows for one operation mode."""

    mode: str
    minimum: float | None
    maximum: float | None
    step: float | None
    fans: tuple[str, ...]

    @property
    def has_temperature(self) -> bool:
        return self.minimum is not None


@dataclass(frozen=True, slots=True)
class AirconRules:
    """The modes and limits a change of an air-conditioner program may use."""

    modes: Mapping[str, ModeRule]
    max_switch_points: int
    max_switch_points_per_day: int
    time_raster: int

    @property
    def offered(self) -> tuple[str, ...]:
        """The modes in the app's order, ``off`` last."""
        return (*(mode for mode in MODES if mode in self.modes), OFF)


def number_text(value: float) -> str:
    """The shortest decimal text with at least one decimal, e.g. ``24.0``."""
    text = repr(round(value, 2))
    return text if "." in text else f"{text}.0"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def setting_from_wire(item: object) -> Setting | None:
    """The setting of one wire point's ``parameters``, or ``None``.

    The mode must be a known token; a temperature is a JSON number, a fan
    speed a known token. ``off`` carries neither.
    """
    if not isinstance(item, Mapping) or not set(item) <= _PARAMETER_FIELDS:
        return None
    mode = item.get("operationMode")
    if mode != OFF and mode not in MODES:
        return None
    assert isinstance(mode, str)
    raw_temperature = item.get("temperatureSetpoint")
    raw_fan = item.get("fanSpeed")
    temperature = _number(raw_temperature)
    if raw_temperature is not None and temperature is None:
        return None
    if raw_fan is not None and raw_fan not in FAN_SPEEDS:
        return None
    fan = raw_fan if isinstance(raw_fan, str) else None
    if mode == OFF and (temperature is not None or fan is not None):
        return None
    return Setting(mode, temperature, fan)


def parse_point(item: object) -> tuple[str, int, Setting] | None:
    """Day, minute and setting of one wire point, or ``None`` when malformed."""
    if not isinstance(item, Mapping) or set(item) != _POINT_FIELDS:
        return None
    day, minute = item.get("dayOfWeek"), item.get("time")
    if not isinstance(day, str) or type(minute) is not int:
        return None
    setting = setting_from_wire(item.get("parameters"))
    if setting is None:
        return None
    return day, minute, setting


def parse_setting(text: object) -> Setting | None:
    """A setting from its text: the mode first, then a temperature and a
    fan speed in any order (``cool 24 auto``, ``fanOnly low``, ``off``).

    The mode is the first word because ``auto`` is a mode and a fan speed.
    """
    if not isinstance(text, str):
        return None
    words = text.split()
    if not words or len(words) > 3:
        return None
    mode, rest = words[0], words[1:]
    if mode != OFF and mode not in MODES:
        return None
    temperature: float | None = None
    fan: str | None = None
    for word in rest:
        try:
            number = float(word.replace(",", "."))
        except ValueError:
            if word not in FAN_SPEEDS or fan is not None:
                return None
            fan = word
            continue
        if temperature is not None or not math.isfinite(number):
            return None
        temperature = number
    if mode == OFF and rest:
        return None
    return Setting(mode, temperature, fan)


def canonical_setting(text: object) -> str | None:
    """The canonical text of an entered setting, or ``None``.

    A temperature with more decimals than the canonical text holds keeps
    them, so the step check refuses it instead of rounding it.
    """
    setting = parse_setting(text)
    if setting is None:
        return None
    if setting.temperature is not None and not math.isclose(
        float(number_text(setting.temperature)), setting.temperature, abs_tol=1e-9
    ):
        parts = [setting.mode, repr(setting.temperature)]
        if setting.fan is not None:
            parts.append(setting.fan)
        return " ".join(parts)
    return setting.text


def _celsius(unit: object) -> bool:
    return isinstance(unit, str) and TEMPERATURE_UNITS.get(unit) == "°C"


def _mode_rule(
    item: object, unit_fallback: object, step_fallback: float | None
) -> ModeRule | None:
    """One configuration entry; ``None`` for an unknown or broken entry."""
    if not isinstance(item, Mapping):
        return None
    operation = item.get("operationMode")
    allowed = operation.get("allowedValues") if isinstance(operation, Mapping) else None
    # The app takes the first allowed value as the mode of the entry.
    mode = allowed[0] if isinstance(allowed, list) and allowed else None
    if mode not in MODES:
        return None
    assert isinstance(mode, str)
    minimum = maximum = step = None
    setpoint = item.get("temperatureSetpoint")
    if setpoint is not None:
        if not isinstance(setpoint, Mapping):
            return None
        low, high = _number(setpoint.get("minValue")), _number(setpoint.get("maxValue"))
        unit = setpoint.get("unitOfMeasure", unit_fallback)
        reported_step = _number(setpoint.get("stepSize"))
        if low is None or high is None or low >= high or not _celsius(unit):
            # A range that cannot be used: the mode is not offered.
            return None
        minimum, maximum = max(low, PLAUSIBLE_C[0]), min(high, PLAUSIBLE_C[1])
        if minimum >= maximum:
            return None
        step = (
            reported_step
            if reported_step is not None and reported_step > 0
            else step_fallback or DEFAULT_STEP
        )
    fan = item.get("fanSpeed")
    fan_values = fan.get("allowedValues") if isinstance(fan, Mapping) else None
    fans = (
        tuple(
            token
            for token in FAN_SPEEDS
            if isinstance(fan_values, list) and token in fan_values
        )
        if fan is not None
        else ()
    )
    return ModeRule(mode, minimum, maximum, step, fans)


def bounded_int(value: object, maximum: int) -> int | None:
    """A positive whole number up to ``maximum``; ``15.0`` counts as 15 (the
    app reads the raster as a float)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if type(value) is not int or not 1 <= value <= maximum:
        return None
    return value


def program_limits(
    program: Resource, configuration: Resource | None
) -> tuple[int, int, int] | None:
    """Points in all, per day and the raster: from the program as the app
    reads them, else from the configuration; ``None`` when one is missing."""
    limits: list[int] = []
    for key, maximum in (
        ("maxNbOfSwitchPoints", 7 * 48),
        ("maxNbOfSwitchPointsPerDay", 48),
        ("switchPointTimeRaster", 60),
    ):
        value = bounded_int(program.program_fields.get(key), maximum)
        if value is None and configuration is not None:
            value = bounded_int(configuration.program_fields.get(key), maximum)
        if value is None:
            return None
        limits.append(value)
    total, per_day, raster = limits
    if 24 * 60 % raster:
        return None
    return total, per_day, raster


def rules(program: Resource, readings: Mapping[str, Resource]) -> AirconRules | str:
    """The rules of a change, or why there are none as a stable token."""
    configuration = readings.get(CONFIGURATION)
    if configuration is None:
        return "program_configuration_missing"
    if not configuration.metadata.writable:
        return "configuration_not_writable"
    entries: JsonValue = configuration.program_fields.get("allowedValues")
    if not isinstance(entries, list):
        return "program_configuration_missing"
    limits = program_limits(program, configuration)
    if limits is None:
        return "program_limits_missing"
    target = readings.get(TEMPERATURE_SETPOINT)
    unit_fallback = target.metadata.unit if target is not None else None
    step_fallback = (
        target.metadata.step_size
        if target is not None
        and target.metadata.step_size is not None
        and target.metadata.step_size > 0
        else None
    )
    modes: dict[str, ModeRule] = {}
    for item in entries:
        rule = _mode_rule(item, unit_fallback, step_fallback)
        if rule is not None and rule.mode not in modes:
            modes[rule.mode] = rule
    if not modes:
        return "program_modes_unknown"
    return AirconRules(modes, *limits)


def setting_issue(text: object, rules_: AirconRules) -> str | None:
    """Why a setting text may not be used, as a token, or ``None``.

    ``off`` always; another mode only from the configuration, with a
    temperature exactly when the mode reports a range (in it and on its
    step) and a fan speed only from the mode's list.
    """
    setting = parse_setting(text)
    if setting is None or setting.text != text:
        return "setting_not_allowed"
    if setting.mode == OFF:
        return None
    rule = rules_.modes.get(setting.mode)
    if rule is None:
        return "setting_not_allowed"
    if rule.has_temperature != (setting.temperature is not None):
        return "setting_not_allowed"
    if setting.temperature is not None:
        assert rule.minimum is not None and rule.maximum is not None
        assert rule.step is not None
        if not rule.minimum <= setting.temperature <= rule.maximum:
            return "value_out_of_range"
        steps = (setting.temperature - rule.minimum) / rule.step
        if abs(steps - round(steps)) > 1e-6:
            return "value_off_step"
    if setting.fan is not None and setting.fan not in rule.fans:
        return "setting_not_allowed"
    return None


def wire_point(day: str, minute: int, text: str) -> dict[str, JsonValue]:
    """One point in the form the app writes; the temperature as a number."""
    setting = parse_setting(text)
    if setting is None:
        raise ValueError("Not an air-conditioner setting")
    parameters: dict[str, JsonValue] = {"operationMode": setting.mode}
    if setting.temperature is not None:
        parameters["temperatureSetpoint"] = setting.temperature
    if setting.fan is not None:
        parameters["fanSpeed"] = setting.fan
    return {"dayOfWeek": day, "time": minute, "parameters": parameters}


def describe(rules_: AirconRules) -> str:
    """The allowed settings as text for an error, e.g. ``cool 16-30 (auto,
    low), off``; tokens and numbers only."""
    parts: list[str] = []
    for mode in rules_.offered:
        rule = rules_.modes.get(mode)
        if rule is None:
            parts.append(mode)
            continue
        text = mode
        if rule.minimum is not None and rule.maximum is not None:
            text += f" {rule.minimum:g}-{rule.maximum:g}/{rule.step:g}"
        if rule.fans:
            text += f" ({', '.join(rule.fans)})"
        parts.append(text)
    return "; ".join(parts)
