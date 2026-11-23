"""Answers of the local data port and of the cloud as placed samples.

Both carry the plant's own sums: ``y`` is the sum of a slot (energy, starts,
working time, or the sum of temperature readings) and ``c`` how many values
went into it. ``c = 0`` is a slot without data (a gap or a placeholder in the
future), never a zero.

The local data port answers ``yRecordingExtended`` with groups per sample
rate; every group has its start and end with the plant's UTC offset, so a
change of summer time shows in the offsets. The cloud answers ``yRecording``
without times: an ``interval`` of a day holds its hours, of a month its days
and of a year its months, counted from the start of the interval in the
plant's time zone (the time zone of Home Assistant stands in for it).

A series kept as samples of the mean (``minute_sums``, the energy monitoring
of iCom gateways) is read as the mean ``y / c`` times the hours of each slot
(``mean_form``), for hours, days and months alike.

Hours are placed in UTC. A group whose number of hours fits neither the
elapsed hours nor the hours on the clock is not used; a day whose hours
cannot be placed (a change of summer time counted on the clock) is kept as
a day only.
"""

from __future__ import annotations

import calendar
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from enum import StrEnum
from typing import Final

from ..local_api.models import Recording, RecordingGroup
from .series import Quantity, Series, series_unit_factor

HOUR: Final = 3600
# A plausible upper bound of one hour's energy: 1.5 times a nominal output of
# 60 kW. The nominal output of a plant is not reliably known here.
MAX_KWH_PER_HOUR: Final = 1.5 * 60.0
MAX_STARTS_PER_HOUR: Final = 60.0
# A softener's water per hour, litres (a household line, far above any use).
MAX_LITRES_PER_HOUR: Final = 10000.0
# Readings outside this range are no outdoor or supply temperature.
TEMPERATURE_RANGE: Final = (-60.0, 110.0)
# Nothing recorded before this day can be a real value (a wrong clock).
EARLIEST: Final = date(2010, 1, 1)
_DAY_INTERVAL: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_MONTH_INTERVAL: Final = re.compile(r"[0-9]{4}-[0-9]{2}")
_YEAR_INTERVAL: Final = re.compile(r"[0-9]{4}")


class Rate(StrEnum):
    HOUR = "hour"
    DAY = "day"
    MONTH = "month"


@dataclass(frozen=True, slots=True)
class Sample:
    """One slot: its local day (the first of the month for a month), the
    UTC start of an hour (None when it cannot be placed), the sum ``y`` in
    the series' unit and the count ``c``."""

    rate: Rate
    day: date
    hour: int | None
    y: float
    c: float


@dataclass(slots=True)
class Parsed:
    samples: list[Sample] = field(default_factory=list)
    issues: Counter[str] = field(default_factory=Counter)
    # The days whose hours this answer holds completely.
    complete_days: set[date] = field(default_factory=set)
    # Days whose hours could not be placed (summer time counted on the clock).
    unplaced_days: set[date] = field(default_factory=set)
    # The plant's UTC offset at the end of the newest group (local only).
    offset: timedelta | None = None


def interval_text(value: date, rate: Rate) -> str:
    """The cloud's ``interval`` for the day, month or year of ``value``."""
    if rate is Rate.HOUR:
        return value.isoformat()
    if rate is Rate.DAY:
        return f"{value.year:04d}-{value.month:02d}"
    return f"{value.year:04d}"


def valid_interval(value: str) -> bool:
    """A calendar day, month or year in the cloud's ``interval`` form."""
    try:
        if _DAY_INTERVAL.fullmatch(value):
            date.fromisoformat(value)
            return True
        if _MONTH_INTERVAL.fullmatch(value):
            return 1 <= int(value[5:]) <= 12 and int(value[:4]) >= 1
        return _YEAR_INTERVAL.fullmatch(value) is not None and int(value) >= 1
    except ValueError:
        return False


def month_start(value: date) -> date:
    return value.replace(day=1)


def add_months(value: date, months: int) -> date:
    index = value.year * 12 + value.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def local_midnight(day: date, zone: tzinfo) -> datetime:
    return datetime.combine(day, time(0), tzinfo=zone)


def day_hours(day: date, zone: tzinfo) -> int:
    """The length of a local day in hours (23 and 25 at the changes)."""
    start = local_midnight(day, zone).astimezone(UTC)
    end = local_midnight(day + timedelta(days=1), zone).astimezone(UTC)
    return round((end - start).total_seconds() / HOUR)


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return None
    return number if math.isfinite(number) else None


