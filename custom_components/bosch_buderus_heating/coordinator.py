"""PointT polling coordinator for one heating gateway."""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections import Counter, deque
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Mapping,
)
from contextlib import ExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, tzinfo
from enum import StrEnum
from itertools import islice
from time import monotonic
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DOMAIN, RATE_LIMIT_ISSUE_PREFIX, PollingProfile
from .discovery import (
    MAX_DISCOVERY_RESOURCES,
    ROOT_RESOURCE_PATHS,
    DiscoveryDiagnostics,
    DiscoveryPathSource,
    async_discover_resources,
)
from .discovery_inventory import (
    RECOVERY_SCHEDULE,
    DiscoveryInventory,
    DiscoveryRecovery,
    is_transient_result,
)
from .energy_contracts import assess_energy, is_energy_collection
from .faults import (
    FaultTracker,
    discovery_fully_answered,
    fault_resource_candidates,
    is_fault_resource_path,
)
from .firmware_watch import FirmwareWatch, firmware_values
from .holiday_timezones import async_resolve_holiday_timezones
from .holiday_writes import (
    HolidayAlreadyExists,
    HolidayWriteNotConfirmed,
    HolidayWriteService,
    holiday_resources_from_snapshots,
)
from .holidays import (
    HOLIDAY_CONFIGURATION_PATH,
    HOLIDAY_CREATE_PATH,
    HOLIDAY_LIST_PATH,
    HolidayWriteValues,
    parse_holiday_write_configuration,
)
from .legacy_holiday_writes import IcomHolidayWriteService
from .legacy_holidays import is_icom_slot
from .plant_model import PlantModel
from .pointt import (
    AuthenticationError,
    Gateway,
    PointTClient,
    PointTError,
    RateLimited,
    Resource,
    ResourceError,
    ResourceOmitted,
    WriteNotConfirmed,
    WriteRejected,
    WriteValidationError,
)
from .pointt.bulk import chunk_resource_paths
from .pointt.const import MAX_BULK_PATHS
from .pointt.exceptions import (
    ProtocolError,
    RequestTimeout,
    ServiceUnavailable,
    SourceStopped,
    TransportError,
    UnexpectedHttpStatus,
)
from .pointt.models import BatchItemResult
from .pointt.redaction import resource_path_template
from .pointt.retry_after import MAX_CLOUD_PAUSE_SECONDS
from .poll_slots import plan_poll_slots
from .refresh_outage import RefreshOutageLog
from .repair_scope import repair_data, repair_id
from .resource_catalog import PollGroup, poll_group, slows_when_unused
from .rrc import PROGRAM_LIST, program_list_expected
from .rrc_programs import WEEK, RrcProgramService
from .start_counters import is_start_counter_path
from .startup_data import (
    LICENSE_PATH,
    STARTUP_BUDGET_SECONDS,
    StartupSession,
    is_startup_path,
)
from .startup_reader import StartupReader
from .update_load import update_load
from .writes import EnumWritePolicy, NumberWritePolicy, WriteBusy, WriteService

if TYPE_CHECKING:
    from .counter_selection import CounterGuard
    from .local_selection import LocalSelection

_LOGGER = logging.getLogger(__name__)

POLL_INTERVALS: dict[PollGroup, timedelta] = {
    PollGroup.FAST: timedelta(seconds=60),
    PollGroup.NOTIFICATIONS: timedelta(minutes=5),
    PollGroup.CONTROL: timedelta(minutes=5),
    PollGroup.ENERGY: timedelta(minutes=5),
    PollGroup.SLOW: timedelta(minutes=15),
    PollGroup.HOURLY: timedelta(hours=1),
}
POLL_INTERVALS_CLOUD_FRIENDLY: dict[PollGroup, timedelta] = {
    PollGroup.FAST: timedelta(minutes=2),
    PollGroup.NOTIFICATIONS: timedelta(minutes=5),
    PollGroup.CONTROL: timedelta(minutes=10),
    PollGroup.ENERGY: timedelta(minutes=10),
    PollGroup.SLOW: timedelta(minutes=30),
    PollGroup.HOURLY: timedelta(hours=1),
}
# A cloud reading stays valid for three polling intervals of its group, the
# same rule as for local readings. Static resources are read once and do not
# expire while the connection exists.
VALIDITY_INTERVALS = 3
# Cloud cycle durations kept for the 95th percentile in diagnostics.
RECENT_POLL_DURATIONS = 200
# A wall clock set back by more than this no longer confirms stored readings.
CLOCK_SKEW_TOLERANCE = timedelta(seconds=60)
VALIDITY_TIMER_MARGIN = timedelta(seconds=1)
EXPIRED_CATEGORY = "expired"
SOURCE_STOPPED_CATEGORY = "source_stopped"
# A counter whose only valid source would count backwards is not shown.
COUNTER_HELD_CATEGORY = "counter_held"
# Counters switch to the local access with the option off only for a durable
# outage: an unreachable cloud counts after this many seconds of failed cycles.
COUNTER_OUTAGE_SECONDS = 15 * 60
NO_ANSWER_PREFIX = "no_answer_"
# Categories of failures that are no answer for the reading itself: the last
# good value stays displayed within its validity limit. Explicit answers for
# the reading (403/404/406 and other client errors, invalid data, unconfirmed
# writes, a stopped source) end it until the next successful read.
_TRANSIENT_FAILURES = frozenset({"poll_deferred", "http_401", "http_429"})


def _explicit_client_error(status: int | None) -> bool:
    """A client error for the reading, except authorization and rate limits."""
    return status is not None and 400 <= status < 500 and status not in (401, 429)


def _no_answer(result: BatchItemResult, *, whole_request: bool) -> bool:
    """Return whether a failed result says nothing about the reading itself."""
    error = result.error
    if _explicit_client_error(result.status):
        return False
    if whole_request or isinstance(
        error, (ResourceOmitted, TransportError, AuthenticationError)
    ):
        return True
    status = result.status
    return status is not None and (status in (401, 429) or 500 <= status <= 599)


def _transient_failure(category: str | None) -> bool:
    return category is not None and (
        category in _TRANSIENT_FAILURES
        or category.startswith(("http_5", NO_ANSWER_PREFIX))
    )


def _no_usable_item(results: tuple[BatchItemResult, ...]) -> bool:
    """Every item of an answered request failed with a server error."""
    return bool(results) and all(
        result.resource is None and result.status is not None and result.status >= 500
        for result in results
    )


def _gateway_unanswered(results: tuple[BatchItemResult, ...]) -> bool:
    """B02: the gateway itself did not answer, not one slow reading.

    Several items of one request, each with a server error: this says nothing
    about a single reading, so no reading is paused for it; the circuit
    breaker handles the outage. A request with a single item cannot tell the
    two apart and keeps the pause of that reading.
    """
    return len(results) > 1 and _no_usable_item(results)


class SourceOwnedCoordinator[DataT](DataUpdateCoordinator[DataT]):
    """B04: a coordinator that its managed source shuts down.

    Home Assistant keeps the shutdown of every coordinator built with a config
    entry until that entry unloads, so each discarded source attempt or view
    would stay in memory. Managed sources stop their coordinators themselves
    (``async_stop_source``); the entry is still available as ``config_entry``.
    """

    def __init__(
        self, *args: Any, config_entry: ConfigEntry[Any] | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, config_entry=None, **kwargs)
        self.config_entry = config_entry


class IdleAfterFailure(UpdateFailed):
    """B03: a cycle after a failed one that requested nothing.

    No answer confirmed the cloud, so the failure goes on: Home Assistant keeps
    the coordinator unsuccessful, and neither the failure counters nor the
    outage log count this cycle.
    """


DEFAULT_RATE_LIMIT_BACKOFF = timedelta(minutes=5)
MAX_RATE_LIMIT_BACKOFF = timedelta(seconds=MAX_CLOUD_PAUSE_SECONDS)
NOT_FOUND_PAUSE = timedelta(hours=24)


def _connection_problem(error: Exception) -> bool:
    """No answer at all: a timeout or a network failure without HTTP status."""
    return (
        isinstance(error, RequestTimeout)
        or type(error) is TransportError
        or (isinstance(error, ServiceUnavailable) and error.status is None)
    )


FORBIDDEN_PAUSE = timedelta(hours=24)
GATEWAY_TIMEOUT_PAUSE = timedelta(minutes=15)
FORBIDDEN_PAUSE_THRESHOLD = 2
CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_OPEN_INTERVAL = timedelta(minutes=5)
MAX_FALLBACK_PATHS = 5
RATE_LIMIT_REPAIR_THRESHOLD = 3
# COORD-5/CHAOS-5: the repair counts only rate limits the cloud itself
# answered within this window, not requests the shared pause held back.
RATE_LIMIT_REPAIR_WINDOW = timedelta(hours=24)
ACTIVE_NOTIFICATION_INTERVAL = timedelta(seconds=60)
# Short, bounded recovery of controls temporarily absent in automatic mode.
# Delays are incremental: at most three targeted reads per unavailability episode.
MANUAL_SETPOINT_RETRY_DELAYS = (3.0, 10.0, 20.0)
# C58: how long a write waits for a running poll or write of its gateway.
# Longer than a normal cycle with its 50 s budget, far shorter than a slow
# discovery or rescan; a write that cannot start sends nothing.
WRITE_LOCK_TIMEOUT = timedelta(seconds=60)
# HA rounds scheduled callbacks to whole seconds plus a fixed offset. A callback
# just before our exact deadline must not postpone a group by another minute.
# This tolerance applies to group cadence only, never to error/backoff deadlines.
POLL_DUE_TOLERANCE_SECONDS = 1.0
# P41: one polling cycle stops starting new requests after this time; the
# remaining readings keep their values (validity rules) and come first next.
CYCLE_BUDGET_SECONDS = 50.0
# O45: while a paired reading is shown from the local access, its cloud copy
# is read at this rate at most (a whole number of its group's intervals), and
# at once again at its normal rate when the cloud value is shown instead.
LOCAL_SERVED_INTERVAL = timedelta(minutes=15)
# Paired readings whose cloud copy also feeds a control or a calculated entity
# keep their normal cloud rate: the hot-water target and the summer/winter
# check before a heating change read the cloud copy, and so does the
# calculated pressure status.
_CLOUD_COPY_IN_USE = re.compile(
    r"^(?:/dhwCircuits/[^/]+/currentSetpoint"
    r"|/heatingCircuits/[^/]+/currentSuWiMode"
    r"|/heatSources/systemPressure)$"
)

_FALLBACK_PRIORITY_PATHS = (
    "/heatSources/actualSupplyTemperature",
    "/heatSources/returnTemperature",
    "/heatSources/actualModulation",
    "/heatSources/systemPressure",
    "/system/sensors/temperatures/outdoor_t1",
)


class SnapshotSource(StrEnum):
    """Origin of a resource's last confirmed value."""

    DISCOVERY = "discovery"
    BATCH = "batch"
    FALLBACK = "fallback"
    WRITE = "write"
    STARTUP = "startup"
    LOCAL = "local"


class Freshness(StrEnum):
    """Whether the most recent attempt confirmed the stored value."""

    FRESH = "fresh"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    """Last good resource value and the state of its most recent request."""

    resource: Resource
    available: bool
    last_success: datetime
    last_attempt: datetime | None = None
    source: SnapshotSource = SnapshotSource.DISCOVERY
    freshness: Freshness = Freshness.FRESH
    last_error_category: str | None = None
    consecutive_failures: int = 0
    # Set by an explicit answer for this reading (or an unconfirmed write) and
    # kept until the next successful read: later whole-request failures must
    # not bring the old value back.
    ended: bool = False

    @property
    def updated_at(self) -> datetime:
        """Return the last-success timestamp for compatibility."""
        return self.last_success


# P47: stored resources read successfully are confirmed at most daily.
INVENTORY_CONFIRM_SECONDS = 86400


def _snapshot_is_fresh(snapshot: ResourceSnapshot | None) -> bool:
    return (
        snapshot is not None
        and snapshot.available
        and snapshot.freshness is Freshness.FRESH
    )


def _pause_end(seconds: float) -> datetime:
    """End of a pause on a full minute, stable between state writes."""
    end = datetime.now(UTC) + timedelta(seconds=seconds)
    if end.second or end.microsecond:
        end = end.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return end


