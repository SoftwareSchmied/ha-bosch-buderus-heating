"""Privacy-safe metrics for PointT cloud requests."""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from math import ceil
from time import monotonic

ROLLING_MINUTES = 60
MAX_RECENT_REQUESTS = 250
_LATENCY_BOUNDS_MS = (100, 250, 500, 1000, 2000, 5000, 10000)


@dataclass(slots=True)
class _RequestEvent:
    """One sanitized HTTP attempt retained temporarily for diagnostics."""

    sequence: int
    timestamp: float
    request_type: str
    method: str
    status: int | None
    outcome: str
    duration_ms: float
    attempt: int
    retry: bool
    bulk_size: int
    fallback_reason: str | None
    bulk_items_total: int = 0
    bulk_items_successful: int = 0
    bulk_items_failed: int = 0
    bulk_items_parse_failed: int = 0
    bulk_server_statuses: Counter[str] = field(default_factory=Counter)
    bulk_gateway_statuses: Counter[str] = field(default_factory=Counter)

    def as_dict(self, now: float) -> dict[str, object]:
        """Return the bounded, non-identifying diagnostics representation."""
        result: dict[str, object] = {
            "sequence": self.sequence,
            "age_seconds": round(max(0.0, now - self.timestamp), 1),
            "type": self.request_type,
            "method": self.method,
            "http_status": self.status,
            "outcome": self.outcome,
            "duration_ms": _rounded(self.duration_ms),
            "attempt": self.attempt,
            "retry": self.retry,
        }
        if self.bulk_size:
            result["bulk_size"] = self.bulk_size
        if self.fallback_reason is not None:
            result["fallback_reason"] = self.fallback_reason
        if self.bulk_items_total:
            result.update(
                {
                    "bulk_items_total": self.bulk_items_total,
                    "bulk_items_successful": self.bulk_items_successful,
                    "bulk_items_failed": self.bulk_items_failed,
                    "bulk_items_parse_failed": self.bulk_items_parse_failed,
                    "bulk_server_statuses": dict(
                        sorted(self.bulk_server_statuses.items())
                    ),
                    "bulk_gateway_statuses": dict(
                        sorted(self.bulk_gateway_statuses.items())
                    ),
                }
            )
        return result


