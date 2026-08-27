"""Tests for privacy-safe PointT request metrics."""

from unittest.mock import patch

from custom_components.bosch_buderus_heating.pointt.metrics import (
    RequestMetrics,
    bulk_resource_count,
    request_category,
)


def test_request_metrics_aggregate_without_request_details() -> None:
    metrics = RequestMetrics()
    metrics.record_request(
        category="bulk",
        method="POST",
        status=200,
        outcome="success",
        duration_ms=12.34,
        bulk_size=3,
    )
    metrics.record_request(
        category="bulk",
        method="POST",
        status=429,
        outcome="rate_limited",
        duration_ms=20.0,
        bulk_size=2,
    )
    metrics.record_request(
        category="other",
        method="GET",
        status=99,
        outcome="unexpected_http_status",
        duration_ms=-1,
    )
    metrics.record_retry()
    metrics.record_fallback_request()
    metrics.record_bulk_items((200, 404, None))

    assert metrics.snapshot() == {
        "observation_seconds": 0,
        "requests_total": 3,
        "requests_successful": 1,
        "requests_failed": 2,
        "success_rate_percent": 33.3,
        "requests_per_hour": None,
        "retry_attempts": 1,
        "fallback_requests": 1,
        "requests_by_category": {"bulk": 2, "other": 1},
        "requests_by_method": {"GET": 1, "POST": 2},
        "responses_by_status_class": {"2xx": 1, "4xx": 1, "other": 1},
        "outcomes": {
            "rate_limited": 1,
            "success": 1,
            "unexpected_http_status": 1,
        },
        "rate_limit_events": 1,
        "bulk_resource_paths_total": 5,
        "bulk_items_total": 3,
        "bulk_items_successful": 1,
        "bulk_items_failed": 2,
        "bulk_items_parse_failed": 0,
        "bulk_items_by_status_class": {"2xx": 1, "4xx": 1, "none": 1},
        "maximum_bulk_size": 3,
        "average_duration_ms": 10.8,
        "maximum_duration_ms": 20.0,
        "last_duration_ms": 0.0,
    }


def test_bulk_metrics_distinguish_http_success_from_usable_payload() -> None:
    metrics = RequestMetrics()

    metrics.record_bulk_items((200, 200, 404), usable=(True, False, False))

    snapshot = metrics.snapshot()
    assert snapshot["bulk_items_total"] == 3
    assert snapshot["bulk_items_successful"] == 1
    assert snapshot["bulk_items_failed"] == 2
    assert snapshot["bulk_items_parse_failed"] == 1


def test_empty_metrics_and_request_classification_are_bounded() -> None:
    snapshot = RequestMetrics().snapshot()
    assert snapshot["average_duration_ms"] is None
    assert snapshot["last_duration_ms"] is None
    assert request_category("bulk") == "bulk"
    assert request_category("gateways/") == "gateway_list"
    assert request_category("gateways/private-id") == "gateway_metadata"
    assert request_category("gateways/private-id/partnumber") == "gateway_metadata"
    assert request_category("gateways/private-id/resource/secret") == "resource"
    assert request_category("unknown/private-id") == "other"


def test_request_rate_uses_a_meaningful_observation_period() -> None:
    metrics = RequestMetrics(started_at=100.0)
    metrics.requests_total = 12
    metrics.outcomes["success"] = 9
    with patch(
        "custom_components.bosch_buderus_heating.pointt.metrics.monotonic",
        return_value=3700.0,
    ):
        snapshot = metrics.snapshot()

    assert snapshot["observation_seconds"] == 3600
    assert snapshot["requests_per_hour"] == 12.0
    assert snapshot["requests_successful"] == 9
    assert snapshot["requests_failed"] == 3
    assert snapshot["success_rate_percent"] == 75.0


def test_bulk_count_uses_only_envelope_sizes() -> None:
    assert bulk_resource_count(None) == 0
    assert bulk_resource_count(["invalid", {"resourcePaths": "invalid"}]) == 0
    assert (
        bulk_resource_count(
            [
                {"gatewayId": "private", "resourcePaths": ["/one", "/two"]},
                {"gatewayId": "private-two", "resourcePaths": ["/three"]},
            ]
        )
        == 3
    )