def _slower_rate(interval: timedelta) -> timedelta:
    """O45: the cloud rate of a locally shown reading of this group: the most
    whole intervals within ``LOCAL_SERVED_INTERVAL``, so reads stay in step
    with the group's own wake-ups."""
    return interval * max(1, LOCAL_SERVED_INTERVAL // interval)


def _slower_rate_applies(interval: timedelta) -> bool:
    """O45: only groups read more often than ``LOCAL_SERVED_INTERVAL`` (in
    whole intervals) get the slower rate; slower groups keep their own."""
    return interval * 2 <= LOCAL_SERVED_INTERVAL


def _manual_setpoint_mode_path(path: str) -> str | None:
    """Match only direct leaves of an actual heating circuit, not cooling paths."""
    parts = path.split("/")
    if (
        len(parts) == 4
        and parts[1] == "heatingCircuits"
        and parts[2]
        and parts[3] in ("operationMode", "manualRoomSetpoint")
    ):
        return path.rsplit("/", 1)[0] + "/operationMode"
    return None


def _manual_mode_active(mode_path: str, snapshots: dict[str, ResourceSnapshot]) -> bool:
    mode = snapshots.get(mode_path)
    return (
        mode is not None
        and _snapshot_is_fresh(mode)
        and mode.resource.value == "manual"
    )


@dataclass(slots=True)
class CapabilityMetrics:
    """Count attempts for one resource without retaining values."""

    attempts: int = 0
    successful: int = 0
    failed: int = 0
    # Created with the first attempt (O24): most paths share one result and
    # one source, and an unread path never needs counters at all.
    results: Counter[str] | None = None
    sources: Counter[str] | None = None
    last_result: str | None = None

    def record(self, *, result: str, source: SnapshotSource) -> None:
        """Record one resource-level attempt."""
        self.attempts += 1
        if self.sources is None:
            self.sources = Counter()
        if self.results is None:
            self.results = Counter()
        self.sources[source.value] += 1
        self.results[result] += 1
        self.last_result = result
        if result == "success":
            self.successful += 1
        else:
            self.failed += 1

    def snapshot(self) -> dict[str, object]:
        """Return bounded counters for diagnostics."""
        return {
            "attempts_total": self.attempts,
            "successful": self.successful,
            "failed": self.failed,
            "success_rate_percent": _percentage(self.successful, self.attempts),
            "results": dict(sorted((self.results or {}).items())),
            "attempts_by_source": dict(sorted((self.sources or {}).items())),
            "last_result": self.last_result,
        }


class BoschBuderusDataUpdateCoordinator(
    DataUpdateCoordinator[dict[str, ResourceSnapshot]]
):
    """Discover and refresh safe PointT resources for one gateway."""

    # P40: a stopped cloud source whose entities local readings keep showing.
    local_retained: bool = False
    # P49: age of the inventory a start used, for its announcement.
    startup_age_seconds: int | None = None
    # P49b: a view from local readings alone: never writes, never requests.
    read_only_view: bool = False
    # P49b step 7: the brand a local setup read, for a plant without a cloud.
    brand_hint: str | None = None

    def __init__(
        self,
        hass: HomeAssistant,
        client: PointTClient,
        gateway: Gateway,
        config_entry: ConfigEntry[Any],
        polling_profile: PollingProfile = PollingProfile.STANDARD,
        *,
        startup_session: StartupSession | None = None,
        connection_id: str | None = None,
        target_id: str | None = None,
    ) -> None:
        poll_intervals = (
            POLL_INTERVALS_CLOUD_FRIENDLY
            if polling_profile is PollingProfile.CLOUD_FRIENDLY
            else POLL_INTERVALS
        )
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}-{gateway.gateway_id[-4:]}",
            # Wake every minute so an active notification can use its promised
            # cadence even when all ordinary groups use the cloud-friendly
            # profile. A wake-up with no due group performs no cloud request.
            update_interval=min(
                min(poll_intervals.values()), ACTIVE_NOTIFICATION_INTERVAL
            ),
            config_entry=config_entry,
        )
        self.client = client
        self.connection_id = connection_id
        self.gateway = gateway
        self.plant_model = PlantModel.from_pointt_gateway(
            config_entry.entry_id, gateway, target_id=target_id
        )
        self.startup_session = startup_session or StartupSession((gateway.gateway_id,))
        self.startup_data = self.startup_session.gateway(gateway.gateway_id)
        self._startup_reader = StartupReader(
            client, gateway.gateway_id, self.startup_data, roots=ROOT_RESOURCE_PATHS
        )
        self._main_cycle_failed = False
        # #60: this cycle sent no cloud request (nothing was due).
        self._cycle_idle = False
        self._config_entry = config_entry
        self.polling_profile = polling_profile
        self._poll_intervals = poll_intervals
        self.resources: dict[str, Resource] = {}
        self.registered_start_counter_paths: set[str] = set()
        self.registered_start_counter_shapes: dict[str, set[bool]] = {}
        self._start_counter_recovery_limited = False
        self.holiday_timezones: dict[str, tzinfo | None] = {}
        self._paths_by_group: dict[PollGroup, tuple[str, ...]] = {}
        self._deferred_poll_paths: tuple[str, ...] = ()
        self._next_update: dict[PollGroup, float] = {}
        self._cloud_backoff_until = 0.0
        self._negative_until: dict[str, float] = {}
        self._gateway_failure_count = 0
        self._circuit_open_until = 0.0
        # O80: monotonic time the breaker first opened in the current outage;
        # None while closed. Only the first opening logs a warning.
        self._circuit_outage_since: float | None = None
        # O81: the failed batch request was reported once in this outage.
        self._batch_failure_logged = False
        # #60: failed refreshes are an error only once the outage lasts.
        self._refresh_outage = RefreshOutageLog(_LOGGER, self.name)
        self._update_lock = asyncio.Lock()
        self._manual_setpoint_tasks: dict[str, asyncio.Task[None]] = {}
        self._manual_setpoint_attempted: set[str] = set()
        self._manual_setpoint_candidates: dict[str, str] = {}
        self._manual_setpoint_recovery_closed = False
        self._polls_total = 0
        self._poll_failures = 0
        self._last_poll_duration_ms: float | None = None
        self._total_poll_duration_ms = 0.0
        self._maximum_poll_duration_ms = 0.0
        self._polls_longer_than_interval = 0
        self._cycles_over_budget = 0
        # O94: the latest cycle durations, for the 95th percentile.
        self._recent_poll_durations_ms: deque[float] = deque(
            maxlen=RECENT_POLL_DURATIONS
        )
        # Paths of a due group already read in an earlier, budget-limited
        # cycle -> monotonic read time: they are not read again until the
        # group completes (P41), while it keeps making progress (``_progress``).
        self._budget_progress: dict[PollGroup, dict[str, float]] = {}
        # O41: one wake-up per tick; paths of longer groups read a few ticks
        # before their group's deadline (their slot) wait here until it.
        self._tick_seconds = min(
            min(poll_intervals.values()), ACTIVE_NOTIFICATION_INTERVAL
        ).total_seconds()
        self._spread_progress: dict[PollGroup, dict[str, float]] = {}
        # COORD-3: paths of a group that a failed batch kept from completing,
        # read in that cycle -> monotonic read time. Each waits one interval
        # of its group, so a failing batch does not read the others each tick.
        self._interrupted_progress: dict[PollGroup, dict[str, float]] = {}
        # The paths of that failed batch, read again alone in later cycles.
        self._failing_batch_paths: frozenset[str] = frozenset()
        # SEC-2: the last explicit refresh that read every group, and how
        # many explicit refreshes read everything or only the controls.
        self._last_full_refresh: float | None = None
        self._explicit_refreshes_full = 0
        self._explicit_refreshes_control_only = 0
        self._poll_slots: dict[str, tuple[PollGroup, int]] = {}
        self._poll_slots_key: tuple[object, ...] = ()
        # O45: paired readings shown from the local access -> monotonic time
        # their cloud copy is read next; when they left that state (UTC) and
        # cloud copies to read in the next cycle after such a switch.
        self._local_served: dict[str, float] = {}
        self._served_left: dict[str, datetime] = {}
        self._urgent_reads: set[str] = set()
        # Readings kept after a firmware rediscovery; a 404 drops them.
        self._kept_after_firmware: set[str] = set()
        # Managed setups attach a per-target view; legacy setups keep None.
        self.local_selection: LocalSelection | None = None
        # Managed setups attach a guard: counters never count backwards when
        # their source changes. Views are decided once per update, not per read.
        self.counter_guard: CounterGuard | None = None
        self._counter_views: dict[str, ResourceSnapshot | None] = {}
        self._validity_unsub: CALLBACK_TYPE | None = None
        # The expiry the armed timer waits for; re-armed only when it changes.
        self._validity_due: datetime | None = None
        self._validity_index: dict[str, PollGroup] = {}
        # Holds the scheduled tuples themselves, so equal keys mean equal plans.
        self._validity_index_key: tuple[tuple[PollGroup, tuple[str, ...]], ...] = ()
        # Like HA's own refresh timer, the expiry timer ends when HA stops.
        self._validity_stop_unsub: CALLBACK_TYPE | None = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._validity_stopped
        )
        self._rate_limit_events = 0
        # Monotonic times of the rate limits within the repair window.
        self._rate_limit_times: deque[float] = deque()
        self._energy_counter_resets = 0
        # NRG-3: the produced heat falls while the heat pump defrosts; such a
        # decrease is counted apart from the counter resets.
        self._produced_heat_decreases = 0
        self._capability_metrics: dict[str, CapabilityMetrics] = {}
        self._unknown_enum_value_counts: Counter[str] = Counter()
        self._contract_changes: Counter[str] = Counter()
        self.discovery_diagnostics = DiscoveryDiagnostics()
        # P46: resources discovered before, and the search for missing ones.
        self.inventory: DiscoveryInventory | None = None
        self.discovery_recovery = DiscoveryRecovery()
        # P22: firmware versions seen and the discovery a change triggers.
        self.firmware = FirmwareWatch()
        self._discovery_cycle_failed = False
        # P47: started with the stored inventory while the cloud did not answer.
        self.startup_mode = "live"
        self._startup_reason: str | None = None
        self._inventory_age_seconds: int | None = None
        self._stored_resources: dict[str, Resource] = {}
        self._reconcile_due: float | None = None
        # A6: a discovery the owner asked for in the options dialog.
        self._rescan_due: float | None = None
        self.rescan_result: str | None = None
        self._reconcile_attempts = 0
        self._reconcile_postponed = 0
        self._reconcile_result: str | None = None
        self._assume_stored = False
        self._inventory_confirmed_at: float | None = None
        self._write_service = WriteService(client)
        self._rrc_program_service = RrcProgramService(client)
        self._holiday_write_service = HolidayWriteService(client)
        self._icom_holiday_write_service = IcomHolidayWriteService(client)
        self.faults = FaultTracker(hass, config_entry.entry_id, gateway.gateway_id)
        # O51: control paths whose entities are all disabled poll slowly.
        from .entity_use import entity_use

        self.entity_use = entity_use(hass, config_entry)
        self._remove_use_listener: CALLBACK_TYPE | None = self.entity_use.add_listener(
            self._entity_use_changed
        )

    def _poll_group(self, resource: Resource) -> PollGroup:
        """The group a resource is polled in (O51: a control path whose
        entities are all disabled uses the slow group)."""
        group = poll_group(resource)
        if (
            group is PollGroup.CONTROL
            and resource.path in self.entity_use.disabled_paths(self.gateway.gateway_id)
            and slows_when_unused(resource.path)
        ):
            return PollGroup.SLOW
        return group

    @callback
    def _entity_use_changed(self) -> None:
        """An entity was enabled or disabled: poll its paths at their pace.

        A path that moves to a group read more often (an entity was enabled
        again) and whose cloud copy is older than that group's interval is
        read at once, like a reading that switched back to its cloud value
        (O45), instead of waiting for the group's next deadline.
        """
        if not self.resources:
            return
        before = {
            path: group
            for group, paths in self._paths_by_group.items()
            for path in paths
        }
        self._rebuild_poll_groups()
        now = dt_util.utcnow()
        faster = False
        for group, paths in self._paths_by_group.items():
            interval = self._poll_intervals[group]
            for path in paths:
                previous = before.get(path)
                snapshot = (self.data or {}).get(path)
                if (
                    previous is None
                    or previous is group
                    or self._poll_intervals[previous] <= interval
                    or snapshot is None
                    or now - snapshot.last_success < interval
                ):
                    continue
                self._urgent_reads.add(path)
                faster = True
        if faster:
            self._request_switch_read()

    async def async_load_fault_state(self) -> None:
        """Load the persisted active-fault baseline before the first refresh."""
        await self.faults.async_load()

    def _request_reauthentication(self) -> None:
        """Managed sources override this to repair only their owning access."""
        self._config_entry.async_start_reauth(self.hass)

    async def async_request_refresh(self) -> None:
        """Force all dynamic groups due for an explicit Home Assistant refresh.

        SEC-2: a further explicit refresh within the shortest group interval
        of the profile reads only the controls, so an automation that asks
        every few seconds does not read the whole plant each time.
        """
        now_monotonic = monotonic()
        full = (
            self._last_full_refresh is None
            or now_monotonic - self._last_full_refresh >= self._explicit_refresh_gap()
        )
        groups = tuple(self._poll_intervals) if full else (PollGroup.CONTROL,)
        for group in groups:
            self._next_update[group] = min(
                self._next_update.get(group, now_monotonic), now_monotonic
            )
        if full:
            self._last_full_refresh = now_monotonic
            self._explicit_refreshes_full += 1
            # Everything is read again, also paths read early or at the
            # slower rate of locally shown readings.
            self._spread_progress.clear()
            self._interrupted_progress.clear()
            for path in self._local_served:
                self._local_served[path] = 0.0
        else:
            self._explicit_refreshes_control_only += 1
            self._spread_progress.pop(PollGroup.CONTROL, None)
            self._interrupted_progress.pop(PollGroup.CONTROL, None)
        await super().async_request_refresh()

    def _explicit_refresh_gap(self) -> float:
        """SEC-2: the minimum time between two full explicit refreshes."""
        return min(self._poll_intervals.values()).total_seconds()

    async def _async_request_switch_refresh(self) -> None:
        """O45: read switched readings now, without forcing any group."""
        await DataUpdateCoordinator.async_request_refresh(self)

    def _queue_manual_setpoint_recovery(
        self, mode_path: str, snapshots: dict[str, ResourceSnapshot]
    ) -> None:
        """Start one bounded series until the target or its mode recovers."""
        if (
            self._manual_setpoint_recovery_closed
            or _manual_setpoint_mode_path(mode_path) != mode_path
        ):
            return
        setpoint_path = mode_path.removesuffix("/operationMode") + "/manualRoomSetpoint"
        mode = snapshots.get(mode_path)
        target = snapshots.get(setpoint_path)
        if _snapshot_is_fresh(target) or (
            mode is not None
            and _snapshot_is_fresh(mode)
            and mode.resource.value != "manual"
        ):
            self._reset_manual_setpoint_recovery(setpoint_path)
            return
        if not _manual_mode_active(mode_path, snapshots):
            return
        if target is None:
            if not self.startup_data.reserve_discovery((setpoint_path,)):
                return
            # This exact path belongs to a reported, currently manual circuit.
            # Keep it eligible for normal group reads after the short series.
            self._manual_setpoint_candidates[setpoint_path] = mode_path
        if setpoint_path in self._manual_setpoint_attempted:
            return
        self._manual_setpoint_attempted.add(setpoint_path)
        self._manual_setpoint_tasks[setpoint_path] = (
            self.hass.async_create_background_task(
                self._async_recover_manual_setpoint(mode_path, setpoint_path),
                f"{DOMAIN} recover manual setpoint",
                eager_start=False,
            )
        )

    def _reset_manual_setpoint_recovery(self, setpoint_path: str) -> None:
        """End this episode without letting an old task remove its successor."""
        self._manual_setpoint_attempted.discard(setpoint_path)
        self._manual_setpoint_candidates.pop(setpoint_path, None)
        task = self._manual_setpoint_tasks.pop(setpoint_path, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _async_recover_manual_setpoint(
        self, mode_path: str, setpoint_path: str
    ) -> None:
        """Recheck only the missing value without bypassing cloud rate limits."""
        try:
            for delay in MANUAL_SETPOINT_RETRY_DELAYS:
                await asyncio.sleep(delay)
                async with self._update_lock:
                    snapshots = dict(self.data or {})
                    target = snapshots.get(setpoint_path)
                    if (
                        self._manual_setpoint_recovery_closed
                        or not _manual_mode_active(mode_path, snapshots)
                        or _snapshot_is_fresh(target)
                    ):
                        return
                    now = monotonic()
                    if (
                        now < self._cloud_backoff_until
                        or now < self._circuit_open_until
                        or self._shared_pause_remaining() > 0
                        or self._resource_is_paused(setpoint_path, now)
                    ):
                        return
                    try:
                        results = await self.client.get_resources_bulk(
                            self.gateway.gateway_id,
                            (setpoint_path,),
                            retryable=False,
                        )
                    except RateLimited as err:
                        self._record_capability(
                            setpoint_path, "rate_limited", SnapshotSource.BATCH
                        )
                        self._activate_rate_limit_backoff(err, monotonic())
                        return
                    except AuthenticationError:
                        self._record_capability(
                            setpoint_path, "authentication_error", SnapshotSource.BATCH
                        )
                        self._request_reauthentication()
                        return
                    except PointTError as err:
                        # Transport problems remain subject to normal polling.
                        self._record_capability(
                            setpoint_path, _pointt_result(err), SnapshotSource.BATCH
                        )
                        return
                    successes = self._apply_results(
                        snapshots,
                        results,
                        attempted_at=datetime.now(UTC),
                        now_monotonic=monotonic(),
                        source=SnapshotSource.BATCH,
                    )
                    # A targeted read must not clear an unrelated failed main
                    # cycle or reset the coordinator's normal polling timer.
                    self.data = snapshots
                    self.async_update_listeners()
                    for result in results:
                        if result.status == 429 or isinstance(
                            result.error, RateLimited
                        ):
                            error = (
                                result.error
                                if isinstance(result.error, RateLimited)
                                else RateLimited(retry_after=None)
                            )
                            self._activate_rate_limit_backoff(error, monotonic())
                            return
                    # AUTH-1: a 401 for this one item of an answered request
                    # says nothing about the sign-in. As in the regular poll it
                    # is a temporary failure of the reading, and the regular
                    # poll reads it again; only a whole request decides about
                    # a new sign-in (the AuthenticationError above).
                    if (
                        successes
                        or not results
                        or any(
                            result.status not in (403, 404, 406) for result in results
                        )
                    ):
                        return
        finally:
            if self._manual_setpoint_tasks.get(setpoint_path) is asyncio.current_task():
                self._manual_setpoint_tasks.pop(setpoint_path, None)

    def cancel_manual_setpoint_recovery(self) -> None:
        """Cancel outstanding recovery tasks when the integration is unloaded."""
        self._manual_setpoint_recovery_closed = True
        for task in self._manual_setpoint_tasks.values():
            task.cancel()
        self._manual_setpoint_tasks.clear()
        self._manual_setpoint_attempted.clear()
        self._manual_setpoint_candidates.clear()

    @asynccontextmanager
    async def _write_transaction(self) -> AsyncIterator[None]:
        """C58: hold the polling/write lock, but wait for it only so long.

        Only acquiring the lock is limited. Once a write holds it, nothing
        interrupts its PUT or read-back. A write that cannot start sends
        nothing and is reported at once instead of being sent much later.
        """
        try:
            async with asyncio.timeout(WRITE_LOCK_TIMEOUT.total_seconds()):
                await self._update_lock.acquire()
        except TimeoutError as err:
            raise WriteBusy(
                "Another request held the gateway; the change was not sent"
            ) from err
        try:
            yield
        finally:
            self._update_lock.release()

    async def _async_read_write_guards(
        self, expected: tuple[Resource, ...]
    ) -> dict[str, Resource]:
        """C35: read every resource a write depends on in one bulk request.

        Paths the bulk response does not answer are read individually, as
        before. Nothing here writes.
        """
        gateway_id = self.gateway.gateway_id
        paths = tuple(dict.fromkeys(before.path for before in expected))
        read: dict[str, Resource] = {}
        try:
            results: Iterable[BatchItemResult] = await self.client.get_resources_bulk(
                gateway_id, paths
            )
        except AuthenticationError, RateLimited, SourceStopped:
            raise
        except PointTError:
            results = ()
        for item in results:
            if item.status == 429 or isinstance(item.error, RateLimited):
                raise (
                    item.error
                    if isinstance(item.error, RateLimited)
                    else RateLimited(retry_after=None)
                )
            resource = item.resource
            if (
                item.ok
                and resource is not None
                and item.path in paths
                and resource.path == item.path
            ):
                read[item.path] = resource
        for path in paths:
            if path not in read:
                read[path] = await self.client.get_resource(gateway_id, path)
        return read

    def _write_failed(self, path: str, err: BaseException) -> str:
        """Record a failed write step and apply its brake or sign-in request."""
        # C32: a read-back failure after a sent PUT arrives as WriteNotConfirmed;
        # its cause still decides about the cloud brake and the sign-in.
        cause = err.__cause__ if isinstance(err, WriteNotConfirmed) else err
        category = (
            "control_write_unconfirmed"
            if isinstance(err, asyncio.CancelledError)
            else _pointt_result(err if isinstance(err, PointTError) else None)
        )
        self._record_capability(path, category, SnapshotSource.WRITE)
        if isinstance(cause, RateLimited):
            self._activate_rate_limit_backoff(cause, monotonic())
        if isinstance(cause, AuthenticationError):
            self._request_reauthentication()
        return category

    async def async_write_control(
        self,
        path: str,
        value: str | float,
        policy: EnumWritePolicy | NumberWritePolicy,
        *,
        expected: tuple[Resource, ...] = (),
    ) -> Resource:
        """Write one allowlisted control and publish its confirmed read-back."""
        async with self._write_transaction():
            now_monotonic = monotonic()
            if now_monotonic < self._cloud_backoff_until:
                raise RateLimited(self._cloud_backoff_until - now_monotonic)
            if now_monotonic < self._circuit_open_until:
                raise WriteValidationError(
                    "Gateway circuit breaker is active; writing is paused"
                )
            snapshot = (self.data or {}).get(path)
            if (
                snapshot is None
                or not snapshot.available
                or snapshot.freshness is Freshness.STALE
            ):
                raise WriteValidationError(
                    "Resource is unavailable or has no current metadata"
                )
            # A dialog draft and a climate setpoint may depend on other
            # resources. Recheck them under the polling/write lock and on
            # the gateway before writing, never silently change a mode.
            for before in expected:
                current = (self.data or {}).get(before.path)
                if (
                    not _snapshot_is_fresh(current)
                    or current is None
                    or current.resource != before
                ):
                    raise WriteValidationError("Control changed since it was shown")
            live: dict[str, Resource] = {}
            if expected:
                try:
                    live = await self._async_read_write_guards(expected)
                except PointTError as err:
                    # C33: nothing was written. Like any failed read, this
                    # leaves the readings as they are until the next poll.
                    for before in expected:
                        self._record_capability(
                            before.path, _pointt_result(err), SnapshotSource.BATCH
                        )
                    if isinstance(err, RateLimited):
                        self._activate_rate_limit_backoff(err, monotonic())
                    if isinstance(err, AuthenticationError):
                        self._request_reauthentication()
                    raise
                changed = dict(self.data or {})
                for before in expected:
                    refreshed = live[before.path]
                    if refreshed != before and refreshed.path == before.path:
                        changed[before.path] = ResourceSnapshot(
                            refreshed, True, datetime.now(UTC)
                        )
                        self.resources[before.path] = refreshed
                if any(live[before.path] != before for before in expected):
                    self.async_set_updated_data(changed)
                    raise WriteValidationError("Control changed on the gateway")
            try:
                if isinstance(policy, EnumWritePolicy) and isinstance(value, str):
                    result = await self._write_service.async_write_enum(
                        self.gateway.gateway_id,
                        snapshot.resource,
                        value,
                        policy,
                        current=live.get(path),
                    )
                elif (
                    isinstance(policy, NumberWritePolicy)
                    and isinstance(value, int | float)
                    and not isinstance(value, bool)
                ):
                    result = await self._write_service.async_write_number(
                        self.gateway.gateway_id,
                        snapshot.resource,
                        float(value),
                        policy,
                        current=live.get(path),
                    )
                else:
                    raise WriteValidationError(
                        "Control value does not match its write policy"
                    )
            except WriteValidationError:
                raise
            except (PointTError, asyncio.CancelledError) as err:
                category = self._write_failed(path, err)
                # C33: only a PUT that was sent may have taken effect. Keep
                # its last value only as stale until regular polling succeeds;
                # never let reload or another write reuse it as fresh. A
                # brake, a refusal or a failed read before the PUT changes
                # nothing on the gateway and leaves the reading as it is.
                if isinstance(
                    err, (WriteNotConfirmed, WriteRejected, asyncio.CancelledError)
                ):
                    updated = dict(self.data or {})
                    updated[path] = replace(
                        updated[path],
                        available=False,
                        freshness=Freshness.STALE,
                        last_attempt=datetime.now(UTC),
                        source=SnapshotSource.WRITE,
                        last_error_category=category,
                        ended=True,
                    )
                    self.async_set_updated_data(updated)
                raise

            confirmed_at = datetime.now(UTC)
            confirmed = result.resource
            snapshots = dict(self.data or {})
            snapshots[path] = ResourceSnapshot(
                resource=confirmed,
                available=True,
                last_success=confirmed_at,
                last_attempt=confirmed_at,
                source=SnapshotSource.WRITE,
            )
            self.resources[path] = confirmed
            self._negative_until.pop(path, None)
            self._record_capability(path, "success", SnapshotSource.WRITE)
            self._read_circuit_soon(path)
            if self.local_selection is not None:
                # Local readings of this circuit from before the confirmation
                # must not override it; read them again soon. The write is
                # confirmed already: nothing here may turn it into a failure.
                try:
                    self.local_selection.note_confirmed_write(path, snapshots[path])
                    self.local_selection.request_refresh(path)
                except Exception:
                    _LOGGER.debug(
                        "Local refresh after a confirmed write failed", exc_info=True
                    )
            self.async_set_updated_data(snapshots)
            if (mode_path := _manual_setpoint_mode_path(path)) is not None:
                self._queue_manual_setpoint_recovery(mode_path, snapshots)
            return confirmed

    async def async_select_rrc_program(self, path: str, program: int) -> None:
        await self._async_rrc_operation(
            path,
            lambda: self._rrc_program_service.select(
                self.gateway.gateway_id, path, program
            ),
        )

    async def async_edit_rrc_program(
        self, expected: Resource, index: int, minute: int, temperature: float
    ) -> None:
        if WEEK.fullmatch(expected.path) is None:
            raise WriteValidationError("Unsupported RRC schedule path")
        await self._async_rrc_operation(
            expected.path,
            lambda: self._rrc_program_service.edit(
                self.gateway.gateway_id, expected, index, minute, temperature
            ),
        )

    async def _async_rrc_operation(
        self, path: str, operation: Callable[[], Awaitable[tuple[Resource, ...]]]
    ) -> None:
        """Serialize RRC writes with polling and preserve the cloud backoff."""
        async with self._write_transaction():
            now = monotonic()
            if now < self._cloud_backoff_until:
                raise RateLimited(self._cloud_backoff_until - now)
            snapshot = (self.data or {}).get(path)
            if (
                now < self._circuit_open_until
                or snapshot is None
                or not snapshot.available
                or snapshot.freshness is not Freshness.FRESH
            ):
                raise WriteValidationError("RRC resource is unavailable")
            try:
                confirmed = await operation()
            except WriteValidationError:
                # C33: a refused preflight, such as a program no longer
                # offered, sends nothing and leaves the reading as it is.
                raise
            except (PointTError, asyncio.CancelledError) as err:
                if isinstance(err, RateLimited):
                    self._activate_rate_limit_backoff(err, monotonic())
                if isinstance(err, AuthenticationError):
                    self._request_reauthentication()
                self._record_capability(
                    path,
                    "rrc_write_unconfirmed"
                    if isinstance(err, asyncio.CancelledError)
                    else _pointt_result(err),
                    SnapshotSource.WRITE,
                )
                updated = dict(self.data or {})
                updated[path] = replace(
                    snapshot,
                    available=False,
                    freshness=Freshness.STALE,
                    last_error_category="rrc_write_unconfirmed",
                    ended=True,
                )
                self.async_set_updated_data(updated)
                raise
            updated = dict(self.data or {})
            received_at = datetime.now(UTC)
            for resource in confirmed:
                self.resources[resource.path] = resource
                updated[resource.path] = ResourceSnapshot(
                    resource=resource,
                    available=True,
                    last_success=received_at,
                    last_attempt=received_at,
                    source=SnapshotSource.WRITE,
                )
                self._negative_until.pop(resource.path, None)
                self._record_capability(resource.path, "success", SnapshotSource.WRITE)
            self.async_set_updated_data(updated)

    async def async_create_holiday(self, values: HolidayWriteValues) -> Resource:
        """Create one holiday and publish its confirmed list read-back."""
        return await self._async_write_holiday("create", None, values)

    async def async_update_holiday(
        self,
        holiday_id: int,
        values: HolidayWriteValues,
        *,
        expected: HolidayWriteValues | None = None,
    ) -> Resource:
        """Update one holiday and publish its confirmed list read-back."""
        return await self._async_write_holiday(
            "update", holiday_id, values, expected=expected
        )

    async def async_delete_holiday(
        self, holiday_id: int, *, expected: HolidayWriteValues | None = None
    ) -> Resource:
        """Delete one holiday and publish its confirmed list read-back."""
        return await self._async_write_holiday(
            "delete", holiday_id, None, expected=expected
        )

    async def _async_write_holiday(
        self,
        operation: str,
        holiday_id: int | None,
        values: HolidayWriteValues | None,
        *,
        expected: HolidayWriteValues | None = None,
    ) -> Resource:
        """Run one capability-gated holiday mutation and confirm it."""
        async with self._write_transaction():
            now_monotonic = monotonic()
            if now_monotonic < self._cloud_backoff_until:
                raise RateLimited(self._cloud_backoff_until - now_monotonic)
            if now_monotonic < self._circuit_open_until:
                raise WriteValidationError(
                    "Gateway circuit breaker is active; writing is paused"
                )
            snapshots = self.data or {}
            source = values or expected
            if source is not None and source.source_path != HOLIDAY_CREATE_PATH:
                if not is_icom_slot(source.source_path) or (
                    holiday_id is not None
                    and str(holiday_id) != source.source_path.rsplit("hm", 1)[-1]
                ):
                    raise WriteValidationError(
                        "Legacy holiday source does not match its identifier"
                    )
                return await self._async_write_icom_holiday(
                    operation, source.source_path, values, expected
                )
            for path in (HOLIDAY_LIST_PATH, HOLIDAY_CONFIGURATION_PATH):
                snapshot = snapshots.get(path)
                if (
                    snapshot is None
                    or not snapshot.available
                    or snapshot.freshness is Freshness.STALE
                ):
                    raise WriteValidationError(
                        "Holiday resources are unavailable or not current"
                    )
            resources = holiday_resources_from_snapshots(snapshots)
            if parse_holiday_write_configuration(resources) is None:
                raise WriteValidationError(
                    "The gateway does not advertise a complete holiday calendar schema"
                )

            try:
                if operation == "create" and values is not None:
                    confirmed = await self._holiday_write_service.async_create(
                        self.gateway.gateway_id,
                        resources,
                        values,
                        fallback_timezone=dt_util.get_default_time_zone(),
                    )
                elif (
                    operation == "update"
                    and holiday_id is not None
                    and values is not None
                ):
                    confirmed = await self._holiday_write_service.async_update(
                        self.gateway.gateway_id,
                        resources,
                        holiday_id,
                        values,
                        fallback_timezone=dt_util.get_default_time_zone(),
                        expected=expected,
                    )
                elif operation == "delete" and holiday_id is not None:
                    confirmed = await self._holiday_write_service.async_delete(
                        self.gateway.gateway_id,
                        resources,
                        holiday_id,
                        fallback_timezone=dt_util.get_default_time_zone(),
                        expected=expected,
                    )
                else:
                    raise WriteValidationError("Invalid holiday write request")
            except RateLimited as err:
                self._record_capability(
                    HOLIDAY_LIST_PATH, "rate_limited", SnapshotSource.WRITE
                )
                self._activate_rate_limit_backoff(err, monotonic())
                raise
            except AuthenticationError:
                self._record_capability(
                    HOLIDAY_LIST_PATH,
                    "authentication_error",
                    SnapshotSource.WRITE,
                )
                self._request_reauthentication()
                raise
            except (HolidayWriteNotConfirmed, HolidayAlreadyExists) as err:
                if err.resource is not None:
                    await self._async_publish_holiday_readback(err.resource)
                if isinstance(err, HolidayWriteNotConfirmed):
                    self._record_capability(
                        HOLIDAY_LIST_PATH, "write_not_confirmed", SnapshotSource.WRITE
                    )
                raise
            except WriteValidationError:
                raise
            except PointTError as err:
                self._record_capability(
                    HOLIDAY_LIST_PATH, _pointt_result(err), SnapshotSource.WRITE
                )
                raise

            await self._async_publish_holiday_readback(confirmed)
            self._record_capability(HOLIDAY_LIST_PATH, "success", SnapshotSource.WRITE)
            return confirmed

    async def _async_write_icom_holiday(
        self,
        operation: str,
        source_path: str,
        values: HolidayWriteValues | None,
        expected: HolidayWriteValues | None,
    ) -> Resource:
        """Publish partial read-back under the existing gateway transaction lock."""
        service = self._icom_holiday_write_service
        path = f"{source_path}/startStop"
        resources = holiday_resources_from_snapshots(self.data or {})
        try:
            return await service.async_write(
                self.gateway.gateway_id,
                resources,
                operation,
                source_path,
                values,
                expected,
            )
        except RateLimited as err:
            self._activate_rate_limit_backoff(err, monotonic())
            self._record_capability(path, "rate_limited", SnapshotSource.WRITE)
            raise
        except AuthenticationError:
            self._request_reauthentication()
            self._record_capability(path, "authentication_error", SnapshotSource.WRITE)
            raise
        except PointTError as err:
            self._record_capability(path, _pointt_result(err), SnapshotSource.WRITE)
            raise
        finally:
            updated = dict(self.data or {})
            received_at = datetime.now(UTC)
            for invalidated in service.invalidated:
                if previous := updated.get(invalidated):
                    updated[invalidated] = replace(
                        previous,
                        available=False,
                        freshness=Freshness.STALE,
                        last_error_category="holiday_write_unconfirmed",
                        ended=True,
                    )
            for confirmed_path, resource in service.confirmed.items():
                updated[confirmed_path] = ResourceSnapshot(
                    resource=resource,
                    available=True,
                    last_success=received_at,
                    last_attempt=received_at,
                    source=SnapshotSource.WRITE,
                )
                self.resources[confirmed_path] = resource
                self._negative_until.pop(confirmed_path, None)
            self.async_set_updated_data(updated)

    async def _async_publish_holiday_readback(self, resource: Resource) -> None:
        """Expose the actual list, independently of whether a write matched it."""
        received_at = datetime.now(UTC)
        updated = dict(self.data or {})
        updated[HOLIDAY_LIST_PATH] = ResourceSnapshot(
            resource=resource,
            available=True,
            last_success=received_at,
            last_attempt=received_at,
            source=SnapshotSource.WRITE,
        )
        self.resources[HOLIDAY_LIST_PATH] = resource
        self._negative_until.pop(HOLIDAY_LIST_PATH, None)
        await self._async_prepare_holiday_timezones()
        self.async_set_updated_data(updated)

    async def _async_update_data(self) -> dict[str, ResourceSnapshot]:
        started = monotonic()
        self._polls_total += 1
        try:
            async with self._update_lock:
                self._main_cycle_failed = False
                self._discovery_cycle_failed = False
                self._cycle_idle = False
                if (
                    self._reconcile_due is not None
                    and self.last_update_success
                    and monotonic() >= self._reconcile_due
                    and monotonic() >= self._cloud_backoff_until
                    and monotonic() >= self._circuit_open_until
                ):
                    # After a start with the inventory: this cycle discovers
                    # instead of polling, so each reading is processed once.
                    snapshots = await self._async_reconcile()
                elif (
                    self.firmware.due is not None
                    and self.last_update_success
                    and monotonic() >= self.firmware.due
                    and monotonic() >= self._cloud_backoff_until
                    and monotonic() >= self._circuit_open_until
                ):
                    snapshots = await self._async_firmware_discovery()
                elif (
                    self._rescan_due is not None
                    and self.last_update_success
                    and monotonic() >= self._rescan_due
                    and monotonic() >= self._cloud_backoff_until
                    and monotonic() >= self._circuit_open_until
                ):
                    snapshots = await self._async_rescan()
                else:
                    snapshots = await self._async_update_data_locked()
                self.firmware.observe(
                    firmware_values(
                        {
                            path: item.resource
                            for path, item in snapshots.items()
                            if item.available and item.freshness is Freshness.FRESH
                        }
                    ),
                    monotonic(),
                )
                self.startup_data.record_main_cycle(
                    successful=not (
                        self._main_cycle_failed or self._discovery_cycle_failed
                    )
                )
                if not self._cycle_idle:
                    # #60: only an answered request ends an outage; a cycle
                    # with nothing due (O41/O45 leave more of them) does not.
                    self._refresh_outage.recovered()
                if self._discovery_cycle_failed:
                    # A discovery that failed or met the brake returned the
                    # previous values: nothing is confirmed, searched or read
                    # in addition in this cycle.
                    return snapshots
                self._confirm_inventory(snapshots)
                # P46: search missing parts only after a cycle that returned
                # data. During an outage the cycle fails first, so a search
                # never uses up an attempt on it.
                if self.discovery_recovery.due(monotonic()):
                    await self._async_recover_missing(snapshots)
                if self.startup_data.healthy and self.startup_session.claim(
                    self.gateway.gateway_id, monotonic()
                ):
                    try:
                        await self.async_read_startup_batch(snapshots)
                    finally:
                        self.startup_session.release()
                await self._async_prepare_holiday_timezones()
                return snapshots
        except Exception as err:
            self.startup_data.record_main_cycle(successful=False)
            if isinstance(err, IdleAfterFailure):
                # B03: the earlier failure goes on; this cycle sent nothing,
                # so it is no further failure and no answer either.
                raise
            self._poll_failures += 1
            self.faults.mark_unavailable()
            if isinstance(err, UpdateFailed):
                self._refresh_outage.failed(err)
                # #60: Home Assistant logs a failed refresh as an error only
                # while the coordinator still counts as successful; marked
                # here, the outage log decides when the error is due.
                self.last_update_success = False
            raise
        finally:
            duration = (monotonic() - started) * 1000
            self._last_poll_duration_ms = duration
            self._total_poll_duration_ms += duration
            self._maximum_poll_duration_ms = max(
                self._maximum_poll_duration_ms, duration
            )
            self._recent_poll_durations_ms.append(duration)
            # A longer cycle is not overlapped; the next poll simply starts later.
            if (
                self.update_interval is not None
                and duration > self.update_interval.total_seconds() * 1000
            ):
                self._polls_longer_than_interval += 1

    def _validity_group(self, resource: Resource) -> PollGroup:
        """Use the group the path is actually polled in, as scheduled."""
        key = tuple(self._paths_by_group.items())
        if key != self._validity_index_key:
            self._validity_index = {
                path: group
                for group, paths in reversed(self._paths_by_group.items())
                for path in paths
            }
            self._validity_index_key = key
        return self._validity_index.get(resource.path) or self._poll_group(resource)

    def scheduled_interval(self, path: str) -> tuple[bool, timedelta | None]:
        """Return whether the answer is final and the polling interval to match.

        ``None`` means no local cap: static readings (read once), readings that
        can be adjusted through the cloud (never supplied locally) and paths
        this cloud does not provide after its discovery. Before discovery the
        answer is not final. It is always the group's normal interval, also
        while a locally shown reading is read from the cloud at the slower
        rate (O45): the local access keeps its pace and stays the fresher one.
        """
        snapshot = (self.data or {}).get(path)
        if snapshot is None:
            return bool(self.data), None
        if snapshot.resource.metadata.writable:
            return True, None
        return True, self._poll_intervals.get(self._validity_group(snapshot.resource))

    def schedule_token(self) -> tuple[object, ...]:
        """O13: objects replaced whenever ``scheduled_interval`` may answer
        differently (keep both in step); compared by identity only."""
        return (
            self.data,
            self._poll_intervals,
            self.entity_use.disabled_paths(self.gateway.gateway_id),
            *self._paths_by_group.values(),
        )

    def validity_limit(self, snapshot: ResourceSnapshot) -> timedelta | None:
        """Return how long a cloud reading stays valid; ``None`` never expires.

        The next poll starts one interval after the previous cycle finished, so
        each interval is extended by the duration of the last cycle.
        """
        if snapshot.source is SnapshotSource.LOCAL:
            return None
        interval = self._poll_intervals.get(self._validity_group(snapshot.resource))
        if interval is None:
            return None
        # A hanging cycle may extend each interval by at most one interval.
        cycle = min(
            timedelta(milliseconds=self._last_poll_duration_ms or 0.0), interval
        )
        limit = (interval + cycle) * VALIDITY_INTERVALS
        path = snapshot.resource.path
        if path in self._local_served:
            # O45: read at the slower rate while the local reading is shown;
            # never expires only because of that rate.
            return _slower_rate(interval) + limit
        left = self._served_left.get(path)
        if left is not None and snapshot.last_success < left:
            # Read at the slower rate before the cloud value was shown again:
            # its normal limit counts from the switch, until the copy is read
            # again, but never beyond the limit of that slower rate.
            return min(
                _slower_rate(interval) + limit,
                (left - snapshot.last_success) + limit,
            )
        return limit

    def _expired(self, snapshot: ResourceSnapshot, now: datetime) -> bool:
        limit = self.validity_limit(snapshot)
        if limit is None:
            return False
        age = now - snapshot.last_success
        return age > limit or age < -CLOCK_SKEW_TOLERANCE

    def _cloud_auth_required(self) -> bool:
        """Managed sources report a pending sign-in of their access."""
        return False

    def _cloud_source_stopped(self) -> bool:
        """Managed sources report a stopped or replaced cloud source."""
        return False

    def control_status(self) -> tuple[str | None, datetime | None]:
        """Why cloud operation is not possible now, and until when if known.

        ``(None, None)`` means controls can be used. The reason never contains
        values, paths or identifiers.
        """
        reason, until = self._cloud_down()
        if reason is not None:
            return reason, until
        from .entity_resources import fresh_resource

        if not any(
            (resource := fresh_resource(self, path)) is not None
            and resource.metadata.writable
            for path in self.data or {}
        ):
            return "not_current", None
        return None, None

    def cloud_unavailable(self) -> bool:
        """Without a cloud now: sign-in required, switched off or stopped,
        paused, limited or unreachable.

        Paired readings then show their local values, independent of the
        ``local_values`` option. A view kept for local readings after its
        cloud source stopped counts as without a cloud.
        """
        return self.local_retained or self._cloud_down()[0] is not None

    def counter_cloud_unavailable(self) -> bool:
        """Without a cloud for paired counters: only a durable outage.

        Sign-in required, switched off or stopped, a retained view and pauses
        count at once; an unreachable cloud only after
        ``COUNTER_OUTAGE_SECONDS`` of failed cycles. A single failed cycle
        therefore never switches a counter to the local access, whose higher
        value would hold the lagging cloud counter afterwards.
        """
        if self.local_retained:
            return True
        reason = self._cloud_down()[0]
        if reason != "cloud_unreachable":
            return reason is not None
        since = self._unreachable_since
        return since is not None and monotonic() - since >= COUNTER_OUTAGE_SECONDS

    @property
    def last_update_success(self) -> bool:
        return self.__dict__.get("_update_success", True) is True

    @last_update_success.setter
    def last_update_success(self, value: bool) -> None:
        # Remember when the failed cycles began (only the first one counts).
        if value:
            self._unreachable_since = None
        elif self.last_update_success or self._unreachable_since is None:
            self._unreachable_since = monotonic()
        self._update_success = value

    _unreachable_since: float | None = None

    def _cloud_down(self) -> tuple[str | None, datetime | None]:
        """The reason the cloud cannot be used now, without checking values."""
        now = monotonic()
        if self._cloud_auth_required():
            return "auth_required", None
        if self._cloud_source_stopped():
            return "cloud_stopped", None
        # The account-wide brake (siblings, token refresh, restored pauses)
        # rejects writes as well as this gateway's own pause.
        shared = getattr(getattr(self.client, "backoff", None), "remaining", 0.0)
        shared = shared if isinstance(shared, int | float) else 0.0
        paused = max(self._cloud_backoff_until - now, shared)
        if paused > 0:
            return "rate_limited", _pause_end(paused)
        if now < self._circuit_open_until:
            return "protection_pause", _pause_end(self._circuit_open_until - now)
        if not self.last_update_success:
            return "cloud_unreachable", None
        return None, None

    def source_snapshot(self, path: str, source: str) -> ResourceSnapshot | None:
        """One source alone, for the optional per-source entities (P18).

        ``cloud`` is the cloud reading within its validity limit, ``local``
        the current local reading in the cloud entity's shape.
        """
        if source == "cloud":
            return self.displayed_cloud_snapshot(path)
        cloud = (self.data or {}).get(path)
        if source != "local" or cloud is None or self.local_selection is None:
            return None
        return self.local_selection.local_only(path, cloud)

    def displayed_cloud_snapshot(self, path: str) -> ResourceSnapshot | None:
        """Return the cloud snapshot, marked unavailable once it has expired.

        Only the displayed copy changes; stored data, writes and the polling
        schedule keep the original snapshot.
        """
        snapshot = (self.data or {}).get(path)
        if snapshot is None:
            return None
        if not snapshot.available:
            if snapshot.ended or not _transient_failure(snapshot.last_error_category):
                return snapshot
            # Keep showing the last good value; the attempt failed as a whole.
            shown = replace(snapshot, available=True, freshness=Freshness.FRESH)
        else:
            shown = snapshot
        if not self._expired(shown, dt_util.utcnow()):
            return shown
        return replace(
            shown,
            available=False,
            freshness=Freshness.STALE,
            last_error_category=EXPIRED_CATEGORY,
        )

    def display_snapshot(self, path: str) -> ResourceSnapshot | None:
        """Return the snapshot an existing sensor displays.

        A current local reading of an enabled selection comes first, otherwise
        the cloud reading within its validity limit. A failed or paused cloud
        cycle therefore no longer hides readings that are still valid. Without
        a cloud the local reading comes first also with the option off.
        """
        if self.counter_guard is not None and path in self._counter_views:
            return self._counter_views[path]
        selection = self.local_selection
        if selection is None:
            return self.displayed_cloud_snapshot(path)
        # O2: entities ask several times per pass (availability, value, unit,
        # attributes); within one synchronous pass the answer cannot change.
        return selection._cached(
            ("display_snapshot", path), lambda: self._display_snapshot(path)
        )

    def _display_snapshot(self, path: str) -> ResourceSnapshot | None:
        snapshot = self.displayed_cloud_snapshot(path)
        selection = self.local_selection
        if (
            selection is None
            or snapshot is None
            or (
                snapshot.last_error_category == SOURCE_STOPPED_CATEGORY
                and not self.local_retained
            )
        ):
            return snapshot
        return selection.select(path, snapshot, without_cloud=self.cloud_unavailable)

    def _refresh_counter_views(self) -> None:
        """Decide each paired counter once: local first, never backwards."""
        guard = self.counter_guard
        if guard is None:
            return
        from .local_selection import counter_path

        selection = self.local_selection
        # A removed or switched-off local access no longer binds the cloud.
        local_gone = selection is None or not selection.has_local_access
        views: dict[str, ResourceSnapshot | None] = {}
        for path in self.data or {}:
            if not counter_path(path):
                continue
            cloud = self.displayed_cloud_snapshot(path)
            stopped = (
                cloud is not None
                and cloud.last_error_category == SOURCE_STOPPED_CATEGORY
            )
            if cloud is None or (stopped and not self.local_retained):
                views[path] = cloud
                continue
            local = (
                selection.counter_candidate(
                    path, cloud, without_cloud=self.counter_cloud_unavailable
                )
                if selection
                else None
            )
            # A stopped cloud kept for local readings: the local access is the
            # sole source, so a reading lacking some parts may continue.
            chosen = guard.decide(
                path,
                (("local", local), ("cloud", cloud)),
                partial=stopped,
                gone=frozenset({"local"}) if local_gone else frozenset(),
            )
            views[path] = (
                chosen
                if chosen is not None
                else replace(
                    cloud,
                    available=False,
                    freshness=Freshness.STALE,
                    last_error_category=COUNTER_HELD_CATEGORY,
                )
            )
        self._counter_views = views

    @callback
    def async_update_listeners(self) -> None:
        selection = self.local_selection
        load = update_load(self.hass)
        with ExitStack() as stack:
            if load is not None:
                stack.enter_context(load.measure_pass(len(self._listeners)))
            if selection is not None:
                # Options and local readings cannot change during this
                # synchronous pass: work them out once, not per entity.
                stack.enter_context(selection.update_pass())
                # New readings may answer an earlier confirmed write.
                selection.check_writes(datetime.now(UTC))
            self._refresh_counter_views()
            super().async_update_listeners()
            # O45: the shown sources of this pass decide the cloud rates.
            if self._update_local_served():
                self._request_switch_read()
        self._schedule_validity_refresh()

    def _served_period(self, path: str) -> timedelta | None:
        """The slower cloud rate this reading may use while shown locally.

        Only paired readings that are no counter, cannot be adjusted through
        the cloud, are not read by a control or calculated entity, and whose
        group is read more often than ``LOCAL_SERVED_INTERVAL``.
        """
        from .local_selection import eligible

        snapshot = (self.data or {}).get(path)
        if (
            snapshot is None
            or _CLOUD_COPY_IN_USE.match(path)
            or not eligible(path, snapshot)
        ):
            return None
        interval = self._poll_intervals.get(self._validity_group(snapshot.resource))
        if interval is None or not _slower_rate_applies(interval):
            return None
        return _slower_rate(interval)

    def _update_local_served(self) -> bool:
        """Follow which cloud copies the shown local readings make optional.

        A reading counts while ``Use local values for existing entities`` is
        on and its entity shows the local reading. Returns whether a reading
        just switched back to its cloud value and needs a cloud read now.
        """
        selection = self.local_selection
        served: set[str] = set()
        if (
            selection is not None
            and self.update_interval is not None
            and not self.read_only_view
        ):
            # Within one pass: the published readings are worked out once.
            with selection.update_pass():
                if selection.enabled:
                    for path in self.data or {}:
                        if self._served_period(path) is None:
                            continue
                        shown = self.display_snapshot(path)
                        if shown is not None and shown.source is SnapshotSource.LOCAL:
                            served.add(path)
        now = dt_util.utcnow()
        now_monotonic = monotonic()
        urgent = False
        for path in tuple(self._local_served):
            if path in served:
                continue
            del self._local_served[path]
            snapshot = (self.data or {}).get(path)
            if snapshot is None:
                continue
            interval = self._poll_intervals.get(self._validity_group(snapshot.resource))
            if interval is not None and not _slower_rate_applies(interval):
                # Its own group is read this slowly now (O51: its entities
                # were all disabled): no switch to the cloud value, that
                # group's rate and validity simply apply.
                continue
            self._served_left[path] = now
            if interval is not None and now - snapshot.last_success >= interval:
                # Older than its normal rate allows: read it at once.
                self._urgent_reads.add(path)
                urgent = True
        for path in served - self._local_served.keys():
            snapshot = (self.data or {}).get(path)
            period = self._served_period(path)
            if snapshot is None or period is None:  # pragma: no cover - checked
                continue
            age = max(0.0, (now - snapshot.last_success).total_seconds())
            self._local_served[path] = now_monotonic + max(
                0.0, period.total_seconds() - age
            )
            self._served_left.pop(path, None)
            self._urgent_reads.discard(path)
        return urgent

    @callback
    def _request_switch_read(self) -> None:
        """Start a cycle now for readings that switched back to the cloud."""
        if self._cloud_down()[0] is not None or self.config_entry is None:
            return  # the next working cycle reads them first
        self.config_entry.async_create_background_task(
            self.hass,
            self._async_request_switch_refresh(),
            "bosch_buderus_heating_cloud_switch_read",
        )

    def _read_circuit_soon(self, path: str) -> None:
        """After a confirmed write, slower cloud copies of the same circuit are
        read with the next regular read of their group, as before O45."""
        from .local_selection import write_scope

        scope = write_scope(path)
        for item in self._local_served:
            if item == scope or item.startswith(scope + "/"):
                self._local_served[item] = 0.0

    @callback
    def _schedule_validity_refresh(self) -> None:
        """Re-evaluate entities when the next reading expires, without a request."""
        if not self._listeners or self.hass.is_stopping:
            self._cancel_validity_timer()
            return
        now = dt_util.utcnow()
        due = min(
            (
                snapshot.last_success + limit
                for snapshot in (self.data or {}).values()
                if (
                    snapshot.available
                    or (
                        not snapshot.ended
                        and _transient_failure(snapshot.last_error_category)
                    )
                )
                and (limit := self.validity_limit(snapshot)) is not None
                and snapshot.last_success + limit >= now
            ),
            default=None,
        )
        if (
            due is not None
            and due == self._validity_due
            and self._validity_unsub is not None
        ):
            return  # O14: the armed timer already fires at this time
        self._cancel_validity_timer()
        if due is not None:
            self._validity_due = due
            self._validity_unsub = async_track_point_in_utc_time(
                self.hass, self._validity_reached, due + VALIDITY_TIMER_MARGIN
            )

    @callback
    def _validity_reached(self, _now: datetime) -> None:
        self._validity_unsub = None
        self._validity_due = None
        self.async_update_listeners()

    @callback
    def _cancel_validity_timer(self) -> None:
        self._validity_due = None
        if self._validity_unsub is not None:
            self._validity_unsub()
            self._validity_unsub = None

    @callback
    def _validity_stopped(self, _event: Event) -> None:
        self._validity_stop_unsub = None
        self._cancel_validity_timer()

    async def async_shutdown(self) -> None:
        if self._remove_use_listener is not None:
            self._remove_use_listener()
            self._remove_use_listener = None
        self._cancel_validity_timer()
        if self._validity_stop_unsub is not None:
            self._validity_stop_unsub()
            self._validity_stop_unsub = None
        await super().async_shutdown()

    def diagnostics_summary(self) -> dict[str, object]:
        """Return aggregate runtime state without identifiers or resource values."""
        now = monotonic()
        snapshots = tuple((self.data or {}).values())
        average_duration = (
            self._total_poll_duration_ms / self._polls_total
            if self._polls_total
            else None
        )
        capabilities = self._capability_metrics.values()
        capability_attempts = sum(item.attempts for item in capabilities)
        capability_successful = sum(item.successful for item in capabilities)
        # O94: failed attempts as each path counts them, and the paths whose
        # latest attempt failed (a path may fail several times).
        capability_failed = sum(item.failed for item in capabilities)
        failing_paths = sum(
            item.last_result not in (None, "success") for item in capabilities
        )
        return {
            "last_update_success": self.last_update_success,
            "polls_total": self._polls_total,
            "poll_failures": self._poll_failures,
            "average_poll_duration_ms": _rounded_duration(average_duration),
            "last_poll_duration_ms": _rounded_duration(self._last_poll_duration_ms),
            "maximum_poll_duration_ms": _rounded_duration(
                self._maximum_poll_duration_ms if self._polls_total else None
            ),
            "p95_poll_duration_ms": _rounded_duration(
                _percentile(self._recent_poll_durations_ms, 0.95)
            ),
            "poll_duration_samples": len(self._recent_poll_durations_ms),
            "poll_interval_seconds": (
                self.update_interval.total_seconds() if self.update_interval else None
            ),
            "polls_longer_than_interval": self._polls_longer_than_interval,
            "cycles_over_budget": self._cycles_over_budget,
            "resources_total": len(snapshots),
            "resources_available": sum(item.available for item in snapshots),
            "resources_unavailable": sum(not item.available for item in snapshots),
            "resources_expired": sum(
                (
                    item.available
                    or (not item.ended and _transient_failure(item.last_error_category))
                )
                and self._expired(item, dt_util.utcnow())
                for item in snapshots
            ),
            "validity_limits_seconds": {
                group.value: interval.total_seconds() * VALIDITY_INTERVALS
                for group, interval in self._poll_intervals.items()
            },
            "resources_fresh": sum(
                item.freshness is Freshness.FRESH for item in snapshots
            ),
            "resources_stale": sum(
                item.freshness is Freshness.STALE for item in snapshots
            ),
            "negative_pauses_active": sum(
                until > now for until in self._negative_until.values()
            ),
            "rate_limit_backoff_active": now < self._cloud_backoff_until,
            "rate_limit_events": self._rate_limit_events,
            "rate_limit_events_last_24h": sum(
                now - seen < RATE_LIMIT_REPAIR_WINDOW.total_seconds()
                for seen in self._rate_limit_times
            ),
            "energy_counter_resets_detected": self._energy_counter_resets,
            "produced_heat_decreases_detected": self._produced_heat_decreases,
            "unknown_enum_values_detected": sum(
                self._unknown_enum_value_counts.values()
            ),
            "changed_contracts_detected": len(self._contract_changes),
            "circuit_breaker_active": now < self._circuit_open_until,
            "consecutive_gateway_failures": self._gateway_failure_count,
            "polling_profile": self.polling_profile.value,
            "notification_poll_interval_seconds": (
                ACTIVE_NOTIFICATION_INTERVAL.total_seconds()
                if self.faults.active
                else self._poll_intervals[PollGroup.NOTIFICATIONS].total_seconds()
            ),
            "capability_attempts": {
                "total": capability_attempts,
                "successful": capability_successful,
                "failed": capability_failed,
                "paths_failing": failing_paths,
                "success_rate_percent": _percentage(
                    capability_successful, capability_attempts
                ),
            },
        }

    def cloud_rate_summary(self) -> dict[str, object]:
        """O41/O45/O51 figures without paths or values, for the diagnostics."""
        return {
            "early_read_paths": sum(
                1 for _, slot in self._slot_plan().values() if slot
            ),
            "local_served_paths": len(self._local_served),
            "local_served_interval_seconds": LOCAL_SERVED_INTERVAL.total_seconds(),
            "switch_reads_pending": len(self._urgent_reads),
            # SEC-2: explicit refreshes that read everything, those within the
            # minimum gap that read only the controls, and that gap.
            "explicit_refreshes_full": self._explicit_refreshes_full,
            "explicit_refreshes_control_only": self._explicit_refreshes_control_only,
            "explicit_refresh_gap_seconds": self._explicit_refresh_gap(),
            "controls_slowed_unused": sum(
                1
                for path in self._paths_by_group.get(PollGroup.SLOW, ())
                if (resource := self.resources.get(path)) is not None
                and poll_group(resource) is PollGroup.CONTROL
            ),
        }

    def polling_group(self, resource: Resource) -> PollGroup:
        """The group a resource is actually polled in (O51 included)."""
        return self._validity_group(resource)

    def capability_metrics(self, path: str) -> dict[str, object]:
        """Return counters for one resource path."""
        metrics = self._capability_metrics.get(path)
        return (
            metrics.snapshot()
            if metrics is not None
            else CapabilityMetrics().snapshot()
        )

    def resource_pause_remaining_seconds(self, path: str) -> int:
        """Describe a local resource pause without changing the polling state."""
        return max(0, math.ceil(self._negative_until.get(path, 0.0) - monotonic()))

    def record_unknown_enum_value(self, path: str) -> None:
        """Record one distinct undeclared enum value without retaining it."""
        self._unknown_enum_value_counts[path] += 1

    def record_contract_change(self, path: str) -> None:
        """A known reading changed type or unit since discovery (P22)."""
        self._contract_changes[path] += 1

    def unknown_enum_value_count(self, path: str) -> int:
        """Return the number of distinct undeclared enum values seen for a path."""
        return self._unknown_enum_value_counts[path]

    async def _async_update_data_locked(self) -> dict[str, ResourceSnapshot]:
        """Refresh due groups while preventing overlapping gateway polls."""
        if not self.resources:
            return await self._async_discover_data()

        now_monotonic = monotonic()
        if (shared := self._shared_pause_remaining()) > 0:
            # CHAOS-5: the pause of the cloud access (another gateway met the
            # limit) holds this cycle back before any request; no new event.
            self._cloud_backoff_until = max(
                self._cloud_backoff_until, now_monotonic + shared
            )
        if now_monotonic < self._cloud_backoff_until:
            raise UpdateFailed("PointT rate-limit backoff is active")
        if now_monotonic < self._circuit_open_until:
            raise UpdateFailed("PointT gateway circuit breaker is active")
        due_groups = tuple(
            group
            for group in self._poll_intervals
            if now_monotonic + POLL_DUE_TOLERANCE_SECONDS
            >= self._next_update.get(group, 0.0)
        )
        paths_by_group = dict(self._paths_by_group)
        pending_manual = tuple(
            path
            for path, mode_path in self._manual_setpoint_candidates.items()
            if path not in self.resources
            and _manual_mode_active(mode_path, self.data or {})
        )
        paths_by_group[PollGroup.CONTROL] = (
            *paths_by_group.get(PollGroup.CONTROL, ()),
            *pending_manual,
        )
        # O45: a reading that switched back to its cloud value is read now.
        self._update_local_served()
        # Paths of each due group already read in its current period.
        progress = {group: self._progress(group, now_monotonic) for group in due_groups}
        candidate_paths = [
            path
            for group in due_groups
            for path in paths_by_group.get(group, ())
            if path not in progress[group]
            and not self._served_skip(path, now_monotonic)
        ]
        early = self._early_reads(paths_by_group, due_groups, now_monotonic)
        urgent = tuple(path for path in self._urgent_reads if path in self.resources)
        candidate_paths = list(
            dict.fromkeys((*candidate_paths, *(path for _, path in early), *urgent))
        )
        paths = [
            path
            for path in candidate_paths
            if not self._resource_is_paused(path, now_monotonic)
        ]
        # Resume work skipped by an interrupted cycle before repeating earlier
        # batches. A consistently failing batch must not starve later resources.
        path_set = set(paths)
        paths = list(
            dict.fromkeys(
                (
                    *[path for path in self._deferred_poll_paths if path in path_set],
                    *paths,
                )
            )
        )
        if not paths:
            self._main_cycle_failed = not self.last_update_success
            self._cycle_idle = True
            self._advance_groups(due_groups, now_monotonic)
            if not self.last_update_success:
                # B03: nothing confirmed the cloud since the failed cycle; a
                # normal return would report it recovered to Home Assistant.
                raise IdleAfterFailure("PointT cycle after a failure requested nothing")
            return dict(self.data or {})

        snapshots = dict(self.data or {})
        successful = 0
        failed_chunks = 0
        gateway_failure = False
        # O82: the error category a whole failed cycle reports.
        failure_category: str | None = None
        cycle_results: list[BatchItemResult] = []
        attempted_paths: set[str] = set()
        interrupted_paths: set[str] = set()
        over_budget = False
        # FAULT-1: the fault sources form the first batch, so the cycle budget
        # never splits them over two cycles. They share it with no other
        # reading, so a failing reading elsewhere never fails them with it.
        # COORD-3: the paths of a batch that failed beside answered ones form
        # their own batches, read last, so they fail no other reading with them.
        fault_sources = set(paths_by_group.get(PollGroup.NOTIFICATIONS, ()))
        faults = [path for path in paths if path in fault_sources]
        failing = [
            path
            for path in paths
            if path in self._failing_batch_paths and path not in fault_sources
        ]
        others = [
            path
            for path in paths
            if path not in self._failing_batch_paths and path not in fault_sources
        ]
        if set(paths) <= self._failing_batch_paths and (
            probe := self._outage_probe(paths_by_group, now_monotonic)
        ):
            # COORD-3: only the failing batch is due. Its failure alone cannot
            # tell a broken batch from a cloud outage: one answered reading,
            # read before it, tells them apart.
            others = [probe]
        paths = [*faults, *others, *failing]
        chunks = (
            *chunk_resource_paths(faults, size=MAX_BULK_PATHS),
            *chunk_resource_paths(others, size=MAX_BULK_PATHS),
            *chunk_resource_paths(failing, size=MAX_BULK_PATHS),
        )
        for chunk in chunks:
            if attempted_paths and monotonic() - now_monotonic >= CYCLE_BUDGET_SECONDS:
                over_budget = True
                self._cycles_over_budget += 1
                _LOGGER.debug(
                    "PointT cycle budget reached; %d readings move to the next cycle",
                    len(paths) - len(attempted_paths),
                )
                break
            attempted_paths.update(chunk)
            attempted_at = datetime.now(UTC)
            result_source = SnapshotSource.BATCH
            # CHAOS-5: a request the pause of the cloud access holds back is
            # no rate limit of the cloud.
            held_back = self._shared_pause_remaining() > 0
            try:
                results = await self.client.get_resources_bulk(
                    self.gateway.gateway_id, chunk
                )
            except AuthenticationError as err:
                self._record_chunk_failure(
                    chunk, "authentication_error", SnapshotSource.BATCH
                )
                raise ConfigEntryAuthFailed from err
            except RateLimited as err:
                interrupted_paths.update(chunk)
                failure_category = "rate_limited"
                cycle_results.extend(
                    BatchItemResult(self.gateway.gateway_id, path, 429, error=err)
                    for path in chunk
                )
                self._apply_results(
                    snapshots,
                    tuple(
                        BatchItemResult(self.gateway.gateway_id, path, 429, error=err)
                        for path in chunk
                    ),
                    attempted_at=attempted_at,
                    now_monotonic=now_monotonic,
                    source=SnapshotSource.BATCH,
                    whole_request=True,
                )
                # COORD-5: the pause starts when the cloud answered, which
                # may be a later chunk than the first of this cycle.
                self._activate_rate_limit_backoff(
                    err, monotonic(), counted=not held_back
                )
                failed_chunks += 1
                break
            except PointTError as err:
                interrupted_paths.update(chunk)
                cycle_results.extend(
                    BatchItemResult(self.gateway.gateway_id, path, None, error=err)
                    for path in chunk
                )
                self._apply_results(
                    snapshots,
                    tuple(
                        BatchItemResult(self.gateway.gateway_id, path, None, error=err)
                        for path in chunk
                    ),
                    attempted_at=attempted_at,
                    now_monotonic=now_monotonic,
                    source=SnapshotSource.BATCH,
                    whole_request=not isinstance(err, SourceStopped),
                )
                failed_chunks += 1
                gateway_failure = True
                failure_category = _pointt_result(err)
                results = await self._async_core_fallback(chunk)
                self._log_batch_failure(
                    failure_category,
                    fallback_usable=any(item.resource is not None for item in results),
                )
                if not results:
                    break
                result_source = SnapshotSource.FALLBACK

            bulk_item_fallback: tuple[BatchItemResult, ...] = ()
            # B02: decided before any result is applied or read again.
            gateway_unanswered = _gateway_unanswered(results)
            if result_source is SnapshotSource.BATCH:
                self._log_unusable_bulk_results(results)
                if not gateway_unanswered and not any(
                    result.status == 429 for result in results
                ):
                    bulk_item_fallback = await self._async_bulk_item_fallback(results)

            cycle_results.extend(results)
            successful += self._apply_results(
                snapshots,
                results,
                attempted_at=attempted_at,
                now_monotonic=now_monotonic,
                source=result_source,
                pause_readings=not gateway_unanswered,
            )
            if bulk_item_fallback:
                cycle_results.extend(bulk_item_fallback)
                successful += self._apply_results(
                    snapshots,
                    bulk_item_fallback,
                    attempted_at=attempted_at,
                    now_monotonic=now_monotonic,
                    source=SnapshotSource.FALLBACK,
                )
            if any(result.status == 429 for result in results):
                interrupted_paths.update(chunk)
                self._activate_rate_limit_backoff(
                    RateLimited(retry_after=None), monotonic()
                )
                failed_chunks += 1
                break
            if now_monotonic < self._cloud_backoff_until:
                failed_chunks += 1
                break
            if _no_usable_item(results):
                interrupted_paths.update(chunk)
                failed_chunks += 1
                gateway_failure = True
                break

        self._deferred_poll_paths = tuple(
            path for path in paths if path not in attempted_paths
        )
        # A switched reading was requested once; afterwards its normal rate.
        self._urgent_reads -= attempted_paths
        self._main_cycle_failed = bool(failed_chunks) or any(
            not result.ok and result.status not in (403, 404, 406)
            for result in cycle_results
        )
        if not failed_chunks:
            # O81, COORD-3: only a cycle in which every batch answered ends
            # the outage; a later failure then warns again. A batch that keeps
            # failing beside working ones stays one outage.
            self._batch_failure_logged = False
        for path in () if over_budget else self._deferred_poll_paths:
            # Readings skipped only for the cycle budget keep their values.
            if (previous := snapshots.get(path)) is not None:
                # No request was made: retain the attempt timestamp and counters.
                snapshots[path] = replace(
                    previous,
                    available=False,
                    freshness=Freshness.STALE,
                    last_error_category="poll_deferred",
                )
        self.faults.record_results(cycle_results)
        self.faults.record_deferred_paths(self._deferred_poll_paths)
        successful_fault_resources = {
            result.path: result.resource
            for result in cycle_results
            if result.resource is not None and is_fault_resource_path(result.path)
        }
        self.faults.process_resources(
            successful_fault_resources,
            successful_paths={
                result.path for result in cycle_results if result.resource is not None
            },
        )

        # COORD-3: a cycle in which nothing answered fails, also one that read
        # only the batch that failed beside answered ones before; while those
        # wait for their interval, a reading of them is read with it.
        if successful == 0 and failed_chunks:
            # The failing batch stays apart: after the outage, its failure
            # alone again fails no other reading (COORD-3).
            # Retain failures without declaring the coordinator healthy. A later
            # cycle with only paused paths must not resurrect old fresh snapshots.
            self.data = snapshots
            if gateway_failure:
                self._record_gateway_failure(now_monotonic)
            if not self.last_update_success:
                # HA skips listeners after consecutive failures; publish the
                # readings that failed explicitly in this cycle.
                self.async_update_listeners()
            raise UpdateFailed(
                "PointT returned no usable resources ("
                f"{failure_category or _cycle_failure_category(cycle_results)})"
            )
        confirmed_paths = {
            path
            for path in attempted_paths
            if path in snapshots and snapshots[path].available
        }
        # Local path errors keep their normal cadence. Only an interrupted
        # batch or deferred work should prevent a group from advancing.
        completed_paths = (attempted_paths - interrupted_paths) | confirmed_paths
        for group, path in early:
            # O41: read before its group's deadline; not again until then.
            if path in completed_paths:
                self._spread_progress.setdefault(group, {})[path] = now_monotonic
        if successful == 0 and not over_budget:
            self._advance_groups(due_groups, now_monotonic)
            return snapshots
        self._record_gateway_recovery(monotonic())
        # Paths of a failed batch beside answered ones (COORD-3).
        self._failing_batch_paths = frozenset(interrupted_paths - completed_paths)
        # Advance complete groups even if a different group was interrupted.
        completed_groups = tuple(
            group
            for group in due_groups
            if all(
                path not in path_set
                or path in completed_paths
                or path in progress[group]
                for path in paths_by_group.get(group, ())
            )
        )
        self._advance_groups(completed_groups, now_monotonic)
        for group in due_groups:
            if group in completed_groups:
                self._budget_progress.pop(group, None)
            elif over_budget:
                # Remember what this group already got; the rest comes next.
                self._budget_progress.setdefault(group, {}).update(
                    (path, now_monotonic)
                    for path in paths_by_group.get(group, ())
                    if path in completed_paths
                )
            else:
                # COORD-3: a failed batch keeps this group due; the paths it
                # got are not read again before their own interval passed.
                self._interrupted_progress.setdefault(group, {}).update(
                    (path, now_monotonic)
                    for path in paths_by_group.get(group, ())
                    if path in completed_paths
                )
        return snapshots

    def restore_start_counter_path(self, path: str, *, scalar: bool = False) -> None:
        """Remember only bounded, exact leaves from this gateway's registry."""
        if is_start_counter_path(path) and (
            path in self.registered_start_counter_paths
            or len(self.registered_start_counter_paths) < MAX_DISCOVERY_RESOURCES
        ):
            self.registered_start_counter_paths.add(path)
            self.registered_start_counter_shapes.setdefault(path, set()).add(scalar)

    def _merge_start_counter_candidates(self) -> None:
        """Schedule advertised and registered leaves within the normal budget."""
        advertised = {
            path
            for path, report in self.discovery_diagnostics.paths.items()
            if report.source is DiscoveryPathSource.REFERENCE
            and is_start_counter_path(path)
            and report.bulk_result != "not_attempted"
        }
        for path in (*sorted(self.registered_start_counter_paths), *sorted(advertised)):
            if path not in self.resources:
                if not self.startup_data.reserve_discovery((path,)):
                    self._start_counter_recovery_limited = True
                    continue
                # No type or fresh reading is inferred from a failed request.
                self.resources[path] = Resource(path=path)
            report = self.discovery_diagnostics.paths.get(path)
            if report is None or report.discovered:
                continue
            result = report.fallback_result or report.bulk_result
            if result == "http_403" or (
                result == "http_404" and path not in self.registered_start_counter_paths
            ):
                pause = FORBIDDEN_PAUSE if result == "http_403" else NOT_FOUND_PAUSE
                self._negative_until[path] = monotonic() + pause.total_seconds()

    def start_counter_recovery_diagnostics(self) -> dict[str, object]:
        """Describe recovery coverage without disclosing paths or readings."""
        snapshots = self.data or {}
        return {
            "registered_paths": len(self.registered_start_counter_paths),
            "waiting_for_first_response": sum(
                is_start_counter_path(path) and path not in snapshots
                for path in self.resources
            ),
            "resource_limit_reached": self._start_counter_recovery_limited,
        }

    async def _async_discover_data(
        self, *, announce: bool = True
    ) -> dict[str, ResourceSnapshot]:
        """Discover the bounded tree and use its values as the first snapshot.

        ``announce`` false: the caller reports missing parts itself once it
        kept the earlier ones (START-5).
        """
        held_back = self._shared_pause_remaining() > 0
        try:
            resources = await async_discover_resources(
                self.client,
                self.gateway.gateway_id,
                roots=(
                    *sorted(self.registered_start_counter_paths),
                    *ROOT_RESOURCE_PATHS,
                ),
                diagnostics=self.discovery_diagnostics,
                startup_data=self.startup_data,
                gateway=self.gateway,
            )
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed from err
        except RateLimited as err:
            self._activate_rate_limit_backoff(err, monotonic(), counted=not held_back)
            raise UpdateFailed("PointT rate limit reached during discovery") from err
        except PointTError as err:
            raise UpdateFailed("PointT resources could not be discovered") from err
        if not resources:
            raise UpdateFailed("PointT discovery returned no resources")

        successful_resources = dict(resources)
        candidates = tuple(
            path
            for path in fault_resource_candidates(successful_resources)
            if path not in successful_resources
        )
        probe_results: tuple[BatchItemResult, ...] = ()
        if candidates:
            try:
                probe_results = await self.client.get_resources_bulk(
                    self.gateway.gateway_id, candidates
                )
            except AuthenticationError as err:
                raise ConfigEntryAuthFailed from err
            except PointTError:
                _LOGGER.warning(
                    "Optional PointT fault-resource discovery failed; "
                    "continuing with the main resource tree"
                )
            else:
                self.faults.record_results(probe_results)
                for result in probe_results:
                    if result.resource is not None:
                        successful_resources[result.path] = result.resource

        placeholder_paths = {
            result.path
            for result in probe_results
            if result.resource is None and result.status in (403, 404)
        }
        self.resources = {
            **successful_resources,
            **{
                path: Resource(path=path)
                for path in sorted(placeholder_paths)
                if path not in successful_resources
            },
        }
        self._merge_start_counter_candidates()
        if discovery_fully_answered(self.discovery_diagnostics):
            # FAULT-2: a required fault source this tree no longer has is gone.
            self.faults.reconcile_discovery(successful_resources)
        self.faults.process_resources(
            {
                path: resource
                for path, resource in successful_resources.items()
                if is_fault_resource_path(path)
            },
            successful_paths={
                path for path in successful_resources if is_fault_resource_path(path)
            },
        )
        for path in successful_resources:
            self._record_capability(path, "success", SnapshotSource.DISCOVERY)
        for result in probe_results:
            if result.resource is None:
                self._record_capability(
                    result.path,
                    _result_category(result.error, result.status),
                    SnapshotSource.DISCOVERY,
                )
                if result.status in (403, 404):
                    pause = FORBIDDEN_PAUSE if result.status == 403 else NOT_FOUND_PAUSE
                    self._negative_until[result.path] = (
                        monotonic() + pause.total_seconds()
                    )
        self._rebuild_poll_groups()
        now_monotonic = monotonic()
        self._next_update = {
            group: now_monotonic
            + (
                ACTIVE_NOTIFICATION_INTERVAL
                if group is PollGroup.NOTIFICATIONS and self.faults.active
                else interval
            ).total_seconds()
            for group, interval in self._poll_intervals.items()
        }
        # Everything was just read: a new period for every path.
        self._spread_progress.clear()
        self._interrupted_progress.clear()
        self._failing_batch_paths = frozenset()
        for path in successful_resources:
            self._urgent_reads.discard(path)
            self._note_cloud_read(path, now_monotonic)
        updated_at = datetime.now(UTC)
        snapshots = {
            path: ResourceSnapshot(
                resource=resource,
                available=True,
                last_success=updated_at,
                last_attempt=updated_at,
                source=SnapshotSource.DISCOVERY,
            )
            for path, resource in successful_resources.items()
        }
        self._merge_startup_resources(snapshots)
        # A manual setpoint may be absent during discovery even though
        # the circuit already reports Manual. Attempt bounded recovery.
        for path, snapshot in snapshots.items():
            if (
                path.startswith("/heatingCircuits/")
                and path.endswith("/operationMode")
                and snapshot.resource.value == "manual"
            ):
                self._queue_manual_setpoint_recovery(path, snapshots)
        self._plan_discovery_recovery(successful_resources, announce=announce)
        return snapshots

    def start_from_inventory(
        self,
        resources: dict[str, Resource],
        *,
        reason: str,
        age_seconds: int | None,
        seen: Mapping[str, datetime],
        rate_limit: RateLimited | None = None,
    ) -> None:
        """Start with the stored resources while the cloud does not answer.

        Every entity exists at once but stays unavailable: no stored value is
        shown as current. The normal poll reads the stored paths directly, and
        after the first working cycle a discovery compares the inventory with
        the plant (P46 handles parts that are missing then).
        """
        self.startup_mode = "inventory"
        self._startup_reason = reason
        self._inventory_age_seconds = age_seconds
        self._stored_resources = dict(resources)
        self.resources = dict(resources)
        self._rebuild_poll_groups()
        now_monotonic = monotonic()
        self._next_update = dict.fromkeys(self._poll_intervals, now_monotonic)
        self._spread_progress.clear()
        self._interrupted_progress.clear()
        self._failing_batch_paths = frozenset()
        self.data = {
            path: ResourceSnapshot(
                resource=resource,
                available=False,
                last_success=seen.get(path, datetime.now(UTC)),
                freshness=Freshness.STALE,
                last_error_category="startup_inventory",
                # Never shown as a last good value (P12): only a real answer
                # replaces it, also after a first poll without any answer.
                ended=True,
            )
            for path, resource in resources.items()
        }
        self.last_update_success = False
        self._reconcile_due = now_monotonic
        if rate_limit is not None:
            # The account was rate limited during start-up: keep the brake.
            self._activate_rate_limit_backoff(rate_limit, now_monotonic)

    def stored_resources(self) -> dict[str, Resource]:
        """Resources to build entities from before the first fresh reading."""
        return self._stored_resources

    @contextmanager
    def stored_as_fresh(self) -> Iterator[None]:
        """Decide entity creation as a normal start would, from stored data."""
        self._assume_stored = True
        try:
            yield
        finally:
            self._assume_stored = False

    def assumed_fresh(self, path: str) -> Resource | None:
        return self._stored_resources.get(path) if self._assume_stored else None

    def _confirm_inventory(self, snapshots: dict[str, ResourceSnapshot]) -> None:
        """Once a day, mark stored resources read successfully as current."""
        if self.inventory is None:
            return
        now = monotonic()
        if (
            self._inventory_confirmed_at is not None
            and now - self._inventory_confirmed_at < INVENTORY_CONFIRM_SECONDS
        ):
            return
        fresh = [path for path, item in snapshots.items() if _snapshot_is_fresh(item)]
        if not fresh:
            return
        self._inventory_confirmed_at = now
        self.inventory.confirm(self.gateway.gateway_id, fresh)

    async def _async_firmware_discovery(self) -> dict[str, ResourceSnapshot]:
        """After a firmware change, discover the plant once more (P22)."""
        watch = self.firmware
        watch.attempts += 1
        previous_resources = dict(self.resources)
        previous_data = dict(self.data or {})
        # New firmware: earlier "not found"/"forbidden" pauses no longer hold.
        self._negative_until.clear()
        try:
            discovered = await self._async_discover_data(announce=False)
        except UpdateFailed:
            self._discovery_cycle_failed = True
            now = monotonic()
            if now < self._cloud_backoff_until:
                watch.attempts -= 1
                watch.postponed += 1
                watch.due = self._cloud_backoff_until
                watch.result = "postponed"
            else:
                watch.failed(now, RECOVERY_SCHEDULE)
            return dict(self.data or {})
        watch.due = None
        watch.result = "success"
        self._keep_previous_resources(previous_resources, previous_data, discovered)
        self.discovery_recovery.announce()
        _LOGGER.info(
            "PointT capabilities checked again after a firmware change: %d resources",
            len(self.resources),
        )
        return discovered

    def _keep_previous_resources(
        self,
        previous_resources: dict[str, Resource],
        previous_data: dict[str, ResourceSnapshot],
        discovered: dict[str, ResourceSnapshot],
    ) -> None:
        """After a further discovery, readings it did not report stay."""
        # P41: a reading the gateway does not report right after its update is
        # kept and polled as before; its own answers decide (404 ends it).
        room = max(0, MAX_DISCOVERY_RESOURCES - len(self.resources))
        kept = dict(
            islice(
                (
                    (path, resource)
                    for path, resource in previous_resources.items()
                    if path not in self.resources
                ),
                room,
            )
        )
        if kept:
            self.resources.update(kept)
            self._rebuild_poll_groups()
            self._kept_after_firmware = set(kept)
            for path in kept:
                if path in previous_data and path not in discovered:
                    discovered[path] = previous_data[path]
                # Polled as before; not also searched as missing (P46).
                self.discovery_recovery.missing.pop(path, None)
            recovery = self.discovery_recovery
            # START-5: only the parts not kept are missing.
            recovery.initially_missing = len(recovery.missing)
            if recovery.state == "pending" and not recovery.missing:
                recovery.state, recovery._next_due = "complete", None

    def rescan_state(self) -> str:
        """A6: whether a requested discovery can be planned now.

        ``ready``, ``paused`` (a cloud or protection pause holds),
        ``running`` (a discovery is already planned or running) or
        ``unavailable`` (the cloud does not answer now).
        """
        now = monotonic()
        if (
            self._cloud_auth_required()
            or self._cloud_source_stopped()
            or not self.last_update_success
        ):
            return "unavailable"
        shared = getattr(getattr(self.client, "backoff", None), "remaining", 0.0)
        shared = shared if isinstance(shared, int | float) else 0.0
        if (
            max(self._cloud_backoff_until - now, shared) > 0
            or now < self._circuit_open_until
        ):
            return "paused"
        if (
            self._rescan_due is not None
            or self.rescan_result == "running"
            or self.firmware.due is not None
            or self._reconcile_due is not None
        ):
            return "running"
        return "ready"

    def request_rescan(self) -> str:
        """A6: search the plant again with the next regular cycle.

        Within the normal cloud limits: the discovery waits for a pause and
        keeps every reading it does not report (no entity is removed).
        Returns ``planned`` or the reason it is refused (``rescan_state``).
        """
        state = self.rescan_state()
        if state != "ready":
            return state
        self._rescan_due = monotonic()
        self.rescan_result = "planned"
        return "planned"

    async def _async_rescan(self) -> dict[str, ResourceSnapshot]:
        """A6: the requested discovery, like the one after a firmware change."""
        self._rescan_due = None
        self.rescan_result = "running"
        previous_resources = dict(self.resources)
        previous_data = dict(self.data or {})
        self._negative_until.clear()
        try:
            discovered = await self._async_discover_data(announce=False)
        except UpdateFailed:
            self._discovery_cycle_failed = True
            if monotonic() < self._cloud_backoff_until:
                # A pause moves the discovery; it is not lost.
                self._rescan_due = self._cloud_backoff_until
                self.rescan_result = "postponed"
            else:
                self.rescan_result = "failed"
            return dict(self.data or {})
        self.rescan_result = "success"
        self._keep_previous_resources(previous_resources, previous_data, discovered)
        self.discovery_recovery.announce()
        _LOGGER.info(
            "PointT discovery completed on request: %d resources", len(self.resources)
        )
        return discovered

    async def _async_reconcile(self) -> dict[str, ResourceSnapshot]:
        """After a start with the inventory, discover the plant once."""
        self._reconcile_attempts += 1
        try:
            discovered = await self._async_discover_data()
        except UpdateFailed:
            self._discovery_cycle_failed = True
            now = monotonic()
            if now < self._cloud_backoff_until:
                # A rate limit moves the discovery; it does not use an attempt.
                self._reconcile_attempts -= 1
                self._reconcile_postponed += 1
                self._reconcile_due = self._cloud_backoff_until
                self._reconcile_result = "postponed"
            else:
                index = min(self._reconcile_attempts, len(RECOVERY_SCHEDULE)) - 1
                self._reconcile_due = now + RECOVERY_SCHEDULE[index]
                self._reconcile_result = "failed"
            return dict(self.data or {})
        self._reconcile_due = None
        self._reconcile_result = "success"
        self._stored_resources = {}
        # This discovery also checked the capabilities of a changed firmware.
        self.firmware.due = None
        _LOGGER.info(
            "PointT discovery completed after starting with the saved inventory"
        )
        return discovered

    def startup_inventory_diagnostics(self, now: float) -> dict[str, object]:
        """How this gateway started, without identifiers or values."""
        return {
            "mode": self.startup_mode,
            "reason": self._startup_reason,
            "inventory_age_hours": (
                self._inventory_age_seconds // 3600
                if self._inventory_age_seconds is not None
                else None
            ),
            "discovery_attempts": self._reconcile_attempts,
            "discovery_postponed": self._reconcile_postponed,
            "discovery_result": self._reconcile_result,
            "next_discovery_in_seconds": (
                max(0, math.ceil(self._reconcile_due - now))
                if self._reconcile_due is not None
                else None
            ),
        }

    def _drop_kept_resource(self, path: str) -> None:
        """A reading kept after a firmware update that now answers 404."""
        self._kept_after_firmware.discard(path)
        if self.resources.pop(path, None) is not None:
            self._rebuild_poll_groups()

    def _rebuild_poll_groups(self) -> None:
        self._paths_by_group = {
            group: tuple(
                path
                for path, resource in self.resources.items()
                if self._poll_group(resource) is group
            )
            for group in self._poll_intervals
        }

    def _missing_reason(self, path: str) -> str:
        report = self.discovery_diagnostics.paths.get(path)
        if report is None:
            return "not_referenced"
        return report.fallback_result or report.bulk_result

    def _plan_discovery_recovery(
        self, discovered: dict[str, Resource], *, announce: bool = True
    ) -> None:
        """Compare with the parts known before and plan a bounded search (P46)."""
        reference = (
            self.inventory.resources(self.gateway.gateway_id)
            if self.inventory is not None
            else {}
        )
        self.firmware.compare_with_last_start(
            firmware_values(reference), firmware_values(discovered)
        )
        missing = {
            path: self._missing_reason(path)
            for path in reference
            if path not in self.resources
            and path != LICENSE_PATH
            and not is_startup_path(path)
        }
        # Without a stored inventory (first start, upgrade), a root that ended
        # with a temporary error is searched again as well. With an inventory,
        # a root never seen before is not expected.
        for root in () if reference else ROOT_RESOURCE_PATHS:
            if root in self.resources or root in missing:
                continue
            reason = self._missing_reason(root)
            if self.discovery_diagnostics.paths.get(root) is not None and (
                is_transient_result(reason)
            ):
                missing[root] = reason
        # #113 (RRC-01): RRC zones without their program list offer no program
        # selection. A list that failed or was not read is searched again on
        # its own, within the same bounded schedule as every other part.
        if (
            program_list_expected(self.resources)
            and PROGRAM_LIST not in self.resources
            and PROGRAM_LIST not in missing
        ):
            missing[PROGRAM_LIST] = self._missing_reason(PROGRAM_LIST)
        if self.inventory is not None:
            self.inventory.record(self.gateway, discovered)
        self.discovery_recovery.plan(
            missing, len(reference), monotonic(), announce=announce
        )

    async def _async_recover_missing(
        self, snapshots: dict[str, ResourceSnapshot]
    ) -> None:
        """Search the missing subtrees once, after this cycle's poll."""
        recovery = self.discovery_recovery
        now_monotonic = monotonic()
        budget = MAX_DISCOVERY_RESOURCES - len(self.resources)
        if budget <= 0:
            self._give_up_recovery("resource_limit")
            return
        report = DiscoveryDiagnostics()
        held_back = self._shared_pause_remaining() > 0
        try:
            found = await async_discover_resources(
                self.client,
                self.gateway.gateway_id,
                roots=recovery.roots(),
                maximum_resources=budget,
                diagnostics=report,
                # Start-up values are queued, not read, within the shared budget.
                startup_data=self.startup_data,
                gateway=self.gateway,
                rediscovery=True,
            )
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed from err
        except RateLimited as err:
            # The poll already succeeded; the brake applies from the next cycle.
            self._activate_rate_limit_backoff(err, monotonic(), counted=not held_back)
            recovery.postpone()
            return
        except PointTError as err:
            if _connection_problem(err) or isinstance(err, ServiceUnavailable):
                # No answer is no result: the search is repeated later without
                # using up an attempt (P41).
                recovery.postpone(now_monotonic)
                return
            recovery.finish((), now_monotonic, result=_result_category(err, None))
            if recovery.state == "given_up":
                self._forget_given_up()
            return
        found = {
            path: resource
            for path, resource in found.items()
            if not is_startup_path(path) and path != LICENSE_PATH
        }
        added = {
            path: resource
            for path, resource in found.items()
            if path not in self.resources
        }
        if added:
            self.resources.update(added)
            self._rebuild_poll_groups()
            updated_at = datetime.now(UTC)
            for path in added:
                self._record_capability(path, "success", SnapshotSource.DISCOVERY)
            # The listeners of every platform add entities for fresh resources.
            snapshots.update(
                {
                    path: ResourceSnapshot(
                        resource=resource,
                        available=True,
                        last_success=updated_at,
                        last_attempt=updated_at,
                        source=SnapshotSource.DISCOVERY,
                    )
                    for path, resource in added.items()
                }
            )
            faults = {
                path: resource
                for path, resource in added.items()
                if is_fault_resource_path(path)
            }
            if faults:
                self.faults.process_resources(faults, successful_paths=set(faults))
            for path, resource in added.items():
                if (
                    path.startswith("/heatingCircuits/")
                    and path.endswith("/operationMode")
                    and resource.value == "manual"
                ):
                    self._queue_manual_setpoint_recovery(path, snapshots)
        if self.inventory is not None and found:
            self.inventory.record(self.gateway, found)
        recovery.finish(
            found, now_monotonic, result="success" if found else "nothing_found"
        )
        if recovery.state == "given_up":
            self._forget_given_up()

    def _give_up_recovery(self, result: str) -> None:
        self.discovery_recovery.give_up(result)
        self._forget_given_up()

    def _forget_given_up(self) -> None:
        """After a full day of searching, a part is no longer expected.

        Its registry entries stay; a later discovery that finds it again simply
        brings it back. This avoids a warning at every start for parts that
        were removed or exist only in some operating modes.
        """
        if self.inventory is not None:
            self.inventory.forget(
                self.gateway.gateway_id, tuple(self.discovery_recovery.missing)
            )

    def _merge_startup_resources(self, snapshots: dict[str, ResourceSnapshot]) -> None:
        """Keep the actual read timestamp, including across automatic setup retries."""
        for read in self.startup_data.cached_reads():
            if read.resource is None or read.last_success is None:
                continue
            path, resource = read.path, read.resource
            if path in snapshots and snapshots[path].last_success >= read.last_success:
                continue
            self.resources[path] = resource
            snapshots[path] = ResourceSnapshot(
                resource=resource,
                available=True,
                last_success=read.last_success,
                last_attempt=read.last_attempt,
                source=SnapshotSource.STARTUP,
            )
        self._paths_by_group = {
            group: tuple(
                path
                for path, resource in self.resources.items()
                if self._poll_group(resource) is group
            )
            for group in self._poll_intervals
        }

    async def async_read_startup_batch(
        self,
        snapshots: dict[str, ResourceSnapshot],
        *,
        budget: float = STARTUP_BUDGET_SECONDS,
    ) -> bool:
        """Apply supplementary results to HA without changing main-poll health."""
        held_back = self._shared_pause_remaining() > 0
        try:
            batch = await self._startup_reader.async_read_batch(
                self.resources.keys(), budget=budget
            )
        except AuthenticationError as err:
            self.startup_session.close()
            raise ConfigEntryAuthFailed from err
        for path, category in batch.outcomes:
            self._record_capability(path, category, SnapshotSource.STARTUP)
        if batch.rate_limit is not None:
            self._activate_rate_limit_backoff(
                batch.rate_limit, monotonic(), counted=not held_back
            )
        if batch.attempted:
            self._merge_startup_resources(snapshots)
            await self._async_prepare_holiday_timezones()
        return batch.attempted

    async def _async_prepare_holiday_timezones(self) -> None:
        """Prepare named zones before publishing resource snapshots to listeners."""
        self.holiday_timezones = await async_resolve_holiday_timezones(
            self.resources, self.holiday_timezones
        )

    async def _async_core_fallback(
        self, chunk: tuple[str, ...]
    ) -> tuple[BatchItemResult, ...]:
        """Read only a small set of essential paths after one batch failure."""
        prioritized = tuple(path for path in _FALLBACK_PRIORITY_PATHS if path in chunk)[
            :MAX_FALLBACK_PATHS
        ]
        return await self._async_single_get_fallback(prioritized)

    async def _async_bulk_item_fallback(
        self, results: tuple[BatchItemResult, ...]
    ) -> tuple[BatchItemResult, ...]:
        """Retry a bounded set of recoverable bulk items with individual GETs."""
        recoverable = tuple(
            (result.path, reason)
            for result in results
            if (reason := result.fallback_reason) is not None
        )
        if not recoverable:
            return ()
        reasons = dict(recoverable)
        paths = tuple(path for path, _reason in recoverable)
        selected = tuple(path for path in _FALLBACK_PRIORITY_PATHS if path in paths)
        selected += tuple(path for path in paths if path not in selected)
        selected = selected[:MAX_FALLBACK_PATHS]
        return await self._async_single_get_fallback(
            selected,
            reasons={path: reasons[path] for path in selected},
        )

    async def _async_single_get_fallback(
        self,
        paths: tuple[str, ...],
        *,
        reasons: dict[str, str] | None = None,
    ) -> tuple[BatchItemResult, ...]:
        """Read a bounded, preselected set of paths individually."""
        results: list[BatchItemResult] = []
        for path in paths:
            fallback_reason = (
                reasons.get(path, "batch_failure") if reasons else "batch_failure"
            )
            held_back = self._shared_pause_remaining() > 0
            try:
                resource = await self.client.get_resource(
                    self.gateway.gateway_id,
                    path,
                    fallback_reason=fallback_reason,
                )
            except AuthenticationError as err:
                self._record_capability(
                    path, "authentication_error", SnapshotSource.FALLBACK
                )
                raise ConfigEntryAuthFailed from err
            except RateLimited as err:
                self._record_capability(path, "rate_limited", SnapshotSource.FALLBACK)
                self._activate_rate_limit_backoff(
                    err, monotonic(), counted=not held_back
                )
                break
            except ResourceError as err:
                results.append(
                    BatchItemResult(
                        gateway_id=self.gateway.gateway_id,
                        path=path,
                        status=err.status,
                        error=err,
                    )
                )
            except PointTError as err:
                status = err.status if isinstance(err, UnexpectedHttpStatus) else None
                if (
                    isinstance(err, ProtocolError)
                    and not isinstance(err, ResourceOmitted)
                ) or _explicit_client_error(status):
                    # Invalid data or a client error for this very reading ends
                    # it; _apply_results records the capability result.
                    results.append(
                        BatchItemResult(
                            gateway_id=self.gateway.gateway_id,
                            path=path,
                            status=status,
                            error=err,
                        )
                    )
                    continue
                self._record_capability(
                    path, _pointt_result(err), SnapshotSource.FALLBACK
                )
                if _connection_problem(err):
                    # A clear connection problem: no further single reads.
                    break
                continue
            else:
                results.append(
                    BatchItemResult(
                        gateway_id=self.gateway.gateway_id,
                        path=path,
                        status=200,
                        resource=resource,
                    )
                )
        return tuple(results)

    @staticmethod
    def _log_unusable_bulk_results(
        results: tuple[BatchItemResult, ...],
    ) -> None:
        """Log value-free details for bulk items that could not be parsed."""
        for result in results:
            if result.resource is not None:
                continue
            _LOGGER.debug(
                "Ignoring PointT bulk item %s: status=%s, server_status=%s, "
                "gateway_status=%s, error=%s (%s)",
                resource_path_template(result.path),
                result.status,
                result.server_status,
                result.gateway_status,
                type(result.error).__name__ if result.error else "none",
                result.error or "no parsed resource",
            )

    def _apply_results(
        self,
        snapshots: dict[str, ResourceSnapshot],
        results: tuple[BatchItemResult, ...],
        *,
        attempted_at: datetime,
        now_monotonic: float,
        source: SnapshotSource,
        whole_request: bool = False,
        pause_readings: bool = True,
    ) -> int:
        """Apply partial results without discarding last-good values.

        ``pause_readings`` false: a failure pauses no reading of its own (B02).
        """
        successful = 0
        affected_modes: set[str] = set()
        missing_manual_setpoints: set[str] = set()
        for result in results:
            if (mode_path := _manual_setpoint_mode_path(result.path)) is not None:
                affected_modes.add(mode_path)
            if result.resource is not None:
                previous = snapshots.get(result.path)
                if previous is not None:
                    resets, decreases = _energy_decreases(
                        previous.resource, result.resource
                    )
                    self._energy_counter_resets += resets
                    self._produced_heat_decreases += decreases
                self._record_capability(result.path, "success", source)
                self._note_cloud_read(result.path, now_monotonic)
                snapshots[result.path] = ResourceSnapshot(
                    resource=result.resource,
                    available=True,
                    last_success=attempted_at,
                    last_attempt=attempted_at,
                    source=source,
                )
                self.resources[result.path] = result.resource
                if (
                    result.path in self._manual_setpoint_candidates
                    or is_start_counter_path(result.path)
                ):
                    # Both a targeted read and a later regular read can discover
                    # the value for the first time. Register normal polling once.
                    group = self._poll_group(result.resource)
                    if (
                        group in self._poll_intervals
                        and result.path not in self._paths_by_group.get(group, ())
                    ):
                        self._paths_by_group[group] = (
                            *self._paths_by_group.get(group, ()),
                            result.path,
                        )
                self._negative_until.pop(result.path, None)
                successful += 1
                continue

            result_category = _result_category(result.error, result.status)
            self._record_capability(result.path, result_category, source)
            if (
                result.status == 404
                and result.path.startswith("/heatingCircuits/")
                and result.path.endswith("/manualRoomSetpoint")
            ):
                missing_manual_setpoints.add(result.path)
            previous = snapshots.get(result.path)
            if previous is not None:
                category = _error_category(result.error, result.status)
                no_answer = _no_answer(result, whole_request=whole_request)
                if no_answer and not _transient_failure(category):
                    category = f"{NO_ANSWER_PREFIX}{category}"
                failed = replace(
                    previous,
                    available=False,
                    last_attempt=attempted_at,
                    freshness=Freshness.STALE,
                    last_error_category=category,
                    consecutive_failures=previous.consecutive_failures + 1,
                    ended=previous.ended or not no_answer,
                )
                snapshots[result.path] = failed
                if pause_readings:
                    self._pause_failed_resource(failed, result.status, now_monotonic)
            elif result.status in (403, 404):
                if result.status == 404 and (
                    result.path in self._manual_setpoint_candidates
                    or result.path in self.registered_start_counter_paths
                ):
                    # Registered counters and pending manual targets recover
                    # through their regular groups. A 404 must not introduce
                    # a day-long pause for either established recovery path.
                    self._negative_until.pop(result.path, None)
                else:
                    pause = FORBIDDEN_PAUSE if result.status == 403 else NOT_FOUND_PAUSE
                    self._negative_until[result.path] = (
                        now_monotonic + pause.total_seconds()
                    )
            _LOGGER.debug(
                "PointT resource unavailable: %s", resource_path_template(result.path)
            )
        for mode_path in sorted(affected_modes):
            self._queue_manual_setpoint_recovery(mode_path, snapshots)
        for setpoint_path in missing_manual_setpoints:
            mode_path = self._manual_setpoint_candidates.get(setpoint_path)
            if mode_path is not None and _manual_mode_active(mode_path, snapshots):
                # Only an absent manual target on a confirmed manual circuit
                # gets this exception. Forbidden-resource pauses remain intact.
                self._negative_until.pop(setpoint_path, None)
        return successful

    def _record_capability(
        self, path: str, result: str, source: SnapshotSource
    ) -> None:
        metrics = self._capability_metrics.get(path)
        if metrics is None:
            metrics = self._capability_metrics[path] = CapabilityMetrics()
        metrics.record(result=result, source=source)

    def _record_chunk_failure(
        self, paths: tuple[str, ...], result: str, source: SnapshotSource
    ) -> None:
        for path in paths:
            self._record_capability(path, result, source)

    def _pause_failed_resource(
        self,
        snapshot: ResourceSnapshot,
        status: int | None,
        now_monotonic: float,
    ) -> None:
        pause: timedelta | None = None
        if status == 404:
            if snapshot.resource.path in self._kept_after_firmware:
                # Kept only through a firmware update: its own 404 ends it.
                self._drop_kept_resource(snapshot.resource.path)
                return
            # A last-good snapshot proves this resource exists. Retry it at its
            # next group poll; another read after an error is permitted.
            self._negative_until.pop(snapshot.resource.path, None)
            return
        elif (
            status == 403 and snapshot.consecutive_failures >= FORBIDDEN_PAUSE_THRESHOLD
        ):
            pause = FORBIDDEN_PAUSE
        elif status == 504:
            pause = GATEWAY_TIMEOUT_PAUSE
        if pause is not None:
            self._negative_until[snapshot.resource.path] = (
                now_monotonic + pause.total_seconds()
            )

    def _outage_probe(
        self, paths_by_group: dict[PollGroup, tuple[str, ...]], now_monotonic: float
    ) -> str | None:
        """COORD-3: the reading the cloud answered last, outside the failing batch.

        Read beside the failing batch, it shows whether the cloud still
        answers; during an outage it stays the same reading. No fault source
        (their reads keep their own cadence), no reading an explicit answer
        ended and no locally shown reading whose cloud read is not due (O45).
        """
        data = self.data or {}
        probe: tuple[datetime, str] | None = None
        for group, group_paths in paths_by_group.items():
            if group is PollGroup.NOTIFICATIONS:
                continue
            for path in group_paths:
                snapshot = data.get(path)
                if (
                    snapshot is None
                    or snapshot.ended
                    or snapshot.source is SnapshotSource.LOCAL
                    or path not in self.resources
                    or path in self._failing_batch_paths
                    or self._resource_is_paused(path, now_monotonic)
                    or self._served_skip(path, now_monotonic)
                ):
                    continue
                if probe is None or snapshot.last_success > probe[0]:
                    probe = (snapshot.last_success, path)
        return probe[1] if probe is not None else None

    def _resource_is_paused(self, path: str, now_monotonic: float) -> bool:
        until = self._negative_until.get(path)
        if until is None:
            return False
        if now_monotonic >= until:
            del self._negative_until[path]
            return False
        return True

    def _advance_groups(
        self, groups: tuple[PollGroup, ...], now_monotonic: float
    ) -> None:
        for group in groups:
            self._next_update[group] = (
                now_monotonic + self._group_interval(group).total_seconds()
            )
            # A new period: paths read before this deadline are due again.
            self._spread_progress.pop(group, None)
            self._interrupted_progress.pop(group, None)

    def _progress(self, group: PollGroup, now_monotonic: float) -> set[str]:
        """Paths of a group already read for its current period (O41 slots,
        P41 budget); they wait for the group to complete.

        This holds only while the group keeps making progress: once none of
        its paths was read for one period of the group and one wake-up, the
        group starts over and every path is read again. A group that stays
        incomplete (another of its paths keeps failing) therefore never keeps
        a path unread until its value expires, while a group split over
        several budget-limited cycles still reads each path once. One wake-up
        of slack, because a slot read and the group's own read each fall on
        the first wake-up after their time.

        COORD-3: a path read in a cycle that a failed batch interrupted waits
        only for its own interval: read again from then on, it keeps its pace
        while the failing batch keeps the group due.
        """
        interval = self._group_interval(group).total_seconds()
        interrupted = {
            path
            for path, read_at in self._interrupted_progress.get(group, {}).items()
            if now_monotonic + POLL_DUE_TOLERANCE_SECONDS < read_at + interval
        }
        read = {
            **self._budget_progress.get(group, {}),
            **self._spread_progress.get(group, {}),
        }
        limit = interval + self._tick_seconds
        if not read or now_monotonic - max(read.values()) >= limit:
            return interrupted
        return set(read) | interrupted

    def _group_interval(self, group: PollGroup) -> timedelta:
        return (
            ACTIVE_NOTIFICATION_INTERVAL
            if group is PollGroup.NOTIFICATIONS and self.faults.active
            else self._poll_intervals[group]
        )

    def _slot_plan(self) -> dict[str, tuple[PollGroup, int]]:
        """O41: the slot of each path, planned again when the groups change."""
        key = tuple(self._paths_by_group.items())
        if key != self._poll_slots_key:
            tick = self._tick_seconds
            self._poll_slots = plan_poll_slots(
                self._paths_by_group,
                {
                    group: max(1, round(interval.total_seconds() / tick))
                    for group, interval in self._poll_intervals.items()
                },
                capacity=MAX_BULK_PATHS,
                previous=self._poll_slots,
                # FAULT-1: the fault sources only count when read together.
                atomic=(PollGroup.NOTIFICATIONS,),
            )
            self._poll_slots_key = key
        return self._poll_slots

    def _early_reads(
        self,
        paths_by_group: Mapping[PollGroup, tuple[str, ...]],
        due_groups: tuple[PollGroup, ...],
        now_monotonic: float,
    ) -> tuple[tuple[PollGroup, str], ...]:
        """O41: paths of groups not due yet whose slot is due in this cycle.

        Slot ``s`` is read ``s`` ticks before the group's deadline, once per
        period of the group; the group itself then reads only the rest.
        """
        slots = self._slot_plan()
        tick = self._tick_seconds
        early: list[tuple[PollGroup, str]] = []
        for group, paths in paths_by_group.items():
            deadline = self._next_update.get(group)
            if group in due_groups or deadline is None:
                continue
            period = round(self._group_interval(group).total_seconds() / tick)
            done = self._progress(group, now_monotonic)
            for path in paths:
                planned = slots.get(path)
                if (
                    planned is None
                    or planned[0] is not group
                    or not 0 < planned[1] < period
                    or path in done
                    or now_monotonic + POLL_DUE_TOLERANCE_SECONDS
                    < deadline - planned[1] * tick
                    or self._served_skip(path, now_monotonic)
                ):
                    continue
                early.append((group, path))
        return tuple(early)

    def _served_skip(self, path: str, now_monotonic: float) -> bool:
        """O45: a locally shown reading whose slower cloud read is not due.

        Its group's read nearest to the due time counts (half an interval
        early at most), so the reads stay in step with the group.
        """
        due = self._local_served.get(path)
        if due is None:
            return False
        resource = self.resources.get(path)
        interval = (
            self._poll_intervals.get(self._validity_group(resource))
            if resource is not None
            else None
        )
        if interval is None:
            return False
        return now_monotonic + interval.total_seconds() / 2 < due

    def _note_cloud_read(self, path: str, now_monotonic: float) -> None:
        """A successful cloud read: the slower rate counts from here."""
        self._served_left.pop(path, None)
        if path in self._local_served:
            period = self._served_period(path)
            if period is None:
                del self._local_served[path]
            else:
                self._local_served[path] = now_monotonic + period.total_seconds()

    def _record_gateway_failure(self, now_monotonic: float) -> None:
        self._gateway_failure_count += 1
        if self._gateway_failure_count >= CIRCUIT_FAILURE_THRESHOLD:
            pause = CIRCUIT_OPEN_INTERVAL.total_seconds()
            self._circuit_open_until = now_monotonic + pause
            if self._circuit_outage_since is None:
                # O80: one warning when the breaker opens; while the outage
                # lasts, each further opening is a debug message.
                self._circuit_outage_since = now_monotonic
                _LOGGER.warning(
                    "PointT gateway circuit breaker opened after %d consecutive "
                    "failures; the next attempt follows in %.0f seconds",
                    self._gateway_failure_count,
                    pause,
                )
            else:
                _LOGGER.debug(
                    "PointT gateway circuit breaker opened again after %d "
                    "consecutive failures; the next attempt follows in %.0f seconds",
                    self._gateway_failure_count,
                    pause,
                )

    def _log_batch_failure(self, category: str, *, fallback_usable: bool) -> None:
        """O81: warn once per outage; a working fallback is only a debug note."""
        if fallback_usable:
            _LOGGER.debug(
                "PointT batch refresh failed (%s); the bounded core fallback answered",
                category,
            )
            return
        if self._batch_failure_logged:
            _LOGGER.debug(
                "PointT batch refresh failed again (%s); the bounded core "
                "fallback did not answer",
                category,
            )
            return
        self._batch_failure_logged = True
        _LOGGER.warning(
            "PointT batch refresh failed (%s); the bounded core fallback did not "
            "answer either. Further failures of this outage are logged at debug "
            "level",
            category,
        )

    def _record_gateway_recovery(self, now_monotonic: float) -> None:
        """Close the breaker after a usable cycle; report a recovered outage once."""
        self._gateway_failure_count = 0
        self._circuit_open_until = 0.0
        # The batch warning (O81) ends only with a cycle without a failed
        # batch (COORD-3), not with every usable one.
        since, self._circuit_outage_since = self._circuit_outage_since, None
        if since is not None:
            _LOGGER.info(
                "PointT gateway answers again; the circuit breaker closed after "
                "%.0f seconds",
                max(0.0, now_monotonic - since),
            )

    def _shared_pause_remaining(self) -> float:
        """Seconds the pause of the whole cloud access still holds requests."""
        shared = getattr(getattr(self.client, "backoff", None), "remaining", 0.0)
        if isinstance(shared, bool) or not isinstance(shared, int | float):
            return 0.0
        return max(0.0, float(shared))

    def _activate_rate_limit_backoff(
        self, error: RateLimited, now_monotonic: float, *, counted: bool = True
    ) -> None:
        """Pause cloud polling after a rate limit.

        ``counted`` false: the pause of the cloud access held the request back
        before it was sent (CHAOS-5). It pauses this gateway as well, but is
        no event of its own: no count, no warning and no repair.
        """
        requested = (
            DEFAULT_RATE_LIMIT_BACKOFF.total_seconds()
            if error.retry_after is None
            else error.retry_after
        )
        update_interval = self.update_interval or DEFAULT_RATE_LIMIT_BACKOFF
        delay = min(
            max(requested, update_interval.total_seconds()),
            MAX_RATE_LIMIT_BACKOFF.total_seconds(),
        )
        if not counted:
            self._cloud_backoff_until = max(
                self._cloud_backoff_until, now_monotonic + delay
            )
            return
        self._cloud_backoff_until = now_monotonic + delay
        self._rate_limit_events += 1
        # CHAOS-5: repeated means within the window; older limits expire.
        self._rate_limit_times.append(now_monotonic)
        window = RATE_LIMIT_REPAIR_WINDOW.total_seconds()
        while now_monotonic - self._rate_limit_times[0] >= window:
            self._rate_limit_times.popleft()
        if (
            len(self._rate_limit_times) >= RATE_LIMIT_REPAIR_THRESHOLD
            # The repair offers the cloud-friendly profile; with it in use
            # already, it would change nothing and only come back.
            and self.polling_profile is not PollingProfile.CLOUD_FRIENDLY
        ):
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                repair_id(
                    RATE_LIMIT_ISSUE_PREFIX, self._config_entry, self.connection_id
                ),
                data=repair_data(self._config_entry, self.connection_id),
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="managed_rate_limit"
                if self._config_entry.version == 4
                else "repeated_rate_limit",
            )
        _LOGGER.warning(
            "PointT rate limit reached; cloud polling paused for %.0f seconds",
            delay,
        )


