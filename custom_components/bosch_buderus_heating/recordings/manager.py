"""The recordings of one gateway: reads, entities' values and statistics.

One manager per configuration entry and gateway. The series belong to the
gateway, not to the access they came from, so switching between the local
data port and the cloud never imports anything twice.

Reads (``async_run``):

- Local first. After every start of Home Assistant every candidate path is
  read once with ``sampleRate=all`` (hours of about ten days, days and
  months since the recording began); afterwards every hour the hours of
  yesterday and today (``PT1H``) of the paths that answered. A path that
  answered 404 is tried again once a day, one that answered 403 not before
  the next start. After a day without local answers the whole history is
  read again.
- The cloud only without a working local access, only for the nine energy
  paths and within ``CLOUD_BUDGET`` reads per run: today and yesterday
  first, then the older days, months and years still missing.
- Nothing is read while no entity, no import and no other feature needs the
  recordings.

Runs start ``START_DELAY`` after the setup in the background and then every
hour at minute ``RUN_MINUTE``, so the hour before has ended for at least 15
minutes. The import into the long-term statistics runs only when switched
on in the options.
"""

from __future__ import annotations

import inspect
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any, Final
from weakref import WeakSet

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from ..const import DOMAIN
from ..log_helpers import log_failure
from ..rrc_energy_statistics import async_gateway_digest
from .ledger import CLOUD, LOCAL, DailyValue, Ledger
from .measures import (
    CALCULATED,
    RECORDING,
    EntitySpec,
    Measure,
    Statistic,
    entity_specs,
    measures,
    statistic_name,
)
from .parse import (
    HOUR,
    MAX_KWH_PER_HOUR,
    Rate,
    add_months,
    local_midnight,
    month_start,
    parse_cloud,
    parse_local,
)
from .periods import Period, PeriodValue, bounds, combine, period_value
from .series import BY_KEY, BY_PATH, SERIES, Quantity, Series
from .sources import Outcome, local_query, read_cloud, read_local

if TYPE_CHECKING:
    from ..coordinator import BoschBuderusDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

OPTION_IMPORT: Final = "recordings_import"
STORE_VERSION: Final = 1
START_DELAY: Final = 90.0
RUN_MINUTE: Final = 20
CLOUD_BUDGET: Final = 30
# Older days whose hours the cloud is asked for (hourly statistics).
CLOUD_HOUR_DAYS: Final = 7
RECHECK_MISSING: Final = timedelta(days=1)
# An open month, year or older day is read again at most this often; today
# and yesterday are read every run.
CLOUD_REREAD: Final = timedelta(hours=12)
LOCAL_FULL_AFTER: Final = timedelta(hours=36)
# Long-term statistics of a counter are complete this long after a period.
COUNTER_SETTLE: Final = timedelta(minutes=80)
RUNS_KEPT: Final = 12
_MANAGERS: HassKey[dict[tuple[str, str], RecordingsManager]] = HassKey(
    f"{DOMAIN}_recordings"
)
_UNIT_CLASS: Final = {
    Quantity.ENERGY: "energy",
    Quantity.DURATION: "duration",
    Quantity.TEMPERATURE: "temperature",
    Quantity.COUNT: None,
}
_UNITS: Final = {
    Quantity.ENERGY: {"energy": "kWh"},
    Quantity.DURATION: {"duration": "h"},
    Quantity.TEMPERATURE: {"temperature": "°C"},
    Quantity.COUNT: None,
}


def import_enabled(entry: ConfigEntry[Any]) -> bool:
    return entry.options.get(OPTION_IMPORT) is True


def statistic_id(digest: str, series: Series) -> str:
    """``bosch_buderus_heating:rec_<digest>_<series>``; the digest is the
    keyed digest of the gateway, never its identifier."""
    return f"{DOMAIN}:rec_{digest[:12]}_{series.key}"


def store_key(entry_id: str, digest: str) -> str:
    return f"{DOMAIN}.recordings.{entry_id}.{digest}"


@dataclass(frozen=True, slots=True)
class Shown:
    """What a period entity shows."""

    start: date
    end: date
    value: float
    source: str
    resolution: str


def _source_text(sources: frozenset[str]) -> str:
    if sources == {"counters"}:
        return "counters"
    if sources == {LOCAL}:
        return "local_recording"
    if sources == {CLOUD}:
        return "cloud_recording"
    return "local_and_cloud_recording"


