"""What is known of one recorded series, and what goes into the statistics.

Every slot (hour, day, month) is kept as ``[y, c, source, reads, final]``:
the sum and count of the plant, where it came from (``local`` or
``cloud``), how often the same values were read after the slot ended, and
whether the values are final. A slot is final when it ended at least
``COMPLETE_AFTER`` ago and two reads gave the same values, or with one read
when it ended more than ``SETTLED`` (48 hours) before; a slot still running
is kept as a live value that is never imported.

The local data port wins over the cloud: a cloud value never replaces a
local one, and a local value replaces a cloud one; different values are
counted. A final value that changes again becomes open until it is
confirmed; an imported row that no longer matches its final slot is
imported again from there on (``plan``).

The statistics are written hour by hour from the first recorded day.
Where an hour is not known and will not come any more, the rest of its day
is written as one row at that hour from the day's value, and where no day
is known either, the rest of the month from the month's value. Nothing is
interpolated; a negative rest (the plant rounds old months to whole kWh) is
left out. ``c = 0`` is a gap, never a zero: it is kept as ``[0, 0]`` (the
plant's placeholder sum, often 1.0, is dropped), and a slot already final
with data keeps its value when a later answer has no data for it (rule 12:
what the plant no longer has stays; counted as ``gap_after_value``).

A plan is worked out on a copy of the import position and taken over
(``commit``) only once its rows were handed to the recorder, so a failed
write is tried again in the next run instead of being marked as imported.
Where the recorder cannot clear a range of rows (Home Assistant before
2026.11), an earlier row that is not written again gets a neutral row with
the sum before it (``stale``); rows written again replace the old ones.
"""

from __future__ import annotations

import copy
import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any, Final

from .parse import (
    HOUR,
    Parsed,
    Rate,
    Sample,
    add_months,
    local_midnight,
    month_start,
    rejection,
    slot_hours,
)
from .series import Series

LOCAL: Final = "local"
CLOUD: Final = "cloud"
_RANK: Final = {CLOUD: 1, LOCAL: 2}
COMPLETE_AFTER: Final = 15 * 60
SETTLED: Final = 48 * HOUR
# Imported rows kept to compare and to import again (a late correction).
ROW_WINDOW: Final = 8 * 24 * HOUR
HOUR_KEEP: Final = 11 * 24 * HOUR
DAY_KEEP: Final = timedelta(days=800)
_EPSILON: Final = 0.0005
_MAX_STEPS: Final = 24 * 900
_NEVER: Final = float("inf")
# Discarded hours that count as gaps (the others are no hour of the history).
_GAP_REASONS: Final = frozenset({"negative", "implausible"})

type Cell = list[Any]


def _same(cell: Cell, y: float, c: float) -> bool:
    return math.isclose(cell[0], y, abs_tol=_EPSILON) and math.isclose(
        cell[1], c, abs_tol=_EPSILON
    )


def _ts(moment: datetime) -> int:
    return int(moment.timestamp())


@dataclass(frozen=True, slots=True)
class DailyValue:
    """One day of a series (internal interface, see ``RecordingsView``)."""

    day: date
    y: float
    c: float
    final: bool
    source: str

    @property
    def value(self) -> float | None:
        """The day's sum, or for a temperature its mean; None for a gap."""
        if self.c <= 0:
            return None
        return self.y / self.c if self.mean else self.y

    mean: bool = False


@dataclass(slots=True)
class Plan:
    """Rows to write: ``(utc start, y, c, sum after)``; a range to clear
    first when rows are imported again; the import position and the counts
    to take over once the rows are written (``Ledger.commit``)."""

    rows: list[tuple[int, float, float, float]] = field(default_factory=list)
    clear: tuple[int, int] | None = None
    state: dict[str, Any] | None = None
    stats: Counter[str] = field(default_factory=Counter)