def _matches_zone(moment: datetime, zone: tzinfo) -> bool:
    return moment.utcoffset() == moment.astimezone(zone).utcoffset()


def _changes_on(day: date, zone: tzinfo) -> bool:
    return day_hours(day, zone) != 24


def count_fits(
    quantity: Quantity, rate: Rate, c: float, day: date, zone: tzinfo
) -> bool:
    """Whether the count ``c`` of a slot has the form this reading knows.

    A sum of energy, starts or working time carries one value per hour
    (``c`` 1 for an hour, up to the hours of the day for a day). A gateway that
    answers minute values (``c`` 60 per hour, ``y`` the sum of the
    minutes) would be read 60 times too high, so such an answer is not
    taken; it is counted as ``count_form`` in the diagnostics. A day with
    fewer values than hours (the plant was off for some hours) is taken;
    only a count above the hours of the day is the minute form. A slot
    without data (``c`` 0), a mean (temperature), a volume and a month are
    not judged.
    """
    if c == 0 or quantity in (Quantity.TEMPERATURE, Quantity.VOLUME):
        return True
    if rate is Rate.HOUR:
        return c == 1
    if rate is Rate.DAY:
        return 0 < c <= max(24, day_hours(day, zone))
    return True


def slot_hours(rate: Rate, day: date, zone: tzinfo) -> int:
    """The hours of a slot: 1 for an hour, the hours of the day (23 to 25)
    and of every day of a month."""
    if rate is Rate.HOUR:
        return 1
    if rate is Rate.DAY:
        return day_hours(day, zone)
    days = calendar.monthrange(day.year, day.month)[1]
    return sum(day_hours(day + timedelta(days=index), zone) for index in range(days))


def mean_form(
    series: Series,
    rate: Rate,
    y: float,
    c: float,
    day: date,
    zone: tzinfo,
    *,
    start: datetime | None = None,
    now: datetime | None = None,
) -> tuple[float, float]:
    """A slot of a series kept as samples of the mean (``minute_sums``).

    ``y`` is the sum of ``c`` samples, whatever the sample rate (60 per
    hour, 4 per hour, one per hour): the energy of the slot is the mean
    ``y / c`` times the hours of the slot, for an hour, a day and a month
    alike. A slot still running at ``now`` counts the hours elapsed since
    its UTC ``start`` (an hour that cannot be placed counts one hour). The
    slot then counts as one value per hour (``c`` 1 for an hour, the hours
    of the day or of the month), so ``count_fits`` takes it. A slot without
    data (``c`` 0) stays a gap; every other series stays as it is.
    """
    if not series.minute_sums or c <= 0:
        return y, c
    full = slot_hours(rate, day, zone)
    hours = float(full)
    if now is not None and start is not None:
        elapsed = (now - start).total_seconds() / HOUR
        if 0 <= elapsed < full:
            hours = elapsed
    return y / c * hours, float(full)


def _hour_group(
    group: RecordingGroup,
    factor: float,
    zone: tzinfo,
    parsed: Parsed,
    quantity: Quantity,
) -> None:
    start, end = group.start, group.end
    count = len(group.samples)
    if start.minute or start.second or end.minute or end.second:
        parsed.issues["hour_group_not_aligned"] += 1
        return
    elapsed = round((end - start).total_seconds() / HOUR) + 1
    clock = (
        round((end.replace(tzinfo=None) - start.replace(tzinfo=None)).total_seconds())
        // HOUR
        + 1
    )
    same_offset = start.utcoffset() == end.utcoffset()
    zone_fits = _matches_zone(start, zone) and _matches_zone(end, zone)
    first = start.astimezone(UTC)
    naive = start.replace(tzinfo=None)
    placed: list[tuple[date, int | None]] = []
    if count == elapsed and (same_offset or zone_fits):
        # Every value is one elapsed hour; its day on the plant's clock.
        offset = start.utcoffset()
        for index in range(count):
            moment = first + timedelta(hours=index)
            local = moment + offset if same_offset and offset is not None else None
            day = local.date() if local is not None else moment.astimezone(zone).date()
            placed.append((day, int(moment.timestamp())))
    elif count == clock:
        # Counted on the clock (a day of a change has 24 values): the days
        # stay, the hours of the day of the change cannot be placed.
        for index in range(count):
            local = naive + timedelta(hours=index)
            day = local.date()
            if not zone_fits or _changes_on(day, zone):
                parsed.unplaced_days.add(day)
                placed.append((day, None))
            else:
                moment = local.replace(tzinfo=zone).astimezone(UTC)
                placed.append((day, int(moment.timestamp())))
        if parsed.unplaced_days:
            parsed.issues["hours_counted_on_clock"] += 1
    else:
        parsed.issues["hour_count_mismatch"] += 1
        return
    for (day, hour), sample in zip(placed, group.samples, strict=True):
        y, c = _number(sample.value), _number(sample.count)
        if y is None or c is None or c < 0:
            parsed.issues["invalid_value"] += 1
            continue
        if not count_fits(quantity, Rate.HOUR, c, day, zone):
            parsed.issues["count_form"] += 1
            continue
        parsed.samples.append(Sample(Rate.HOUR, day, hour, y * factor, c))
    # A day is complete in this answer when its first and last hour on the
    # plant's clock are part of the group.
    last = end.replace(tzinfo=None)
    day = naive.date()
    while day <= last.date():
        if naive <= datetime.combine(day, time(0)) and last >= datetime.combine(
            day, time(23)
        ):
            parsed.complete_days.add(day)
        day += timedelta(days=1)