def _error_category(error: PointTError | None, status: int | None) -> str:
    if status is not None:
        return f"http_{status}"
    if error is not None:
        return error.__class__.__name__.lower()
    return "unknown"


def _result_category(error: PointTError | None, status: int | None) -> str:
    """Reduce a resource result to a stable, understandable category."""
    if status is not None and 200 <= status < 300:
        return "success"
    if status is None:
        return _pointt_result(error)
    return {
        403: "forbidden",
        404: "not_found",
        429: "rate_limited",
        502: "service_unavailable",
        503: "service_unavailable",
        504: "service_unavailable",
    }.get(status, _pointt_result(error))


def _cycle_failure_category(results: list[BatchItemResult]) -> str:
    """The most frequent error category of a cycle without usable readings."""
    categories = Counter(
        _result_category(result.error, result.status)
        for result in results
        if result.resource is None
    )
    categories.pop("success", None)
    if not categories:
        return "unknown"
    return max(sorted(categories), key=categories.__getitem__)


def _pointt_result(error: PointTError | None) -> str:
    if error is None:
        return "unknown"
    name = error.__class__.__name__.lower()
    if "timeout" in name:
        return "timeout"
    if "authentication" in name or "token" in name:
        return "authentication_error"
    if "ratelimit" in name:
        return "rate_limited"
    if "unavailable" in name:
        return "service_unavailable"
    return "request_failed"