class Ledger:
    """The known slots and the import position of one series."""

    def __init__(self, series: Series, data: dict[str, Any] | None = None) -> None:
        self.series = series
        data = data if isinstance(data, dict) else {}
        self.hours: dict[str, Cell] = _cells(data.get("hours"))
        self.days: dict[str, Cell] = _cells(data.get("days"))
        self.months: dict[str, Cell] = _cells(data.get("months"))
        floor = data.get("floor")
        self.floor: str | None = floor if isinstance(floor, str) else None
        self.unplaced: set[str] = {
            item for item in data.get("unplaced", ()) if isinstance(item, str)
        }
        stats = data.get("stats")
        self.stats: Counter[str] = Counter(
            {
                key: value
                for key, value in (stats.items() if isinstance(stats, dict) else ())
                if isinstance(key, str) and type(value) is int
            }
        )
        imp = data.get("import")
        self.imp: dict[str, Any] | None = imp if _valid_import(imp) else None

    # --------------------------------------------------------------- storage
    def to_dict(self) -> dict[str, Any]:
        return {
            "hours": self.hours,
            "days": self.days,
            "months": self.months,
            "floor": self.floor,
            "unplaced": sorted(self.unplaced),
            "stats": dict(self.stats),
            "import": self.imp,
        }

    def prune(self, now: datetime) -> None:
        hour_limit = _ts(now) - HOUR_KEEP
        for key in [key for key in self.hours if int(key) < hour_limit]:
            del self.hours[key]
        day_limit = (now - DAY_KEEP).date().isoformat()
        for key in [key for key in self.days if key < day_limit]:
            del self.days[key]
        self.unplaced = {key for key in self.unplaced if key >= day_limit}
        if self.imp is not None:
            rows = self.imp["rows"]
            limit = _ts(now) - ROW_WINDOW
            while rows and rows[0][0] < limit:
                row = rows.pop(0)
                self.imp["base"] = [row[3], row[4], *row[5:9]]

    def correct_mean_months(self, now: datetime, zone: tzinfo) -> int:
        """Months of a series kept as samples of the mean (``minute_sums``)
        as ``parse.mean_form`` reads them; returns how many changed.

        Earlier versions kept such a month with the plant's sum and count as
        they were (up to about 60 times the energy). A month that ended
        becomes the mean times its hours, with its other fields; one still
        running is dropped and read again. A month already in this form (its
        count is its hours) stays as it is. An imported row made from a
        changed month is imported again (``redo``, within the rows kept).
        """
        if not self.series.minute_sums:
            return 0
        changed: list[str] = []
        for key, cell in list(self.months.items()):
            first = date.fromisoformat(key)
            hours = slot_hours(Rate.MONTH, first, zone)
            if cell[1] <= 0 or math.isclose(cell[1], hours, abs_tol=_EPSILON):
                continue
            if _ts(local_midnight(add_months(first, 1), zone)) > _ts(now):
                del self.months[key]
            else:
                cell[0] = float(cell[0]) / float(cell[1]) * hours
                cell[1] = float(hours)
            changed.append(key[:7])
        if changed:
            self.stats["months_corrected"] += len(changed)
            rows = self.imp["rows"] if self.imp is not None else []
            redo = [
                int(row[0]) for row in rows if row[9] == "m" and row[4][:7] in changed
            ]
            if self.imp is not None and redo:
                self.imp["redo"] = min(redo)
        return len(changed)

    def settle(self, now: datetime, zone: tzinfo) -> None:
        """A slot read after its end and not changed for ``SETTLED`` is
        final: corrections come within 48 hours, so one read is enough."""
        limit = now.timestamp() - SETTLED
        for key, cell in self.hours.items():
            if not cell[4] and cell[3] >= 1 and int(key) + HOUR <= limit:
                cell[4] = True
        for table, step in ((self.days, False), (self.months, True)):
            for key, cell in table.items():
                if cell[4] or cell[3] < 1:
                    continue
                first = date.fromisoformat(key)
                after = add_months(first, 1) if step else first + timedelta(days=1)
                if _ts(local_midnight(after, zone)) <= limit:
                    cell[4] = True

    # ----------------------------------------------------------------- merge
    def merge(
        self,
        parsed: Parsed,
        source: str,
        *,
        read_at: datetime,
        zone: tzinfo,
        full_history: bool = False,
    ) -> None:
        """Take the samples of one answer read at ``read_at``."""
        floor = date.fromisoformat(self.floor) if self.floor else None
        accepted: list[Sample] = []
        for sample in parsed.samples:
            if sample.c <= 0:
                if _start(sample, zone) > read_at:
                    continue  # a placeholder of a slot still to come
                # A gap: the placeholder sum (often 1.0) is no energy.
                accepted.append(Sample(sample.rate, sample.day, sample.hour, 0.0, 0.0))
                continue
            reason = rejection(sample, self.series, now=read_at, zone=zone, floor=floor)
            if reason is not None:
                self.stats[f"discarded_{reason}"] += 1
                if sample.rate is Rate.HOUR and reason in _GAP_REASONS:
                    # A discarded hour is a gap: the other hours of its day
                    # are imported and the day is summed up without it (an
                    # hour known before keeps its value).
                    accepted.append(self._discarded_hour(sample))
                continue
            accepted.append(sample)
        for key, count in parsed.issues.items():
            self.stats[f"answer_{key}"] += count
        self.unplaced.update(day.isoformat() for day in parsed.unplaced_days)
        read = read_at.timestamp()
        day_samples = {s.day for s in accepted if s.rate is Rate.DAY}
        aggregates: dict[date, list[float]] = {}
        for sample in accepted:
            if sample.rate is Rate.HOUR:
                y, c = sample.y, sample.c
                if sample.hour is not None:
                    self._put(
                        self.hours,
                        str(sample.hour),
                        sample,
                        source,
                        sample.hour + HOUR,
                        read,
                    )
                    cell = self.hours.get(str(sample.hour))
                    if c <= 0 and cell is not None and cell[4] and cell[1] > 0:
                        y, c = cell[0], cell[1]  # kept (rule 12): its day too
                if sample.day not in day_samples:
                    total = aggregates.setdefault(sample.day, [0.0, 0.0])
                    total[0] += y if c > 0 else 0.0
                    total[1] += c
            elif sample.rate is Rate.DAY:
                end = _ts(local_midnight(sample.day + timedelta(days=1), zone))
                self._put(self.days, sample.day.isoformat(), sample, source, end, read)
            else:
                end = _ts(local_midnight(add_months(sample.day, 1), zone))
                self._put(
                    self.months, sample.day.isoformat(), sample, source, end, read
                )
        for day, (y, c) in aggregates.items():
            ended: float = (
                _ts(local_midnight(day + timedelta(days=1), zone))
                if day in parsed.complete_days
                else _NEVER
            )
            self._put_values(self.days, day.isoformat(), y, c, source, ended, read)
        if full_history:
            first = min(
                (s.day for s in accepted if s.c > 0),
                default=None,
            )
            if first is not None:
                if self.floor is None:
                    self.floor = first.isoformat()
                elif first.isoformat() > self.floor:
                    # The plant no longer has its oldest values (a reset or a
                    # new controller): what was imported stays.
                    self.stats["history_shrunk"] += 1

    def _discarded_hour(self, sample: Sample) -> Sample:
        cell = self.hours.get(str(sample.hour)) if sample.hour is not None else None
        if cell is not None and cell[4] and cell[1] > 0:
            return Sample(sample.rate, sample.day, sample.hour, cell[0], cell[1])
        return Sample(sample.rate, sample.day, sample.hour, 0.0, 0.0)

    def _put(
        self,
        table: dict[str, Cell],
        key: str,
        sample: Sample,
        source: str,
        ended: float,
        read: float,
    ) -> None:
        self._put_values(table, key, sample.y, sample.c, source, ended, read)

    def _put_values(
        self,
        table: dict[str, Cell],
        key: str,
        y: float,
        c: float,
        source: str,
        ended: float,
        read: float,
    ) -> None:
        cell = table.get(key)
        if c <= 0 and cell is not None and cell[4] and cell[1] > 0:
            # Rule 12: a confirmed value stays when the plant later has no
            # data for it (a reset, a new controller): no correction.
            self.stats["gap_after_value"] += 1
            return
        if read < ended + COMPLETE_AFTER:
            # Still running: a live value, replaced by every newer read.
            if cell is not None and (cell[4] or _RANK.get(cell[2], 0) > _RANK[source]):
                return
            table[key] = [y, c, source, 0, False]
            return
        settled = read >= ended + SETTLED
        if cell is None:
            table[key] = [y, c, source, 1, settled]
            return
        same = _same(cell, y, c)
        if cell[2] != source:
            if not same:
                self.stats["deviations_local_cloud"] += 1
            if _RANK[source] < _RANK.get(cell[2], 0):
                return  # the local value stays
            table[key] = [y, c, source, 1, settled or (same and bool(cell[4]))]
            return
        if same:
            reads = int(cell[3]) + 1
            table[key] = [y, c, source, reads, bool(cell[4]) or reads >= 2 or settled]
            return
        if cell[4]:
            self.stats["late_corrections"] += 1
        table[key] = [y, c, source, 1, settled]

    # ---------------------------------------------------------------- access
    def first_day(self) -> date | None:
        """The first day with data (a month counts from its first day).

        Hours always come with their day (``merge``), so days and months
        are enough.
        """
        return min(
            (
                date.fromisoformat(key)
                for table in (self.days, self.months)
                for key, cell in table.items()
                if cell[1] > 0
            ),
            default=None,
        )

    def first_start(self, zone: tzinfo) -> int | None:
        """The UTC start of the first slot with data."""
        found = [int(key) for key, cell in self.hours.items() if cell[1] > 0]
        first = self.first_day()
        if first is not None:
            found.append(_ts(local_midnight(first, zone)))
        return min(found, default=None)

    def day(self, day: date) -> Cell | None:
        return self.days.get(day.isoformat())

    def month(self, first: date) -> Cell | None:
        return self.months.get(first.isoformat())

    def daily(self, start: date, end: date) -> tuple[DailyValue, ...]:
        """The known days from ``start`` to ``end`` (both included)."""
        result = []
        for key in sorted(self.days):
            day = date.fromisoformat(key)
            if start <= day <= end:
                cell = self.days[key]
                result.append(
                    DailyValue(
                        day,
                        float(cell[0]),
                        float(cell[1]),
                        bool(cell[4]),
                        str(cell[2]),
                        self.series.mean,
                    )
                )
        return tuple(result)

    # ---------------------------------------------------------------- import
    @property
    def importing(self) -> bool:
        return self.imp is not None

    def start_import(
        self, base: float, zone: tzinfo, stale: Iterable[int] = ()
    ) -> bool:
        """Begin at the first recorded day, continuing the sum ``base``;
        ``stale`` are the starts of earlier rows from there on that the
        recorder keeps (written again or neutral)."""
        first = self.first_start(zone)
        if first is None:
            return False
        self.imp = {
            "next": first,
            "sum": base,
            "day": None,
            "parts": [0.0, 0.0, 0.0, 0.0],
            "rows": [],
            "base": [base, None, 0.0, 0.0, 0.0, 0.0],
            "stale": sorted({int(start) for start in stale}),
        }
        return True

    def stop_import(self) -> None:
        self.imp = None

    def import_start(self, zone: tzinfo) -> datetime | None:
        """Where an import would begin (for the sum before it)."""
        first = self.first_start(zone)
        return datetime.fromtimestamp(first, UTC) if first is not None else None

    def plan(
        self,
        now: datetime,
        zone: tzinfo,
        *,
        can_clear: bool = True,
        commit: bool = True,
    ) -> Plan:
        """The rows that are complete now, and a range to clear first.

        Worked out on a copy of the import position; ``commit=False`` leaves
        the ledger as it was until ``commit``. Without ``can_clear`` the
        rows that are imported again are remembered as stale.
        """
        plan = Plan()
        if self.imp is None:
            return plan
        imp = copy.deepcopy(self.imp)
        imp.setdefault("stale", [])
        plan.state = imp
        self.settle(now, zone)
        self._rewind(imp, plan, can_clear)
        cursor = int(imp["next"])
        limit = _ts(now) - COMPLETE_AFTER
        latest = self._latest_start(zone)
        now_ts = _ts(now)
        steps = 0
        while cursor + HOUR <= limit and steps < _MAX_STEPS:
            steps += 1
            day = datetime.fromtimestamp(cursor, zone).date()
            day_end = _ts(local_midnight(day + timedelta(days=1), zone))
            cell = self.hours.get(str(cursor))
            if cell is not None and cell[4]:
                if cell[1] > 0:
                    self._emit(imp, plan, cursor, cell[0], cell[1], day, "h")
                cursor += HOUR
                continue
            if cell is not None and now_ts < cursor + HOUR + SETTLED:
                break  # not confirmed yet; an hour never confirmed is missing
            key = day.isoformat()
            day_cell = self.days.get(key)
            recent = now_ts < day_end + SETTLED
            if (
                day_cell is not None
                and day_cell[4]
                and (
                    key in self.unplaced or not recent or not self._has_hours(day, zone)
                )
            ):
                part = imp["parts"][:2] if imp["day"] == key else [0.0, 0.0]
                self._rest(
                    imp,
                    plan,
                    cursor,
                    day_cell[0] - part[0],
                    day_cell[1] - part[1],
                    day,
                    "d",
                )
                cursor = day_end
                continue
            if recent:
                break  # its hours or its day may still come
            first = month_start(day)
            month_cell = self.months.get(first.isoformat())
            month_end = add_months(first, 1)
            if (
                month_cell is not None
                and month_cell[4]
                and month_cell[1] > 0
                and limit >= _ts(local_midnight(month_end, zone))
            ):
                run_end = day + timedelta(days=1)
                while run_end < month_end and not self._has_data(run_end, zone):
                    run_end += timedelta(days=1)
                later_y = later_c = 0.0
                probe = run_end
                while probe < month_end:
                    y, c = self._day_total(probe, zone)
                    later_y, later_c = later_y + y, later_c + c
                    probe += timedelta(days=1)
                same_month = imp["day"] is not None and imp["day"][:7] == key[:7]
                part = imp["parts"][2:] if same_month else [0.0, 0.0]
                self._rest(
                    imp,
                    plan,
                    cursor,
                    month_cell[0] - part[0] - later_y,
                    month_cell[1] - part[1] - later_c,
                    day,
                    "m",
                )
                cursor = _ts(local_midnight(run_end, zone))
                continue
            if latest is not None and latest > cursor:
                plan.stats["import_gap_days"] += 1
                cursor = day_end
                continue
            break
        self._neutralize(imp, plan, cursor)
        imp["next"] = cursor
        if commit:
            self.commit(plan)
        return plan

    def commit(self, plan: Plan) -> None:
        """Take over a plan's import position once its rows are written."""
        if plan.state is not None and self.imp is not None:
            self.imp = plan.state
            self.stats.update(plan.stats)
            plan.state = None
            plan.stats = Counter()

    def _has_hours(self, day: date, zone: tzinfo) -> bool:
        start = _ts(local_midnight(day, zone))
        end = _ts(local_midnight(day + timedelta(days=1), zone))
        return any(start <= int(key) < end for key in self.hours)

    def _has_data(self, day: date, zone: tzinfo) -> bool:
        cell = self.days.get(day.isoformat())
        if cell is not None and cell[4]:
            return True
        start = _ts(local_midnight(day, zone))
        end = _ts(local_midnight(day + timedelta(days=1), zone))
        return any(
            start <= int(key) < end and cell[4] for key, cell in self.hours.items()
        )

    def _day_total(self, day: date, zone: tzinfo) -> tuple[float, float]:
        cell = self.days.get(day.isoformat())
        if cell is not None and cell[4]:
            return float(cell[0]), float(cell[1])
        start = _ts(local_midnight(day, zone))
        end = _ts(local_midnight(day + timedelta(days=1), zone))
        y = c = 0.0
        for key, hour in self.hours.items():
            if start <= int(key) < end and hour[4] and hour[1] > 0:
                y, c = y + hour[0], c + hour[1]
        return y, c

    def _latest_start(self, zone: tzinfo) -> int | None:
        found = [int(key) for key, cell in self.hours.items() if cell[4]]
        found.extend(
            _ts(local_midnight(date.fromisoformat(key), zone))
            for table in (self.days, self.months)
            for key, cell in table.items()
            if cell[4]
        )
        return max(found, default=None)

    def _rest(
        self,
        imp: dict[str, Any],
        plan: Plan,
        start: int,
        y: float,
        c: float,
        day: date,
        kind: str,
    ) -> None:
        """The rest of a day or month as one row at ``start``."""
        if self.series.mean:
            if c > _EPSILON:
                self._emit(imp, plan, start, y, c, day, kind)
            return
        if y < -_EPSILON:
            plan.stats["negative_rest"] += 1
            return
        if y > _EPSILON:
            self._emit(imp, plan, start, y, max(c, 0.0), day, kind)

    def _emit(
        self,
        imp: dict[str, Any],
        plan: Plan,
        start: int,
        y: float,
        c: float,
        day: date,
        kind: str,
    ) -> None:
        self._neutralize(imp, plan, start)
        key = day.isoformat()
        parts = imp["parts"]
        if imp["day"] != key:
            if imp["day"] is None or imp["day"][:7] != key[:7]:
                parts[2] = parts[3] = 0.0
            parts[0] = parts[1] = 0.0
            imp["day"] = key
        parts[0] += y
        parts[1] += c
        parts[2] += y
        parts[3] += c
        if not self.series.mean:
            imp["sum"] = float(imp["sum"]) + y
        total = float(imp["sum"])
        imp["rows"].append([start, y, c, total, key, *parts, kind])
        plan.stats[f"imported_{kind}"] += 1
        plan.rows.append((start, y, c, total))

    def _neutralize(self, imp: dict[str, Any], plan: Plan, before: int) -> None:
        """Earlier rows before ``before`` that were not written again get the
        sum before them (no energy in that hour); one at ``before`` is
        replaced by the row written there."""
        stale = imp.get("stale") or []
        if not stale or stale[0] > before:
            return
        keep = []
        for start in stale:
            if start > before:
                keep.append(start)
            elif start < before and not self.series.mean:
                # A mean cannot be neutral: the old mean stays until the hour
                # is written again (a temperature turned into a gap).
                total = float(imp["sum"])
                imp["rows"].append(
                    [start, 0.0, 0.0, total, imp["day"], *imp["parts"], "n"]
                )
                plan.stats["imported_neutral"] += 1
                plan.rows.append((start, 0.0, 0.0, total))
        imp["stale"] = keep

    def _rewind(self, imp: dict[str, Any], plan: Plan, can_clear: bool) -> None:
        """Import again from the first row whose final slot changed, or from
        the row ``redo`` names (a corrected month, ``correct_mean_months``)."""
        rows = imp["rows"]
        redo = imp.pop("redo", None)
        for index, row in enumerate(rows):
            start, y, c, key, kind = row[0], row[1], row[2], row[4], row[9]
            again = type(redo) is int and start >= redo
            if kind == "h":
                cell = self.hours.get(str(start))
                expected = (cell[0], cell[1]) if cell and cell[4] else None
                if expected is not None and cell is not None and cell[1] <= 0:
                    expected = (0.0, 0.0)
            elif kind == "d":
                cell = self.days.get(key)
                expected = (
                    (cell[0] - (row[5] - y), cell[1] - (row[6] - c))
                    if cell and cell[4]
                    else None
                )
            else:
                expected = None
            if not again and (
                expected is None
                or (
                    math.isclose(expected[0], y, abs_tol=_EPSILON)
                    and (
                        self.series.mean is False
                        or math.isclose(expected[1], c, abs_tol=_EPSILON)
                    )
                )
            ):
                continue
            removed = rows[index:]
            del rows[index:]
            imp["next"] = removed[0][0]
            if rows:
                previous = rows[-1]
                imp["sum"], imp["day"] = previous[3], previous[4]
                imp["parts"] = list(previous[5:9])
            else:
                base = imp["base"]
                imp["sum"], imp["day"] = base[0], base[1]
                imp["parts"] = list(base[2:6])
            plan.clear = (removed[0][0], removed[-1][0] + HOUR)
            if not can_clear:
                imp["stale"] = sorted(
                    {*imp.get("stale", ()), *(int(row[0]) for row in removed)}
                )
            plan.stats["imported_again"] += 1
            return