class RecordingsManager:
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[Any],
        gateway_id: str,
        *,
        now: Callable[[], datetime] = dt_util.utcnow,
    ) -> None:
        self.hass, self.entry, self.gateway_id = hass, entry, gateway_id
        self._now = now
        self._coordinators: WeakSet[BoschBuderusDataUpdateCoordinator] = WeakSet()
        self._latest: BoschBuderusDataUpdateCoordinator | None = None
        self.digest = ""
        self._store: Store[dict[str, Any]] | None = None
        self.ledgers: dict[str, Ledger] = {}
        self.local_paths: dict[str, list[Any]] = {}
        self.cloud_paths: dict[str, list[Any]] = {}
        self.published: dict[str, list[Any]] = {}
        self.cloud_done: set[str] = set()
        self.last_local_ok: float | None = None
        self.offset_mismatch: bool | None = None
        # Not stored: 403 is not tried again before the next start.
        self._forbidden: set[tuple[str, str]] = set()
        # When each open cloud interval was read last (not stored).
        self._cloud_read_at: dict[str, float] = {}
        self._full_done = False
        self._detected = False
        # The counter entity a calculated value came from, per measure.
        self.counter_entities: dict[str, str] = {}
        self._requested: dict[str, int] = {}
        self._consumers = 0
        self._listeners: list[Callable[[], None]] = []
        self._counted: dict[tuple[str, Period], PeriodValue] = {}
        self.runs: deque[dict[str, Any]] = deque(maxlen=RUNS_KEPT)
        self.loaded = False
        self.running = False
        self.stopped = False
        self.waiting: str | None = "not_started"
        self.next_run: datetime | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._schedule_wanted = False

    # ------------------------------------------------------------ lifecycle
    @callback
    def attach(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        self._coordinators.add(coordinator)
        self._latest = coordinator
        if self._schedule_wanted and not self._unsubs and not self.stopped:
            self._schedule()

    @callback
    def detach(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        """A source attempt ended; without any, the timers pause (a new
        attempt of the same gateway resumes them, with the same state)."""
        self._coordinators.discard(coordinator)
        if self._latest is coordinator:
            self._latest = None
        if not self._coordinators:
            self._cancel_timers()

    def coordinator(self) -> BoschBuderusDataUpdateCoordinator | None:
        latest = self._latest
        if latest is not None and latest in self._coordinators:
            return latest
        return next(iter(self._coordinators), None)

    async def async_start(self, *, schedule: bool = True) -> None:
        self.digest = await async_gateway_digest(self.hass, self.gateway_id)
        self._store = Store[dict[str, Any]](
            self.hass,
            STORE_VERSION,
            store_key(self.entry.entry_id, self.digest),
            private=True,
            atomic_writes=True,
        )
        try:
            self._load(await self._store.async_load())
        except Exception as err:
            log_failure(
                _LOGGER,
                logging.WARNING,
                "The stored recordings could not be read",
                error=err,
            )
            self._load(None)
        self.loaded = True
        self.waiting = None
        self._notify()  # entities of the series known from the last run
        if self.stopped or not schedule:
            return
        self._schedule_wanted = True
        if self._coordinators:
            self._schedule()

    @callback
    def _schedule(self) -> None:
        self._unsubs.append(async_call_later(self.hass, START_DELAY, self._due_start))
        self._unsubs.append(
            async_track_time_change(
                self.hass, self._due_hourly, minute=RUN_MINUTE, second=0
            )
        )
        self.next_run = self._now() + timedelta(seconds=START_DELAY)

    @callback
    def _cancel_timers(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self.next_run = None

    @callback
    def stop(self) -> None:
        self.stopped = True
        self._cancel_timers()
        managers = self.hass.data.get(_MANAGERS)
        if managers is not None:
            managers.pop((self.entry.entry_id, self.gateway_id), None)

    @callback
    def _due_start(self, _now: datetime) -> None:
        self._background("start")

    @callback
    def _due_hourly(self, now: datetime) -> None:
        self.next_run = now + timedelta(hours=1)
        self._background("hourly")

    @callback
    def request_run(self) -> None:
        """Run soon, for example after the import was switched on."""
        if self._unsubs:
            self._unsubs.append(async_call_later(self.hass, 5, self._due_start))

    def _background(self, reason: str) -> None:
        if self.stopped or self.running:
            return
        self.entry.async_create_background_task(
            self.hass, self.async_run(reason), "bosch_buderus_heating_recordings"
        )

    # --------------------------------------------------------------- store
    def _load(self, data: object) -> None:
        data = data if isinstance(data, dict) else {}
        raw_series = data.get("series")
        self.ledgers = {
            key: Ledger(BY_KEY[key], value)
            for key, value in (
                raw_series.items() if isinstance(raw_series, dict) else ()
            )
            if key in BY_KEY
        }
        self.local_paths = _paths(data.get("local"))
        self.cloud_paths = _paths(data.get("cloud"))
        published = data.get("published")
        self.published = {
            key: value
            for key, value in (published.items() if isinstance(published, dict) else ())
            if isinstance(key, str) and isinstance(value, list) and len(value) == 5
        }
        done = data.get("cloud_done")
        self.cloud_done = (
            {item for item in done if isinstance(item, str)}
            if isinstance(done, list)
            else set()
        )
        last = data.get("last_local_ok")
        self.last_local_ok = float(last) if isinstance(last, int | float) else None

    def _data(self) -> dict[str, Any]:
        return {
            "series": {key: ledger.to_dict() for key, ledger in self.ledgers.items()},
            "local": self.local_paths,
            "cloud": self.cloud_paths,
            "published": self.published,
            "cloud_done": sorted(self.cloud_done),
            "last_local_ok": self.last_local_ok,
        }

    def _save(self) -> None:
        if self._store is not None:
            self._store.async_delay_save(self._data, 10)

    # ------------------------------------------------------------- demand
    @callback
    def request(self, key: str) -> Callable[[], None]:
        """An enabled entity needs its value; returns its release."""
        self._requested[key] = self._requested.get(key, 0) + 1

        @callback
        def release() -> None:
            count = self._requested.get(key, 0) - 1
            if count > 0:
                self._requested[key] = count
            else:
                self._requested.pop(key, None)

        return release

    @callback
    def acquire(self) -> Callable[[], None]:
        """Another feature (``RecordingsView``) needs the recordings read."""
        self._consumers += 1
        released = False

        @callback
        def release() -> None:
            nonlocal released
            if not released:
                released = True
                self._consumers -= 1

        return release

    def needed(self) -> bool:
        """Read only for an entity, the import or another feature; and once
        after every start, to find the series (and so the entities)."""
        return bool(
            import_enabled(self.entry)
            or self._requested
            or self._consumers > 0
            or not self._detected
        )

    @callback
    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        @callback
        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    # ---------------------------------------------------------------- series
    def available(self) -> tuple[str, ...]:
        """Series that answered, by key."""
        keys = []
        for series in SERIES:
            if series.key not in self.ledgers:
                continue
            local = self.local_paths.get(series.path)
            cloud = self.cloud_paths.get(series.path)
            if (local and local[0] == "ok") or (cloud and cloud[0] == "ok"):
                keys.append(series.key)
        return tuple(keys)

    def measures(self) -> tuple[Measure, ...]:
        return measures(self.available())

    def specs(self) -> tuple[EntitySpec, ...]:
        return entity_specs(self.measures())

    def _ledger(self, series: Series) -> Ledger:
        ledger = self.ledgers.get(series.key)
        if ledger is None:
            ledger = self.ledgers[series.key] = Ledger(series)
        return ledger

    # ------------------------------------------------------------------ run
    async def async_run(self, reason: str = "manual") -> None:
        if self.running or self.stopped or not self.loaded:
            return
        if not self.needed():
            self.waiting = "not_needed"
            return
        self.running = True
        now = self._now()
        zone = dt_util.get_default_time_zone()
        report: dict[str, Any] = {
            "reason": reason,
            "at": now.timestamp(),
            "source": None,
            "local_reads": 0,
            "cloud_reads": 0,
            "outcomes": {},
        }
        try:
            self.waiting = None
            done = False
            for driver in self._local_drivers():
                done = await self._run_local(driver, now, zone, report)
                if done:
                    report["source"] = LOCAL
                    break
            if not done and await self._run_cloud(now, zone, report):
                report["source"] = CLOUD
            for ledger in self.ledgers.values():
                ledger.settle(now, zone)
                ledger.prune(now)
        except Exception as err:
            log_failure(
                _LOGGER, logging.WARNING, "Reading the recordings failed", error=err
            )
            report["failed"] = True
        try:
            # The import and the entities' values do not depend on each
            # other: a failed import leaves the values, and the reverse.
            if import_enabled(self.entry):
                await self._import(now, zone, report)
        except Exception as err:
            log_failure(
                _LOGGER, logging.WARNING, "Importing the recordings failed", error=err
            )
            report["import_failed"] = True
        try:
            await self._count(now, zone)
            self._publish_all(now, zone)
            self._detected = True
        except Exception as err:
            log_failure(
                _LOGGER,
                logging.WARNING,
                "Working out the recordings' values failed",
                error=err,
            )
            report["failed"] = True
        finally:
            self.runs.append(report)
            self.running = False
            self._save()
            self._notify()

    def _local_drivers(self) -> list[Any]:
        coordinator = self.coordinator()
        selection = getattr(coordinator, "local_selection", None)
        if selection is None:
            return []
        running = getattr(selection, "running_sources", None)
        if not callable(running):
            return []
        return [
            driver
            for driver in running()
            if callable(getattr(driver, "read_explicit", None))
        ]

    def _count_outcome(
        self, report: dict[str, Any], source: str, outcome: Outcome
    ) -> None:
        name = f"{source}_{outcome.value}"
        report["outcomes"][name] = report["outcomes"].get(name, 0) + 1

    async def _run_local(
        self, driver: Any, now: datetime, zone: tzinfo, report: dict[str, Any]
    ) -> bool:
        stale = (
            self.last_local_ok is None
            or now.timestamp() - self.last_local_ok > LOCAL_FULL_AFTER.total_seconds()
        )
        full = not self._full_done or stale
        today = now.astimezone(zone).date()
        succeeded = False
        complete = True
        for series in SERIES:
            path = series.path
            if (LOCAL, path) in self._forbidden:
                continue
            known = self.local_paths.get(path)
            state = known[0] if known else None
            if state == "unit":
                continue
            if (
                known is not None
                and state == "missing"
                and now.timestamp() - float(known[1]) < RECHECK_MISSING.total_seconds()
            ):
                continue
            whole = full or state != "ok"
            answer = await read_local(driver, series, local_query(whole, today))
            report["local_reads"] += 1
            self._count_outcome(report, LOCAL, answer.outcome)
            if answer.outcome is Outcome.UNREACHABLE:
                return succeeded
            if answer.outcome is Outcome.OK:
                read_at = self._now()
                parsed = parse_local(answer.data, series, zone)
                if parsed is None:
                    self.local_paths[path] = ["unit", read_at.timestamp()]
                    continue
                self._ledger(series).merge(
                    parsed, LOCAL, read_at=read_at, zone=zone, full_history=whole
                )
                if parsed.offset is not None:
                    self.offset_mismatch = (
                        parsed.offset != read_at.astimezone(zone).utcoffset()
                    )
                self.local_paths[path] = ["ok", read_at.timestamp()]
                succeeded = True
            elif answer.outcome is Outcome.NOT_FOUND:
                self.local_paths[path] = ["missing", now.timestamp()]
            elif answer.outcome is Outcome.FORBIDDEN:
                self._forbidden.add((LOCAL, path))
            else:
                complete = False
        if succeeded:
            self.last_local_ok = now.timestamp()
            if full and complete:
                self._full_done = True
        return succeeded

    # --------------------------------------------------------------- cloud
    def _cloud_tasks(self, today: date) -> list[tuple[Series, str, Rate]]:
        tasks: list[tuple[Series, str, Rate]] = []
        candidates = [
            series
            for series in SERIES
            if series.cloud
            and (CLOUD, series.path) not in self._forbidden
            and (self.cloud_paths.get(series.path) or [None])[0] != "unit"
        ]
        yesterday = today - timedelta(days=1)
        for series in candidates:
            for day in (today, yesterday):
                tasks.append((series, day.isoformat(), Rate.HOUR))
        later: list[tuple[Series, str, Rate]] = []
        for series in candidates:
            first = month_start(today)
            for month in (first, add_months(first, -1)):
                later.append((series, f"{month.year:04d}-{month.month:02d}", Rate.DAY))
            for year in (today.year, today.year - 1):
                later.append((series, f"{year:04d}", Rate.MONTH))
            for back in range(2, CLOUD_HOUR_DAYS + 1):
                later.append(
                    (series, (today - timedelta(days=back)).isoformat(), Rate.HOUR)
                )
            ledger = self.ledgers.get(series.key)
            if ledger is not None:
                for key, cell in sorted(ledger.months.items(), reverse=True):
                    if cell[1] > 0 and key[:7] < f"{add_months(first, -1):%Y-%m}":
                        later.append((series, key[:7], Rate.DAY))
        # A past interval whose slots are all final is not read again.
        tasks.extend(
            task for task in later if f"{task[0].path}|{task[1]}" not in self.cloud_done
        )
        return tasks

    def _cloud_complete(
        self, series: Series, interval: str, rate: Rate, today: date, zone: tzinfo
    ) -> bool:
        """Every slot of a past interval is final: it is not read again."""
        ledger = self.ledgers.get(series.key)
        if ledger is None:
            return False
        if rate is Rate.HOUR:
            day = date.fromisoformat(interval)
            if day >= today - timedelta(days=1):
                return False
            cell = ledger.day(day)
            return cell is not None and bool(cell[4])
        if rate is Rate.DAY:
            first = date(int(interval[:4]), int(interval[5:]), 1)
            if add_months(first, 1) > today - timedelta(days=1):
                return False
            day = first
            while day < add_months(first, 1):
                cell = ledger.day(day)
                if cell is not None and not cell[4]:
                    return False
                day += timedelta(days=1)
            return True
        year = int(interval)
        if year >= today.year:
            return False
        return all(
            bool(cell[4])
            for key, cell in ledger.months.items()
            if key.startswith(interval)
        )

    async def _run_cloud(
        self, now: datetime, zone: tzinfo, report: dict[str, Any]
    ) -> bool:
        coordinator = self.coordinator()
        if coordinator is None or coordinator.cloud_unavailable():
            report["cloud"] = "unavailable"
            return False
        if not any(path.startswith("/heatSources") for path in coordinator.resources):
            # No heat source at all (a room controller, a water softener).
            report["cloud"] = "no_heat_source"
            return False
        client = coordinator.client
        today = now.astimezone(zone).date()
        budget = CLOUD_BUDGET
        succeeded = False
        regular = {today.isoformat(), (today - timedelta(days=1)).isoformat()}
        for series, interval, rate in self._cloud_tasks(today):
            known = self.cloud_paths.get(series.path)
            if (CLOUD, series.path) in self._forbidden:
                continue
            task = f"{series.path}|{interval}"
            last = self._cloud_read_at.get(task)
            if (
                interval not in regular
                and last is not None
                and now.timestamp() - last < CLOUD_REREAD.total_seconds()
            ):
                continue
            if (
                known
                and known[0] == "missing"
                and now.timestamp() - float(known[1]) < RECHECK_MISSING.total_seconds()
            ):
                continue
            if budget <= 0:
                report["cloud_budget_used"] = True
                break
            budget -= 1
            answer = await read_cloud(
                client, coordinator.gateway.gateway_id, series, interval
            )
            report["cloud_reads"] += 1
            self._cloud_read_at[task] = now.timestamp()
            self._count_outcome(report, CLOUD, answer.outcome)
            if answer.outcome is Outcome.OK:
                read_at = self._now()
                parsed = parse_cloud(answer.data, series, interval, zone)
                if parsed is None:
                    self.cloud_paths[series.path] = ["unit", read_at.timestamp()]
                    continue
                self._ledger(series).merge(parsed, CLOUD, read_at=read_at, zone=zone)
                self.cloud_paths[series.path] = ["ok", read_at.timestamp()]
                succeeded = True
                if self._cloud_complete(series, interval, rate, today, zone):
                    self.cloud_done.add(task)
            elif answer.outcome is Outcome.NOT_FOUND:
                self.cloud_paths[series.path] = ["missing", now.timestamp()]
            elif answer.outcome is Outcome.FORBIDDEN:
                self._forbidden.add((CLOUD, series.path))
            else:
                # A rate limit, a pause or an error: the next run goes on.
                break
        limit = f"{today.year - 2:04d}"
        self.cloud_done = {
            item for item in self.cloud_done if item.rpartition("|")[2] >= limit
        }
        return succeeded

    # -------------------------------------------------------------- import
    def recorder_ready(self) -> bool:
        return "recorder" in self.hass.config.components

    async def _import(
        self, now: datetime, zone: tzinfo, report: dict[str, Any]
    ) -> None:
        if not self.recorder_ready():
            report["import"] = "recorder_unavailable"
            return
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.models import (
            StatisticData,
            StatisticMeanType,
            StatisticMetaData,
        )
        from homeassistant.components.recorder.statistics import (
            async_add_external_statistics,
        )

        instance = get_instance(self.hass)
        can_clear = clears_ranges(instance)
        language = self.hass.config.language
        written = 0
        failed = 0
        for key in self.available():
            ledger = self.ledgers[key]
            series = ledger.series
            wanted = statistic_id(self.digest, series)
            try:
                earlier: tuple[float, list[int], bool] | None = None
                if not ledger.importing:
                    start = ledger.import_start(zone)
                    if start is None:
                        continue
                    earlier = await instance.async_add_executor_job(
                        _earlier_import, self.hass, wanted, start
                    )
                # No await from here on: switched off or deleted meanwhile,
                # nothing is written (and nothing marked as written).
                if not import_enabled(self.entry):
                    break
                if earlier is not None:
                    self._begin_import(ledger, wanted, earlier, zone, can_clear)
                plan = ledger.plan(now, zone, can_clear=can_clear, commit=False)
                if plan.clear is not None and can_clear:
                    instance.async_clear_statistics(
                        [wanted],
                        start_time=datetime.fromtimestamp(plan.clear[0], UTC),
                        end_time=datetime.fromtimestamp(plan.clear[1], UTC),
                    )
                if plan.rows:
                    metadata = StatisticMetaData(
                        mean_type=StatisticMeanType.ARITHMETIC
                        if series.mean
                        else StatisticMeanType.NONE,
                        has_sum=not series.mean,
                        name=statistic_name(language, series),
                        source=DOMAIN,
                        statistic_id=wanted,
                        unit_class=_UNIT_CLASS[series.quantity],
                        unit_of_measurement=series.unit,
                    )
                    rows = [
                        StatisticData(
                            start=datetime.fromtimestamp(start, UTC),
                            mean=y / c,
                            min=y / c,
                            max=y / c,
                        )
                        if series.mean
                        else StatisticData(
                            start=datetime.fromtimestamp(start, UTC),
                            sum=total,
                            state=total,
                        )
                        for start, y, c, total in plan.rows
                    ]
                    async_add_external_statistics(self.hass, metadata, rows)
                    written += len(rows)
                ledger.commit(plan)
            except Exception as err:  # this series only; tried again next run
                failed += 1
                log_failure(
                    _LOGGER, logging.WARNING, "Importing a recording failed", error=err
                )
        report["imported_rows"] = written
        if failed:
            report["import_failed_series"] = failed

    def _begin_import(
        self,
        ledger: Ledger,
        wanted: str,
        earlier: tuple[float, list[int], bool],
        zone: tzinfo,
        can_clear: bool,
    ) -> None:
        """Start a series' import on top of what an earlier import left (a
        new entry, a lost state): its rows from the first recorded day on
        are cleared or, where the recorder cannot clear a range, written
        again or made neutral; the sum before them continues."""
        from homeassistant.components.recorder import get_instance

        base, existing, before = earlier
        stale: list[int] = []
        if existing:
            instance = get_instance(self.hass)
            if can_clear:
                instance.async_clear_statistics(
                    [wanted],
                    start_time=datetime.fromtimestamp(existing[0], UTC),
                    end_time=datetime.fromtimestamp(existing[-1] + HOUR, UTC),
                )
            elif not before:
                # Every row is written again from the first day: clear all.
                instance.async_clear_statistics([wanted])
            else:
                stale = existing
        ledger.start_import(0.0 if ledger.series.mean else base, zone, stale)

    async def async_delete_statistics(self) -> int:
        """Remove every imported statistic of this gateway; the import is
        switched off by the caller. Returns how many statistics."""
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            async_list_statistic_ids,
        )

        if not self.recorder_ready():
            return 0
        if not self.digest:
            # Asked for before the start finished.
            self.digest = await async_gateway_digest(self.hass, self.gateway_id)
        known = sorted(statistic_id(self.digest, series) for series in SERIES)
        listed = await async_list_statistic_ids(self.hass)
        found = {item.get("statistic_id") for item in listed} & set(known)
        for ledger in self.ledgers.values():
            ledger.stop_import()
        # All of them: a row a running import has just handed over may not
        # be listed yet; the recorder clears after it.
        get_instance(self.hass).async_clear_statistics(known)
        self._save()
        return len(found)

    # ------------------------------------------------------------- counters
    async def _count(self, now: datetime, zone: tzinfo) -> None:
        """The counters' change (or mean) over the periods entities need."""
        self._counted = {}
        if not self.recorder_ready() or not self._requested:
            return
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            statistic_during_period,
        )

        registry = er.async_get(self.hass)
        instance = get_instance(self.hass)
        today = now.astimezone(zone).date()
        for spec in self.specs():
            measure = spec.measure
            counter = measure.counter
            if counter is None or spec.key not in self._requested:
                continue
            if (measure.key, spec.period) in self._counted:
                continue
            entity_id = next(
                (
                    found
                    for suffix in counter.suffixes
                    if (
                        found := registry.async_get_entity_id(
                            "sensor", DOMAIN, f"{self.gateway_id}:{suffix}"
                        )
                    )
                    is not None
                ),
                None,
            )
            if entity_id is None:
                continue
            self.counter_entities[measure.key] = entity_id
            first, after = bounds(spec.period, today)
            start = local_midnight(first, zone).astimezone(UTC)
            end = min(local_midnight(after, zone).astimezone(UTC), now)
            kind = "mean" if counter.statistic is Statistic.MEAN else "change"
            try:
                result = await instance.async_add_executor_job(
                    statistic_during_period,
                    self.hass,
                    start,
                    end,
                    entity_id,
                    {kind},
                    _UNITS[measure.quantity],
                )
            except Exception as err:  # one value only
                log_failure(
                    _LOGGER,
                    logging.DEBUG,
                    "A counter statistic could not be read",
                    error=err,
                )
                continue
            value = result.get(kind)
            if not isinstance(value, int | float):
                continue
            self._counted[(measure.key, spec.period)] = PeriodValue(
                first,
                after,
                float(value),
                1.0,
                spec.period.past
                and now >= local_midnight(after, zone) + COUNTER_SETTLE,
                "hourly",
                frozenset({"counters"}),
            )

    # --------------------------------------------------------------- values
    def recorded(
        self, measure: Measure, period: Period, today: date
    ) -> PeriodValue | None:
        values = []
        for part in measure.parts:
            ledger = self.ledgers.get(part)
            if ledger is None:
                return None
            value = period_value(ledger, period, today)
            if value is None:
                return None
            values.append(value)
        return combine(values)

    def _candidate(self, spec: EntitySpec, today: date) -> PeriodValue | None:
        recorded = self.recorded(spec.measure, spec.period, today)
        counted = self._counted.get((spec.measure.key, spec.period))
        if not spec.primary:
            chain = [counted] if spec.method == CALCULATED else [recorded]
        elif spec.measure.primary == RECORDING:
            chain = [recorded, counted]
        else:
            chain = [counted, recorded]
        return next((item for item in chain if item is not None), None)

    def _publish_all(self, now: datetime, zone: tzinfo) -> None:
        today = now.astimezone(zone).date()
        wanted = set(self._requested)
        for spec in self.specs():
            if spec.key not in wanted:
                continue
            candidate = self._candidate(spec, today)
            if candidate is None or (spec.period.past and not candidate.final):
                # A past period changes only once its new value is final;
                # until then the last one shown stays (with its dates).
                if not spec.period.past:
                    self.published.pop(spec.key, None)
                continue
            value = candidate.value(spec.measure.mean)
            if value is None:
                continue
            self.published[spec.key] = [
                candidate.start.isoformat(),
                candidate.end.isoformat(),
                round(value, 4),
                _source_text(candidate.sources),
                candidate.resolution,
            ]

    def shown(self, spec: EntitySpec) -> Shown | None:
        item = self.published.get(spec.key)
        if item is None:
            return None
        try:
            return Shown(
                date.fromisoformat(item[0]),
                date.fromisoformat(item[1]),
                float(item[2]),
                str(item[3]),
                str(item[4]),
            )
        except TypeError, ValueError:
            return None

    # ---------------------------------------------------------- internal API
    def daily(self, key: str, start: date, end: date) -> tuple[DailyValue, ...]:
        """Days of a series or measure (electricity: the sum of its parts,
        only on days every part knows)."""
        measure = next((item for item in self.measures() if item.key == key), None)
        if measure is None:
            return ()
        rows = [
            self.ledgers[part].daily(start, end)
            for part in measure.parts
            if part in self.ledgers
        ]
        if len(rows) != len(measure.parts):
            return ()
        if len(rows) == 1:
            return rows[0]
        by_day: dict[date, list[DailyValue]] = {}
        for row in rows:
            for item in row:
                by_day.setdefault(item.day, []).append(item)
        return tuple(
            DailyValue(
                day,
                sum(item.y for item in items),
                sum(item.c for item in items),
                all(item.final for item in items),
                items[0].source
                if len({item.source for item in items}) == 1
                else "mixed",
            )
            for day, items in sorted(by_day.items())
            if len(items) == len(rows)
        )

    # ---------------------------------------------------------- diagnostics
    def diagnostics(self) -> dict[str, object]:
        """Where the reads and the import stand: states, counts and ages,
        never values, dates or identifiers."""
        now = self._now()
        zone = dt_util.get_default_time_zone()
        today = now.astimezone(zone).date()
        last = self.runs[-1] if self.runs else None
        series: dict[str, object] = {}
        for item in SERIES:
            local = self.local_paths.get(item.path)
            cloud = self.cloud_paths.get(item.path)
            ledger = self.ledgers.get(item.key)
            forbidden = {
                source for source, path in self._forbidden if path == item.path
            }
            if local is None and cloud is None and ledger is None and not forbidden:
                continue
            report: dict[str, object] = {
                "local": "forbidden"
                if (LOCAL, item.path) in self._forbidden
                else (local[0] if local else None),
                "cloud": "forbidden"
                if (CLOUD, item.path) in self._forbidden
                else (cloud[0] if cloud else None),
            }
            if ledger is not None:
                first = ledger.first_day()
                imp = ledger.imp
                report.update(
                    {
                        "hours_known": len(ledger.hours),
                        "days_known": len(ledger.days),
                        "months_known": len(ledger.months),
                        "days_since_first_value": (today - first).days
                        if first
                        else None,
                        "importing": imp is not None,
                        "import_behind_hours": (
                            max(0, int((now.timestamp() - int(imp["next"])) // 3600))
                            if imp is not None
                            else None
                        ),
                        "days_hours_not_placeable": len(ledger.unplaced),
                        "events": dict(sorted(ledger.stats.items())),
                    }
                )
            series[item.key] = report
        return {
            "confirmation": "unconfirmed",
            "import_enabled": import_enabled(self.entry),
            "recorder_available": self.recorder_ready(),
            "needed": self.needed(),
            "waiting": self.waiting,
            "plant_offset_differs_from_time_zone": self.offset_mismatch,
            # The upper bound per hour that applies to energy values.
            "plausibility_limit_kwh_per_hour": MAX_KWH_PER_HOUR,
            "history_read_since_start": self._full_done,
            "entities_with_value": len(self.published),
            "next_run_in_seconds": (
                max(0, int((self.next_run - now).total_seconds()))
                if self.next_run
                else None
            ),
            "last_run": (
                {key: value for key, value in last.items() if key != "at"}
                | {"seconds_ago": int(now.timestamp() - last["at"])}
                if last
                else None
            ),
            "runs": [
                {
                    "reason": run["reason"],
                    "source": run["source"],
                    "local_reads": run["local_reads"],
                    "cloud_reads": run["cloud_reads"],
                }
                for run in self.runs
            ],
            "series": series,
        }


def _paths(value: object) -> dict[str, list[Any]]:
    if not isinstance(value, dict):
        return {}
    return {
        key: item
        for key, item in value.items()
        if key in BY_PATH
        and isinstance(item, list)
        and len(item) == 2
        and item[0] in ("ok", "missing", "unit")
        and isinstance(item[1], int | float)
    }


def clears_ranges(instance: Any) -> bool:
    """Whether the recorder clears a time range of a statistic (Home
    Assistant 2026.11 on); before, it clears whole statistics only."""
    try:
        parameters = inspect.signature(instance.async_clear_statistics).parameters
    except TypeError, ValueError:
        return False
    return "start_time" in parameters and "end_time" in parameters


def _earlier_import(
    hass: HomeAssistant, wanted: str, start: datetime
) -> tuple[float, list[int], bool]:
    """What an earlier import left: the sum just before ``start`` (a new
    entry or a lost state continues it), the starts of its rows from
    ``start`` on (written again) and whether it has rows before."""
    from homeassistant.components.recorder.statistics import (
        get_last_statistics,
        statistics_during_period,
    )

    last = get_last_statistics(hass, 1, wanted, False, {"sum"}).get(wanted)
    if not last:
        return 0.0, [], False
    row = last[0]
    existing: list[int] = []
    if row["start"] >= start.timestamp():
        # Rows of the earlier import overlap (the plant keeps about a year
        # and a half).
        found = statistics_during_period(
            hass,
            start - timedelta(days=800),
            None,
            {wanted},
            "hour",
            None,
            {"sum"},
        ).get(wanted)
        rows = list(found or ())
        before = [item for item in rows if item["start"] < start.timestamp()]
        existing = sorted(
            int(item["start"]) for item in rows if item["start"] >= start.timestamp()
        )
        if not before:
            return 0.0, existing, False
        row = before[-1]
    total = row.get("sum")
    return (float(total) if isinstance(total, int | float) else 0.0), existing, True


async def async_remove_recordings_store(
    hass: HomeAssistant, entry_id: str, gateway_id: str
) -> None:
    """Delete the stored recordings of a removed entry's gateway. Imported
    statistics stay, as Home Assistant keeps them (options: delete)."""
    digest = await async_gateway_digest(hass, gateway_id)
    await Store[dict[str, Any]](
        hass, STORE_VERSION, store_key(entry_id, digest), private=True
    ).async_remove()


# ------------------------------------------------------------- registry
def manager_for(
    coordinator: BoschBuderusDataUpdateCoordinator,
) -> RecordingsManager | None:
    """The manager of a coordinator's gateway, started on first use."""
    entry = coordinator.config_entry
    if entry is None or entry.state not in (
        ConfigEntryState.SETUP_IN_PROGRESS,
        ConfigEntryState.LOADED,
    ):
        # Only for an entry being set up or running: its unload stops the
        # reads and their timers.
        return None
    hass = coordinator.hass
    managers = hass.data.setdefault(_MANAGERS, {})
    key = (entry.entry_id, coordinator.gateway.gateway_id)
    manager = managers.get(key)
    if manager is None or manager.stopped:
        manager = managers[key] = RecordingsManager(hass, entry, key[1])
        entry.async_on_unload(manager.stop)
        entry.async_create_background_task(
            hass, manager.async_start(), "bosch_buderus_heating_recordings_start"
        )
    manager.attach(coordinator)
    return manager


def managers_of(hass: HomeAssistant, entry_id: str) -> list[RecordingsManager]:
    return [
        manager
        for (owner, _), manager in hass.data.get(_MANAGERS, {}).items()
        if owner == entry_id
    ]


def manager_of_gateway(
    hass: HomeAssistant, gateway_id: str
) -> RecordingsManager | None:
    return next(
        (
            manager
            for (_, gateway), manager in hass.data.get(_MANAGERS, {}).items()
            if gateway == gateway_id and not manager.stopped
        ),
        None,
    )
