"""The time a radio device lost its connection (``rfTimeofConnectionLost``).

A zone controller keeps the time as text. Without a connection loss it holds
the fixed placeholder ``2000-00-00T00:00``, which is no date: nothing was
lost, so there is no time. Digits may be single (``2026-1-5T7:3``), seconds
are optional. The text carries no time zone as a rule; it is a local time of
the controller, like the app reads it. A text with an offset (``Z`` or
``+hh:mm``) keeps it; a text without one is read as time of the Home
Assistant time zone. The entity shows the text as reported in its attributes.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from typing import Final

from homeassistant.util import dt as dt_util

PLACEHOLDER: Final = "2000-00-00T00:00"
# Longer texts are no timestamp of this form; never shown in attributes.
MAX_TEXT_LENGTH: Final = 40
FIRST_YEAR: Final = 2001  # the controller's null time is in the year 2000
# A clock a little ahead of ours is no future time.
FUTURE_TOLERANCE: Final = timedelta(days=1)

_PATTERN: Final = re.compile(
    r"^([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})T([0-9]{1,2}):([0-9]{1,2})"
    r"(?::([0-9]{1,2}))?(Z|[+-][0-9]{2}:?[0-9]{2})?$"
)


def printable_text(value: object) -> str | None:
    """The text of a reading that can be shown, ``None`` otherwise."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_TEXT_LENGTH or not text.isprintable():
        return None
    return text


def connection_lost_time(text: str, now: datetime | None = None) -> datetime | None:
    """The instant of a valid text, ``None`` otherwise.

    A text without offset is a local time of the controller and read in the
    Home Assistant time zone. ``None`` for the placeholder (month and day 00
    are no calendar date, it needs no rule of its own), a calendar-invalid
    date, a date before the controller's null year and a time in the future.
    """
    match = _PATTERN.fullmatch(text)
    if match is None:
        return None
    year, month, day, hour, minute, second, offset = match.groups()
    if int(year) < FIRST_YEAR:
        return None
    if offset is None:
        zone: tzinfo = dt_util.get_default_time_zone()
    elif offset == "Z":
        zone = UTC
    else:
        sign = -1 if offset[0] == "-" else 1
        digits = offset[1:].replace(":", "")
        hours, minutes = int(digits[:2]), int(digits[2:])
        if hours > 14 or minutes > 59:
            return None
        zone = timezone(sign * timedelta(hours=hours, minutes=minutes))
    try:
        moment = datetime(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            int(second or 0),
            tzinfo=zone,
        )
    except ValueError:
        return None
    if moment > (now or datetime.now(UTC)) + FUTURE_TOLERANCE:
        return None
    return moment