def _start(sample: Sample, zone: tzinfo) -> datetime:
    if sample.hour is not None:
        return datetime.fromtimestamp(sample.hour, UTC)
    return local_midnight(sample.day, zone).astimezone(UTC)


def _cells(value: object) -> dict[str, Cell]:
    if not isinstance(value, dict):
        return {}
    cells: dict[str, Cell] = {}
    for key, cell in value.items():
        if (
            isinstance(key, str)
            and isinstance(cell, list)
            and len(cell) == 5
            and all(isinstance(item, int | float) for item in cell[:2])
            and cell[2] in _RANK
        ):
            cells[key] = [
                # A gap kept by an earlier version with the plant's
                # placeholder sum: no energy.
                float(cell[0]) if cell[1] > 0 else 0.0,
                float(cell[1]),
                cell[2],
                int(cell[3]),
                bool(cell[4]),
            ]
    return cells


def _valid_import(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("next"), int)
        and isinstance(value.get("sum"), int | float)
        and isinstance(value.get("rows"), list)
        and isinstance(value.get("parts"), list)
        and len(value["parts"]) == 4
        and isinstance(value.get("base"), list)
        and len(value["base"]) == 6
        and all(isinstance(row, list) and len(row) == 10 for row in value["rows"])
        and isinstance(value.get("stale", []), list)
        and all(type(start) is int for start in value.get("stale", ()))
    )
