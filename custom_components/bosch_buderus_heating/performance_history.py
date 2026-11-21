"""Heat and electricity per day and mode, for the performance factors.

A performance factor is the heat a heat pump produced in a period divided
by the electricity (compressor plus heater) it used. The periods: today,
yesterday, this and the previous month, this and the previous year, and
since commissioning; each for heating, hot water and in total.

Where the values come from:

* Since commissioning: the device's own counters, as they are now.
* The other periods stay unknown after a start until the history (the
  recordings and the counters' statistics) was read once, so a month or a
  year never shows the value of today alone.
* Today: the counters now minus the counters at midnight. The midnight
  reading is the last reading before midnight while Home Assistant runs;
  after a start during the day it is the counters' long-term statistic at
  midnight. Without either, today stays unknown until the next midnight.
* Earlier days: the plant's own recordings (``recordings``) for the
  electricity and the total heat, the counters' long-term statistics for the
  heat per mode (the recordings count hours in which the plant switches
  between the modes for both). Each falls back to the other source.
  Months without all of their days take the plant's month value.

A day without counter statistics (Home Assistant off) puts its energy into
the change of the next day; where the recordings fill such a day, the
counters of the days next to it do not count (no energy twice).

The counters' change includes the dips while the heat pump defrosts (the
heat taken back from the water), so the defrost is netted. Days with no
value from any source count as ``missing_days``; days before the first
known day are no part of the history.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .burner_share import burner_share
from .const import DOMAIN
from .counter_drop import DropHold, large_drop
from .derived_store import derived_store
from .log_helpers import log_failure
from .performance import (
    COLLECTIONS,
    HP_MODES,
    HP_PREFIX,
    MODES,
    CounterReading,
    heat_pump_root,
    lifetime,
    ratio,
)

if TYPE_CHECKING:
    from .coordinator import BoschBuderusDataUpdateCoordinator
    from .recordings import RecordingsView

_LOGGER = logging.getLogger(__name__)

HEAT: Final = "heat"
ELECTRICITY: Final = "electricity"
QUANTITIES: Final = (HEAT, ELECTRICITY)
COUNTERS: Final = "counters"
LOCAL_RECORDING: Final = "local_recording"
CLOUD_RECORDING: Final = "cloud_recording"
MIXED_RECORDING: Final = "local_and_cloud_recording"
# The recordings' measure keys per mode.
RECORDING_KEYS: Final = {
    (mode, quantity): (f"{mode}_heat" if quantity == HEAT else f"{mode}_electricity")
    for mode in MODES
    for quantity in QUANTITIES
}
# The integration's own counter entities (unique ID after ``<gateway>:``),
# the first registered one counts.
_COUNTER_PATH: Final = {
    "total": "totalConsumption",
    "ch": "chConsumption",
    "dhw": "dhwConsumption",
}
_LEGACY: Final = {
    ("total", HEAT): "gateway:total_heat_produced",
    ("ch", HEAT): "gateway:heating_heat_produced",
    ("dhw", HEAT): "gateway:hot_water_heat_produced",
    ("total", ELECTRICITY): "gateway:total_electricity",
    ("ch", ELECTRICITY): "gateway:heating_electricity",
    ("dhw", ELECTRICITY): "gateway:hot_water_electricity",
}
START_DELAY: Final = 120.0
REFRESH_MINUTE: Final = 25
# The last reading before midnight is the next day's start when it is at
# most this old at the first reading after midnight.
ROLL_GAP: Final = timedelta(minutes=30)
# Unchanged readings are saved at most this often (their time only).
SAVE_UNCHANGED: Final = timedelta(minutes=1)
# Days whose counter statistics are read again every hour.
RECENT_DAYS: Final = 3
_ENERGY_UNITS: Final = {"energy": "kWh"}
_ENGINES: HassKey[dict[tuple[str, str], PerformanceHistory]] = HassKey(
    f"{DOMAIN}_performance"
)


class Period(StrEnum):
    TODAY = "today"
    YESTERDAY = "yesterday"
    CURRENT_MONTH = "current_month"
    PREVIOUS_MONTH = "previous_month"
    CURRENT_YEAR = "current_year"
    PREVIOUS_YEAR = "previous_year"
    SINCE_COMMISSIONING = "since_commissioning"


@dataclass(frozen=True, slots=True)
class Amount:
    kwh: float
    source: str


@dataclass(frozen=True, slots=True)
class PeriodResult:
    """Heat and electricity of a period (``end`` excluded) and where they
    came from. ``start``/``end`` are ``None`` since commissioning."""

    start: date | None
    end: date | None
    heat: float
    electricity: float
    heat_sources: frozenset[str]
    electricity_sources: frozenset[str]
    days: int
    missing_days: int
    first_day: date | None = None

    @property
    def ratio(self) -> float | None:
        return ratio(self.heat, self.electricity)


def source_text(sources: Iterable[str]) -> str:
    return "+".join(sorted(set(sources)))


def _recording_source(source: str) -> str:
    if source == "local":
        return LOCAL_RECORDING
    if source == "cloud":
        return CLOUD_RECORDING
    return MIXED_RECORDING


def month_start(day: date) -> date:
    return day.replace(day=1)


def add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def bounds(period: Period, today: date) -> tuple[date, date]:
    """First day and the day after the last day of a period."""
    if period is Period.TODAY:
        return today, today + timedelta(days=1)
    if period is Period.YESTERDAY:
        return today - timedelta(days=1), today
    if period is Period.CURRENT_MONTH:
        return month_start(today), add_months(today, 1)
    if period is Period.PREVIOUS_MONTH:
        return add_months(today, -1), month_start(today)
    if period is Period.CURRENT_YEAR:
        return date(today.year, 1, 1), date(today.year + 1, 1, 1)
    return date(today.year - 1, 1, 1), date(today.year, 1, 1)


def history_start(today: date) -> date:
    """The first day any period may need: 1 January of the previous year."""
    return date(today.year - 1, 1, 1)


def local_midnight(day: date, zone: tzinfo) -> datetime:
    return datetime.combine(day, time(0), tzinfo=zone)


class PerformanceHistory:
    """The days, months and today's start of one gateway's counters."""

    def __init__(
        self, hass: HomeAssistant, coordinator: BoschBuderusDataUpdateCoordinator
    ) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.gateway_id = coordinator.gateway.gateway_id
        self._store = derived_store(hass)
        self._recorded: dict[tuple[str, str], dict[date, Amount]] = {}
        self._recorded_months: dict[tuple[str, str], dict[date, Amount]] = {}
        self._counted: dict[tuple[str, str], dict[date, float]] = {}
        # First and last counted day per counter (``_counted_amount``).
        self._counted_span: dict[tuple[str, str], tuple[date, date]] = {}
        self.counter_entities: dict[tuple[str, str], str] = {}
        self._live: dict[str, CounterReading] = {}
        self._last_at: datetime | None = None
        self._saved_at: datetime | None = None
        # A single collection reading far below the last one (an outlier)
        # waits for a later reading before it counts.
        self._drops = DropHold()
        self._baseline_day: date | None = None
        self._baseline: dict[str, tuple[float, float]] = {}
        self._baseline_method: str | None = None
        # The first day of a counter's statistics: incomplete, never used.
        self._partial_first: dict[tuple[str, str], date] = {}
        # How the last search for the counters at midnight ended.
        self._lookup: str | None = None
        self._listeners: list[Callable[[], None]] = []
        self._timers: list[CALLBACK_TYPE] = []
        self._view: RecordingsView | None = None
        self._unsubscribe: list[Callable[[], None]] = []
        self._full_done = False
        self._first: dict[str, date | None] = {}
        self.stopped = False
        self.loaded = False
        # Whether the recordings and the counters' statistics were read once
        # since the start; until then only today's counters are known.
        self.history_read = False
        self.report: dict[str, Any] = {}

    @property
    def _key(self) -> str:
        return f"{self.gateway_id}:performance:today"

    # ------------------------------------------------------------ lifecycle
    async def async_start(self, *, schedule: bool = True) -> None:
        await self._store.async_load()
        if not self.loaded:
            self._restore(self._store.get(self._key))
            self.loaded = True
        if schedule and not self.stopped:
            self._timers.append(
                async_call_later(self.hass, START_DELAY, self._due_start)
            )
            self._timers.append(
                async_track_time_change(
                    self.hass, self._due_hourly, minute=REFRESH_MINUTE, second=0
                )
            )

    @callback
    def _due_start(self, _now: datetime) -> None:
        self._background(full=True)

    @callback
    def _due_hourly(self, _now: datetime) -> None:
        self._background(full=not self._full_done)

    def _background(self, *, full: bool) -> None:
        if self.stopped:
            return
        self.hass.async_create_background_task(
            self.async_refresh(full=full), "bosch_buderus_heating_performance"
        )

    @callback
    def stop(self) -> None:
        self.stopped = True
        for cancel in self._timers:
            cancel()
        self._timers.clear()
        for release in self._unsubscribe:
            release()
        self._unsubscribe.clear()
        self._view = None

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    # ---------------------------------------------------------- persistence
    def _restore(self, row: dict[str, Any] | None) -> None:
        if row is None:
            return
        try:
            day = date.fromisoformat(str(row.get("day")))
            self._baseline = {
                mode: (float(pair[0]), float(pair[1]))
                for mode, pair in dict(row.get("baseline") or {}).items()
                if mode in MODES or mode in HP_MODES
            }
            self._baseline_day = day
            method = row.get("method")
            self._baseline_method = method if isinstance(method, str) else None
            last_at = row.get("last_at")
            if isinstance(last_at, int | float):
                self._last_at = datetime.fromtimestamp(float(last_at), UTC)
            for mode, pair in dict(row.get("last") or {}).items():
                if mode in MODES or mode in HP_MODES:
                    self._live[mode] = CounterReading(
                        float(pair[0]), float(pair[1]), "restored"
                    )
        except TypeError, ValueError, IndexError:
            self._baseline, self._baseline_day, self._live = {}, None, {}
            self._last_at = None

    def _save(self) -> None:
        if self._baseline_day is None:
            return
        self._store.set(
            self._key,
            {
                "day": self._baseline_day.isoformat(),
                "baseline": {
                    mode: [round(heat, 4), round(power, 4)]
                    for mode, (heat, power) in self._baseline.items()
                },
                "method": self._baseline_method,
                "last_at": self._last_at.timestamp() if self._last_at else None,
                "last": {
                    mode: [round(item.heat, 4), round(item.electricity, 4)]
                    for mode, item in self._live.items()
                },
            },
        )

    # ------------------------------------------------------------ today
    def _zone(self) -> tzinfo:
        return dt_util.get_default_time_zone()

    def today(self, now: datetime | None = None) -> date:
        return (now or dt_util.utcnow()).astimezone(self._zone()).date()

    @callback
    def sync(self, now: datetime | None = None) -> None:
        """Take the counters' current readings; at the first reading of a
        day the start of the day is set."""
        if not self.loaded:
            return
        now = now or dt_util.utcnow()
        readings = {
            mode: reading
            for mode in (*MODES, *self._heat_pump_modes())
            if (reading := lifetime(self.coordinator, mode)) is not None
            and not self._outlier(mode, reading)
        }
        if not readings:
            return
        today = self.today(now)
        if self._baseline_day != today:
            self._roll(today, now)
        elif readings == self._live:
            # Unchanged (the heat pump stands still): still a reading at this
            # time, so the next midnight starts the day from it; saved at
            # most once a minute.
            self._last_at = now
            if self._saved_at is None or now - self._saved_at >= SAVE_UNCHANGED:
                self._saved_at = now
                self._save()
            return
        self._live.update(readings)
        self._last_at = self._saved_at = now
        self._save()

    def _heat_pump_modes(self) -> tuple[str, ...]:
        """The modes of the heat pump's own counters, while the plant shows a
        burner share: the gateway's collections then count the boiler, and the
        heat pump's counters are the basis of the factors (see
        ``burner_share``). Without a burner share there are none."""
        if any(burner_share(self.coordinator, COLLECTIONS[mode]) for mode in MODES):
            return HP_MODES
        return ()

    def _outlier(self, mode: str, reading: CounterReading) -> bool:
        """A single reading far below the last one of the mode, held until a
        later reading confirms it (like the shown counters)."""
        live = self._live.get(mode)
        dropped = live is not None and (
            large_drop(reading.heat, live.heat)
            or large_drop(reading.electricity, live.electricity)
        )
        return self._drops.held(mode, dropped, reading.observed_at)

    def live_origin(self, mode: str) -> str | None:
        """Where the mode's current counter reading came from."""
        live = self._live.get(mode)
        return live.origin if live is not None and live.origin != "restored" else None

    def _roll(self, today: date, now: datetime) -> None:
        previous = self._last_at
        self._baseline_day = today
        if (
            previous is not None
            and self._live
            and self.today(previous) == today - timedelta(days=1)
            and now - previous <= ROLL_GAP
        ):
            # The last readings before midnight start the new day.
            self._baseline = {
                mode: (item.heat, item.electricity) for mode, item in self._live.items()
            }
            self._baseline_method = "midnight"
            return
        self._baseline = {}
        self._baseline_method = None
        if not self.stopped:
            self.hass.async_create_background_task(
                self._async_statistics_baseline(today),
                "bosch_buderus_heating_performance_baseline",
            )

    def _today(self, mode: str, today: date) -> tuple[float, float] | None:
        base = self._baseline.get(mode)
        live = self._live.get(mode)
        if self._baseline_day != today or base is None or live is None:
            return None
        return live.heat - base[0], live.electricity - base[1]

    async def _async_statistics_baseline(self, today: date) -> None:
        """The counters' long-term statistic in the hour before midnight."""
        if not self._recorder_ready():
            self._lookup = "recorder_unavailable"
            return
        midnight = local_midnight(today, self._zone()).astimezone(UTC)
        ids = self._counter_ids()
        if not ids:
            self._lookup = "no_counters"
            return
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )

        try:
            rows = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                midnight - timedelta(hours=1),
                midnight,
                set(ids.values()),
                "hour",
                _ENERGY_UNITS,
                {"state"},
            )
        except Exception as err:  # the value stays unknown
            log_failure(
                _LOGGER,
                logging.DEBUG,
                "A counter statistic could not be read",
                error=err,
            )
            self._lookup = "failed"
            return
        if self._baseline_day != today or self._baseline_method is not None:
            self._lookup = "not_needed"
            return
        found: dict[str, tuple[float, float]] = {}
        for mode in (*MODES, *HP_MODES):
            values = []
            for quantity in QUANTITIES:
                entity_id = ids.get((mode, quantity))
                items = rows.get(entity_id, []) if entity_id else []
                state = items[-1].get("state") if items else None
                if not isinstance(state, int | float):
                    break
                values.append(float(state))
            else:
                found[mode] = (values[0], values[1])
        self._lookup = "found" if found else "no_statistic"
        if found:
            self._baseline = found
            self._baseline_method = "statistics"
            self._save()
            self._notify()

    # ------------------------------------------------------------ history
    def _recorder_ready(self) -> bool:
        return "recorder" in self.hass.config.components

    def _counter_ids(self) -> dict[tuple[str, str], str]:
        registry = er.async_get(self.hass)
        found: dict[tuple[str, str], str] = {}
        for mode in MODES:
            for quantity in QUANTITIES:
                part = "outputProduced" if quantity == HEAT else "total_electricity"
                suffixes = (
                    _LEGACY[(mode, quantity)],
                    f"heatSources:emon:{_COUNTER_PATH[mode]}:{part}",
                )
                for suffix in suffixes:
                    entity_id = registry.async_get_entity_id(
                        "sensor", DOMAIN, f"{self.gateway_id}:{suffix}"
                    )
                    if entity_id is not None:
                        found[(mode, quantity)] = entity_id
                        break
        if self._heat_pump_modes() and (
            root := heat_pump_root(self.coordinator, current=False)
        ):
            stem = root.strip("/").replace("/", ":")
            for mode in MODES:
                for quantity in QUANTITIES:
                    part = "outputProduced" if quantity == HEAT else "total_electricity"
                    entity_id = registry.async_get_entity_id(
                        "sensor",
                        DOMAIN,
                        f"{self.gateway_id}:{stem}:emon:{_COUNTER_PATH[mode]}:{part}",
                    )
                    if entity_id is not None:
                        found[(HP_PREFIX + mode, quantity)] = entity_id
        self.counter_entities = found
        return found

    def _attach_recordings(self) -> None:
        if self._view is not None:
            return
        from .recordings import recordings_view

        view = recordings_view(self.hass, self.gateway_id)
        if view is None:
            return
        self._view = view
        self._unsubscribe.append(view.acquire())
        self._unsubscribe.append(view.add_listener(self._recordings_updated))

    @callback
    def _recordings_updated(self) -> None:
        if self.stopped:
            return
        self._load_recordings(self.today())
        self._notify()

    def _load_recordings(self, today: date) -> None:
        view = self._view
        if view is None or not view.loaded:
            return
        start = history_start(today)
        yesterday = today - timedelta(days=1)
        keys = set(view.measure_keys())
        recorded: dict[tuple[str, str], dict[date, Amount]] = {}
        months: dict[tuple[str, str], dict[date, Amount]] = {}
        for item, key in RECORDING_KEYS.items():
            if key not in keys:
                continue
            days: dict[date, Amount] = {}
            for value in view.daily(key, start, yesterday):
                amount = value.value
                # Only confirmed days: a day still open (yesterday before its
                # second read) falls back to the counters.
                if amount is not None and amount >= 0 and value.final:
                    days[value.day] = Amount(amount, _recording_source(value.source))
            recorded[item] = days
            found: dict[date, Amount] = {}
            month = month_start(start)
            while month < month_start(today):
                result = view.monthly(key, month, today)
                if (
                    result is not None
                    and result.final
                    and result.resolution == "monthly"
                    and not result.gap  # a month without data is no 0 kWh
                    and result.y >= 0
                ):
                    only = (
                        next(iter(result.sources)) if len(result.sources) == 1 else ""
                    )
                    found[month] = Amount(result.y, _recording_source(only))
                month = add_months(month, 1)
            months[item] = found
        self._recorded, self._recorded_months = recorded, months
        self._first.clear()

    async def async_refresh(self, *, full: bool = False) -> None:
        """Read the recordings and the counters' daily statistics again."""
        if self.stopped:
            return
        now = dt_util.utcnow()
        today = self.today(now)
        self._attach_recordings()
        self._load_recordings(today)
        report: dict[str, Any] = {"full": full or not self._full_done}
        if self._recorder_ready():
            report["statistics"] = await self._async_count(
                today, full=full or not self._full_done
            )
        else:
            report["statistics"] = "recorder_unavailable"
        if self._baseline_day == today and self._baseline_method is None:
            await self._async_statistics_baseline(today)
        self.report = report
        self.history_read = True
        self._first.clear()
        self._notify()

    async def _async_count(self, today: date, *, full: bool) -> str:
        ids = self._counter_ids()
        if not ids:
            return "no_counters"
        zone = self._zone()
        start = history_start(today) if full else today - timedelta(days=RECENT_DAYS)
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )

        try:
            rows = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                local_midnight(start, zone).astimezone(UTC),
                local_midnight(today, zone).astimezone(UTC),
                set(ids.values()),
                "day",
                _ENERGY_UNITS,
                {"change"},
            )
        except Exception as err:  # keep what is known
            log_failure(
                _LOGGER,
                logging.DEBUG,
                "The counters' statistics could not be read",
                error=err,
            )
            return "failed"
        for item, entity_id in ids.items():
            days: dict[date, float] = {}
            for row in rows.get(entity_id, []):
                change = row.get("change")
                begin = row.get("start")
                if not isinstance(change, int | float) or not isinstance(
                    begin, int | float
                ):
                    continue
                days[datetime.fromtimestamp(begin, zone).date()] = float(change)
            if full and days and min(days) > start:
                # The counter's first day of statistics is incomplete.
                self._partial_first[item] = min(days)
            if (partial := self._partial_first.get(item)) is not None:
                days.pop(partial, None)
            known = self._counted.get(item, {})
            if full:
                known = {}
            else:
                known = {day: value for day, value in known.items() if day < start}
            known.update(days)
            self._counted[item] = known
            if known:
                self._counted_span[item] = (min(known), max(known))
            else:
                self._counted_span.pop(item, None)
        if full:
            self._full_done = True
        return "ok"

    # ------------------------------------------------------------- periods
    def _amount(self, mode: str, quantity: str, day: date) -> Amount | None:
        recorded = self._recorded.get((mode, quantity), {}).get(day)
        counted = self._counted_amount((mode, quantity), day)
        if quantity == HEAT and mode != "total":
            # Heat per mode: the counters first (see the module text).
            return counted or recorded
        return recorded or counted

    def _counted_amount(self, item: tuple[str, str], day: date) -> Amount | None:
        """A day's change of the counters, unless a neighbouring day has no
        statistics (Home Assistant was off) and the recordings fill it: the
        change after such a day carries its energy and the day before is
        partial, so next to a filled day only the recordings count."""
        days = self._counted.get(item, {})
        value = days.get(day)
        if value is None:
            return None
        recorded = self._recorded.get(item, {})
        span = self._counted_span.get(item)
        if span is None:
            span = self._counted_span[item] = (min(days), max(days))
        first, last = span
        for neighbour in (day - timedelta(days=1), day + timedelta(days=1)):
            if (
                first < neighbour < last
                and neighbour not in days
                and neighbour in recorded
            ):
                return None
        return Amount(value, COUNTERS)

    def _pair(self, mode: str, day: date) -> tuple[Amount, Amount] | None:
        heat = self._amount(mode, HEAT, day)
        power = self._amount(mode, ELECTRICITY, day)
        return (heat, power) if heat is not None and power is not None else None

    def _month_pair(self, mode: str, first: date) -> tuple[Amount, Amount] | None:
        heat = self._recorded_months.get((mode, HEAT), {}).get(first)
        power = self._recorded_months.get((mode, ELECTRICITY), {}).get(first)
        return (heat, power) if heat is not None and power is not None else None

    def first_day(self, mode: str) -> date | None:
        """The first day with heat and electricity (or the first month)."""
        if mode in self._first:
            return self._first[mode]
        candidates: set[date] = set()
        for quantity in QUANTITIES:
            candidates.update(self._recorded.get((mode, quantity), {}))
            candidates.update(self._counted.get((mode, quantity), {}))
        days = [day for day in sorted(candidates) if self._pair(mode, day)]
        months = [
            month
            for month in self._recorded_months.get((mode, HEAT), {})
            if self._month_pair(mode, month) is not None
        ]
        found = min((*days[:1], *months), default=None)
        self._first[mode] = found
        return found

    def value(
        self, period: Period, mode: str, now: datetime | None = None
    ) -> PeriodResult | None:
        """Heat and electricity of a period, ``None`` without any day."""
        if period is Period.SINCE_COMMISSIONING:
            live = self._live.get(mode)
            if live is None or live.origin == "restored":
                return None
            return PeriodResult(
                None,
                None,
                live.heat,
                live.electricity,
                frozenset({COUNTERS}),
                frozenset({COUNTERS}),
                0,
                0,
            )
        if period is not Period.TODAY and not self.history_read:
            # Month and year hold only today's counters until the history is
            # read: unknown rather than the value of today.
            return None
        today = self.today(now)
        start, end = bounds(period, today)
        heat = power = 0.0
        heat_sources: set[str] = set()
        power_sources: set[str] = set()
        days = missing = 0
        first = self.first_day(mode)
        last = min(end, today) - timedelta(days=1)
        if first is not None:
            month = month_start(max(start, first))
            while month <= last:
                after = add_months(month, 1)
                begin = max(month, start, first)
                finish = min(after - timedelta(days=1), last)
                span = [
                    begin + timedelta(days=offset)
                    for offset in range((finish - begin).days + 1)
                ]
                pairs = [pair for day in span if (pair := self._pair(mode, day))]
                monthly = (
                    self._month_pair(mode, month)
                    if len(pairs) < len(span) and after <= today and month >= start
                    else None
                )
                if monthly is not None:
                    pairs = [monthly]
                    days += (after - month).days
                else:
                    days += len(pairs)
                    missing += len(span) - len(pairs)
                for heat_amount, power_amount in pairs:
                    heat += heat_amount.kwh
                    power += power_amount.kwh
                    heat_sources.add(heat_amount.source)
                    power_sources.add(power_amount.source)
                month = after
        if end > today:
            current = self._today(mode, today)
            if current is not None:
                heat += current[0]
                power += current[1]
                heat_sources.add(COUNTERS)
                power_sources.add(COUNTERS)
                days += 1
            else:
                missing += 1
        if days == 0:
            return None
        return PeriodResult(
            start,
            end,
            heat,
            power,
            frozenset(heat_sources),
            frozenset(power_sources),
            days,
            missing,
            first if first is not None and first > start else None,
        )

    # ---------------------------------------------------------- diagnostics
    def diagnostics(self) -> dict[str, object]:
        """Counts and states only: no values, dates or identifiers."""
        return {
            "recordings_attached": self._view is not None,
            "counter_entities": len(self.counter_entities),
            "days_recorded": {
                f"{mode}_{quantity}": len(days)
                for (mode, quantity), days in sorted(self._recorded.items())
            },
            "months_recorded": {
                f"{mode}_{quantity}": len(months)
                for (mode, quantity), months in sorted(self._recorded_months.items())
            },
            "days_counted": {
                f"{mode}_{quantity}": len(days)
                for (mode, quantity), days in sorted(self._counted.items())
            },
            "today_start": self._baseline_method
            if self._baseline_day == self.today()
            else None,
            "today_modes": sorted(self._baseline)
            if self._baseline_day == self.today()
            else [],
            "today_start_lookup": self._lookup,
            "history_read": self.history_read,
            "last_refresh": dict(self.report),
        }


