"""PointT polling coordinator for one heating gateway."""

from __future__ import annotations

import asyncio
import logging
import math
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from time import monotonic
from typing import Any, TypeIs

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, RATE_LIMIT_ISSUE_PREFIX, PollingProfile
from .discovery import async_discover_resources
from .faults import FaultTracker, fault_resource_candidates, is_fault_resource_path
from .holiday_writes import HolidayWriteService, holiday_resources_from_snapshots
from .holidays import (
    HOLIDAY_CONFIGURATION_PATH,
    HOLIDAY_LIST_PATH,
    HolidayWriteValues,
    parse_holiday_write_configuration,
)
from .pointt import (
    AuthenticationError,
    Gateway,
    InvalidPayload,
    PointTClient,
    PointTError,
    RateLimited,
    Resource,
    ResourceError,
    WriteValidationError,
)
from .pointt.bulk import chunk_resource_paths
from .pointt.const import MAX_BULK_PATHS
from .pointt.models import BatchItemResult
from .pointt.redaction import resource_path_template
from .resource_catalog import PollGroup, poll_group
from .writes import EnumWritePolicy, NumberWritePolicy, WriteService

_LOGGER = logging.getLogger(__name__)

POLL_INTERVALS: dict[PollGroup, timedelta] = {
    PollGroup.FAST: timedelta(seconds=60),
    PollGroup.NOTIFICATIONS: timedelta(minutes=5),
    PollGroup.CONTROL: timedelta(minutes=5),
    PollGroup.ENERGY: timedelta(minutes=5),
    PollGroup.SLOW: timedelta(minutes=15),
}
POLL_INTERVALS_CLOUD_FRIENDLY: dict[PollGroup, timedelta] = {
    PollGroup.FAST: timedelta(minutes=2),
    PollGroup.NOTIFICATIONS: timedelta(minutes=5),
    PollGroup.CONTROL: timedelta(minutes=10),
    PollGroup.ENERGY: timedelta(minutes=10),
    PollGroup.SLOW: timedelta(minutes=30),
}
DEFAULT_RATE_LIMIT_BACKOFF = timedelta(minutes=5)
MAX_RATE_LIMIT_BACKOFF = timedelta(hours=1)
NOT_FOUND_PAUSE = timedelta(hours=24)
FORBIDDEN_PAUSE = timedelta(hours=24)
GATEWAY_TIMEOUT_PAUSE = timedelta(minutes=15)
FORBIDDEN_PAUSE_THRESHOLD = 2
CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_OPEN_INTERVAL = timedelta(minutes=5)
MAX_FALLBACK_PATHS = 5
RATE_LIMIT_REPAIR_THRESHOLD = 3
ACTIVE_NOTIFICATION_INTERVAL = timedelta(seconds=60)

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

    @property
    def updated_at(self) -> datetime:
        """Return the last-success timestamp for compatibility."""
        return self.last_success


@dataclass(slots=True)
class CapabilityMetrics:
    """Count attempts for one resource without retaining values."""

    attempts: int = 0
    successful: int = 0
    failed: int = 0
    results: Counter[str] = field(default_factory=Counter)
    sources: Counter[str] = field(default_factory=Counter)
    last_result: str | None = None

    def record(self, *, result: str, source: SnapshotSource) -> None:
        """Record one resource-level attempt."""
        self.attempts += 1
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
            "results": dict(sorted(self.results.items())),
            "attempts_by_source": dict(sorted(self.sources.items())),
            "last_result": self.last_result,
        }


