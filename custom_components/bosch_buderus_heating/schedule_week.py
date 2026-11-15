"""TP-07: the data of the read-only weekly view in the program dialogs.

The view shows the week as a table of seven days with segments from
midnight to midnight. A day starts with the level of the last point of the
previous days (the program repeats weekly), unless the program kind has no
such carry-over (WDDW2); an RRC day without points is marked as "no switch
points", never as "off". Minutes are the heating system's own clock time:
the time zone of Home Assistant does not change them.

Only data that the dialog already shows is handed to the browser: day and
level names, minutes and level keys; no paths, no identifiers. The view
never writes; changes go through the dialog steps as before.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, override

from homeassistant.helpers.selector import ObjectSelector

from .i18n import dialog_config
from .switch_programs import DAYS, MINUTES_PER_DAY, SwitchPoint

# The level keys the view colours; every other one gets a neutral pattern,
# and every segment carries its text as well (colour is never the only cue).
KNOWN_LEVELS = frozenset(
    {
        "comfort",
        "comfort1",
        "comfort2",
        "comfort3",
        "eco",
        "eco_plus",
        "low",
        "high",
        "off",
        "on",
        "min",
        "red",
        "nom",
        "max",
        "dem",
    }
)


def _segment(
    start: int,
    end: int,
    level: str | None,
    labels: Mapping[str, str],
    *,
    point: bool,
    carried: bool,
) -> dict[str, Any]:
    return {
        "start": start,
        "end": end,
        "level": level,
        "label": None if level is None else labels.get(level, level),
        "point": point,
        "carried": carried,
        "known": level is None or level in KNOWN_LEVELS or _is_number(level),
    }


def _is_number(level: str) -> bool:
    try:
        float(level)
    except ValueError:
        return False
    return True


def week_days(
    points: Sequence[SwitchPoint],
    *,
    labels: Mapping[str, str],
    day_names: Mapping[str, str],
    carry_over: bool = True,
    mark_empty_days: bool = False,
    changed: frozenset[int] = frozenset(),
) -> list[dict[str, Any]]:
    """The seven days with gapless segments from 0 to 1440 minutes.

    ``labels`` name the levels, ``day_names`` the weekdays (keys ``mo`` to
    ``su``). ``mark_empty_days``: a day without points has no level (RRC).
    """
    ordered = sorted(points, key=lambda point: (point.day, point.minute))
    result: list[dict[str, Any]] = []
    for day in range(len(DAYS)):
        own = [point for point in ordered if point.day == day]
        key = DAYS[day].lower()
        segments: list[dict[str, Any]] = []
        empty = not own
        if empty and (mark_empty_days or not carry_over or not ordered):
            segments.append(
                _segment(0, MINUTES_PER_DAY, None, labels, point=False, carried=False)
            )
        else:
            level = _carried_level(ordered, day) if carry_over else None
            cursor = 0
            for point in own:
                if point.minute > cursor:
                    segments.append(
                        _segment(
                            cursor,
                            point.minute,
                            level,
                            labels,
                            point=False,
                            carried=cursor == 0 and level is not None,
                        )
                    )
                cursor, level = point.minute, point.level
                # Its end is the start of the next segment (set below).
                segments.append(
                    _segment(cursor, cursor, level, labels, point=True, carried=False)
                )
            # Close every segment at the start of the next one.
            for index, segment in enumerate(segments):
                segment["end"] = (
                    segments[index + 1]["start"]
                    if index + 1 < len(segments)
                    else MINUTES_PER_DAY
                )
            if not segments:
                segments.append(
                    _segment(
                        0,
                        MINUTES_PER_DAY,
                        level,
                        labels,
                        point=False,
                        carried=level is not None,
                    )
                )
        result.append(
            {
                "key": key,
                "name": day_names.get(key, DAYS[day]),
                "empty": empty,
                "changed": day in changed,
                "segments": [item for item in segments if item["end"] > item["start"]],
            }
        )
    return result


def _carried_level(ordered: Sequence[SwitchPoint], day: int) -> str | None:
    """The level of the last point before ``day`` starts (weekly repeat)."""
    before = [point for point in ordered if point.day < day]
    if before:
        return before[-1].level
    return ordered[-1].level if ordered else None


class ScheduleWeekSelector(ObjectSelector):
    """A read-only weekly view; its field value is never used."""

    selector_type = "bosch-buderus-schedule-week"

    def __init__(self, days: list[dict[str, Any]], title: str = "") -> None:
        super().__init__()
        self.days = days
        self.title = title

    @override
    def serialize(self) -> dict[str, Any]:
        return {
            "selector": {
                self.selector_type: {
                    "days": self.days,
                    "title": self.title,
                    **dialog_config("schedule"),
                }
            }
        }
