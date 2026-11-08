"""Fault parsing, lifecycle tracking, and privacy-safe persistence."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .pointt import Resource
from .pointt.models import BatchItemResult, JsonValue

FAULT_STORAGE_VERSION = 1
FAULT_STORAGE_SAVE_DELAY = 1.0
FAULT_RESOLUTION_CONFIRMATIONS = 2
# P42: without a new local error list for this long, the cloud decides alone
# again (as before P42); also the grace period after a restart. At least three
# local query intervals, so a slow interval or one missed reading never counts.
LOCAL_FAULT_MAX_AGE = timedelta(minutes=15)
LOCAL_FAULT_MISSED_READINGS = 3
# A1: how long a view that handed its baseline to a starting source shows it
# unchanged at most.
HAND_OVER_HOLD = LOCAL_FAULT_MAX_AGE
MAX_FAULT_ATTRIBUTES = 25

_LOGGER = logging.getLogger(__name__)

_NOTIFICATIONS_PATH = "/notifications"
_HEAT_SOURCE_PATH = re.compile(r"^/heatSources/([^/]+)(?:/|$)")
_DEVICE_PATH = re.compile(r"^/devices/([^/]+)(?:/|$)")
_ACTIVE_FAILURE_PATH = re.compile(r"^/heatSources/[^/]+/activefailure$", re.IGNORECASE)
_FAILURE_LIST_PATH = re.compile(r"^/heatSources/[^/]+/failurelist$", re.IGNORECASE)
_DEVICE_ERRORS_PATH = re.compile(r"^/devices/[^/]+/errors$", re.IGNORECASE)
# B3: the local device error lists of the documented device slots.
_LOCAL_DEVICE_ERRORS_PATH = re.compile(
    r"/devices/device([1-9]|[12][0-9]|3[0-2])/errors"
)
_DEVICE_ERROR_TYPE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
MAX_DEVICE_ERRORS = 16
MAX_DEVICE_ERROR_LISTS = 32

_KNOWN_SUMMARIES: dict[tuple[str, str | None], dict[str, str]] = {
    ("6249", None): {
        "en": "Communication between indoor and outdoor unit interrupted",
        "de": "Kommunikation zwischen Innen- und Außeneinheit gestört",
    },
    ("1000", "A11"): {
        "en": "System configuration not confirmed",
        "de": "Systemkonfiguration nicht bestätigt",
    },
    ("1038", None): {
        "en": "Date or time value invalid",
        "de": "Zeit- oder Datumswert ungültig",
    },
}


class FaultSeverity(StrEnum):
    """Manufacturer-independent notification severity."""

    INFO = "info"
    MAINTENANCE = "maintenance"
    WARNING = "warning"
    FAULT = "fault"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class FaultTimeSource(StrEnum):
    """Origin of a fault timestamp."""

    DEVICE = "device"
    CLOUD = "cloud"
    HOME_ASSISTANT_OBSERVED = "home_assistant_observed"
    UNKNOWN = "unknown"


class FaultEventType(StrEnum):
    """Lifecycle transitions emitted by the tracker."""

    APPEARED = "appeared"
    RESOLVED = "resolved"


_SEVERITY_LABELS = {
    FaultSeverity.INFO: {"en": "Information", "de": "Information"},
    FaultSeverity.MAINTENANCE: {"en": "Maintenance", "de": "Wartung"},
    FaultSeverity.WARNING: {"en": "Warning", "de": "Warnung"},
    FaultSeverity.FAULT: {"en": "Fault", "de": "Störung"},
    FaultSeverity.CRITICAL: {
        "en": "Critical fault",
        "de": "Kritische Störung",
    },
    FaultSeverity.UNKNOWN: {"en": "Unknown fault", "de": "Unbekannte Störung"},
}


@dataclass(frozen=True, slots=True)
class ActiveFault:
    """One normalized active PointT notification."""

    fingerprint: str
    code: str | None
    subcode: str | None
    severity: FaultSeverity
    pointt_class: str | None
    pointt_class_raw: str | None
    component_type: str | None
    component_id: str | None
    origin_raw: str | None
    display_level_raw: str | None
    occurrence_id: str | None
    occurred_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    time_source: FaultTimeSource
    source_resources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FaultLifecycleEvent:
    """A newly observed or resolved notification."""

    event_type: FaultEventType
    fault: ActiveFault
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class FaultParseResult:
    """Tolerant result for one PointT error resource."""

    faults: tuple[ActiveFault, ...]
    invalid_entries: int = 0


def is_fault_resource_path(path: str) -> bool:
    """Return whether a path can carry fault or notification data."""
    return bool(
        path == _NOTIFICATIONS_PATH
        or _ACTIVE_FAILURE_PATH.fullmatch(path)
        or _FAILURE_LIST_PATH.fullmatch(path)
        or _DEVICE_ERRORS_PATH.fullmatch(path)
    )


def is_active_fault_resource_path(path: str) -> bool:
    """Return whether a path is authoritative for current notifications."""
    return bool(
        path == _NOTIFICATIONS_PATH
        or _ACTIVE_FAILURE_PATH.fullmatch(path)
        or _DEVICE_ERRORS_PATH.fullmatch(path)
    )


def fault_resource_candidates(resources: Mapping[str, Resource]) -> tuple[str, ...]:
    """Build bounded optional fault paths from actually discovered IDs."""
    candidates = {_NOTIFICATIONS_PATH}

    heat_source_root = resources.get("/heatSources")
    if heat_source_root is not None:
        for reference in heat_source_root.references:
            match = _HEAT_SOURCE_PATH.match(reference.path)
            identifier = _safe_identifier(match[1]) if match else None
            if (
                identifier is not None
                and re.fullmatch(r"hs\d+", identifier, re.IGNORECASE)
                and reference.path.rstrip("/") == f"/heatSources/{identifier}"
            ):
                candidates.add(f"/heatSources/{identifier}/activefailure")
                candidates.add(f"/heatSources/{identifier}/failurelist")

    for path in resources:
        match = _HEAT_SOURCE_PATH.match(path)
        if match and re.fullmatch(r"hs\d+", match[1], re.IGNORECASE):
            candidates.add(f"/heatSources/{match[1]}/activefailure")
            candidates.add(f"/heatSources/{match[1]}/failurelist")

    device_ids: set[str] = set()
    for path in resources:
        match = _DEVICE_PATH.match(path)
        identifier = _safe_identifier(match[1]) if match else None
        if identifier is not None and identifier.lower() not in {
            "inclusionwhitelist",
            "list",
            "uhc",
        }:
            device_ids.add(identifier)
    device_list = resources.get("/devices/list")
    if device_list is not None:
        listed_devices: Iterable[JsonValue] = device_list.values
        if not device_list.values and isinstance(device_list.value, list):
            listed_devices = device_list.value
        for value in listed_devices:
            if not isinstance(value, dict):
                continue
            for key in ("id", "deviceId", "device"):
                identifier = _safe_identifier(value.get(key))
                if identifier is not None:
                    device_ids.add(identifier)
                    break
    candidates.update(f"/devices/{identifier}/errors" for identifier in device_ids)
    return tuple(sorted(candidates))


def parse_fault_resource(
    resource: Resource, *, observed_at: datetime | None = None
) -> FaultParseResult:
    """Parse known fault fields while ignoring unknown vendor extensions."""
    now = observed_at or datetime.now(UTC)
    if not is_fault_resource_path(resource.path):
        return FaultParseResult(())
    if _FAILURE_LIST_PATH.fullmatch(resource.path):
        return FaultParseResult(())

    raw_items: Iterable[JsonValue]
    if resource.values or resource.has_values:
        raw_items = resource.values
    elif resource.has_value and isinstance(resource.value, list):
        raw_items = resource.value
    elif resource.has_value and isinstance(resource.value, dict):
        raw_items = (resource.value,)
    elif resource.has_value and isinstance(resource.value, str | int | float):
        raw_items = ({"value": resource.value},)
    else:
        return FaultParseResult((), 1)

    faults: list[ActiveFault] = []
    invalid = 0
    device_errors = _DEVICE_ERRORS_PATH.fullmatch(resource.path) is not None
    for raw in raw_items:
        if not isinstance(raw, dict):
            invalid += 1
            continue
        if _is_explicitly_inactive(raw):
            continue
        if device_errors and _is_inactive_device_error(raw):
            continue
        parsed = _parse_fault_item(resource.path, raw, now)
        if parsed is None:
            invalid += 1
            continue
        faults.append(parsed)
    return FaultParseResult(tuple(faults), invalid)


def local_fault_resource(observation: object) -> Resource | None:
    """The local error list in the cloud's shape, so both give one fingerprint.

    Only ``/notifications`` (``errorList``) is used: its fields are the same on
    both interfaces. Device error lists stay unused until their meaning is
    confirmed on a real device.
    """
    from decimal import Decimal

    from .local_api.models import FaultList

    data = getattr(observation, "data", None)
    request = getattr(observation, "request", None)
    if not isinstance(data, FaultList) or getattr(request, "path", None) != (
        "/notifications"
    ):
        return None

    def number(value: object) -> JsonValue:
        if isinstance(value, Decimal):
            return int(value) if value == value.to_integral_value() else float(value)
        return value  # type: ignore[return-value]

    return Resource(
        "/notifications",
        values=tuple(
            {
                "ccd": number(fault.cause_code),
                "dcd": fault.display_code,
                "orig": fault.origin,
                "dlv": fault.level,
                "fc": fault.fault_class,
            }
            for fault in data.faults
        ),
        has_values=True,
    )


@dataclass(frozen=True, slots=True)
class DeviceError:
    """One entry of a room device's own error list (B3), extra information.

    ``state`` is ``active`` or ``inactive`` when the entry reports one of
    these documented values, otherwise ``other`` (for example a valve status
    whose meaning as an error is not documented).
    """

    device: int
    error_type: str
    state: str


def local_device_errors(
    observation: object,
) -> tuple[str, tuple[DeviceError, ...]] | None:
    """A local device error list (``deviceErrors``) as bounded entries (B3).

    Returns the path and its entries, or ``None`` for any other reading.
    Entries without a well-formed error type are left out. The entries are
    never part of the fault lifecycle: they create no fault, no event and no
    notification.
    """
    from .local_api.models import DeviceFaultList

    data = getattr(observation, "data", None)
    path = getattr(getattr(observation, "request", None), "path", None)
    if not isinstance(path, str) or not isinstance(data, DeviceFaultList):
        return None
    match = _LOCAL_DEVICE_ERRORS_PATH.fullmatch(path)
    if match is None:
        return None
    entries: list[DeviceError] = []
    for item in data.faults[:MAX_DEVICE_ERRORS]:
        error_type = item.type if isinstance(item.type, str) else ""
        if not _DEVICE_ERROR_TYPE.fullmatch(error_type):
            continue
        value = item.value.strip().casefold() if isinstance(item.value, str) else ""
        state = value if value in ("active", "inactive") else "other"
        entries.append(DeviceError(int(match[1]), error_type, state))
    return path, tuple(entries)


def fault_summary(fault: ActiveFault, language: str | None) -> str:
    """Return an independently worded, localized summary."""
    selected_language = "de" if (language or "").lower().startswith("de") else "en"
    normalized_subcode = fault.subcode.upper() if fault.subcode else None
    exact = _KNOWN_SUMMARIES.get((fault.code or "", normalized_subcode))
    generic = _KNOWN_SUMMARIES.get((fault.code or "", None))
    if summary := exact or generic:
        return summary[selected_language]
    if selected_language == "de":
        return (
            f"Unbekannte Störung (Code {fault.code})"
            if fault.code
            else "Unbekannte Störung"
        )
    return f"Unknown fault (code {fault.code})" if fault.code else "Unknown fault"


def fault_severity_label(severity: FaultSeverity, language: str | None) -> str:
    """Return a localized display label while retaining stable enum values."""
    selected_language = "de" if (language or "").lower().startswith("de") else "en"
    return _SEVERITY_LABELS[severity][selected_language]


def no_active_faults_label(language: str | None) -> str:
    """Return the localized healthy-state label used in attributes."""
    return (
        "Keine aktiven Störungen"
        if (language or "").lower().startswith("de")
        else "No active faults"
    )


def fault_store_key(entry_id: str, gateway_id: str) -> str:
    """The private fault baseline of one gateway, without its identifier."""
    gateway_digest = hashlib.sha256(gateway_id.encode()).hexdigest()[:16]
    return f"{DOMAIN}.faults.{entry_id}.{gateway_digest}"


class FaultTracker:
    """Track active notifications without retaining raw cloud payloads."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        gateway_id: str,
    ) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass,
            FAULT_STORAGE_VERSION,
            fault_store_key(entry_id, gateway_id),
            private=True,
        )
        self._active: dict[str, ActiveFault] = {}
        self._absence_counts: Counter[str] = Counter()
        self._listeners: set[Callable[[FaultLifecycleEvent], None]] = set()
        self._event_observers: set[Callable[[FaultLifecycleEvent], None]] = set()
        self._state_listeners: set[Callable[[], None]] = set()
        self._pending_events: list[FaultLifecycleEvent] = []
        self._initialized = False
        self._supported_paths: set[str] = set()
        self._required_source_keys: set[str] = set()
        self._unverified_restored: set[str] = set()
        self._has_valid_state = False
        self._resource_results: dict[str, str] = {}
        self._last_successful_update: datetime | None = None
        self._last_parser_status = "not_run"
        self._parser_errors = 0
        self._save_schedule_failed = False
        # P42: the local error list as a second source of the same faults.
        self._local_attached = False
        self._local_valid = False
        self._local_faults: dict[str, ActiveFault] = {}
        self._local_absence: Counter[str] = Counter()
        self._local_last_reading: float | None = None
        # Monotonic seconds: a clock change never ends the local evidence.
        self._local_fresh_at: float | None = None
        self._local_max_age = LOCAL_FAULT_MAX_AGE.total_seconds()
        self._local_stale = False
        self._clock: Callable[[], float] = time.monotonic
        self._state_version = 0
        self._local_result = "not_attached"
        # P49b: a plant view without any cloud. The local error list is the
        # only source: a current, complete list establishes the state and
        # confirms an absence alone. Set only by the local plant view.
        self.local_authority = False
        # Faults the local list has reported (restored or seen). Without a
        # cloud only these can be resolved by the list's silence: a fault only
        # the cloud knew stays pending instead of an invented all-clear.
        self._locally_known: set[str] = set()
        # P49b: the baseline was handed to the next source of the gateway;
        # this tracker no longer writes it.
        self._handed_over = False
        # The source attempt that took the baseline; only it may give it back,
        # never an older, overlapping attempt (review P49b).
        self._hand_over_owner: object | None = None
        # A1: the stored baseline was read (also when there was none).
        self._store_read = False
        # A1: when the baseline was handed over (monotonic seconds).
        self._handed_over_at: float | None = None
        # B3: the current local device error lists by path; extra
        # information only, never stored and never part of the lifecycle.
        self._device_errors: dict[str, tuple[DeviceError, ...]] = {}

    @property
    def device_errors(self) -> tuple[DeviceError, ...]:
        """Active entries of the current local device error lists (B3)."""
        return tuple(
            sorted(
                (
                    item
                    for entries in self._device_errors.values()
                    for item in entries
                    if item.state == "active"
                ),
                key=lambda item: (item.device, item.error_type),
            )
        )

    def process_device_errors(
        self, lists: Mapping[str, tuple[DeviceError, ...]]
    ) -> bool:
        """Replace the current local device error lists (B3).

        Shown next to the faults only: no fault appears or ends, nothing is
        announced or stored. True when the shown information changed.
        """
        current = dict(sorted(lists.items())[:MAX_DEVICE_ERROR_LISTS])
        if current == self._device_errors:
            return False
        self._device_errors = current
        self._notify_state_listeners()
        return True

    @property
    def active(self) -> tuple[ActiveFault, ...]:
        """Return active notifications in stable order."""
        return tuple(
            sorted(
                self._active.values(),
                key=lambda item: (
                    _severity_rank(item.severity),
                    item.code or "",
                    item.fingerprint,
                ),
                reverse=True,
            )
        )

    @property
    def active_faults(self) -> tuple[ActiveFault, ...]:
        """Return notifications that indicate an actionable system problem."""
        severities = {
            FaultSeverity.FAULT,
            FaultSeverity.CRITICAL,
            FaultSeverity.UNKNOWN,
        }
        return tuple(item for item in self.active if item.severity in severities)

    @property
    def has_supported_source(self) -> bool:
        """Return whether at least one current-fault resource is supported."""
        return bool(self._supported_active_paths())

    @property
    def has_known_state(self) -> bool:
        """Keep known faults visible, but never infer health from unreadable data."""
        # The local list covers only part of the cloud's fault sources, so it
        # never stands in for an unreadable cloud read (P42), except in a view
        # without any cloud (P49b).
        return (
            bool(self.active_faults)
            or self._has_valid_state
            or (self.local_authority and self._local_current and self._local_valid)
        )

    @property
    def current_state_confirmed(self) -> bool:
        """Whether the last fault read fully confirmed the retained baseline.

        A fault the cloud no longer lists is still confirmed while a current
        local error list reports it (P42); one that no source reports any
        more is pending until its resolution.
        """
        local = self._local_current and self._local_valid
        if self.local_authority:
            # Without a cloud: every retained fault is in the current list.
            return local and set(self._active) <= set(self._local_faults)
        if not self._has_valid_state:
            return False
        return not any(
            count and not (local and fingerprint in self._local_faults)
            for fingerprint, count in self._absence_counts.items()
        )

    def async_add_state_listener(
        self, listener: Callable[[], None]
    ) -> Callable[[], None]:
        """Observe complete state updates without consuming lifecycle events."""
        self._state_listeners.add(listener)
        return lambda: self._state_listeners.discard(listener)

    def mark_unavailable(self) -> None:
        """Invalidate freshness without resolving any retained faults."""
        self._has_valid_state = False
        self._absence_counts.clear()
        self._notify_state_listeners()

    @property
    def state_version(self) -> int:
        """Changes with every state notification (for entity refreshes)."""
        return self._state_version

    def _notify_state_listeners(self) -> None:
        self._state_version += 1
        for listener in tuple(self._state_listeners):
            try:
                listener()
            except Exception:
                _LOGGER.error("A fault-state observer could not update")

    @property
    def highest_severity(self) -> FaultSeverity | None:
        """Return the strongest currently active severity."""
        return max(
            (item.severity for item in self.active),
            key=_severity_rank,
            default=None,
        )

    async def async_load(self) -> None:
        """Restore the minimal active baseline used for restart deduplication."""
        try:
            data = await self._store.async_load()
        except Exception:
            _LOGGER.warning("The stored fault baseline could not be read")
            return
        # Read (perhaps empty): a later first list is a real first baseline.
        self._store_read = True
        if not isinstance(data, dict) or not isinstance(data.get("active"), list):
            return
        restored: dict[str, ActiveFault] = {}
        for item in data["active"]:
            fault = _restore_fault(item)
            if fault is not None:
                restored[fault.fingerprint] = fault
        self._active = restored
        raw_keys = data.get("required_source_keys")
        if (
            isinstance(raw_keys, list)
            and raw_keys
            and all(
                isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key)
                for key in raw_keys
            )
        ):
            self._required_source_keys = set(raw_keys)
            pending = data.get("unverified_faults", [])
            self._unverified_restored = (
                {key for key in pending if isinstance(key, str) and key in restored}
                if isinstance(pending, list)
                else set(restored)
            )
        else:
            # Older stores lack source evidence. Reobserve those incidents before
            # permitting absence-based resolution; never invent an all-clear.
            self._unverified_restored = set(restored)
        local = data.get("local_faults")
        if isinstance(local, list) and all(isinstance(key, str) for key in local):
            # P42: the local list was attached before the restart. Its faults
            # stay until it confirms their absence (or stays silent for the
            # grace period), so a restart neither ends nor repeats them.
            self._local_attached = True  # grace starts with the first read
            self._local_faults = {
                key: restored[key] for key in local if key in restored
            }
            self._locally_known = set(self._local_faults)
            self._local_result = "restored"
        self._initialized = True

    def async_add_listener(
        self, listener: Callable[[FaultLifecycleEvent], None]
    ) -> Callable[[], None]:
        """Subscribe to lifecycle transitions and drain setup-time events."""
        self._listeners.add(listener)
        pending = tuple(self._pending_events)
        self._pending_events.clear()
        for event in pending:
            self._notify_listener(listener, event)

        def remove_listener() -> None:
            self._listeners.discard(listener)

        return remove_listener

    async def async_flush(self) -> None:
        """Save incident identities before a dependent dismissal store is flushed."""
        if self._handed_over:
            return  # the next source owns the baseline (P49b)
        await self._store.async_save(self._serialize())

    async def async_close(self) -> None:
        """Finish pending baseline writes before another source replaces this one."""
        if self._initialized:
            await self.async_flush()

    async def async_hand_over(self, owner: object | None = None) -> None:
        """Save the baseline now and stop writing it (P49b).

        The next source of the same gateway loads it right after, so a later
        write of this tracker must never replace that source's baseline.
        ``owner`` is the taking source attempt; a newer attempt takes over
        the ownership of a baseline that is already handed over.
        """
        self._hand_over_owner = owner
        if self._handed_over:
            return
        if self._initialized:
            await self.async_flush()
        self._handed_over = True
        self._handed_over_at = self._clock()

    def discard(self) -> None:
        """A view that was never kept: it never writes the baseline (P49b)."""
        self._handed_over = True

    @property
    def baseline_known(self) -> bool:
        """The stored baseline was read, or a cloud read set one (A1).

        Before that, a first local list must never become the baseline: it
        would replace the stored faults the local list does not report.
        """
        return self._store_read or self._initialized

    @property
    def local_list_current(self) -> bool:
        """A current, valid local error list takes part (P49b)."""
        return self._local_current and self._local_valid

    async def async_resume(self, owner: object | None = None) -> None:
        """The next source did not take over: write the baseline again and
        save it now, after that source's last write (P49b)."""
        if not self._handed_over or owner is not self._hand_over_owner:
            return  # not handed over, or a newer attempt holds it
        self._handed_over = False
        self._hand_over_owner = None
        if self._initialized:
            await self.async_flush()

    def async_add_event_observer(
        self, observer: Callable[[FaultLifecycleEvent], None]
    ) -> Callable[[], None]:
        """Observe future transitions without consuming the entity's event backlog."""
        self._event_observers.add(observer)
        return lambda: self._event_observers.discard(observer)

    def record_results(self, results: Iterable[BatchItemResult]) -> None:
        """Record bounded capability outcomes without response bodies."""
        for result in results:
            if not is_fault_resource_path(result.path):
                continue
            if result.resource is None and is_active_fault_resource_path(result.path):
                self._absence_counts.clear()
                self._has_valid_state = False
            if result.resource is not None:
                self._supported_paths.add(result.path)
                self._resource_results[result.path] = "success"
            elif result.status in (403, 404):
                # A previously readable source remains required for resolution.
                # Losing access is not evidence that its faults disappeared.
                self._resource_results[result.path] = str(result.status)
            elif result.status is not None:
                self._resource_results[result.path] = str(result.status)
            else:
                self._resource_results[result.path] = "error"

    def record_deferred_paths(self, paths: Iterable[str]) -> None:
        """Do not retain an all-clear when active sources could not be polled."""
        for path in paths:
            if is_active_fault_resource_path(path):
                self._absence_counts.clear()
                self._has_valid_state = False
                self._resource_results[path] = "deferred"

    def process_resources(
        self,
        resources: Mapping[str, Resource],
        *,
        successful_paths: set[str] | None = None,
        observed_at: datetime | None = None,
    ) -> tuple[FaultLifecycleEvent, ...]:
        """Apply one cycle, requiring complete success before resolving faults."""
        now = observed_at or datetime.now(UTC)
        self._expire_local()
        previous_sources = set(self._required_source_keys)
        previous_unverified = set(self._unverified_restored)
        successful = set(resources) if successful_paths is None else successful_paths
        for path in successful:
            if is_fault_resource_path(path):
                self._supported_paths.add(path)
                self._resource_results[path] = "success"

        parsed_faults: dict[str, ActiveFault] = {}
        invalid_entries = 0
        parsed_resources = 0
        for path, resource in resources.items():
            if path not in successful or not is_active_fault_resource_path(path):
                continue
            parsed_resources += 1
            self._required_source_keys.add(_source_key(path))
            parsed = parse_fault_resource(resource, observed_at=now)
            invalid_entries += parsed.invalid_entries
            for fault in parsed.faults:
                self._unverified_restored.discard(fault.fingerprint)
                existing = parsed_faults.get(fault.fingerprint)
                parsed_faults[fault.fingerprint] = (
                    fault if existing is None else _merge_faults(existing, fault)
                )

        self._parser_errors += invalid_entries
        if parsed_resources:
            # Unrelated polling groups are not a new parser observation.
            self._last_parser_status = (
                "partial" if invalid_entries else "ok" if parsed_faults else "empty"
            )
            self._last_successful_update = now

        complete = (
            bool(self._required_source_keys)
            and self._required_source_keys.issubset(
                _source_key(path)
                for path in resources
                if path in successful and is_active_fault_resource_path(path)
            )
            and not invalid_entries
            and not self._unverified_restored
        )
        if parsed_resources:
            self._has_valid_state = complete
            if not complete:
                self._absence_counts.clear()

        if not self._initialized:
            self._active = parsed_faults
            self._initialized = True
            self._schedule_save()
            self._notify_state_listeners()
            return ()

        events: list[FaultLifecycleEvent] = []
        changed = (
            previous_sources != self._required_source_keys
            or previous_unverified != self._unverified_restored
            or self._save_schedule_failed
        )
        for fingerprint, fault in parsed_faults.items():
            previous = self._active.get(fingerprint)
            self._absence_counts.pop(fingerprint, None)
            if previous is None:
                self._active[fingerprint] = fault
                events.append(FaultLifecycleEvent(FaultEventType.APPEARED, fault, now))
                changed = True
                continue
            self._active[fingerprint] = replace(
                fault,
                first_seen_at=previous.first_seen_at,
                occurred_at=previous.occurred_at or fault.occurred_at,
                time_source=(
                    previous.time_source
                    if previous.occurred_at is not None
                    else fault.time_source
                ),
                source_resources=tuple(
                    sorted(set(previous.source_resources) | set(fault.source_resources))
                ),
            )

        if complete:
            for fingerprint in set(self._active) - set(parsed_faults):
                self._absence_counts[fingerprint] = min(
                    self._absence_counts[fingerprint] + 1,
                    FAULT_RESOLUTION_CONFIRMATIONS,
                )
                if self._absence_counts[fingerprint] < FAULT_RESOLUTION_CONFIRMATIONS:
                    continue
                if not self._local_confirms_absence(fingerprint):
                    continue  # the local error list has not confirmed it (P42)
                fault = self._active.pop(fingerprint)
                self._absence_counts.pop(fingerprint, None)
                self._local_absence.pop(fingerprint, None)
                events.append(FaultLifecycleEvent(FaultEventType.RESOLVED, fault, now))
                changed = True

        if changed:
            self._schedule_save()
        for event in events:
            self._emit(event)
        self._notify_state_listeners()
        return tuple(events)

    # ------------------------------------------------- P42: local error list

    @property
    def _local_current(self) -> bool:
        """The local list takes part: attached and not silent for too long."""
        return self._local_attached and not self._local_stale

    def _expire_local(self) -> None:
        """A local list without a new reading for too long stops blocking."""
        if not self._local_current:
            return
        now = self._clock()
        if self._local_fresh_at is None:
            self._local_fresh_at = now  # after a restart: the grace period starts
            return
        if now - self._local_fresh_at > self._local_max_age:
            self._local_stale = True
            self._local_valid = False
            self._local_absence.clear()
            self._local_result = "stale"
            self._schedule_save()  # a restart must not attach it again
            self._notify_state_listeners()

    def _local_confirms_absence(self, fingerprint: str) -> bool:
        return not self._local_current or (
            self._local_valid
            and fingerprint not in self._local_faults
            and self._local_absence[fingerprint] >= FAULT_RESOLUTION_CONFIRMATIONS
        )

    def _cloud_confirms_absence(self, fingerprint: str) -> bool:
        if self.local_authority:
            # No cloud takes part (P49b): the list decides only what it knows.
            return fingerprint in self._locally_known
        return (
            self._has_valid_state
            and self._absence_counts[fingerprint] >= FAULT_RESOLUTION_CONFIRMATIONS
        )

    def process_local(
        self,
        resource: Resource | None,
        *,
        reading: float | None,
        ready: bool,
        interval: float | None = None,
        observed_at: datetime | None = None,
    ) -> tuple[FaultLifecycleEvent, ...]:
        """Apply the local error list as a second source of the same faults.

        Only a new, current and complete local reading counts. A missing,
        late or unreadable answer never resolves anything, and a fault is
        resolved only when every taking-part source confirms its absence.
        ``resource`` is ``None`` while the access is ready but has no new list
        (for example a late reading after a timeout): no new evidence, so
        nothing changes. ``ready`` false suspends the local evidence.
        ``interval`` is the local list's query interval in seconds.
        """
        now = observed_at or datetime.now(UTC)
        initializing = False
        if not self._initialized:
            if not (self.local_authority and resource is not None and ready):
                return ()  # the stored baseline is not loaded yet
            # P49b: no stored baseline and no cloud. Like a first cloud read,
            # the first complete list is the baseline, without events.
            initializing = True
        if self._handed_over and (
            self._handed_over_at is None
            or self._clock() - self._handed_over_at <= HAND_OVER_HOLD.total_seconds()
        ):
            # The next source of the gateway holds the baseline: this view
            # keeps showing it unchanged until that source publishes or gives
            # it back, so no fault is announced by both (A1). Never longer
            # than the hold, so a hand-over that is never ended cannot stop
            # the view's fault display.
            self._expire_local()
            return ()
        was_current = self._local_current
        if resource is not None and ready:
            # Attached once it delivered a list; a gateway without one never
            # blocks the cloud's resolution.
            self._local_attached = True
        if not self._local_attached:
            return ()
        self._expire_local()
        if not ready:
            # No evidence while not ready, but the confirmations of earlier new
            # readings stay: brief pauses between readings never block forever.
            if self._local_valid:
                self._local_valid = False
                self._local_result = "no_answer"
                self._notify_state_listeners()
            return ()
        if resource is None:
            return ()  # ready, but no new list: keep the current evidence
        if reading is not None and reading == self._local_last_reading:
            return ()  # the same reading again: no new evidence
        self._local_last_reading = reading
        self._local_fresh_at = self._clock()
        self._local_stale = False
        self._local_max_age = max(
            LOCAL_FAULT_MAX_AGE.total_seconds(),
            LOCAL_FAULT_MISSED_READINGS * (interval or 0),
        )
        parsed = parse_fault_resource(resource, observed_at=now)
        faults: dict[str, ActiveFault] = {}
        for fault in parsed.faults:
            existing = faults.get(fault.fingerprint)
            faults[fault.fingerprint] = (
                fault if existing is None else _merge_faults(existing, fault)
            )
        if initializing and parsed.invalid_entries:
            return ()  # only a complete first list becomes the baseline
        self._locally_known.update(faults)
        if initializing:
            self._active = dict(faults)
            self._local_faults = faults
            self._local_valid = not parsed.invalid_entries
            self._local_result = "partial" if parsed.invalid_entries else "success"
            self._initialized = True
            self._schedule_save()
            self._notify_state_listeners()
            return ()
        before = self._local_signature()
        # A changed local set is saved, so a restart knows which faults it held.
        # Also after attaching or returning from silence, which the store
        # does not know yet.
        changed = not was_current or set(faults) != set(self._local_faults)
        self._local_faults = faults
        self._local_valid = not parsed.invalid_entries
        self._local_result = "partial" if parsed.invalid_entries else "success"
        events: list[FaultLifecycleEvent] = []
        for fingerprint, fault in faults.items():
            self._local_absence.pop(fingerprint, None)
            if fingerprint in self._unverified_restored:
                # Seen again after a restart: verified, like a cloud sighting.
                self._unverified_restored.discard(fingerprint)
                changed = True
            previous = self._active.get(fingerprint)
            if previous is None:
                self._active[fingerprint] = fault
                events.append(FaultLifecycleEvent(FaultEventType.APPEARED, fault, now))
                changed = True
            else:
                self._active[fingerprint] = replace(
                    previous,
                    last_seen_at=fault.last_seen_at,
                    source_resources=tuple(
                        sorted(
                            set(previous.source_resources) | set(fault.source_resources)
                        )
                    ),
                )
        if self._local_valid:
            for fingerprint in set(self._active) - set(faults):
                self._local_absence[fingerprint] = min(
                    self._local_absence[fingerprint] + 1,
                    FAULT_RESOLUTION_CONFIRMATIONS,
                )
                if self._local_absence[
                    fingerprint
                ] < FAULT_RESOLUTION_CONFIRMATIONS or not (
                    self._cloud_confirms_absence(fingerprint)
                ):
                    continue
                fault = self._active.pop(fingerprint)
                self._absence_counts.pop(fingerprint, None)
                self._local_absence.pop(fingerprint, None)
                events.append(FaultLifecycleEvent(FaultEventType.RESOLVED, fault, now))
                changed = True
        else:
            self._local_absence.clear()
        if changed:
            self._schedule_save()
        for event in events:
            self._emit(event)
        if events or changed or self._local_signature() != before:
            # Only a real change refreshes the entities, not every reading.
            self._notify_state_listeners()
        return tuple(events)

    def _local_signature(self) -> tuple[object, ...]:
        return (
            self._local_valid,
            self._local_result,
            frozenset(self._local_absence.items()),
        )

    def detach_local(self) -> None:
        """The local access is gone: the cloud decides alone again."""
        if not self._local_attached:
            return
        self._local_attached = self._local_valid = False
        self._local_faults = {}
        self._local_absence.clear()
        self._local_last_reading = None
        self._local_fresh_at = None
        self._local_stale = False
        self._local_result = "not_attached"
        self._schedule_save()
        self._notify_state_listeners()

    def diagnostics(self) -> dict[str, object]:
        """Return fault state without identifiers or unknown payloads."""
        severity_counts = Counter(item.severity.value for item in self.active)
        codes = sorted({item.code for item in self.active if item.code is not None})
        return {
            "supported": self.has_supported_source,
            "supported_resources": tuple(sorted(self._supported_paths)),
            "resource_results": dict(sorted(self._resource_results.items())),
            "active_notifications": len(self.active),
            "active_faults": len(self.active_faults),
            "severity_counts": dict(sorted(severity_counts.items())),
            "codes": tuple(codes[:MAX_FAULT_ATTRIBUTES]),
            "codes_truncated": len(codes) > MAX_FAULT_ATTRIBUTES,
            "last_successful_update": (
                self._last_successful_update.isoformat()
                if self._last_successful_update
                else None
            ),
            "parser_status": self._last_parser_status,
            "parser_errors": self._parser_errors,
            "local_error_list": self._local_result,
            "local_faults": len(self._local_faults),
            # B3: counts only, never device slots or error types.
            "device_errors": {
                "lists": len(self._device_errors),
                **{
                    state: sum(
                        item.state == state
                        for entries in self._device_errors.values()
                        for item in entries
                    )
                    for state in ("active", "inactive", "other")
                },
            },
        }

    def _supported_active_paths(self) -> set[str]:
        return {
            path
            for path in self._supported_paths
            if is_active_fault_resource_path(path)
        }

    def _emit(self, event: FaultLifecycleEvent) -> None:
        for observer in tuple(self._event_observers):
            self._notify_listener(observer, event)
        if not self._listeners:
            self._pending_events.append(event)
            self._pending_events = self._pending_events[-50:]
            return
        for listener in tuple(self._listeners):
            self._notify_listener(listener, event)

    @staticmethod
    def _notify_listener(
        listener: Callable[[FaultLifecycleEvent], None], event: FaultLifecycleEvent
    ) -> None:
        try:
            listener(event)
        except Exception:
            _LOGGER.exception(
                "Fault lifecycle listener failed for transition %s",
                event.event_type.value,
            )

    def _schedule_save(self) -> None:
        if self._handed_over:
            return
        try:
            self._store.async_delay_save(self._serialize, FAULT_STORAGE_SAVE_DELAY)
        except Exception:
            if not self._save_schedule_failed:
                _LOGGER.warning("The fault baseline could not be queued for storage")
            self._save_schedule_failed = True
        else:
            self._save_schedule_failed = False

    def _serialize(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "active": [_serialize_fault(item) for item in self.active],
            "required_source_keys": sorted(self._required_source_keys),
            "unverified_faults": sorted(self._unverified_restored),
        }
        if self._local_current:
            # Which faults the local list reported (P42), for a restart.
            data["local_faults"] = sorted(set(self._local_faults) & set(self._active))
        return data