@dataclass(slots=True)
class _MinuteBucket:
    """Bounded aggregate data for one monotonic clock minute."""

    minute: int
    requests: int = 0
    successful: int = 0
    retries: int = 0
    fallback_requests: int = 0
    rate_limit_events: int = 0
    latest_duration_ms: float | None = None
    successful_duration_total_ms: float = 0.0
    successful_duration_samples: int = 0
    maximum_successful_duration_ms: float = 0.0
    requests_by_type: Counter[str] = field(default_factory=Counter)
    responses_by_status: Counter[str] = field(default_factory=Counter)
    outcomes: Counter[str] = field(default_factory=Counter)
    latency_histogram: Counter[int] = field(default_factory=Counter)
    bulk_items_total: int = 0
    bulk_items_successful: int = 0
    bulk_items_failed: int = 0
    bulk_items_parse_failed: int = 0


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
    _sequence: int = 0
    _recent_requests: deque[_RequestEvent] = field(
        default_factory=lambda: deque(maxlen=MAX_RECENT_REQUESTS)
    )
    _minute_buckets: dict[int, _MinuteBucket] = field(default_factory=dict)

    def record_request(
        self,
        *,
        category: str,
        method: str,
        status: int | None,
        outcome: str,
        duration_ms: float,
        bulk_size: int = 0,
        request_type: str | None = None,
        attempt: int = 1,
        retry: bool = False,
        fallback_reason: str | None = None,
    ) -> int:
        """Record one actual HTTP attempt using bounded categorical data only."""
        now = monotonic()
        self._prune(now)
        safe_duration = max(0.0, duration_ms)
        safe_type = request_type or _request_type(category, method)
        self._sequence += 1

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
        if retry:
            self.retry_attempts += 1
        if fallback_reason is not None:
            self.fallback_requests += 1
            self.fallback_requests_by_reason[fallback_reason] += 1

        event = _RequestEvent(
            sequence=self._sequence,
            timestamp=now,
            request_type=safe_type,
            method=method.upper(),
            status=status,
            outcome=outcome,
            duration_ms=safe_duration,
            attempt=max(1, attempt),
            retry=retry,
            bulk_size=max(0, bulk_size),
            fallback_reason=fallback_reason,
        )
        self._recent_requests.append(event)

        bucket = self._bucket(now)
        bucket.requests += 1
        bucket.successful += outcome == "success"
        bucket.retries += retry
        bucket.fallback_requests += fallback_reason is not None
        bucket.rate_limit_events += outcome == "rate_limited"
        bucket.latest_duration_ms = safe_duration
        bucket.requests_by_type[safe_type] += 1
        bucket.responses_by_status[_exact_status(status)] += 1
        bucket.outcomes[outcome] += 1
        if outcome == "success":
            bucket.successful_duration_total_ms += safe_duration
            bucket.successful_duration_samples += 1
            bucket.maximum_successful_duration_ms = max(
                bucket.maximum_successful_duration_ms, safe_duration
            )
            bucket.latency_histogram[_latency_bucket(safe_duration)] += 1
        return event.sequence

    def record_bulk_items(
        self,
        statuses: tuple[int | None, ...],
        *,
        usable: tuple[bool, ...] | None = None,
        server_statuses: tuple[int | None, ...] | None = None,
        gateway_statuses: tuple[int | None, ...] | None = None,
        request_sequence: int | None = None,
    ) -> None:
        """Count item outcomes from a successful bulk response envelope."""
        if usable is not None and len(usable) != len(statuses):
            raise ValueError("Bulk status and usability counts must match")
        if server_statuses is not None and len(server_statuses) != len(statuses):
            raise ValueError("Bulk status and server-status counts must match")
        if gateway_statuses is not None and len(gateway_statuses) != len(statuses):
            raise ValueError("Bulk status and gateway-status counts must match")

        successful = 0
        failed = 0
        parse_failed = 0
        event = self._event(request_sequence)
        bucket = self._bucket(event.timestamp) if event is not None else None
        for index, status in enumerate(statuses):
            self.bulk_items_total += 1
            self.bulk_items_by_status_class[_status_class(status)] += 1
            server_status = (
                server_statuses[index] if server_statuses is not None else None
            )
            gateway_status = (
                gateway_statuses[index] if gateway_statuses is not None else None
            )
            if server_statuses is not None:
                self.bulk_items_by_server_status_class[
                    _status_class(server_status)
                ] += 1
            if gateway_statuses is not None:
                self.bulk_items_by_gateway_status_class[
                    _status_class(gateway_status)
                ] += 1
            item_usable = (
                usable[index]
                if usable is not None
                else status is not None and 200 <= status < 300
            )
            if item_usable:
                self.bulk_items_successful += 1
                successful += 1
            else:
                self.bulk_items_failed += 1
                failed += 1
                if status is not None and 200 <= status < 300:
                    self.bulk_items_parse_failed += 1
                    parse_failed += 1
            if event is not None:
                if server_statuses is not None:
                    event.bulk_server_statuses[_exact_status(server_status)] += 1
                if gateway_statuses is not None:
                    event.bulk_gateway_statuses[_exact_status(gateway_status)] += 1

        if event is not None:
            event.bulk_items_total += len(statuses)
            event.bulk_items_successful += successful
            event.bulk_items_failed += failed
            event.bulk_items_parse_failed += parse_failed
        if bucket is not None:
            bucket.bulk_items_total += len(statuses)
            bucket.bulk_items_successful += successful
            bucket.bulk_items_failed += failed
            bucket.bulk_items_parse_failed += parse_failed

    def snapshot(self) -> dict[str, object]:
        """Return a JSON-safe aggregate snapshot for Home Assistant diagnostics."""
        now = monotonic()
        self._prune(now)
        observation_seconds = max(0.0, now - self.started_at)
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
            "rolling_60_minutes": self._rolling_snapshot(now),
            "recent_requests": [event.as_dict(now) for event in self._recent_requests],
        }

    def _prune(self, now: float) -> None:
        oldest_timestamp = now - ROLLING_MINUTES * 60
        while (
            self._recent_requests
            and self._recent_requests[0].timestamp < oldest_timestamp
        ):
            self._recent_requests.popleft()
        oldest_minute = int(now // 60) - ROLLING_MINUTES + 1
        for key in tuple(self._minute_buckets):
            if key < oldest_minute:
                del self._minute_buckets[key]

    def _bucket(self, timestamp: float) -> _MinuteBucket:
        minute = int(timestamp // 60)
        bucket = self._minute_buckets.get(minute)
        if bucket is None:
            bucket = self._minute_buckets[minute] = _MinuteBucket(minute)
        return bucket

    def _event(self, sequence: int | None) -> _RequestEvent | None:
        if sequence is None:
            return None
        return next(
            (
                event
                for event in reversed(self._recent_requests)
                if event.sequence == sequence
            ),
            None,
        )

    def _rolling_snapshot(self, now: float) -> dict[str, object]:
        current_minute = int(now // 60)
        oldest = current_minute - ROLLING_MINUTES + 1
        buckets = [
            bucket
            for minute, bucket in self._minute_buckets.items()
            if oldest <= minute <= current_minute
        ]
        requests = sum(bucket.requests for bucket in buckets)
        successful = sum(bucket.successful for bucket in buckets)
        samples = sum(bucket.successful_duration_samples for bucket in buckets)
        duration_total = sum(bucket.successful_duration_total_ms for bucket in buckets)
        histogram: Counter[int] = Counter()
        requests_by_type: Counter[str] = Counter()
        responses_by_status: Counter[str] = Counter()
        outcomes: Counter[str] = Counter()
        for bucket in buckets:
            histogram.update(bucket.latency_histogram)
            requests_by_type.update(bucket.requests_by_type)
            responses_by_status.update(bucket.responses_by_status)
            outcomes.update(bucket.outcomes)
        latest_bucket = max(buckets, key=lambda item: item.minute) if buckets else None
        return {
            "requests_total": requests,
            "requests_successful": successful,
            "requests_failed": max(0, requests - successful),
            "success_rate_percent": _percentage(successful, requests),
            "retry_attempts": sum(bucket.retries for bucket in buckets),
            "fallback_requests": sum(bucket.fallback_requests for bucket in buckets),
            "rate_limit_events": sum(bucket.rate_limit_events for bucket in buckets),
            "requests_by_type": dict(sorted(requests_by_type.items())),
            "responses_by_http_status": dict(sorted(responses_by_status.items())),
            "outcomes": dict(sorted(outcomes.items())),
            "bulk_items_total": sum(bucket.bulk_items_total for bucket in buckets),
            "bulk_items_successful": sum(
                bucket.bulk_items_successful for bucket in buckets
            ),
            "bulk_items_failed": sum(bucket.bulk_items_failed for bucket in buckets),
            "bulk_items_parse_failed": sum(
                bucket.bulk_items_parse_failed for bucket in buckets
            ),
            "successful_response_time_samples": samples,
            "average_successful_response_time_ms": _rounded(
                duration_total / samples if samples else None
            ),
            "p95_successful_response_time_ms": _rounded(
                _percentile_from_histogram(histogram, samples, 0.95)
            ),
            "maximum_successful_response_time_ms": _rounded(
                max(
                    (bucket.maximum_successful_duration_ms for bucket in buckets),
                    default=0.0,
                )
                if samples
                else None
            ),
            "latest_response_time_ms": _rounded(
                latest_bucket.latest_duration_ms if latest_bucket is not None else None
            ),
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


def _request_type(category: str, method: str) -> str:
    if category == "bulk":
        return "bulk"
    if method.upper() != "GET":
        return "write"
    if category == "resource":
        return "single"
    return category


def _status_class(status: int | None) -> str:
    if status is None:
        return "none"
    if 100 <= status <= 599:
        return f"{status // 100}xx"
    return "other"


def _exact_status(status: int | None) -> str:
    return str(status) if status is not None and 100 <= status <= 599 else "none"


def _latency_bucket(duration_ms: float) -> int:
    return next(
        (bound for bound in _LATENCY_BOUNDS_MS if duration_ms <= bound),
        _LATENCY_BOUNDS_MS[-1] + 1,
    )


def _percentile_from_histogram(
    histogram: Counter[int], samples: int, percentile: float
) -> float | None:
    if not samples:
        return None
    target = ceil(samples * percentile)
    seen = 0
    for bound in (*_LATENCY_BOUNDS_MS, _LATENCY_BOUNDS_MS[-1] + 1):
        seen += histogram[bound]
        if seen >= target:
            return float(bound) if bound <= _LATENCY_BOUNDS_MS[-1] else 10000.0
    return None


def _rounded(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


def _percentage(successful: int, total: int) -> float | None:
    return round(successful / total * 100, 1) if total else None


def _hourly_rate(total: int, observation_seconds: float) -> float | None:
    if observation_seconds < 60:
        return None
    return round(total * 3600 / observation_seconds, 1)
