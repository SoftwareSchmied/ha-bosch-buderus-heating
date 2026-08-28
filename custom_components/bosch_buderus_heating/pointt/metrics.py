"""Privacy-safe aggregate metrics for PointT cloud requests."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from time import monotonic


@dataclass(slots=True)
class RequestMetrics:
    """Collect request metadata without URLs, identifiers, or payload values."""

    started_at: float = field(default_factory=monotonic)
    requests_total: int = 0
    retry_attempts: int = 0
    fallback_requests: int = 0
    bulk_resource_paths_total: int = 0
    bulk_items_total: int = 0
    bulk_items_successful: int = 0
    bulk_items_failed: int = 0
    bulk_items_parse_failed: int = 0
    maximum_bulk_size: int = 0
    total_duration_ms: float = 0.0
    maximum_duration_ms: float = 0.0
    last_duration_ms: float | None = None
    requests_by_category: Counter[str] = field(default_factory=Counter)
    requests_by_method: Counter[str] = field(default_factory=Counter)
    responses_by_status_class: Counter[str] = field(default_factory=Counter)
    outcomes: Counter[str] = field(default_factory=Counter)
    bulk_items_by_status_class: Counter[str] = field(default_factory=Counter)
    bulk_items_by_server_status_class: Counter[str] = field(default_factory=Counter)
    bulk_items_by_gateway_status_class: Counter[str] = field(default_factory=Counter)
    fallback_requests_by_reason: Counter[str] = field(default_factory=Counter)

    def record_request(
        self,
        *,
        category: str,
        method: str,
        status: int | None,
        outcome: str,
        duration_ms: float,
        bulk_size: int = 0,
    ) -> None:
        """Record one actual HTTP attempt using bounded categorical data only."""
        safe_duration = max(0.0, duration_ms)
        self.requests_total += 1
        self.requests_by_category[category] += 1
        self.requests_by_method[method.upper()] += 1
        self.responses_by_status_class[_status_class(status)] += 1
        self.outcomes[outcome] += 1
        self.bulk_resource_paths_total += max(0, bulk_size)
        self.maximum_bulk_size = max(self.maximum_bulk_size, bulk_size)
        self.total_duration_ms += safe_duration
        self.maximum_duration_ms = max(self.maximum_duration_ms, safe_duration)
        self.last_duration_ms = safe_duration

    def record_retry(self) -> None:
        """Count a retry without retaining its request details."""
        self.retry_attempts += 1

    def record_fallback_request(self, reason: str = "other") -> None:
        """Count one bounded single-resource fallback request."""
        self.fallback_requests += 1
        self.fallback_requests_by_reason[reason] += 1

    def record_bulk_items(
        self,
        statuses: tuple[int | None, ...],
        *,
        usable: tuple[bool, ...] | None = None,
        server_statuses: tuple[int | None, ...] | None = None,
        gateway_statuses: tuple[int | None, ...] | None = None,
    ) -> None:
        """Count item outcomes from a successful bulk response envelope."""
        if usable is not None and len(usable) != len(statuses):
            raise ValueError("Bulk status and usability counts must match")
        if server_statuses is not None and len(server_statuses) != len(statuses):
            raise ValueError("Bulk status and server-status counts must match")
        if gateway_statuses is not None and len(gateway_statuses) != len(statuses):
            raise ValueError("Bulk status and gateway-status counts must match")
        for index, status in enumerate(statuses):
            self.bulk_items_total += 1
            self.bulk_items_by_status_class[_status_class(status)] += 1
            if server_statuses is not None:
                self.bulk_items_by_server_status_class[
                    _status_class(server_statuses[index])
                ] += 1
            if gateway_statuses is not None:
                self.bulk_items_by_gateway_status_class[
                    _status_class(gateway_statuses[index])
                ] += 1
            item_usable = (
                usable[index]
                if usable is not None
                else status is not None and 200 <= status < 300
            )
            if item_usable:
                self.bulk_items_successful += 1
            else:
                self.bulk_items_failed += 1
                if status is not None and 200 <= status < 300:
                    self.bulk_items_parse_failed += 1

    def snapshot(self) -> dict[str, object]:
        """Return a JSON-safe aggregate snapshot for Home Assistant diagnostics."""
        observation_seconds = max(0.0, monotonic() - self.started_at)
        successful = self.outcomes.get("success", 0)
        failed = max(0, self.requests_total - successful)
        average = (
            self.total_duration_ms / self.requests_total
            if self.requests_total
            else None
        )
        return {
            "observation_seconds": round(observation_seconds),
            "requests_total": self.requests_total,
            "requests_successful": successful,
            "requests_failed": failed,
            "success_rate_percent": _percentage(successful, self.requests_total),
            "requests_per_hour": _hourly_rate(self.requests_total, observation_seconds),
            "retry_attempts": self.retry_attempts,
            "fallback_requests": self.fallback_requests,
            "fallback_requests_by_reason": dict(
                sorted(self.fallback_requests_by_reason.items())
            ),
            "requests_by_category": dict(sorted(self.requests_by_category.items())),
            "requests_by_method": dict(sorted(self.requests_by_method.items())),
            "responses_by_status_class": dict(
                sorted(self.responses_by_status_class.items())
            ),
            "outcomes": dict(sorted(self.outcomes.items())),
            "rate_limit_events": self.outcomes.get("rate_limited", 0),
            "bulk_resource_paths_total": self.bulk_resource_paths_total,
            "bulk_items_total": self.bulk_items_total,
            "bulk_items_successful": self.bulk_items_successful,
            "bulk_items_failed": self.bulk_items_failed,
            "bulk_items_parse_failed": self.bulk_items_parse_failed,
            "bulk_items_by_status_class": dict(
                sorted(self.bulk_items_by_status_class.items())
            ),
            "bulk_items_by_server_status_class": dict(
                sorted(self.bulk_items_by_server_status_class.items())
            ),
            "bulk_items_by_gateway_status_class": dict(
                sorted(self.bulk_items_by_gateway_status_class.items())
            ),
            "maximum_bulk_size": self.maximum_bulk_size,
            "average_duration_ms": _rounded(average),
            "maximum_duration_ms": _rounded(self.maximum_duration_ms),
            "last_duration_ms": _rounded(self.last_duration_ms),
        }


def request_category(path: str) -> str:
    """Reduce an API path to a non-identifying request category."""
    normalized = path.strip("/")
    if normalized == "bulk":
        return "bulk"
    if "/resource/" in normalized:
        return "resource"
    if normalized.endswith("/partnumber"):
        return "gateway_metadata"
    if normalized == "gateways":
        return "gateway_list"
    if normalized.startswith("gateways/"):
        return "gateway_metadata"
    return "other"


def bulk_resource_count(body: object) -> int:
    """Count bulk paths without retaining gateway identifiers or path strings."""
    if not isinstance(body, list):
        return 0
    total = 0
    for envelope in body:
        if not isinstance(envelope, dict):
            continue
        paths = envelope.get("resourcePaths")
        if isinstance(paths, list):
            total += len(paths)
    return total


def _status_class(status: int | None) -> str:
    if status is None:
        return "none"
    if 100 <= status <= 599:
        return f"{status // 100}xx"
    return "other"


def _rounded(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


def _percentage(successful: int, total: int) -> float | None:
    return round(successful / total * 100, 1) if total else None


def _hourly_rate(total: int, observation_seconds: float) -> float | None:
    if observation_seconds < 60:
        return None
    return round(total * 3600 / observation_seconds, 1)