def _source_key(path: str) -> str:
    """Persist source identity without storing device identifiers or raw paths."""
    return hashlib.sha256(path.encode()).hexdigest()


def _parse_fault_item(
    path: str, data: Mapping[str, JsonValue], now: datetime
) -> ActiveFault | None:
    code = _token(data.get("ccd", data.get("code", data.get("value"))))
    if (
        code is not None
        and code.casefold() == "active"
        and _DEVICE_ERRORS_PATH.fullmatch(path)
    ):
        # A documented device error row: ``value`` is only its state, the
        # error itself is named by ``type``.
        code = _token(data.get("type")) or code
    subcode = _token(data.get("dcd", data.get("subcode")))
    occurrence_id = _token(data.get("occurrenceId", data.get("occurrence_id")))
    class_raw = _token(data.get("fc", data.get("errorType", data.get("cat"))))
    pointt_class, severity = _classify(class_raw)
    origin = _token(data.get("orig", data.get("origin")))
    display_level = _token(data.get("dlv", data.get("displayLevel")))
    component_type, component_id = _component_from_path(path)
    occurred_at = _parse_timestamp(data.get("t"), now)
    time_source = (
        FaultTimeSource.CLOUD
        if occurred_at is not None
        else FaultTimeSource.HOME_ASSISTANT_OBSERVED
    )
    if not any((code, subcode, occurrence_id, class_raw)):
        return None
    # A vendor occurrence ID is the strongest available identity and remains
    # stable when the same incident is reported by more than one resource.
    # Without it, retain the component fields to avoid collapsing two real,
    # simultaneous faults that happen to share a manufacturer code.
    identity = (
        f"occurrence|{occurrence_id}"
        if occurrence_id is not None
        else "|".join(
            (
                code or "",
                subcode or "",
                component_type or "",
                component_id or "",
                origin or "",
                class_raw or "",
            )
        )
    )
    fingerprint = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return ActiveFault(
        fingerprint=fingerprint,
        code=code,
        subcode=subcode,
        severity=severity,
        pointt_class=pointt_class,
        pointt_class_raw=class_raw,
        component_type=component_type,
        component_id=component_id,
        origin_raw=origin,
        display_level_raw=display_level,
        occurrence_id=occurrence_id,
        occurred_at=occurred_at,
        first_seen_at=now,
        last_seen_at=now,
        time_source=time_source,
        source_resources=(path,),
    )


