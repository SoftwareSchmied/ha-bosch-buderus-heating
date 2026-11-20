"""A note on a performance factor whose period holds very little heat.

The value stays as it is: a ratio of a few kilowatt hours says little (a
single start, a defrost dip or a rounded counter can move it a lot). The
attribute `note` tells so, with the limit that applied.
"""

from __future__ import annotations

from typing import Final

from .performance_history import Period

LITTLE_HEAT: Final = "little_heat"
# Heat below which a period's factor is a weak figure: a day, and the longer
# periods (month, year, since commissioning).
DAY_LIMIT_KWH: Final = 5.0
LONG_LIMIT_KWH: Final = 30.0
_DAYS: Final = frozenset({Period.TODAY, Period.YESTERDAY})


def little_heat_limit(period: Period) -> float:
    return DAY_LIMIT_KWH if period in _DAYS else LONG_LIMIT_KWH


def little_heat_attributes(period: Period, heat: float) -> dict[str, object]:
    """The note and its limit when the period's heat is below the limit."""
    limit = little_heat_limit(period)
    if heat >= limit:
        return {}
    return {"note": LITTLE_HEAT, "little_heat_below_kwh": limit}
