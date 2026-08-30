"""Tests for privacy-safe PointT request metrics."""

from unittest.mock import patch

from custom_components.bosch_buderus_heating.pointt.metrics import (
    MAX_RECENT_REQUESTS,
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
        attempt=2,
        retry=True,
    )
    metrics.record_request(
        category="other",
        method="GET",
        status=99,
        outcome="unexpected_http_status",
        duration_ms=-1,
        request_type="fallback",
        fallback_reason="other",
    )
    metrics.record_bulk_items((200, 404, None))

    snapshot = metrics.snapshot()
    rolling = snapshot.pop("rolling_60_minutes")
    recent = snapshot.pop("recent_requests")
    assert snapshot == {
        "observation_seconds": 0,
        "requests_total": 3,
        "requests_successful": 1,
        "requests_failed": 2,
        "success_rate_percent": 33.3,
        "requests_per_hour": None,
        "retry_attempts": 1,
        "fallback_requests": 1,
        "fallback_requests_by_reason": {"other": 1},
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
        "bulk_items_by_server_status_class": {},
        "bulk_items_by_gateway_status_class": {},
        "maximum_bulk_size": 3,
        "average_duration_ms": 10.8,
        "maximum_duration_ms": 20.0,
        "last_duration_ms": 0.0,
    }
    assert rolling["requests_total"] == 3
    assert rolling["requests_successful"] == 1
    assert rolling["requests_failed"] == 2
    assert rolling["responses_by_http_status"] == {
        "200": 1,
        "429": 1,
        "none": 1,
    }
    assert rolling["average_successful_response_time_ms"] == 12.3
    assert len(recent) == 3
    assert recent[0] == {
        "sequence": 1,
        "age_seconds": 0.0,
        "type": "bulk",
        "method": "POST",
        "http_status": 200,
        "outcome": "success",
        "duration_ms": 12.3,
        "attempt": 1,
        "retry": False,
        "bulk_size": 3,
    }


def test_bulk_metrics_distinguish_http_success_from_usable_payload() -> None:
    metrics = RequestMetrics()

    metrics.record_bulk_items((200, 200, 404), usable=(True, False, False))

    snapshot = metrics.snapshot()
    assert snapshot["bulk_items_total"] == 3
    assert snapshot["bulk_items_successful"] == 1
    assert snapshot["bulk_items_failed"] == 2
    assert snapshot["bulk_items_parse_failed"] == 1


def test_bulk_metrics_distinguish_server_and_gateway_statuses() -> None:
    metrics = RequestMetrics()

    metrics.record_bulk_items(
        (503, 502, 200),
        usable=(False, False, True),
        server_statuses=(503, 200, 200),
        gateway_statuses=(None, 502, 200),
    )

    snapshot = metrics.snapshot()
    assert snapshot["bulk_items_by_server_status_class"] == {"2xx": 2, "5xx": 1}
    assert snapshot["bulk_items_by_gateway_status_class"] == {
        "2xx": 1,
        "5xx": 1,
        "none": 1,
    }


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


def test_rolling_metrics_expire_old_minutes_and_exclude_failed_latency() -> None:
    metrics = RequestMetrics(started_at=0.0)
    with patch(
        "custom_components.bosch_buderus_heating.pointt.metrics.monotonic",
        side_effect=(60.0, 120.0, 3700.0, 3721.0),
    ):
        metrics.record_request(
            category="resource",
            method="GET",
            status=200,
            outcome="success",
            duration_ms=40.0,
        )
        metrics.record_request(
            category="resource",
            method="GET",
            status=None,
            outcome="timeout",
            duration_ms=9000.0,
            attempt=2,
            retry=True,
            request_type="fallback",
            fallback_reason="gateway_5xx",
        )
        metrics.record_request(
            category="bulk",
            method="POST",
            status=200,
            outcome="success",
            duration_ms=275.0,
            bulk_size=4,
        )
        snapshot = metrics.snapshot()

    rolling = snapshot["rolling_60_minutes"]
    assert rolling["requests_total"] == 1
    assert rolling["requests_successful"] == 1
    assert rolling["average_successful_response_time_ms"] == 275.0
    assert rolling["p95_successful_response_time_ms"] == 500.0
    assert rolling["requests_by_type"] == {"bulk": 1}
    assert len(snapshot["recent_requests"]) == 1


def test_recent_requests_are_bounded_and_do_not_contain_private_data() -> None:
    metrics = RequestMetrics()
    for _index in range(MAX_RECENT_REQUESTS + 10):
        metrics.record_request(
            category="resource",
            method="GET",
            status=200,
            outcome="success",
            duration_ms=1.0,
        )

    recent = metrics.snapshot()["recent_requests"]
    assert len(recent) == MAX_RECENT_REQUESTS
    assert recent[0]["sequence"] == 11
    assert not any(
        forbidden in str(recent).lower()
        for forbidden in ("gatewayid", "resource_path", "token", "payload", "url")
    )


def test_bulk_details_enrich_the_matching_request_only() -> None:
    metrics = RequestMetrics()
    sequence = metrics.record_request(
        category="bulk",
        method="POST",
        status=200,
        outcome="success",
        duration_ms=50.0,
        bulk_size=3,
    )
    metrics.record_bulk_items(
        (200, 200, 200),
        usable=(True, False, False),
        server_statuses=(200, 200, 503),
        gateway_statuses=(200, 502, None),
        request_sequence=sequence,
    )

    snapshot = metrics.snapshot()
    event = snapshot["recent_requests"][0]
    assert event["http_status"] == 200
    assert event["bulk_items_successful"] == 1
    assert event["bulk_items_failed"] == 2
    assert event["bulk_items_parse_failed"] == 2
    assert event["bulk_server_statuses"] == {"200": 2, "503": 1}
    assert event["bulk_gateway_statuses"] == {"200": 1, "502": 1, "none": 1}
    assert snapshot["rolling_60_minutes"]["bulk_items_total"] == 3