def _percentile(values: Iterable[float], share: float) -> float | None:
    """The nearest-rank percentile; None without values."""
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


def _rounded_duration(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


def _percentage(successful: int, total: int) -> float | None:
    return round(successful / total * 100, 1) if total else None


# NRG-3: the produced heat of an energy collection is a net total that falls
# while the heat pump defrosts; its entity is a ``total``, not a
# ``total_increasing`` counter. Its decreases are counted on their own.
_PRODUCED_HEAT_PART = "outputProduced"


def _energy_decreases(previous: Resource, current: Resource) -> tuple[int, int]:
    """Counter resets and produced-heat decreases between two readings.

    Compared in kWh as the entities show them (``assess_energy``), so a unit
    change is no reset; a part without a valid reading in both is skipped.
    """
    if previous.path != current.path or not (
        is_energy_collection(previous) and is_energy_collection(current)
    ):
        return 0, 0
    before = _energy_kwh(previous)
    resets = decreases = 0
    for key, value in _energy_kwh(current).items():
        earlier = before.get(key)
        if (
            earlier is None
            or value >= earlier
            or math.isclose(value, earlier, rel_tol=1e-9, abs_tol=1e-9)
        ):
            continue
        if key == _PRODUCED_HEAT_PART:
            decreases += 1
        else:
            resets += 1
    return resets, decreases


def _energy_kwh(resource: Resource) -> dict[str, float]:
    """The valid parts of an energy collection, in kWh."""
    return {
        key: reading.value
        for key, reading in assess_energy(resource).readings
        if reading.value is not None
    }