def _is_inactive_device_error(data: Mapping[str, JsonValue]) -> bool:
    """A device error row whose documented ``value`` says it is inactive.

    Device error lists report each error type with ``value`` ``active`` or
    ``inactive``; an inactive row is no fault.
    """
    value = data.get("value")
    return isinstance(value, str) and value.strip().casefold() == "inactive"


def _is_explicitly_inactive(data: Mapping[str, JsonValue]) -> bool:
    """Recognize only unambiguous inactive markers from known wire variants."""
    for key in ("active", "isActive"):
        value = data.get(key)
        if value is False or value == 0:
            return True
        if isinstance(value, str) and value.strip().casefold() in {
            "false",
            "inactive",
            "no",
            "off",
            "resolved",
            "0",
        }:
            return True
    for key in ("resolved", "cleared"):
        value = data.get(key)
        if value is True or value == 1:
            return True
        if isinstance(value, str) and value.strip().casefold() in {
            "true",
            "yes",
            "on",
            "1",
        }:
            return True
    status = data.get("status", data.get("state"))
    return isinstance(status, str) and status.strip().casefold() in {
        "inactive",
        "resolved",
        "cleared",
        "closed",
    }


def _classify(value: str | None) -> tuple[str | None, FaultSeverity]:
    if value is None:
        return None, FaultSeverity.UNKNOWN
    normalized = value.strip().upper().replace(" ", "_").replace("-", "_")
    named = {
        "INFO": FaultSeverity.INFO,
        "INFORMATION": FaultSeverity.INFO,
        "MAINTENANCE": FaultSeverity.MAINTENANCE,
        "WARNING": FaultSeverity.WARNING,
        "LOCKING": FaultSeverity.FAULT,
        "BLOCKING": FaultSeverity.FAULT,
        "FAULT": FaultSeverity.FAULT,
        "GENERIC_ERROR": FaultSeverity.FAULT,
        "FATAL": FaultSeverity.CRITICAL,
        "CRITICAL": FaultSeverity.CRITICAL,
    }
    if normalized in named:
        return normalized.lower(), named[normalized]
    # This is the one numeric class confirmed by the real K40 fault 6249.
    if normalized in {"12", "12.0"}:
        return "blocking", FaultSeverity.FAULT
    return None, FaultSeverity.UNKNOWN


