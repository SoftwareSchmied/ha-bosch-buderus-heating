"""Reviewed unit aliases shared by PointT measurements and controls."""

TEMPERATURE_UNITS = {
    **dict.fromkeys(("C", "degC", "°C", "ºC"), "°C"),
    **dict.fromkeys(("F", "degF", "°F"), "°F"),
    "K": "K",
}
DURATION_UNITS = {
    **dict.fromkeys(("s", "sec", "secs", "seconds"), "s"),
    **dict.fromkeys(("m", "min", "mins", "minutes"), "min"),
    **dict.fromkeys(("h", "hour", "hours"), "h"),
    **dict.fromkeys(("d", "days"), "d"),
}
# The display contract uses this factor; kBtu is not a native HA energy unit.
ENERGY_UNITS = {
    "Wh": ("Wh", 1.0),
    "wh": ("Wh", 1.0),
    "kWh": ("kWh", 1.0),
    "KWh": ("kWh", 1.0),
    "kBtu": ("kWh", 0.29307107),
}


_SPELLINGS = {
    **TEMPERATURE_UNITS,
    **DURATION_UNITS,
    "KWh": "kWh",
    "wh": "Wh",
}


def canonical_unit(unit: str | None) -> str | None:
    """Match equivalent spellings, without treating different scales as aliases.

    Temperatures use the reviewed aliases: "C", "degC", "°C" and "ºC" are one
    unit, so a schema that names "C" also accepts a reading reported in "°C".
    """
    return _SPELLINGS.get(unit or "", unit)


def energy_kwh_scale(unit: str | None) -> float | None:
    """Convert a reported energy unit without guessing an unknown spelling."""
    if (conversion := ENERGY_UNITS.get(unit or "")) is None:
        return None
    canonical, scale = conversion
    return scale / 1000 if canonical == "Wh" else scale