def engine_for(
    coordinator: BoschBuderusDataUpdateCoordinator,
) -> PerformanceHistory | None:
    """The history of a coordinator's gateway, started on first use."""
    entry = coordinator.config_entry
    if entry is None:
        return None
    hass = coordinator.hass
    engines = hass.data.setdefault(_ENGINES, {})
    key = (entry.entry_id, coordinator.gateway.gateway_id)
    engine = engines.get(key)
    if engine is None or engine.stopped:
        engine = engines[key] = PerformanceHistory(hass, coordinator)
        entry.async_on_unload(engine.stop)
        entry.async_create_background_task(
            hass, engine.async_start(), "bosch_buderus_heating_performance_start"
        )
    return engine


def existing_engine(
    hass: HomeAssistant, entry_id: str, gateway_id: str
) -> PerformanceHistory | None:
    return hass.data.get(_ENGINES, {}).get((entry_id, gateway_id))


def performance_diagnostics(
    coordinator: BoschBuderusDataUpdateCoordinator,
) -> dict[str, object]:
    """The ``performance`` section of a gateway's diagnostics; never fails."""
    try:
        entry = coordinator.config_entry
        engine = (
            existing_engine(
                coordinator.hass, entry.entry_id, coordinator.gateway.gateway_id
            )
            if entry is not None
            else None
        )
        if engine is None:
            return {"waiting": "not_started"}
        return engine.diagnostics()
    except Exception:  # the report must stay available
        return {"error": "unavailable"}


__all__ = [
    "COLLECTIONS",
    "PerformanceHistory",
    "Period",
    "PeriodResult",
    "engine_for",
    "performance_diagnostics",
]
