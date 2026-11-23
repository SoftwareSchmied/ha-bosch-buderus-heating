"""The recordings of one gateway: reads, entities' values and statistics.

One manager per configuration entry and gateway. The series belong to the
gateway, not to the access they came from, so switching between the local
data port and the cloud never imports anything twice.

Reads (``async_run``):

- Local first. After every start of Home Assistant every candidate path is
  read once with ``sampleRate=all`` (hours of about ten days, days and
  months since the recording began); a path the local access reports busy
  is asked again twice within the same run, so every series (and its
  entities) is known after the first run. Afterwards every hour the hours
  of the last two days (``PT1H``), only of the series something needs:
  every series while the import is on, otherwise the series of enabled
  entities and the energy series other features use (performance factors).
  A series not read for longer than ``LOCAL_FULL_AFTER`` is read with
  ``all`` again once it is needed. A path that answered 404 is tried again
  once a day, one that answered 403 not before the next start. A path whose
  answers fail (another error, no recording) is tried again once a day too
  (one that answered before only after ``LOCAL_FAILURES`` failures in a
  row), so it neither blocks the others nor makes every run read the whole
  history; one whose history does not come within ``READ_SECONDS`` is read
  by its last two days only. After a day without local answers the whole
  history is read again. A run without anything to read locally, with the
  local access answering, never falls back to the cloud.
- The cloud only without a working local access and only for the nine
  energy paths (and, on a hybrid system, the heat pump's own six; locally
  they are read first, from the cloud only where the local access does not
  answer them), throttled for the many installations without a local
  access: yesterday until it is final (then at most every
  ``CLOUD_REREAD``) for the series something needs, today only for the
  current month and year or the import, and ``CLOUD_BACKFILL`` older days,
  months and years per hourly run (none in the start run, none after a
  rate limit until the next day) until the history is complete; at most
  ``CLOUD_BUDGET`` reads per run. A path whose read fails is left out for
  ``CLOUD_FAIL_PAUSE`` while the others answer; a rate limit, a pause or no
  connection ends the run.
- Nothing is read while no entity, no import and no other feature needs the
  recordings.

Runs start ``START_DELAY`` after the setup in the background and then every
hour at minute ``RUN_MINUTE``, so the hour before has ended for at least 15
minutes. An entity added after a run (the first run finds the series and so
the entities) gets its value at once from what is stored, without a read;
a value that needs a second read (yesterday, rule 5) follows from a read of
the needed series ``CONFIRM_DELAY`` after the start run, unless the hourly
run comes first anyway. The import into the long-term statistics runs only
when switched on in the options.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any, Final
from weakref import WeakSet

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from ..const import DOMAIN
from ..log_helpers import log_failure
from ..rrc_energy_statistics import async_gateway_digest
from .anchor import has_recording_anchor
from .gating import (
    SOFTENER_CIRCUIT,
    allowed_keys,
    heat_pump_keys,
    heat_pump_source,
    heat_pump_source_basis,
    icom_gateway,
    icom_heat_pump,
    softener_plant,
)
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
    cloud_unit,
    is_cloud_recording,
    local_midnight,
    month_start,
    parse_cloud,
    parse_local,
)
from .periods import Period, PeriodValue, bounds, combine, period_value
from .series import (
    ALL_SERIES,
    BY_KEY,
    BY_PATH,
    HEAT_SOURCE_SERIES,
    SERIES,
    Quantity,
    Series,
    series_unit_factor,
)
from .sources import Outcome, local_query, read_cloud, read_local

if TYPE_CHECKING:
    from ..coordinator import BoschBuderusDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

OPTION_IMPORT: Final = "recordings_import"
# The repair offered after an entry with imported statistics was removed.
ISSUE_PREFIX: Final = "recordings_statistics_"
STORE_VERSION: Final = 1
START_DELAY: Final = 90.0
RUN_MINUTE: Final = 20
CLOUD_BUDGET: Final = 24
# Older intervals (back-fill) read from the cloud per hourly run.
CLOUD_BACKFILL: Final = 4
# A cloud path whose read failed while others answered is left out this long.
CLOUD_FAIL_PAUSE: Final = timedelta(hours=3)
# Failed cloud reads that end a run.
CLOUD_FAILURES_PER_RUN: Final = 3
# Failed reads in a row after which a local path that answered before is
# treated as failing (tried again once a day).
LOCAL_FAILURES: Final = 2
# Older days whose hours the cloud is asked for (hourly statistics).
CLOUD_HOUR_DAYS: Final = 7
RECHECK_MISSING: Final = timedelta(days=1)
# An open month, year or older day (and yesterday once final) is read again
# at most this often; today and an open yesterday are read every run.
CLOUD_REREAD: Final = timedelta(hours=12)
LOCAL_FULL_AFTER: Final = timedelta(hours=36)
# Long-term statistics of a counter are complete this long after a period.
COUNTER_SETTLE: Final = timedelta(minutes=80)
RUNS_KEPT: Final = 12
# A path the local access reports busy is asked again this often per run,
# this long apart.
BUSY_RETRIES: Final = 2
BUSY_RETRY_SECONDS: Final = 15.0
# Entities added close together get their values worked out once.
VALUES_DELAY: Final = 1.0
# After the start run, the series something needs are read once more this
# much later: a past hour counts only after two reads with the same value
# (rule 5), so yesterday is shown then instead of after the hourly run.
CONFIRM_DELAY: Final = 300.0
# A unit text shown in the diagnostics.
# The unit text kept for the diagnostics: the unit only, never a value.
UNIT_TEXT_LENGTH: Final = 16
_MANAGERS: HassKey[dict[tuple[str, str], RecordingsManager]] = HassKey(
    f"{DOMAIN}_recordings"
)
_UNIT_CLASS: Final = {
    Quantity.ENERGY: "energy",
    Quantity.DURATION: "duration",
    Quantity.TEMPERATURE: "temperature",
    Quantity.VOLUME: "volume",
    Quantity.COUNT: None,
}
_UNITS: Final = {
    Quantity.ENERGY: {"energy": "kWh"},
    Quantity.DURATION: {"duration": "h"},
    Quantity.TEMPERATURE: {"temperature": "°C"},
    Quantity.VOLUME: {"volume": "L"},
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


def statistics_issue_id(digest: str) -> str:
    return f"{ISSUE_PREFIX}{digest[:12]}"


@dataclass(frozen=True, slots=True)
class Shown:
    """What a period entity shows."""

    start: date
    end: date
    value: float
    source: str
    resolution: str
    # The first day of the counter's statistics when it lies after the start
    # of the period (a value from the counters then covers less).
    history_start: date | None = None


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
        # Cloud paths left out after a failed read, until then (not stored).
        self._cloud_deferred: dict[str, float] = {}
        # The unit a recording answered in, per path, when it was not taken
        # (``unit``) or read for the diagnostics only (stored).
        self.unit_texts: dict[str, str] = {}
        # The day of the last cloud rate limit: no back-fill until the next.
        self._cloud_limited: date | None = None
        self._cloud_runs_ok = 0
        # Local paths whose whole history was read since the start, those
        # whose whole history did not come in time, and failures in a row.
        self._full_read: set[str] = set()
        self._slow: set[str] = set()
        self._local_failures: dict[str, int] = {}
        self._full_done = False
        self.time_zone_on_the_hour: bool | None = None
        self._detected = False
        # The counter entity a calculated value came from, per measure.
        self.counter_entities: dict[str, str] = {}
        self._requested: dict[str, int] = {}
        self._consumers = 0
        self._listeners: list[Callable[[], None]] = []
        self._counted: dict[tuple[str, Period], PeriodValue] = {}
        # Where the counter statistics begin, per measure and period start
        # (``None``: not later than the period start); found once.
        self._counter_first: dict[tuple[str, date], date | None] = {}
        self.runs: deque[dict[str, Any]] = deque(maxlen=RUNS_KEPT)
        self.loaded = False
        self.running = False
        self.stopped = False
        self.waiting: str | None = "not_started"
        self.next_run: datetime | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._schedule_wanted = False
        # Working out the entities' values without a read (newly added ones).
        self._values_unsub: CALLBACK_TYPE | None = None
        self._values_after_run = False
        self._values_lock = asyncio.Lock()

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
        # The gateway is back: the options delete its statistics again.
        ir.async_delete_issue(self.hass, DOMAIN, statistics_issue_id(self.digest))
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
        if any(key not in self.published for key in self._requested):
            self._schedule_values()
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
        if self._values_unsub is not None:
            self._values_unsub()
            self._values_unsub = None
        self.next_run = None

    def _next_hourly(self) -> datetime:
        """The next hourly run (minute ``RUN_MINUTE``, local time)."""
        now = dt_util.as_local(self._now())
        due = now.replace(minute=RUN_MINUTE, second=0, microsecond=0)
        if due <= now:
            due += timedelta(hours=1)
        return due

    @callback
    def stop(self) -> None:
        self.stopped = True
        self._cancel_timers()
        managers = self.hass.data.get(_MANAGERS)
        if managers is not None:
            managers.pop((self.entry.entry_id, self.gateway_id), None)

    @callback
    def _due_start(self, _now: datetime) -> None:
        # Afterwards the hourly run is the next one (diagnostics).
        self.next_run = self._next_hourly()
        self._background("start")

    @callback
    def _schedule_confirm(self) -> None:
        """Read the needed series once more soon after the start run."""
        due = self._now() + timedelta(seconds=CONFIRM_DELAY)
        if self._next_hourly() - due < timedelta(seconds=CONFIRM_DELAY):
            return  # the hourly run comes soon enough
        self._unsubs.append(
            async_call_later(self.hass, CONFIRM_DELAY, self._due_confirm)
        )
        self.next_run = due

    @callback
    def _due_confirm(self, _now: datetime) -> None:
        self.next_run = self._next_hourly()
        self._background("confirm")

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
        zone = dt_util.get_default_time_zone()
        now = self._now()
        for ledger in self.ledgers.values():
            ledger.correct_mean_months(now, zone)
        published = data.get("published")
        self.published = {
            key: value
            for key, value in (published.items() if isinstance(published, dict) else ())
            if isinstance(key, str) and isinstance(value, list) and len(value) in (5, 6)
        }
        done = data.get("cloud_done")
        self.cloud_done = (
            {item for item in done if isinstance(item, str)}
            if isinstance(done, list)
            else set()
        )
        last = data.get("last_local_ok")
        self.last_local_ok = float(last) if isinstance(last, int | float) else None
        units = data.get("units")
        self.unit_texts = {
            path: text
            for path, text in (units.items() if isinstance(units, dict) else ())
            if path in BY_PATH and isinstance(text, str) and _unit_text(text) == text
        }
        self._recheck_units(rechecked=data.get("units_rechecked") is True)

    def _recheck_units(self, *, rechecked: bool = True, icom: bool = False) -> None:
        """A recording left out for its unit is asked again when that unit is
        taken now, and once when an earlier version kept no unit text for it
        (``units_rechecked``). ``icom``: the plant is behind an iCom gateway
        (checked again in each run, when the gateway is known)."""
        for known in (self.local_paths, self.cloud_paths):
            for path, item in list(known.items()):
                if item[0] != "unit":
                    continue
                text = self.unit_texts.get(path)
                if (text is None and not rechecked) or (
                    text is not None
                    and series_unit_factor(BY_PATH[path], text, icom=icom) is not None
                ):
                    del known[path]

    def _data(self) -> dict[str, Any]:
        return {
            "series": {key: ledger.to_dict() for key, ledger in self.ledgers.items()},
            "local": self.local_paths,
            "cloud": self.cloud_paths,
            "published": self.published,
            "cloud_done": sorted(self.cloud_done),
            "last_local_ok": self.last_local_ok,
            "units": dict(sorted(self.unit_texts.items())),
            "units_rechecked": True,
        }

    def _save(self) -> None:
        if self._store is not None:
            self._store.async_delay_save(self._data, 10)

    # ------------------------------------------------------------- demand
    @callback
    def request(self, key: str) -> Callable[[], None]:
        """An enabled entity needs its value; returns its release.

        An entity without a value yet gets it from what is stored, without
        a read and without waiting for the next hourly run.
        """
        self._requested[key] = self._requested.get(key, 0) + 1
        if key not in self.published:
            self._schedule_values()

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

    def hourly_series(self) -> frozenset[str]:
        """The series read every hour (V5): every series while the import is
        on; otherwise those of enabled entities (their recorded method) and,
        while another feature reads the recordings, the energy series."""
        if import_enabled(self.entry):
            return frozenset(BY_KEY)
        keys: set[str] = set()
        if self._consumers > 0:
            keys.update(series.key for series in SERIES if series.cloud)
            # A hybrid system's heat pump (only read where ``gating`` allows).
            keys.update(series.key for series in HEAT_SOURCE_SERIES)
        wanted = set(self._requested)
        for spec in self.specs():
            if spec.key in wanted and (spec.primary or spec.method == RECORDING):
                keys.update(spec.measure.parts)
        return frozenset(keys)

    @callback
    def _schedule_values(self) -> None:
        if self._values_unsub is not None or self.stopped or not self.loaded:
            return
        self._values_unsub = async_call_later(
            self.hass,
            VALUES_DELAY,
            HassJob(self._due_values, "recordings values", cancel_on_shutdown=True),
        )

    @callback
    def _due_values(self, _now: datetime) -> None:
        self._values_unsub = None
        if self.stopped:
            return
        if self.running:
            # The run publishes too; anything added after it, afterwards.
            self._values_after_run = True
            return
        self.entry.async_create_background_task(
            self.hass, self.async_refresh_values(), "bosch_buderus_heating_recordings"
        )

    async def async_refresh_values(self) -> None:
        """Work out the requested values from the stored series, no read."""
        if self.stopped or not self.loaded:
            return
        now = self._now()
        zone = dt_util.get_default_time_zone()
        try:
            await self._values(now, zone)
        except Exception as err:
            log_failure(
                _LOGGER,
                logging.WARNING,
                "Working out the recordings' values failed",
                error=err,
            )
        self._save()
        self._notify()

    async def _values(self, now: datetime, zone: tzinfo) -> None:
        async with self._values_lock:
            await self._count(now, zone)
            self._publish_all(now, zone)

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
    def _heat_source_series_known(self) -> bool:
        """Whether a series of the heat source has answered before.

        The plant's readings may be missing right after a start (cloud not
        reachable, no local reading yet); stored answers anchor the run then.
        """
        return any(
            row
            and row[0] == "ok"
            and not path.startswith(SOFTENER_CIRCUIT)
            and not (path in BY_PATH and BY_PATH[path].diagnostic)
            for known in (self.local_paths, self.cloud_paths)
            for path, row in known.items()
        )

    def available(self) -> tuple[str, ...]:
        """Series that answered, by key."""
        keys = []
        for series in ALL_SERIES:
            if series.key not in self.ledgers:
                continue
            local = self.local_paths.get(series.path)
            cloud = self.cloud_paths.get(series.path)
            # A path failing after it answered keeps what was read.
            if (local and local[0] in ("ok", "failed")) or (cloud and cloud[0] == "ok"):
                keys.append(series.key)
        return tuple(keys)

    def heat_source_answered(self, source: str) -> bool:
        """Whether a series of the heat source `source` has answered."""
        answered = set(self.available())
        return any(
            series.heat_source == source and series.key in answered
            for series in ALL_SERIES
        )

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
        coordinator = self.coordinator()
        heat = (
            coordinator is None
            or has_recording_anchor(coordinator)
            or self._heat_source_series_known()
        )
        if not heat and not softener_plant(coordinator):
            # A room controller with a bare heat source (no counters, no
            # starts, no heat pump): nothing the recordings belong to.
            self.waiting = "no_heat_source_anchor"
            return
        self.running = True
        if icom_gateway(coordinator):
            # kW of the energy monitoring is taken behind an iCom gateway only.
            self._recheck_units(icom=True)
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
            # A plant with a softener circuit only (no heat source): only
            # the series without a local path are read, from the cloud.
            for driver in self._local_drivers() if heat else []:
                done = await self._run_local(driver, now, zone, report)
                if done:
                    report["source"] = LOCAL
                    break
            if not done:
                if await self._run_cloud(now, zone, report, reason, exclusive=not heat):
                    report["source"] = CLOUD
            elif self._cloud_candidates(now, exclusive=True):
                # The local access has no path of these series.
                await self._run_cloud(now, zone, report, reason, exclusive=True)
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
            await self._values(now, zone)
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
            if self._values_after_run:
                self._values_after_run = False
                self._schedule_values()
            if (
                reason == "start"
                and report["source"] == LOCAL
                and self._unsubs
                and not self.stopped
            ):
                # Locally only: the cloud's reads stay spread over hours.
                self._schedule_confirm()

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
        """Read locally; True when the local access handled the run (it
        answered, even with nothing to read), so the cloud is not asked."""
        stale = (
            self.last_local_ok is None
            or now.timestamp() - self.last_local_ok > LOCAL_FULL_AFTER.total_seconds()
        )
        if stale:
            self._full_read.clear()  # the whole history again
        today = now.astimezone(zone).date()
        wanted = self.hourly_series()
        local_series = self._local_series()
        todo: list[tuple[Series, bool]] = []
        for series in local_series:
            path = series.path
            if (LOCAL, path) in self._forbidden:
                continue
            known = self.local_paths.get(path)
            state = known[0] if known else None
            if state == "unit":
                continue
            if (
                known is not None
                and state in ("missing", "failed")
                and now.timestamp() - float(known[1]) < RECHECK_MISSING.total_seconds()
            ):
                continue
            history = path not in self._full_read
            if state == "ok" and not history and series.key not in wanted:
                continue  # V5: hourly only what something needs
            # Not read for a while (not needed until now): its history again.
            outdated = (
                known is not None
                and now.timestamp() - float(known[1]) > LOCAL_FULL_AFTER.total_seconds()
            )
            whole = (history or state != "ok" or outdated) and path not in self._slow
            todo.append((series, whole))
        succeeded = False
        reachable = True
        busy: list[tuple[Series, bool]] = []
        for attempt in range(BUSY_RETRIES + 1):
            if attempt:
                # N3: asked again within the run, so the first run already
                # knows every series and their entities.
                await asyncio.sleep(BUSY_RETRY_SECONDS)
                if self.stopped:
                    break
            busy = []
            for series, whole in todo:
                answer = await read_local(driver, series, local_query(whole, today))
                report["local_reads"] += 1
                self._count_outcome(report, LOCAL, answer.outcome)
                outcome = answer.outcome
                if outcome is Outcome.BUSY:
                    busy.append((series, whole))
                    continue
                if outcome is Outcome.TIMEOUT:
                    # A slow gateway: this path is read by its last two days
                    # from now on; timing out even then, it is failing.
                    if whole:
                        self._slow.add(series.path)
                    else:
                        self._local_failed(series, now)
                if outcome is Outcome.SERVER_ERROR:
                    # The local access is paused: this path is failing.
                    self._local_failed(series, now)
                if outcome in (
                    Outcome.UNREACHABLE,
                    Outcome.TIMEOUT,
                    Outcome.SERVER_ERROR,
                ):
                    reachable = False
                    break
                result = self._take_local(series, answer, whole, now, zone)
                succeeded = succeeded or result
            if not reachable or not busy:
                break
            todo = busy
        if busy and reachable:
            report["local_busy_left"] = len(busy)
        if succeeded:
            self.last_local_ok = now.timestamp()
        self._full_done = bool(self._full_read) and not self._history_pending(
            local_series
        )
        return succeeded or (reachable and not busy)

    def _local_series(self) -> tuple[Series, ...]:
        """The local paths of a run: the catalogue's and, on a hybrid system,
        the heat pump's own (``gating``)."""
        pump = heat_pump_keys(self.coordinator())
        if not pump:
            return SERIES
        return (*SERIES, *(item for item in HEAT_SOURCE_SERIES if item.key in pump))

    def _history_pending(self, local_series: tuple[Series, ...] = SERIES) -> bool:
        """A local path still to be read with its whole history: one that
        answered (or never did) and is neither failing nor slow."""
        for series in local_series:
            path = series.path
            known = self.local_paths.get(path)
            if (
                (LOCAL, path) in self._forbidden
                or path in self._full_read
                or path in self._slow
                or (known is not None and known[0] != "ok")
            ):
                continue
            return True
        return False

    def _take_local(
        self, series: Series, answer: Any, whole: bool, now: datetime, zone: tzinfo
    ) -> bool:
        """Keep one local answer: True for a kept recording, False for an
        answer about the path (404, 403, unit, a failed read)."""
        path = series.path
        if answer.outcome is Outcome.OK:
            read_at = self._now()
            parsed = parse_local(answer.data, series, zone)
            if parsed is None:
                self.local_paths[path] = ["unit", read_at.timestamp()]
                self._note_unit(path, getattr(answer.data, "unit", None))
                return False
            self._ledger(series).merge(
                parsed, LOCAL, read_at=read_at, zone=zone, full_history=whole
            )
            if parsed.offset is not None:
                self.offset_mismatch = (
                    parsed.offset != read_at.astimezone(zone).utcoffset()
                )
            self.local_paths[path] = ["ok", read_at.timestamp()]
            self._local_failures.pop(path, None)
            if whole:
                self._full_read.add(path)
            return True
        if answer.outcome is Outcome.NOT_FOUND:
            self.local_paths[path] = ["missing", now.timestamp()]
        elif answer.outcome is Outcome.FORBIDDEN:
            self._forbidden.add((LOCAL, path))
        else:
            self._local_failed(series, now)
        return False

    def _local_failed(self, series: Series, now: datetime) -> None:
        """A failed read: tried again once a day; a path that answered
        before only after ``LOCAL_FAILURES`` failures in a row."""
        path = series.path
        known = self.local_paths.get(path)
        count = self._local_failures.get(path, 0) + 1
        if known is None or known[0] != "ok" or count >= LOCAL_FAILURES:
            self.local_paths[path] = ["failed", now.timestamp()]
            self._local_failures.pop(path, None)
        else:
            self._local_failures[path] = count

    # --------------------------------------------------------------- cloud
    def running_series(self) -> frozenset[str]:
        """The series whose today is needed: every series while the import
        is on, otherwise those of enabled entities of a running period
        (current month, current year)."""
        if import_enabled(self.entry):
            return frozenset(BY_KEY)
        keys: set[str] = set()
        wanted = set(self._requested)
        for spec in self.specs():
            if (
                spec.key in wanted
                and not spec.period.past
                and (spec.primary or spec.method == RECORDING)
            ):
                keys.update(spec.measure.parts)
        return frozenset(keys)

    def _cloud_candidates(self, now: datetime, exclusive: bool = False) -> list[Series]:
        """The series the cloud is asked for: the energy paths of a heat
        pump (not when ``exclusive``) and the series without a local path
        of a plant that shows what they belong to. A hybrid system's heat
        pump's own series come first (the gateway's count the boiler there),
        and not while the local access answers them."""
        coordinator = self.coordinator()
        allowed = allowed_keys(coordinator)
        pump = heat_pump_keys(coordinator)
        ordered = (
            (
                *HEAT_SOURCE_SERIES,
                *(item for item in ALL_SERIES if not item.heat_source),
            )
            if pump
            else ALL_SERIES
        )
        return [
            series
            for series in ordered
            if (
                (
                    series.key in pump
                    and not (
                        exclusive
                        and (self.local_paths.get(series.path) or [None])[0] == "ok"
                    )
                )
                if series.heat_source is not None
                else ((series.cloud and not exclusive) or series.key in allowed)
            )
            and (CLOUD, series.path) not in self._forbidden
            and (self.cloud_paths.get(series.path) or [None])[0] != "unit"
            # A series for the diagnostics only: until it answered once.
            and not (
                series.diagnostic
                and (self.cloud_paths.get(series.path) or [None])[0] == "ok"
            )
            and self._cloud_deferred.get(series.path, 0.0) <= now.timestamp()
        ]

    def _current_tasks(
        self, today: date, now: datetime, exclusive: bool = False
    ) -> list[tuple[Series, str, Rate]]:
        """Yesterday for the series something needs (and once for a path
        that never answered, to find the series), today for the running
        periods."""
        needed = self.hourly_series()
        running = self.running_series()
        yesterday = today - timedelta(days=1)
        tasks: list[tuple[Series, str, Rate]] = []
        for series in self._cloud_candidates(now, exclusive):
            if series.diagnostic:
                # Its state and unit only: one day, until it answers.
                tasks.append((series, yesterday.isoformat(), Rate.HOUR))
                continue
            if series.key in running:
                tasks.append((series, today.isoformat(), Rate.HOUR))
            if series.key in needed or series.path not in self.cloud_paths:
                tasks.append((series, yesterday.isoformat(), Rate.HOUR))
        return tasks

    def _later_tasks(
        self, today: date, now: datetime, exclusive: bool = False
    ) -> list[tuple[Series, str, Rate]]:
        """The back-fill of the series something needs: months, years,
        older months by the day and, for the import, older days by the
        hour; a past interval whose slots are all final is not read again."""
        needed = self.hourly_series()
        hours = import_enabled(self.entry)
        later: list[tuple[Series, str, Rate]] = []
        first = month_start(today)
        for series in self._cloud_candidates(now, exclusive):
            if series.key not in needed or series.diagnostic:
                continue
            for month in (first, add_months(first, -1)):
                later.append((series, f"{month.year:04d}-{month.month:02d}", Rate.DAY))
            for year in (today.year, today.year - 1):
                later.append((series, f"{year:04d}", Rate.MONTH))
            if hours:
                for back in range(2, CLOUD_HOUR_DAYS + 1):
                    later.append(
                        (series, (today - timedelta(days=back)).isoformat(), Rate.HOUR)
                    )
            ledger = self.ledgers.get(series.key)
            if ledger is not None:
                for key, cell in sorted(ledger.months.items(), reverse=True):
                    if cell[1] > 0 and key[:7] < f"{add_months(first, -1):%Y-%m}":
                        later.append((series, key[:7], Rate.DAY))
        return [
            task for task in later if f"{task[0].path}|{task[1]}" not in self.cloud_done
        ]

    def _cloud_tasks(
        self, today: date, now: datetime | None = None
    ) -> list[tuple[Series, str, Rate]]:
        """Every cloud task of a run: the current ones, then the back-fill."""
        moment = now or self._now()
        return [*self._current_tasks(today, moment), *self._later_tasks(today, moment)]

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
            # Every day needs a final value (a gap counts): an answer with
            # the wrong number of slots left none, so it is read again.
            day = first
            while day < add_months(first, 1):
                cell = ledger.day(day)
                if cell is None or not cell[4]:
                    return False
                day += timedelta(days=1)
            return True
        year = int(interval)
        if year >= today.year:
            return False
        months = [ledger.month(date(year, month, 1)) for month in range(1, 13)]
        return all(cell is not None and bool(cell[4]) for cell in months)

    def _every_run(self, series: Series, interval: str, today: date) -> bool:
        """Today is read every run, yesterday until its day is final; then
        like an older day at most every ``CLOUD_REREAD``."""
        if interval == today.isoformat():
            return True
        yesterday = today - timedelta(days=1)
        if interval != yesterday.isoformat():
            return False
        ledger = self.ledgers.get(series.key)
        cell = ledger.day(yesterday) if ledger is not None else None
        return cell is None or not cell[4]

    def _backfill_allowed(self, reason: str, today: date) -> bool:
        """Older intervals only in hourly runs, after a cloud run without a
        limit, and not on the day of a rate limit."""
        return (
            reason == "hourly"
            and self._cloud_runs_ok > 0
            and self._cloud_limited != today
        )

    async def _run_cloud(
        self,
        now: datetime,
        zone: tzinfo,
        report: dict[str, Any],
        reason: str = "hourly",
        exclusive: bool = False,
    ) -> bool:
        coordinator = self.coordinator()
        if coordinator is None or coordinator.cloud_unavailable():
            report["cloud"] = "unavailable"
            return False
        if not exclusive and not any(
            path.startswith("/heatSources") for path in coordinator.resources
        ):
            # No heat source at all (a room controller, a water softener).
            report["cloud"] = "no_heat_source"
            return False
        client = coordinator.client
        icom = icom_gateway(coordinator)
        today = now.astimezone(zone).date()
        tasks = [(*task, False) for task in self._current_tasks(today, now, exclusive)]
        if self._backfill_allowed(reason, today):
            tasks.extend(
                (*task, True) for task in self._later_tasks(today, now, exclusive)
            )
        budget = CLOUD_BUDGET
        backfill = CLOUD_BACKFILL
        succeeded = False
        limited = False
        failed: list[str] = []
        for series, interval, rate, older in tasks:
            known = self.cloud_paths.get(series.path)
            if (CLOUD, series.path) in self._forbidden or series.path in failed:
                continue
            task = f"{series.path}|{interval}"
            last = self._cloud_read_at.get(task)
            if (
                not self._every_run(series, interval, today)
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
            if older and backfill <= 0:
                report["cloud_backfill_left"] = True
                continue
            if budget <= 0:
                report["cloud_budget_used"] = True
                break
            if report["cloud_reads"] and coordinator.cloud_unavailable():
                # Paused or limited meanwhile (the polling's reads).
                report["cloud"] = "unavailable"
                break
            budget -= 1
            if older:
                backfill -= 1
            answer = await read_cloud(
                client, coordinator.gateway.gateway_id, series, interval
            )
            report["cloud_reads"] += 1
            self._cloud_read_at[task] = now.timestamp()
            outcome = answer.outcome
            if outcome is Outcome.OK and not is_cloud_recording(answer.data):
                outcome = Outcome.FAILED  # no recording at all: no unit issue
            self._count_outcome(report, CLOUD, outcome)
            if outcome is Outcome.OK:
                read_at = self._now()
                parsed = parse_cloud(
                    answer.data, series, interval, zone, read_at, icom=icom
                )
                if parsed is None:
                    self.cloud_paths[series.path] = ["unit", read_at.timestamp()]
                    self._note_unit(series.path, cloud_unit(answer.data))
                    continue
                if series.diagnostic:
                    # Read for its state and unit only: no values are kept.
                    self.cloud_paths[series.path] = ["ok", read_at.timestamp()]
                    self._note_unit(series.path, cloud_unit(answer.data))
                    continue
                self._ledger(series).merge(parsed, CLOUD, read_at=read_at, zone=zone)
                if series.minute_sums:
                    # The unit it was taken in, for the diagnostics.
                    self._note_unit(series.path, cloud_unit(answer.data))
                self.cloud_paths[series.path] = ["ok", read_at.timestamp()]
                succeeded = True
                if self._cloud_complete(series, interval, rate, today, zone):
                    self.cloud_done.add(task)
            elif outcome is Outcome.NOT_FOUND:
                self.cloud_paths[series.path] = ["missing", now.timestamp()]
            elif outcome is Outcome.FORBIDDEN:
                self._forbidden.add((CLOUD, series.path))
            elif outcome is Outcome.FAILED:
                # This path only; the others go on, a few failures end it.
                failed.append(series.path)
                if len(failed) >= CLOUD_FAILURES_PER_RUN:
                    break
            else:
                # A rate limit or no connection: the next run goes on.
                limited = outcome is Outcome.RATE_LIMITED
                if limited:
                    self._cloud_limited = today
                break
        if failed and succeeded:
            # Failing while the others answered: left out for a while. When
            # nothing answered, the cloud itself failed; no path is left out.
            until = now.timestamp() + CLOUD_FAIL_PAUSE.total_seconds()
            for path in failed:
                self._cloud_deferred[path] = until
            report["cloud_paths_left_out"] = len(failed)
        if succeeded and not limited:
            self._cloud_runs_ok += 1
        limit = f"{today.year - 2:04d}"
        self.cloud_done = {
            item for item in self.cloud_done if item.rpartition("|")[2] >= limit
        }
        return succeeded

    def _note_unit(self, path: str, unit: object) -> None:
        """Keep the unit a recording answered in, as a safe text."""
        text = _unit_text(unit)
        if text is not None:
            self.unit_texts[path] = text

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

        self.time_zone_on_the_hour = on_the_hour(now, zone)
        if not self.time_zone_on_the_hour:
            # The recorder takes rows on the full UTC hour only; local hours
            # of a zone with a half-hour offset never are.
            report["import"] = "time_zone_not_on_the_hour"
            return
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
                if any(row[0] % HOUR for row in plan.rows):
                    # A plant clock off the full hour: nothing is written.
                    report["import_rows_not_on_the_hour"] = (
                        report.get("import_rows_not_on_the_hour", 0) + 1
                    )
                    continue
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
        known = sorted(statistic_id(self.digest, series) for series in ALL_SERIES)
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
            if kind == "change" and spec.period is not Period.YESTERDAY:
                await self._async_counter_start(
                    instance, entity_id, measure, first, after, zone, now
                )
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

    async def _async_counter_start(
        self,
        instance: Any,
        entity_id: str,
        measure: Measure,
        first: date,
        after: date,
        zone: tzinfo,
        now: datetime,
    ) -> None:
        """The first day of a counter's statistics within a period (read
        once per period); only a later start than the period's is kept."""
        if (measure.key, first) in self._counter_first:
            return
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )

        try:
            rows = await instance.async_add_executor_job(
                statistics_during_period,
                self.hass,
                local_midnight(first, zone).astimezone(UTC),
                min(local_midnight(after, zone).astimezone(UTC), now),
                {entity_id},
                "day",
                _UNITS[measure.quantity],
                {"change"},
            )
        except Exception as err:  # the attribute stays out
            log_failure(
                _LOGGER,
                logging.DEBUG,
                "A counter statistic could not be read",
                error=err,
            )
            return
        days = [
            datetime.fromtimestamp(row["start"], zone).date()
            for row in rows.get(entity_id, [])
            if isinstance(row.get("start"), int | float)
            and isinstance(row.get("change"), int | float)
        ]
        begin = min(days, default=None)
        if begin is None:
            # No row: nothing to say, and no answer kept either.
            return
        self._counter_first[(measure.key, first)] = begin if begin > first else None

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
        # A recorded gap (rule 7) falls back to the next source; with none,
        # the gap itself (no value).
        found = [item for item in chain if item is not None]
        return next(
            (item for item in found if not item.gap), found[0] if found else None
        )

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
                # A gap: no value rather than the last one or a zero.
                self.published.pop(spec.key, None)
                continue
            item: list[Any] = [
                candidate.start.isoformat(),
                candidate.end.isoformat(),
                round(value, 4),
                _source_text(candidate.sources),
                candidate.resolution,
            ]
            history = (
                self._counter_first.get((spec.measure.key, candidate.start))
                if candidate.sources == {"counters"}
                else None
            )
            if history is not None:
                item.append(history.isoformat())
            self.published[spec.key] = item

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
                date.fromisoformat(item[5]) if len(item) > 5 else None,
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
        for item in ALL_SERIES:
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
            if item.path in self.unit_texts:
                # The unit of an answer not taken, of a diagnostic series, or
                # of the last answer taken of a series of mean samples.
                report["unit"] = self.unit_texts[item.path]
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
        found: dict[str, object] = {}
        source = heat_pump_source(self.coordinator())
        if source is not None:
            # A hybrid system: the heat pump's own series are read too.
            found["heat_pump_source"] = source
            found["heat_pump_source_basis"] = heat_pump_source_basis(self.coordinator())
        if icom_heat_pump(self.coordinator()):
            # A heat pump behind an iCom gateway: its own energy series.
            found["icom_heat_pump"] = True
        return found | {
            "confirmation": "unconfirmed",
            "import_enabled": import_enabled(self.entry),
            "recorder_available": self.recorder_ready(),
            "needed": self.needed(),
            "waiting": self.waiting,
            "plant_offset_differs_from_time_zone": self.offset_mismatch,
            # False: a time zone off the full hour, the import waits.
            "time_zone_on_the_hour": self.time_zone_on_the_hour,
            "local_paths_read_by_days_only": len(self._slow),
            "cloud_paths_left_out": sum(
                1 for until in self._cloud_deferred.values() if until > now.timestamp()
            ),
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


def _unit_text(value: object) -> str | None:
    """The unit an answer named, as it was written (at most 16 characters);
    None without one or with a character that cannot be printed."""
    if not isinstance(value, str) or not value or not value.isprintable():
        return None
    return value[:UNIT_TEXT_LENGTH]


def _paths(value: object) -> dict[str, list[Any]]:
    if not isinstance(value, dict):
        return {}
    return {
        key: item
        for key, item in value.items()
        if key in BY_PATH
        and isinstance(item, list)
        and len(item) == 2
        and item[0] in ("ok", "missing", "unit", "failed")
        and isinstance(item[1], int | float)
    }


def on_the_hour(now: datetime, zone: tzinfo) -> bool:
    """Whether local midnight and the hours of ``zone`` fall on full UTC
    hours (not in zones with a half-hour or 45-minute offset)."""
    today = now.astimezone(zone).date()
    return all(
        (offset := moment.utcoffset()) is not None
        and offset.total_seconds() % HOUR == 0
        for moment in (now.astimezone(zone), local_midnight(today, zone))
    )


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


async def async_imported_statistics(hass: HomeAssistant, digest: str) -> list[str]:
    """The imported statistics of a gateway that the recorder has."""
    from homeassistant.components.recorder.statistics import (
        async_list_statistic_ids,
    )

    known = {statistic_id(digest, series) for series in ALL_SERIES}
    listed = await async_list_statistic_ids(hass)
    return sorted(
        str(item["statistic_id"])
        for item in listed
        if item.get("statistic_id") in known
    )


async def async_offer_statistics_removal(hass: HomeAssistant, gateway_id: str) -> None:
    """After the last entry of a gateway was removed: offer to delete its
    imported statistics (a repair, until the next restart), as the options
    that delete them are gone with the entry."""
    if "recorder" not in hass.config.components:
        return
    digest = await async_gateway_digest(hass, gateway_id)
    found = await async_imported_statistics(hass, digest)
    if not found:
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        statistics_issue_id(digest),
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="recordings_statistics_left",
        translation_placeholders={"count": str(len(found))},
        data={"digest": digest},
    )


async def async_remove_recordings_store(
    hass: HomeAssistant, entry_id: str, gateway_id: str
) -> None:
    """Delete the stored recordings of a removed entry's gateway. Imported
    statistics stay, as Home Assistant keeps them (options: delete; after
    the removal a repair offers it)."""
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