def _component_from_path(path: str) -> tuple[str | None, str | None]:
    heat_source = _HEAT_SOURCE_PATH.match(path)
    if heat_source and path not in {"/heatSources", "/heatSources/emon"}:
        return "heat_source", heat_source[1]
    device = _DEVICE_PATH.match(path)
    if device and device[1].lower() not in {"list", "uhc", "inclusionwhitelist"}:
        return "device", device[1]
    return "system", None


def _merge_faults(first: ActiveFault, second: ActiveFault) -> ActiveFault:
    preferred = (
        first
        if _severity_rank(first.severity) >= _severity_rank(second.severity)
        else second
    )
    return replace(
        preferred,
        component_type=(
            first.component_type
            if first.component_type not in (None, "system")
            else second.component_type
        ),
        component_id=first.component_id or second.component_id,
        source_resources=tuple(
            sorted(set(first.source_resources) | set(second.source_resources))
        ),
    )


def _parse_timestamp(value: JsonValue, now: datetime) -> datetime | None:
    parsed: datetime | None = None
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    elif isinstance(value, int | float) and not isinstance(value, bool):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000
        try:
            parsed = datetime.fromtimestamp(seconds, UTC)
        except OverflowError, OSError, ValueError:
            return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    parsed = parsed.astimezone(UTC)
    minimum = datetime(2000, 1, 1, tzinfo=UTC)
    return parsed if minimum <= parsed <= now + timedelta(days=1) else None


