"""Discovery result categories are stable codes: never a message, an address
or a payload, whatever text the error carries."""

from __future__ import annotations

import pytest

from custom_components.bosch_buderus_heating.discovery import (
    _error_category,
    _result_category,
    error_category,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    BatchItemResult,
    InvalidPayload,
    RateLimited,
    RequestTimeout,
    Resource,
    ResourceForbidden,
    ResourceNotFound,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.pointt.exceptions import (
    PointTError,
    TransportError,
)

SECRET = "https://example.invalid/token=secret-value"


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (AuthenticationError(SECRET), "authentication_error"),
        (RateLimited(30), "rate_limited"),
        (RequestTimeout(SECRET), "timeout"),
        (ServiceUnavailable(), "service_unavailable"),
        (TransportError(SECRET), "transport_error"),
        (InvalidPayload(SECRET), "malformed"),
        (ResourceForbidden("/x", 403), "http_403"),
        (ResourceNotFound("/x", 404), "http_404"),
        (PointTError(SECRET), "request_failed"),
    ],
)
def test_an_error_becomes_a_category_without_its_text(error, category) -> None:
    assert error_category(error) == category
    assert _error_category(error) == category
    assert "secret" not in category


def _result(**fields) -> BatchItemResult:
    return BatchItemResult(
        gateway_id="gw", path="/x", status=fields.pop("status", None), **fields
    )


def test_a_bulk_item_is_categorised_by_what_it_returned() -> None:
    assert (
        _result_category(
            _result(status=200, resource=Resource("/x", value=1, has_value=True))
        )
        == "success"
    )
    # A malformed answer is malformed whatever the status says.
    assert _result_category(_result(status=200, error=InvalidPayload(SECRET))) == (
        "malformed"
    )
    assert _result_category(_result(status=403)) == "http_403"
    assert _result_category(_result(error=RequestTimeout(SECRET))) == "timeout"
    assert _result_category(_result()) == "unavailable"
