"""Fault parsing, lifecycle tracking, and privacy-safe persistence."""

from __future__ import annotations

import hashlib
import logging
import re
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
MAX_FAULT_ATTRIBUTES = 25

_LOGGER = logging.getLogger(__name__)

_NOTIFICATIONS_PATH = "/notifications"
_HEAT_SOURCE_PATH = re.compile(r"^/heatSources/([^/]+)(?:/|$)")
_DEVICE_PATH = re.compile(r"^/devices/([^/]+)(?:/|$)")
_ACTIVE_FAILURE_PATH = re.compile(r"^/heatSources/[^/]+/activefailure$", re.IGNORECASE)
_FAILURE_LIST_PATH = re.compile(r"^/heatSources/[^/]+/failurelist$", re.IGNORECASE)
_DEVICE_ERRORS_PATH = re.compile(r"^/devices/[^/]+/errors$", re.IGNORECASE)

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
    if resource.values:
        raw_items = resource.values
    elif resource.has_value and isinstance(resource.value, list):
        raw_items = resource.value
    elif resource.has_value and isinstance(resource.value, dict):
        raw_items = (resource.value,)
    elif resource.has_value and isinstance(resource.value, str | int | float):
        raw_items = ({"value": resource.value},)
    else:
        raw_items = ()

    faults: list[ActiveFault] = []
    invalid = 0
    for raw in raw_items:
        if not isinstance(raw, dict):
            invalid += 1
            continue
        if _is_explicitly_inactive(raw):
            continue
        parsed = _parse_fault_item(resource.path, raw, now)
        if parsed is None:
            invalid += 1
            continue
        faults.append(parsed)
    return FaultParseResult(tuple(faults), invalid)


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


class FaultTracker:
    """Track active notifications without retaining raw cloud payloads."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        gateway_id: str,
    ) -> None:
        gateway_digest = hashlib.sha256(gateway_id.encode()).hexdigest()[:16]
        self._store: Store[dict[str, Any]] = Store(
            hass,
            FAULT_STORAGE_VERSION,
            f"{DOMAIN}.faults.{entry_id}.{gateway_digest}",
            private=True,
        )
        self._active: dict[str, ActiveFault] = {}
        self._absence_counts: Counter[str] = Counter()
        self._listeners: set[Callable[[FaultLifecycleEvent], None]] = set()
        self._pending_events: list[FaultLifecycleEvent] = []
        self._initialized = False
        self._supported_paths: set[str] = set()
        self._resource_results: dict[str, str] = {}
        self._last_successful_update: datetime | None = None
        self._last_parser_status = "not_run"
        self._parser_errors = 0

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
    def highest_severity(self) -> FaultSeverity | None:
        """Return the strongest currently active severity."""
        return max(
            (item.severity for item in self.active),
            key=_severity_rank,
            default=None,
        )

    async def async_load(self) -> None:
        """Restore the minimal active baseline used for restart deduplication."""
        data = await self._store.async_load()
        if not isinstance(data, dict) or not isinstance(data.get("active"), list):
            return
        restored: dict[str, ActiveFault] = {}
        for item in data["active"]:
            fault = _restore_fault(item)
            if fault is not None:
                restored[fault.fingerprint] = fault
        self._active = restored
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

    def record_results(self, results: Iterable[BatchItemResult]) -> None:
        """Record bounded capability outcomes without response bodies."""
        for result in results:
            if not is_fault_resource_path(result.path):
                continue
            if result.resource is not None:
                self._supported_paths.add(result.path)
                self._resource_results[result.path] = "success"
            elif result.status in (403, 404):
                self._supported_paths.discard(result.path)
                self._resource_results[result.path] = str(result.status)
            elif result.status is not None:
                self._resource_results[result.path] = str(result.status)
            else:
                self._resource_results[result.path] = "error"

    def process_resources(
        self,
        resources: Mapping[str, Resource],
        *,
        successful_paths: set[str] | None = None,
        observed_at: datetime | None = None,
    ) -> tuple[FaultLifecycleEvent, ...]:
        """Apply one cycle, requiring complete success before resolving faults."""
        now = observed_at or datetime.now(UTC)
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
            parsed = parse_fault_resource(resource, observed_at=now)
            invalid_entries += parsed.invalid_entries
            for fault in parsed.faults:
                existing = parsed_faults.get(fault.fingerprint)
                parsed_faults[fault.fingerprint] = (
                    fault if existing is None else _merge_faults(existing, fault)
                )

        self._parser_errors += invalid_entries
        self._last_parser_status = (
            "partial"
            if invalid_entries
            else "empty"
            if parsed_resources and not parsed_faults
            else "ok"
            if parsed_resources
            else "not_run"
        )
        if parsed_resources:
            self._last_successful_update = now

        if not self._initialized:
            self._active = parsed_faults
            self._initialized = True
            self._schedule_save()
            return ()

        events: list[FaultLifecycleEvent] = []
        changed = False
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

        supported = self._supported_active_paths()
        complete = (
            bool(supported) and supported.issubset(successful) and not invalid_entries
        )
        if complete:
            for fingerprint in set(self._active) - set(parsed_faults):
                self._absence_counts[fingerprint] += 1
                if self._absence_counts[fingerprint] < FAULT_RESOLUTION_CONFIRMATIONS:
                    continue
                fault = self._active.pop(fingerprint)
                self._absence_counts.pop(fingerprint, None)
                events.append(FaultLifecycleEvent(FaultEventType.RESOLVED, fault, now))
                changed = True

        if changed:
            self._schedule_save()
        for event in events:
            self._emit(event)
        return tuple(events)

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
        }

    def _supported_active_paths(self) -> set[str]:
        return {
            path
            for path in self._supported_paths
            if is_active_fault_resource_path(path)
        }

    def _emit(self, event: FaultLifecycleEvent) -> None:
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
        self._store.async_delay_save(self._serialize, FAULT_STORAGE_SAVE_DELAY)

    def _serialize(self) -> dict[str, Any]:
        return {
            "active": [_serialize_fault(item) for item in self.active],
        }


def _parse_fault_item(
    path: str, data: Mapping[str, JsonValue], now: datetime
) -> ActiveFault | None:
    code = _token(data.get("ccd", data.get("code", data.get("value"))))
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
