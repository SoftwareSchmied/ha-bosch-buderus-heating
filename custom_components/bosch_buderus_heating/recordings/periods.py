"""The values of a series for yesterday, a month and a year.

The periods follow the days of the plant. A month is the sum of its days
when every day since the first of the month (or since the first recorded
day) is known, otherwise the plant's own month value (the month still
running: the known days, or its month value where larger); a year is the sum of
its months since the first recorded month. A value is final when every
part is final; a value of a past period is only shown once it is final
(``manager``), so "previous month" changes after the first confirmed read
of the finished month.

``c = 0`` is a gap, never a zero (rule 7): a day without data is no day of
a month's sum (the plant's month value is used instead), a period that is
a gap has no value, and a month that is a gap adds nothing to its year.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Final

from .ledger import Cell, Ledger
from .parse import add_months, month_start


class Period(StrEnum):
    YESTERDAY = "yesterday"
    PREVIOUS_MONTH = "previous_month"
    PREVIOUS_YEAR = "previous_year"
    CURRENT_MONTH = "current_month"
    CURRENT_YEAR = "current_year"

    @property
    def past(self) -> bool:
        return self in _PAST


_PAST: Final = frozenset(
    {Period.YESTERDAY, Period.PREVIOUS_MONTH, Period.PREVIOUS_YEAR}
)
# Coarser resolution wins when parts are combined.
_RESOLUTIONS: Final = ("hourly", "daily", "monthly")


@dataclass(frozen=True, slots=True)
class PeriodValue:
    """``y``/``c`` of a period from ``start`` up to (excluding) ``end``."""

    start: date
    end: date
    y: float
    c: float
    final: bool
    resolution: str
    sources: frozenset[str]
    # Parts counted as 0 because they had no data at all in the period
    # (``eheater``, see ``RecordingsManager.recorded``).
    missing: frozenset[str] = frozenset()

    @property
    def gap(self) -> bool:
        """No data in the period (``c = 0``): no value, never a zero."""
        return self.c <= 0

    def value(self, mean: bool) -> float | None:
        if self.gap:
            return None
        return self.y / self.c if mean else self.y


def bounds(period: Period, today: date) -> tuple[date, date]:
    """First day and the day after the last day of a period."""
    if period is Period.YESTERDAY:
        return today - timedelta(days=1), today
    if period is Period.CURRENT_MONTH:
        first = month_start(today)
        return first, add_months(first, 1)
    if period is Period.PREVIOUS_MONTH:
        first = add_months(month_start(today), -1)
        return first, add_months(first, 1)
    if period is Period.CURRENT_YEAR:
        return date(today.year, 1, 1), date(today.year + 1, 1, 1)
    return date(today.year - 1, 1, 1), date(today.year, 1, 1)


def combine(values: list[PeriodValue]) -> PeriodValue | None:
    """The sum of the same period of several series (electricity); a gap
    when one of them is a gap."""
    if not values:
        return None
    first = values[0]
    # A part counted as 0 (``missing``) is no gap of the sum.
    gap = any(item.gap and not item.missing for item in values)
    return PeriodValue(
        first.start,
        first.end,
        0.0 if gap else sum(item.y for item in values),
        0.0 if gap else sum(item.c for item in values),
        all(item.final for item in values),
        max((item.resolution for item in values), key=_RESOLUTIONS.index),
        frozenset().union(*(item.sources for item in values)),
        frozenset().union(*(item.missing for item in values)),
    )


def _cell_value(cell: Cell, start: date, end: date, resolution: str) -> PeriodValue:
    """A day or month of the plant; without data (``c <= 0``) a gap, whatever
    placeholder the plant sent as its sum."""
    gap = float(cell[1]) <= 0
    return PeriodValue(
        start,
        end,
        0.0 if gap else float(cell[0]),
        0.0 if gap else float(cell[1]),
        bool(cell[4]),
        resolution,
        frozenset({str(cell[2])}),
    )


def _day(ledger: Ledger, day: date) -> PeriodValue | None:
    cell = ledger.day(day)
    if cell is None:
        return None
    return _cell_value(cell, day, day + timedelta(days=1), "daily")


def month_value(ledger: Ledger, first: date, today: date) -> PeriodValue | None:
    """One month, from its days or from the plant's month value."""
    end = add_months(first, 1)
    history = ledger.first_day()
    if history is None or history >= end or first > today:
        return None
    last = min(end - timedelta(days=1), today)
    begin = max(first, history)
    days: list[PeriodValue] = []
    day = begin
    while day <= last:
        value = _day(ledger, day)
        if value is None or value.gap:
            # A day without data: the month needs the plant's month value.
            break
        days.append(value)
        day += timedelta(days=1)
    else:
        if days:
            # A month still running is never final.
            return PeriodValue(
                first,
                end,
                sum(item.y for item in days),
                sum(item.c for item in days),
                all(item.final for item in days) and last == end - timedelta(days=1),
                "daily",
                frozenset().union(*(item.sources for item in days)),
            )
    cell = ledger.month(first)
    if today < end:
        return _running_month(ledger, first, begin, last, cell)
    if cell is None:
        return None
    return _cell_value(cell, first, end, "monthly")


def _running_month(
    ledger: Ledger, first: date, begin: date, last: date, cell: Cell | None
) -> PeriodValue | None:
    """The month still running with a day missing. Its month value is read
    at the start of Home Assistant and not again, so the known days count;
    for a sum the month value stays where it is the larger one (both are a
    lower bound). Never final."""
    end = add_months(first, 1)
    known = [
        value
        for offset in range((last - begin).days + 1)
        if (value := _day(ledger, begin + timedelta(days=offset))) is not None
        and not value.gap
    ]
    y = sum(item.y for item in known)
    if (
        cell is not None
        and float(cell[1]) > 0
        and (not known or (not ledger.series.mean and float(cell[0]) > y))
    ):
        return PeriodValue(
            first,
            end,
            float(cell[0]),
            float(cell[1]),
            False,
            "monthly",
            frozenset({str(cell[2])}),
        )
    if not known:
        return None
    return PeriodValue(
        first,
        end,
        y,
        sum(item.c for item in known),
        False,
        "daily",
        frozenset().union(*(item.sources for item in known)),
    )


def year_value(ledger: Ledger, year: int, today: date) -> PeriodValue | None:
    history = ledger.first_day()
    if history is None or history.year > year or year > today.year:
        return None
    first = month_start(max(date(year, 1, 1), history))
    last = month_start(min(date(year, 12, 31), today))
    months: list[PeriodValue] = []
    month = first
    while month <= last:
        value = month_value(ledger, month, today)
        if value is None:
            return None
        months.append(value)
        month = add_months(month, 1)
    if not months:
        return None
    # A month without data adds nothing (its sum is 0, ``_cell_value``); a
    # year of gaps only is a gap.
    return PeriodValue(
        date(year, 1, 1),
        date(year + 1, 1, 1),
        sum(item.y for item in months),
        sum(item.c for item in months),
        all(item.final for item in months) and last == date(year, 12, 1),
        max((item.resolution for item in months), key=_RESOLUTIONS.index),
        frozenset().union(*(item.sources for item in months)),
    )


def period_value(ledger: Ledger, period: Period, today: date) -> PeriodValue | None:
    """The value of ``period`` as the plant recorded it, or None."""
    start, _ = bounds(period, today)
    if period is Period.YESTERDAY:
        return _day(ledger, start)
    if period in (Period.CURRENT_MONTH, Period.PREVIOUS_MONTH):
        return month_value(ledger, start, today)
    return year_value(ledger, start.year, today)
