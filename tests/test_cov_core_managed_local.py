"""Local source: explicit reads, rescans and refused connections when the
source stops being current, is replaced or never started."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.bosch_buderus_heating.configuration import ConfigurationError
from custom_components.bosch_buderus_heating.local_api.catalog import request_for_path
from custom_components.bosch_buderus_heating.local_api.models import (
    SystemInfo,
    SystemModule,
)
from custom_components.bosch_buderus_heating.local_read import ReadResult, ReadState
from custom_components.bosch_buderus_heating.local_transport import StaleLocalRequest
from custom_components.bosch_buderus_heating.managed_local import (
    SYSTEM_READ_ATTEMPTS,
    LocalConnection,
    LocalSource,
)

ROOT = "custom_components.bosch_buderus_heating.managed_local"
GATEWAY = "000123"
REQUEST = request_for_path("/heatSources/returnTemperature")


def _source(results, *, current=True) -> LocalSource:
    source = object.__new__(LocalSource)
    source.binding = SimpleNamespace(to_data=lambda: {"locator": {"value": GATEWAY}})
    source.client = SimpleNamespace(read=AsyncMock(side_effect=results))
    source.current = lambda: current  # type: ignore[method-assign]
    return source


def _deferred(wait: float | None = 0.5, problem: str = "busy") -> ReadResult:
    return ReadResult(
        REQUEST, ReadState.DEFERRED, problem=problem, retry_after_seconds=wait
    )


def _success() -> ReadResult:
    return ReadResult(REQUEST, ReadState.SUCCESS)


# ------------------------------------------------------- read_explicit


async def test_an_explicit_read_that_is_not_running_gives_nothing() -> None:
    source = _source([], current=False)
    assert await source.read_explicit(REQUEST) is None
    source = _source([])
    source.client = None
    assert await source.read_explicit(REQUEST) is None


async def test_an_explicit_read_waits_for_its_turn_once_and_returns_the_answer() -> (
    None
):
    source = _source([_deferred(), _success()])
    with patch(ROOT + ".asyncio.sleep", AsyncMock()) as sleep:
        result = await source.read_explicit(REQUEST)
    assert result is not None
    assert result.state is ReadState.SUCCESS
    assert source.client.read.await_count == 2
    sleep.assert_awaited_once_with(0.5)


async def test_an_explicit_read_that_stays_deferred_returns_the_deferred_result() -> (
    None
):
    source = _source([_deferred()] * SYSTEM_READ_ATTEMPTS)
    with patch(ROOT + ".asyncio.sleep", AsyncMock()) as sleep:
        result = await source.read_explicit(REQUEST)
    assert result is not None
    assert result.state is ReadState.DEFERRED
    # Never more than the bounded number of reads, no wait after the last.
    assert source.client.read.await_count == SYSTEM_READ_ATTEMPTS
    assert sleep.await_count == SYSTEM_READ_ATTEMPTS - 1


async def test_a_long_pause_is_returned_without_waiting() -> None:
    source = _source([_deferred(wait=600.0, problem="paused")])
    with patch(ROOT + ".asyncio.sleep", AsyncMock()) as sleep:
        result = await source.read_explicit(REQUEST)
    assert result is not None
    assert result.problem == "paused"
    sleep.assert_not_awaited()
    assert source.client.read.await_count == 1


async def test_an_answer_without_a_wait_time_is_not_retried() -> None:
    source = _source([_deferred(wait=None)])
    result = await source.read_explicit(REQUEST)
    assert result is not None
    assert source.client.read.await_count == 1


async def test_an_explicit_read_stops_when_the_source_ends_while_waiting() -> None:
    source = _source([_deferred(), _success()])
    state = {"current": True}
    source.current = lambda: state["current"]  # type: ignore[method-assign]

    async def sleep(_seconds):
        state["current"] = False

    with patch(ROOT + ".asyncio.sleep", sleep):
        assert await source.read_explicit(REQUEST) is None
    assert source.client.read.await_count == 1


async def test_an_answer_of_a_source_that_ended_is_dropped() -> None:
    source = _source([_success()])
    state = {"current": True}
    source.current = lambda: state["current"]  # type: ignore[method-assign]

    async def read(_request):
        state["current"] = False
        return _success()

    source.client = SimpleNamespace(read=read)
    assert await source.read_explicit(REQUEST) is None


async def test_a_stale_request_ends_the_explicit_read_quietly() -> None:
    assert await _source([StaleLocalRequest()]).read_explicit(REQUEST) is None


async def test_a_source_closed_during_the_read_gives_nothing() -> None:
    assert await _source([asyncio.CancelledError()]).read_explicit(REQUEST) is None


async def test_cancelling_the_caller_is_not_swallowed() -> None:
    started = asyncio.Event()

    async def read(_request):
        started.set()
        await asyncio.sleep(60)

    source = _source([])
    source.client = SimpleNamespace(read=read)
    task = asyncio.ensure_future(source.read_explicit(REQUEST))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------- read_system, test_connection


async def test_cancelling_the_caller_of_a_system_read_is_not_swallowed() -> None:
    started = asyncio.Event()

    async def read(_request):
        started.set()
        await asyncio.sleep(60)

    source = _source([])
    source.client = SimpleNamespace(read=read)
    task = asyncio.ensure_future(source.read_system())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_system_read_of_a_replaced_source_gives_nothing() -> None:
    assert await _source([StaleLocalRequest()]).read_system() is None
    assert await _source([asyncio.CancelledError()]).read_system() is None
    assert await _source([], current=False).read_system() is None


async def test_a_system_read_stops_when_the_source_ends_while_waiting() -> None:
    source = _source([_deferred(), _success()])
    state = {"current": True}
    source.current = lambda: state["current"]  # type: ignore[method-assign]

    async def sleep(_seconds):
        state["current"] = False

    with patch(ROOT + ".asyncio.sleep", sleep):
        assert await source.read_system() is None


async def test_a_system_answer_that_is_not_a_system_record_is_not_used() -> None:
    module = SystemModule("t", "1", "1.0", "s", "hw", None, None)
    wrong = SimpleNamespace(
        state=ReadState.SUCCESS,
        retry_after_seconds=None,
        observation=SimpleNamespace(data=object()),
        problem=None,
    )
    assert await _source([wrong]).read_system() is None
    # A path error is no system record either.
    failed = ReadResult(REQUEST, ReadState.PATH_ERROR, status=404)
    assert await _source([failed]).read_system() is None
    other = ReadResult(
        REQUEST,
        ReadState.SUCCESS,
        observation=SimpleNamespace(  # type: ignore[arg-type]
            data=SystemInfo("completed", "", "other-gateway", (module,))
        ),
    )
    # The identity wrapper turns an answer of another gateway into an error.
    assert await _source([other]).read_system() is None


async def test_the_connection_test_of_a_replaced_source_is_not_running() -> None:
    assert await _source([StaleLocalRequest()]).test_connection() == (
        "not_running",
        0,
    )


async def test_the_connection_test_does_not_swallow_a_cancelled_caller() -> None:
    started = asyncio.Event()

    async def read(_request):
        started.set()
        await asyncio.sleep(60)

    source = _source([])
    source.client = SimpleNamespace(read=read)
    task = asyncio.ensure_future(source.test_connection())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ------------------------------------------------------------- rescans


def test_a_rescan_needs_a_running_current_source() -> None:
    source = _source([])
    source.service = None
    assert source.rescan_state() == "unavailable"
    assert source.rescan() == "unavailable"
    source.service = SimpleNamespace(rescan_state=lambda: "idle", rescan=lambda: "ok")
    assert source.rescan_state() == "idle"
    assert source.rescan() == "ok"
    source.current = lambda: False  # type: ignore[method-assign]
    assert source.rescan_state() == "unavailable"
    assert source.rescan() == "unavailable"


# ------------------------------------------------- connection lifecycle


def _connection(kind="k40_local", closed=False) -> LocalConnection:
    factory = SimpleNamespace(
        runtime=lambda: SimpleNamespace(
            snapshot=SimpleNamespace(bindings={"b": object()})
        ),
        hass=Mock(),
    )
    return LocalConnection(factory, "conn", SimpleNamespace(kind=kind))  # type: ignore[arg-type]


async def test_a_closed_or_foreign_connection_never_connects() -> None:
    await _connection().connect()
    with pytest.raises(ConfigurationError) as foreign:
        await _connection(kind="singlekey_cloud").connect()
    assert str(foreign.value) == "unsupported_connection"
    closed = _connection()
    closed.closed = True
    with pytest.raises(ConfigurationError):
        await closed.connect()


async def test_a_source_is_only_made_for_its_own_open_connection() -> None:
    connection = _connection()
    own = SimpleNamespace(connection_id="conn", adapter="k40_local")
    source = connection.source("b", own)  # type: ignore[arg-type]
    assert connection.sources == {"b": source}
    for binding in (
        SimpleNamespace(connection_id="other", adapter="k40_local"),
        SimpleNamespace(connection_id="conn", adapter="singlekey_cloud"),
    ):
        with pytest.raises(ConfigurationError) as error:
            connection.source("c", binding)  # type: ignore[arg-type]
        assert str(error.value) == "local_configuration_changed"
    assert set(connection.sources) == {"b"}
    # After closing, no new source is handed out and the old one is closed.
    source.close = AsyncMock()  # type: ignore[method-assign]
    await connection.close()
    source.close.assert_awaited_once()
    with pytest.raises(ConfigurationError):
        connection.source("d", own)  # type: ignore[arg-type]