def _calendar_group(
    group: RecordingGroup,
    factor: float,
    parsed: Parsed,
    quantity: Quantity,
    zone: tzinfo,
) -> None:
    rate = Rate.DAY if group.sample_rate == "P1D" else Rate.MONTH
    first, last = group.start.date(), group.end.date()
    if rate is Rate.DAY:
        expected = (last - first).days + 1
        days = [first + timedelta(days=index) for index in range(len(group.samples))]
    else:
        first, last = month_start(first), month_start(last)
        expected = (last.year - first.year) * 12 + last.month - first.month + 1
        days = [add_months(first, index) for index in range(len(group.samples))]
    if len(group.samples) != expected:
        parsed.issues[f"{rate.value}_count_mismatch"] += 1
        return
    for day, sample in zip(days, group.samples, strict=True):
        y, c = _number(sample.value), _number(sample.count)
        if y is None or c is None or c < 0:
            parsed.issues["invalid_value"] += 1
            continue
        if not count_fits(quantity, rate, c, day, zone):
            parsed.issues["count_form"] += 1
            continue
        parsed.samples.append(Sample(rate, day, None, y * factor, c))


def parse_local(recording: Recording, series: Series, zone: tzinfo) -> Parsed | None:
    """The samples of a decoded local answer; None for an unknown unit."""
    factor = series_unit_factor(series, recording.unit)
    if factor is None:
        return None
    parsed = Parsed()
    newest: datetime | None = None
    for group in recording.groups:
        if group.sample_rate == "PT1H":
            _hour_group(group, factor, zone, parsed, series.quantity)
        elif group.sample_rate in ("P1D", "P1M"):
            _calendar_group(group, factor, parsed, series.quantity, zone)
        else:
            parsed.issues["unknown_rate"] += 1
            continue
        if newest is None or group.end > newest:
            newest = group.end
    parsed.offset = newest.utcoffset() if newest is not None else None
    return parsed


def is_cloud_recording(payload: object) -> bool:
    """Whether a cloud answer has the shape of a recording at all; one that
    has not is a failed read, not a recording in an unknown unit."""
    return (
        isinstance(payload, dict)
        and payload.get("type") == "yRecording"
        and isinstance(payload.get("recording"), list)
    )


def cloud_unit(payload: object) -> str | None:
    """The unit a cloud answer names (``unitOfMeasure``), as it is."""
    unit = payload.get("unitOfMeasure") if isinstance(payload, dict) else None
    return unit if isinstance(unit, str) else None