class BoschBuderusDataUpdateCoordinator(
    DataUpdateCoordinator[dict[str, ResourceSnapshot]]
):
    """Discover and refresh safe PointT resources for one gateway."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: PointTClient,
        gateway: Gateway,
        config_entry: ConfigEntry[Any],
        polling_profile: PollingProfile = PollingProfile.STANDARD,
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
        self.gateway = gateway
        self._config_entry = config_entry
        self.polling_profile = polling_profile
        self._poll_intervals = poll_intervals
        self.resources: dict[str, Resource] = {}
        self._paths_by_group: dict[PollGroup, tuple[str, ...]] = {}
        self._next_update: dict[PollGroup, float] = {}
        self._cloud_backoff_until = 0.0
        self._negative_until: dict[str, float] = {}
        self._gateway_failure_count = 0
        self._circuit_open_until = 0.0
        self._update_lock = asyncio.Lock()
        self._polls_total = 0
        self._poll_failures = 0
        self._last_poll_duration_ms: float | None = None
        self._total_poll_duration_ms = 0.0
        self._rate_limit_events = 0
        self._energy_counter_resets = 0
        self._capability_metrics: dict[str, CapabilityMetrics] = {}
        self._write_service = WriteService(client)
        self._holiday_write_service = HolidayWriteService(client)
        self.faults = FaultTracker(hass, config_entry.entry_id, gateway.gateway_id)

    async def async_load_fault_state(self) -> None:
        """Load the persisted active-fault baseline before the first refresh."""
        await self.faults.async_load()

    async def async_request_refresh(self) -> None:
        """Force all dynamic groups due for an explicit Home Assistant refresh."""
        now_monotonic = monotonic()
        for group in self._poll_intervals:
            self._next_update[group] = min(
                self._next_update.get(group, now_monotonic), now_monotonic
            )
        await super().async_request_refresh()

    async def async_write_control(
        self,
        path: str,
        value: str | float,
        policy: EnumWritePolicy | NumberWritePolicy,
    ) -> Resource:
        """Write one allowlisted control and publish its confirmed read-back."""
        async with self._update_lock:
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
            try:
                if isinstance(policy, EnumWritePolicy) and isinstance(value, str):
                    result = await self._write_service.async_write_enum(
                        self.gateway.gateway_id, snapshot.resource, value, policy
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
                    )
                else:
                    raise WriteValidationError(
                        "Control value does not match its write policy"
                    )
            except RateLimited as err:
                self._record_capability(path, "rate_limited", SnapshotSource.WRITE)
                self._activate_rate_limit_backoff(err, monotonic())
                raise
            except AuthenticationError:
                self._record_capability(
                    path, "authentication_error", SnapshotSource.WRITE
                )
                self._config_entry.async_start_reauth(self.hass)
                raise
            except WriteValidationError:
                raise
            except PointTError as err:
                self._record_capability(path, _pointt_result(err), SnapshotSource.WRITE)
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
            self.async_set_updated_data(snapshots)
            return confirmed

    async def async_create_holiday(self, values: HolidayWriteValues) -> Resource:
        """Create one holiday and publish its confirmed list read-back."""
        return await self._async_write_holiday("create", None, values)

    async def async_update_holiday(
        self, holiday_id: int, values: HolidayWriteValues
    ) -> Resource:
        """Update one holiday and publish its confirmed list read-back."""
        return await self._async_write_holiday("update", holiday_id, values)

    async def async_delete_holiday(self, holiday_id: int) -> Resource:
        """Delete one holiday and publish its confirmed list read-back."""
        return await self._async_write_holiday("delete", holiday_id, None)

    async def _async_write_holiday(
        self,
        operation: str,
        holiday_id: int | None,
        values: HolidayWriteValues | None,
    ) -> Resource:
        """Run one capability-gated holiday mutation and confirm it."""
        async with self._update_lock:
            now_monotonic = monotonic()
            if now_monotonic < self._cloud_backoff_until:
                raise RateLimited(self._cloud_backoff_until - now_monotonic)
            if now_monotonic < self._circuit_open_until:
                raise WriteValidationError(
                    "Gateway circuit breaker is active; writing is paused"
                )
            snapshots = self.data or {}
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
                        fallback_timezone=self.hass.config.time_zone,
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
                        fallback_timezone=self.hass.config.time_zone,
                    )
                elif operation == "delete" and holiday_id is not None:
                    confirmed = await self._holiday_write_service.async_delete(
                        self.gateway.gateway_id,
                        resources,
                        holiday_id,
                        fallback_timezone=self.hass.config.time_zone,
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
                self._config_entry.async_start_reauth(self.hass)
                raise
            except WriteValidationError:
                raise
            except PointTError as err:
                self._record_capability(
                    HOLIDAY_LIST_PATH, _pointt_result(err), SnapshotSource.WRITE
                )
                raise

            confirmed_at = datetime.now(UTC)
            updated = dict(snapshots)
            updated[HOLIDAY_LIST_PATH] = ResourceSnapshot(
                resource=confirmed,
                available=True,
                last_success=confirmed_at,
                last_attempt=confirmed_at,
                source=SnapshotSource.WRITE,
            )
            self.resources[HOLIDAY_LIST_PATH] = confirmed
            self._negative_until.pop(HOLIDAY_LIST_PATH, None)
            self._record_capability(HOLIDAY_LIST_PATH, "success", SnapshotSource.WRITE)
            self.async_set_updated_data(updated)
            return confirmed

    async def _async_update_data(self) -> dict[str, ResourceSnapshot]:
        started = monotonic()
        self._polls_total += 1
        try:
            async with self._update_lock:
                return await self._async_update_data_locked()
        except Exception:
            self._poll_failures += 1
            raise
        finally:
            duration = (monotonic() - started) * 1000
            self._last_poll_duration_ms = duration
            self._total_poll_duration_ms += duration

    def diagnostics_summary(self) -> dict[str, object]:
        """Return aggregate runtime state without identifiers or resource values."""
        now = monotonic()
        snapshots = tuple((self.data or {}).values())
        average_duration = (
            self._total_poll_duration_ms / self._polls_total
            if self._polls_total
            else None
        )
        capability_attempts = sum(
            item.attempts for item in self._capability_metrics.values()
        )
        capability_successful = sum(
            item.successful for item in self._capability_metrics.values()
        )
        return {
            "last_update_success": self.last_update_success,
            "polls_total": self._polls_total,
            "poll_failures": self._poll_failures,
            "average_poll_duration_ms": _rounded_duration(average_duration),
            "last_poll_duration_ms": _rounded_duration(self._last_poll_duration_ms),
            "resources_total": len(snapshots),
            "resources_available": sum(item.available for item in snapshots),
            "resources_unavailable": sum(not item.available for item in snapshots),
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
            "energy_counter_resets_detected": self._energy_counter_resets,
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
                "failed": capability_attempts - capability_successful,
                "success_rate_percent": _percentage(
                    capability_successful, capability_attempts
                ),
            },
        }

    def capability_metrics(self, path: str) -> dict[str, object]:
        """Return counters for one resource path."""
        metrics = self._capability_metrics.get(path)
        return (
            metrics.snapshot()
            if metrics is not None
            else CapabilityMetrics().snapshot()
        )

    async def _async_update_data_locked(self) -> dict[str, ResourceSnapshot]:
        """Refresh due groups while preventing overlapping gateway polls."""
        if not self.resources:
            return await self._async_discover_data()

        now_monotonic = monotonic()
        if now_monotonic < self._cloud_backoff_until:
            raise UpdateFailed("PointT rate-limit backoff is active")
        if now_monotonic < self._circuit_open_until:
            raise UpdateFailed("PointT gateway circuit breaker is active")
        due_groups = tuple(
            group
            for group in self._poll_intervals
            if now_monotonic >= self._next_update.get(group, 0.0)
        )
        candidate_paths = [
            path for group in due_groups for path in self._paths_by_group.get(group, ())
        ]
        paths = [
            path
            for path in candidate_paths
            if not self._resource_is_paused(path, now_monotonic)
        ]
        if not paths:
            self._advance_groups(due_groups, now_monotonic)
            return dict(self.data or {})

        snapshots = dict(self.data or {})
        successful = 0
        failed_chunks = 0
        gateway_failure = False
        cycle_results: list[BatchItemResult] = []
        for chunk in chunk_resource_paths(paths, size=MAX_BULK_PATHS):
            attempted_at = datetime.now(UTC)
            result_source = SnapshotSource.BATCH
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
                self._record_chunk_failure(chunk, "rate_limited", SnapshotSource.BATCH)
                self._activate_rate_limit_backoff(err, now_monotonic)
                failed_chunks += 1
                break
            except PointTError as err:
                self._record_chunk_failure(
                    chunk, _pointt_result(err), SnapshotSource.BATCH
                )
                failed_chunks += 1
                gateway_failure = True
                _LOGGER.warning(
                    "PointT batch refresh failed; trying bounded core fallback"
                )
                results = await self._async_core_fallback(chunk)
                if not results:
                    break
                result_source = SnapshotSource.FALLBACK

            malformed_fallback: tuple[BatchItemResult, ...] = ()
            if result_source is SnapshotSource.BATCH:
                self._log_unusable_bulk_results(results)
                malformed_fallback = await self._async_malformed_bulk_fallback(results)

            cycle_results.extend(results)
            successful += self._apply_results(
                snapshots,
                results,
                attempted_at=attempted_at,
                now_monotonic=now_monotonic,
                source=result_source,
            )
            if malformed_fallback:
                cycle_results.extend(malformed_fallback)
                successful += self._apply_results(
                    snapshots,
                    malformed_fallback,
                    attempted_at=attempted_at,
                    now_monotonic=now_monotonic,
                    source=SnapshotSource.FALLBACK,
                )
            if any(result.status == 429 for result in results):
                self._activate_rate_limit_backoff(
                    RateLimited(retry_after=None), now_monotonic
                )
                failed_chunks += 1
                break
            if now_monotonic < self._cloud_backoff_until:
                failed_chunks += 1
                break
            if results and all(
                result.resource is None
                and result.status is not None
                and result.status >= 500
                for result in results
            ):
                failed_chunks += 1
                gateway_failure = True
                break

        self.faults.record_results(cycle_results)
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

        if successful == 0 and failed_chunks:
            if gateway_failure:
                self._record_gateway_failure(now_monotonic)
            raise UpdateFailed("PointT returned no usable resources")
        if successful == 0:
            self._advance_groups(due_groups, now_monotonic)
            return snapshots
        self._gateway_failure_count = 0
        self._circuit_open_until = 0.0
        if failed_chunks == 0:
            self._advance_groups(due_groups, now_monotonic)
        return snapshots

    async def _async_discover_data(self) -> dict[str, ResourceSnapshot]:
        """Discover the bounded tree and use its values as the first snapshot."""
        try:
            resources = await async_discover_resources(
                self.client, self.gateway.gateway_id
            )
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed from err
        except RateLimited as err:
            self._activate_rate_limit_backoff(err, monotonic())
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
        self._paths_by_group = {
            group: tuple(
                path
                for path, resource in self.resources.items()
                if poll_group(resource) is group
            )
            for group in self._poll_intervals
        }
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
        updated_at = datetime.now(UTC)
        return {
            path: ResourceSnapshot(
                resource=resource,
                available=True,
                last_success=updated_at,
                last_attempt=updated_at,
                source=SnapshotSource.DISCOVERY,
            )
            for path, resource in successful_resources.items()
        }

    async def _async_core_fallback(
        self, chunk: tuple[str, ...]
    ) -> tuple[BatchItemResult, ...]:
        """Read only a small set of essential paths after one batch failure."""
        prioritized = tuple(path for path in _FALLBACK_PRIORITY_PATHS if path in chunk)[
            :MAX_FALLBACK_PATHS
        ]
        return await self._async_single_get_fallback(prioritized)

    async def _async_malformed_bulk_fallback(
        self, results: tuple[BatchItemResult, ...]
    ) -> tuple[BatchItemResult, ...]:
        """Retry a bounded set of malformed bulk items with individual GETs."""
        malformed = tuple(
            result.path
            for result in results
            if result.resource is None and isinstance(result.error, InvalidPayload)
        )
        if not malformed:
            return ()
        selected = tuple(path for path in _FALLBACK_PRIORITY_PATHS if path in malformed)
        selected += tuple(path for path in malformed if path not in selected)
        return await self._async_single_get_fallback(selected[:MAX_FALLBACK_PATHS])

    async def _async_single_get_fallback(
        self, paths: tuple[str, ...]
    ) -> tuple[BatchItemResult, ...]:
        """Read a bounded, preselected set of paths individually."""
        results: list[BatchItemResult] = []
        for path in paths:
            self.client.metrics.record_fallback_request()
            try:
                resource = await self.client.get_resource(self.gateway.gateway_id, path)
            except AuthenticationError as err:
                self._record_capability(
                    path, "authentication_error", SnapshotSource.FALLBACK
                )
                raise ConfigEntryAuthFailed from err
            except RateLimited as err:
                self._record_capability(path, "rate_limited", SnapshotSource.FALLBACK)
                self._activate_rate_limit_backoff(err, monotonic())
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
                self._record_capability(
                    path, _pointt_result(err), SnapshotSource.FALLBACK
                )
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
                "Ignoring PointT bulk item %s: status=%s, error=%s (%s)",
                resource_path_template(result.path),
                result.status,
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
    ) -> int:
        """Apply partial results without discarding last-good values."""
        successful = 0
        for result in results:
            if result.resource is not None:
                previous = snapshots.get(result.path)
                if previous is not None:
                    self._energy_counter_resets += _energy_reset_count(
                        previous.resource, result.resource
                    )
                self._record_capability(result.path, "success", source)
                snapshots[result.path] = ResourceSnapshot(
                    resource=result.resource,
                    available=True,
                    last_success=attempted_at,
                    last_attempt=attempted_at,
                    source=source,
                )
                self.resources[result.path] = result.resource
                self._negative_until.pop(result.path, None)
                successful += 1
                continue

            result_category = _result_category(result.error, result.status)
            self._record_capability(result.path, result_category, source)
            previous = snapshots.get(result.path)
            if previous is not None:
                failed = replace(
                    previous,
                    available=False,
                    last_attempt=attempted_at,
                    freshness=Freshness.STALE,
                    last_error_category=_error_category(result.error, result.status),
                    consecutive_failures=previous.consecutive_failures + 1,
                )
                snapshots[result.path] = failed
                self._pause_failed_resource(failed, result.status, now_monotonic)
            elif result.status in (403, 404):
                pause = FORBIDDEN_PAUSE if result.status == 403 else NOT_FOUND_PAUSE
                self._negative_until[result.path] = (
                    now_monotonic + pause.total_seconds()
                )
            _LOGGER.debug("PointT resource unavailable: %s", result.path)
        return successful

    def _record_capability(
        self, path: str, result: str, source: SnapshotSource
    ) -> None:
        self._capability_metrics.setdefault(path, CapabilityMetrics()).record(
            result=result, source=source
        )

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
            pause = NOT_FOUND_PAUSE
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
            interval = (
                ACTIVE_NOTIFICATION_INTERVAL
                if group is PollGroup.NOTIFICATIONS and self.faults.active
                else self._poll_intervals[group]
            )
            self._next_update[group] = now_monotonic + interval.total_seconds()

    def _record_gateway_failure(self, now_monotonic: float) -> None:
        self._gateway_failure_count += 1
        if self._gateway_failure_count >= CIRCUIT_FAILURE_THRESHOLD:
            self._circuit_open_until = (
                now_monotonic + CIRCUIT_OPEN_INTERVAL.total_seconds()
            )
            _LOGGER.warning(
                "PointT gateway circuit breaker opened for %.0f seconds",
                CIRCUIT_OPEN_INTERVAL.total_seconds(),
            )

    def _activate_rate_limit_backoff(
        self, error: RateLimited, now_monotonic: float
    ) -> None:
        self._rate_limit_events += 1
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
        self._cloud_backoff_until = now_monotonic + delay
        if self._rate_limit_events >= RATE_LIMIT_REPAIR_THRESHOLD:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"{RATE_LIMIT_ISSUE_PREFIX}{self._config_entry.entry_id}",
                data={"entry_id": self._config_entry.entry_id},
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="repeated_rate_limit",
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


def _rounded_duration(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


def _percentage(successful: int, total: int) -> float | None:
    return round(successful / total * 100, 1) if total else None


def _energy_reset_count(previous: Resource, current: Resource) -> int:
    """Count confirmed non-negative energy counters that moved backwards."""
    if "/emon/" not in current.path or previous.path != current.path:
        return 0
    before = _energy_counter_values(previous)
    after = _energy_counter_values(current)
    return sum(
        value < before[key] and not math.isclose(value, before[key], abs_tol=1e-9)
        for key, value in after.items()
        if key in before
    )


def _energy_counter_values(resource: Resource) -> dict[str, float]:
    """Extract only finite non-negative PointT energy counter components."""
    values: dict[str, float] = {}
    candidates: tuple[object, ...] = resource.values
    if isinstance(resource.value, dict):
        candidates = (resource.value, *candidates)
    for item in candidates:
        if not isinstance(item, dict):
            continue
        typed_key = item.get("type")
        typed_value = item.get("value")
        if isinstance(typed_key, str) and _valid_counter_number(typed_value):
            values[typed_key] = float(typed_value)
            continue
        for key, raw_value in item.items():
            if isinstance(key, str) and _valid_counter_number(raw_value):
                values[key] = float(raw_value)
    return values


def _valid_counter_number(value: object) -> TypeIs[int | float]:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(value)
        and value >= 0
    )