def _token(value: JsonValue) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _safe_identifier(value: JsonValue | None) -> str | None:
    candidate = _token(value)
    if (
        candidate
        and candidate not in {".", ".."}
        and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", candidate)
    ):
        return candidate
    return None


def _severity_rank(severity: FaultSeverity) -> int:
    return {
        FaultSeverity.INFO: 0,
        FaultSeverity.MAINTENANCE: 1,
        FaultSeverity.WARNING: 2,
        FaultSeverity.UNKNOWN: 3,
        FaultSeverity.FAULT: 4,
        FaultSeverity.CRITICAL: 5,
    }[severity]


def _serialize_fault(fault: ActiveFault) -> dict[str, Any]:
    """Serialize only the normalized baseline required for restart deduplication."""
    return {
        "fingerprint": fault.fingerprint,
        "code": fault.code,
        "subcode": fault.subcode,
        "severity": fault.severity.value,
        "pointt_class": fault.pointt_class,
        "component_type": fault.component_type,
        "occurred_at": fault.occurred_at.isoformat() if fault.occurred_at else None,
        "first_seen_at": fault.first_seen_at.isoformat(),
        "last_seen_at": fault.last_seen_at.isoformat(),
        "time_source": fault.time_source.value,
    }


def _restore_fault(value: object) -> ActiveFault | None:
    if not isinstance(value, dict):
        return None
    try:
        fingerprint = value["fingerprint"]
        severity = FaultSeverity(value["severity"])
        first_seen = datetime.fromisoformat(value["first_seen_at"])
        last_seen = datetime.fromisoformat(value["last_seen_at"])
        time_source = FaultTimeSource(value["time_source"])
    except KeyError, TypeError, ValueError:
        return None
    if not isinstance(fingerprint, str):
        return None
    occurred_raw = value.get("occurred_at")
    try:
        occurred = (
            datetime.fromisoformat(occurred_raw)
            if isinstance(occurred_raw, str)
            else None
        )
    except ValueError:
        occurred = None
    return ActiveFault(
        fingerprint=fingerprint,
        code=_stored_optional_string(value.get("code")),
        subcode=_stored_optional_string(value.get("subcode")),
        severity=severity,
        pointt_class=_stored_optional_string(value.get("pointt_class")),
        pointt_class_raw=None,
        component_type=_stored_optional_string(value.get("component_type")),
        component_id=None,
        origin_raw=None,
        display_level_raw=None,
        occurrence_id=None,
        occurred_at=occurred,
        first_seen_at=first_seen,
        last_seen_at=last_seen,
        time_source=time_source,
        source_resources=(),
    )


def _stored_optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None