def parse_cloud(
    payload: object,
    series: Series,
    interval: str,
    zone: tzinfo,
    now: datetime | None = None,
    *,
    icom: bool = False,
) -> Parsed | None:
    """The samples of a cloud answer to ``?interval=``; None when the answer
    is no recording or has an unknown unit. ``now``: when it was read (the
    hours of a slot still running, ``mean_form``); ``icom``: the answer of an
    iCom gateway (``series_unit_factor``)."""
    if not isinstance(payload, dict) or not is_cloud_recording(payload):
        return None
    rows = payload.get("recording")
    if not isinstance(rows, list) or not valid_interval(interval):
        return None
    unit = payload.get("unitOfMeasure")
    factor = series_unit_factor(
        series, unit if isinstance(unit, str) else None, icom=icom
    )
    if factor is None:
        return None
    values: list[tuple[float, float] | None] = []
    for row in rows:
        y = _number(row.get("y")) if isinstance(row, dict) else None
        c = _number(row.get("c")) if isinstance(row, dict) else None
        values.append(
            (y * factor, c) if y is not None and c is not None and c >= 0 else None
        )
    parsed = Parsed()
    if _DAY_INTERVAL.fullmatch(interval):
        day = date.fromisoformat(interval)
        hours = day_hours(day, zone)
        start = local_midnight(day, zone).astimezone(UTC)
        placeable = len(values) == hours
        if not placeable:
            parsed.issues["hour_count_mismatch"] += 1
            parsed.unplaced_days.add(day)
        for index, item in enumerate(values):
            if item is None:
                parsed.issues["invalid_value"] += 1
                continue
            begin = start + timedelta(hours=index) if placeable else None
            item = mean_form(series, Rate.HOUR, *item, day, zone, start=begin, now=now)
            if not count_fits(series.quantity, Rate.HOUR, item[1], day, zone):
                parsed.issues["count_form"] += 1
                continue
            hour = int(begin.timestamp()) if begin is not None else None
            parsed.samples.append(Sample(Rate.HOUR, day, hour, *item))
        parsed.complete_days.add(day)
        return parsed
    if _MONTH_INTERVAL.fullmatch(interval):
        first = date(int(interval[:4]), int(interval[5:]), 1)
        days = calendar.monthrange(first.year, first.month)[1]
        if len(values) != days:
            parsed.issues["day_count_mismatch"] += 1
            return parsed
        for index, item in enumerate(values):
            if item is None:
                parsed.issues["invalid_value"] += 1
                continue
            sample_day = first + timedelta(days=index)
            item = mean_form(
                series,
                Rate.DAY,
                *item,
                sample_day,
                zone,
                start=local_midnight(sample_day, zone).astimezone(UTC),
                now=now,
            )
            if not count_fits(series.quantity, Rate.DAY, item[1], sample_day, zone):
                parsed.issues["count_form"] += 1
                continue
            parsed.samples.append(Sample(Rate.DAY, sample_day, None, *item))
        return parsed
    first = date(int(interval), 1, 1)
    if len(values) != 12:
        parsed.issues["month_count_mismatch"] += 1
        return parsed
    for index, item in enumerate(values):
        if item is None:
            parsed.issues["invalid_value"] += 1
            continue
        month = add_months(first, index)
        item = mean_form(
            series,
            Rate.MONTH,
            *item,
            month,
            zone,
            start=local_midnight(month, zone).astimezone(UTC),
            now=now,
        )
        parsed.samples.append(Sample(Rate.MONTH, month, None, *item))
    return parsed


def sample_start(sample: Sample, zone: tzinfo) -> datetime:
    """The UTC start of a sample's slot."""
    if sample.hour is not None:
        return datetime.fromtimestamp(sample.hour, UTC)
    return local_midnight(sample.day, zone).astimezone(UTC)


def _hours_in(sample: Sample) -> float:
    if sample.rate is Rate.HOUR:
        return 1.0
    if sample.rate is Rate.DAY:
        return 25.0
    return 24.0 * 31 + 1


def rejection(
    sample: Sample,
    series: Series,
    *,
    now: datetime,
    zone: tzinfo,
    floor: date | None,
) -> str | None:
    """Why a sample with data (``c > 0``) cannot be a real value, or None."""
    if sample_start(sample, zone) > now + timedelta(hours=1):
        return "future"
    if sample.day < max(EARLIEST, floor or EARLIEST):
        return "before_history"
    hours = _hours_in(sample)
    if series.quantity is Quantity.TEMPERATURE:
        mean = sample.y / sample.c
        low, high = TEMPERATURE_RANGE
        return None if low <= mean <= high else "implausible"
    if sample.y < 0:
        return "negative"
    limit = {
        Quantity.ENERGY: MAX_KWH_PER_HOUR,
        Quantity.COUNT: MAX_STARTS_PER_HOUR,
        Quantity.DURATION: 1.01,
        Quantity.VOLUME: MAX_LITRES_PER_HOUR,
    }[series.quantity]
    return "implausible" if sample.y > limit * hours else None
