"""Supplementary recovery stays isolated from HA and other gateway replies."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    BatchItemResult,
    RateLimited,
    RequestTimeout,
    Resource,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.pointt.transport import RateLimitBackoff
from custom_components.bosch_buderus_heating.startup_data import (
    CIRCUIT_COUNT_PATH,
    StartupGatewayData,
    StartupSession,
)
from custom_components.bosch_buderus_heating.startup_reader import StartupReader

PRODUCT = "/devices/synthetic/productName"
STAMP = datetime(2026, 10, 11, tzinfo=UTC)


@pytest.fixture
def reader():
    """No coordinator, config entry, or HA instance is needed for the service."""
    client = Mock()
    client.backoff = RateLimitBackoff()
    client.get_resources_bulk = AsyncMock()
    client.get_resource = AsyncMock()
    state = StartupGatewayData()
    state.schedule(PRODUCT, 2, advertised=True)
    state.schedule(CIRCUIT_COUNT_PATH, 1, registered=True)
    service = StartupReader(client, "one", state, roots=("/devices", "/heatSources"))
    return client, state, service


async def test_reader_returns_partial_success_and_keeps_missing_sensor_recoverable(
    reader,
):
    client, state, service = reader
    client.get_resources_bulk.return_value = (
        BatchItemResult(
            "one", PRODUCT, 200, Resource(PRODUCT, value="MX400", has_value=True)
        ),
        BatchItemResult("one", CIRCUIT_COUNT_PATH, 504),
    )
    with patch(
        "custom_components.bosch_buderus_heating.startup_reader.monotonic",
        return_value=100,
    ):
        result = await service.async_read_batch(())
    assert result.attempted
    assert dict(result.outcomes) == {
        PRODUCT: "success",
        CIRCUIT_COUNT_PATH: "service_unavailable",
    }
    assert state.reads[CIRCUIT_COUNT_PATH].next_attempt == 160
    assert state.due(160) == (CIRCUIT_COUNT_PATH,)
    assert not state.healthy  # Metadata success cannot confirm functional health.
    client.get_resource.assert_not_called()


@pytest.mark.parametrize("wrong_identity", ["gateway", "path", "resource"])
async def test_reader_never_caches_a_reply_for_a_different_resource(
    reader, wrong_identity
):
    client, state, service = reader
    client.get_resources_bulk.return_value = (
        BatchItemResult(
            "other" if wrong_identity == "gateway" else "one",
            "/devices/unrequested/productName" if wrong_identity == "path" else PRODUCT,
            200,
            Resource(
                "/devices/unrequested/productName"
                if wrong_identity == "resource"
                else PRODUCT,
                value="MX400",
                has_value=True,
            ),
        ),
    )
    result = await service.async_read_batch(())
    assert result.attempted
    assert not state.cached_reads()
    assert all(read.pending and read.attempts == 1 for read in state.reads.values())
    assert set(state.reads) == {PRODUCT, CIRCUIT_COUNT_PATH}


@pytest.mark.parametrize("auth_position", [0, 1])
async def test_item_401_is_a_temporary_failure_of_that_path_only(reader, auth_position):
    """C14/START-3: an item 401 in an accepted request asks for no sign-in."""
    client, state, service = reader
    paths = (PRODUCT, CIRCUIT_COUNT_PATH)
    client.get_resources_bulk.return_value = tuple(
        BatchItemResult("one", path, 401 if index == auth_position else 503)
        for index, path in enumerate(paths)
    )
    with patch(
        "custom_components.bosch_buderus_heating.startup_reader.monotonic"
    ) as now:
        now.return_value = 1000.0
        result = await service.async_read_batch(())
    assert dict(result.outcomes) == {
        paths[auth_position]: "http_401",
        paths[1 - auth_position]: "service_unavailable",
    }
    assert result.rate_limit is None
    unauthorized = state.reads[paths[auth_position]]
    # Retried with the normal delay, like any temporary failure.
    assert unauthorized.pending and unauthorized.result == "http_401"
    assert unauthorized.attempts == unauthorized.failures == 1
    assert unauthorized.next_attempt == 1060.0
    assert not state.cached_reads()


async def test_refused_request_still_asks_for_sign_in(reader):
    client, state, service = reader
    client.get_resources_bulk.side_effect = AuthenticationError()
    with pytest.raises(AuthenticationError):
        await service.async_read_batch(())
    assert all(
        read.result == "authentication_error" and read.attempts == read.failures == 1
        for read in state.reads.values()
    )


@pytest.mark.parametrize(
    "error,category",
    [
        (RequestTimeout(), "timeout"),
        (ServiceUnavailable(504), "service_unavailable"),
        (RateLimited(120), "rate_limited"),
    ],
)
async def test_reader_reports_temporary_failure_without_an_immediate_retry(
    reader, error, category
):
    client, state, service = reader
    client.get_resources_bulk.side_effect = error
    result = await service.async_read_batch(())
    assert dict(result.outcomes) == {path: category for path in state.reads}
    assert result.rate_limit is (error if isinstance(error, RateLimited) else None)
    assert all(read.pending for read in state.reads.values())
    client.get_resources_bulk.assert_awaited_once()
    client.get_resource.assert_not_called()


def test_observers_cannot_mutate_the_queue_or_its_captured_status():
    state = StartupGatewayData()
    state.schedule(PRODUCT, 2, registered=True)
    view = state.reads
    original = view[PRODUCT]
    with pytest.raises(TypeError):
        view[PRODUCT] = original
    with pytest.raises(FrozenInstanceError):
        original.next_attempt = 0
    state.start((PRODUCT,), STAMP)
    state.fail((PRODUCT,), "service_unavailable", 100)
    assert original.attempts == 0 and original.result == "pending"
    assert view[PRODUCT].attempts == 1 and view[PRODUCT].next_attempt == 160
    with pytest.raises(AttributeError):
        state.requested.add("/unrequested")
    assert state.requested == {PRODUCT}


def test_session_observes_lifecycle_once_across_automatic_retries():
    options = {"profile": "standard"}
    session = StartupSession(("two", "one"), options=options)
    remove = Mock()
    register = Mock(return_value=remove)
    session.attach_state_listener(register)
    session.attach_state_listener(register)
    register.assert_called_once_with()
    options["profile"] = "changed"
    assert session.can_resume(("one", "two"), {"profile": "standard"})
    assert not session.can_resume(("one", "two"), options)
    assert not session.can_resume(("one",), {"profile": "standard"})
    session.mark_setup_complete()
    assert not session.can_resume(("one", "two"), {"profile": "standard"})
    session.detach()
    session.detach()
    remove.assert_called_once_with()


def test_closed_session_cannot_resume_or_claim_even_if_setup_finishes():
    session = StartupSession(("one",))
    state = session.gateway("one")
    state.schedule(PRODUCT, 2)
    state.record_main_cycle(successful=True)
    session.close()
    assert not session.can_resume(("one",), {})
    session.mark_setup_complete()
    assert not session.claim("one", 100)
