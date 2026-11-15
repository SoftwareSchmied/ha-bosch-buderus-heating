"""Connection runtime: refused misuse, closing drivers that fail or hang,
and sources that stop for good."""

from __future__ import annotations

import asyncio
import logging

import pytest

from custom_components.bosch_buderus_heating import connection_runtime as runtime_module
from custom_components.bosch_buderus_heating.connection_runtime import (
    ConnectionPhase,
    SourceFailure,
)
from tests.test_connection_runtime import build, settle

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def test_a_source_failure_has_only_the_known_phases() -> None:
    for phase in (
        ConnectionPhase.UNAVAILABLE,
        ConnectionPhase.AUTH_REQUIRED,
        ConnectionPhase.UNSUPPORTED,
    ):
        assert SourceFailure(phase).phase is phase
    with pytest.raises(ValueError, match="Invalid source failure category"):
        SourceFailure(ConnectionPhase.READY)


async def test_a_runtime_starts_once_and_never_after_closing(hass) -> None:
    _, _, _, _, runtime = build(hass)
    runtime.start()
    try:
        with pytest.raises(RuntimeError, match="cannot be started twice"):
            runtime.start()
    finally:
        await runtime.close()
    with pytest.raises(RuntimeError, match="cannot be started twice"):
        runtime.start()


async def test_a_closed_runtime_ignores_new_configuration(hass) -> None:
    _, _, _, _, runtime = build(hass)
    runtime.start()
    await settle(hass)
    snapshot = runtime.snapshot
    sources = dict(runtime._data)
    await runtime.close()
    runtime.synchronize(runtime.management.current())
    assert runtime.snapshot is snapshot
    assert runtime._data == {} and sources
    assert not runtime._tasks


async def test_an_unpublished_source_has_no_context(hass) -> None:
    _, cloud, _, harness, runtime = build(hass)
    harness.gates[cloud] = asyncio.Event()
    runtime.start()
    await settle(hass)
    try:
        cloud_binding = next(
            key
            for key, binding in runtime.snapshot.bindings.items()
            if binding.connection_id == cloud
        )
        assert runtime.source_context(cloud_binding) is None
        assert runtime.source_context("local") is not None
        assert runtime.source_context("unknown") is None
    finally:
        harness.gates[cloud].set()
        await runtime.close()


async def test_a_driver_that_cannot_close_is_logged_by_class_only(
    hass, caplog: pytest.LogCaptureFixture
) -> None:
    _, cloud, _, harness, runtime = build(hass)
    runtime.start()
    await settle(hass)

    async def broken():
        raise RuntimeError("token=secret-value")

    driver = harness.drivers[cloud][0]
    driver.close = broken  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        await runtime.close()
    visible = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert "A connection resource could not be closed (RuntimeError)" in visible
    assert not any("secret-value" in message for message in visible)
    assert not runtime._tasks


async def test_a_driver_that_hangs_while_closing_does_not_block_the_close(
    hass, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(runtime_module, "CLOSE_TIMEOUT", 0.05)
    _, cloud, _, harness, runtime = build(hass)
    runtime.start()
    await settle(hass)

    async def hang():
        await asyncio.sleep(60)

    harness.drivers[cloud][0].close = hang  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        async with asyncio.timeout(10):
            await runtime.close()
    assert "A connection resource could not be closed (TimeoutError)" in [
        r.getMessage() for r in caplog.records
    ]


async def test_a_source_that_is_unsupported_stops_for_good_and_alone(hass) -> None:
    _, cloud, _, harness, runtime = build(hass)
    runtime.start()
    await settle(hass)
    keys = [
        key
        for key, binding in runtime.snapshot.bindings.items()
        if binding.connection_id == cloud
    ]
    try:
        harness.source(cloud, keys[0]).messages.put_nowait(
            SourceFailure(ConnectionPhase.UNSUPPORTED)
        )
        await settle(hass)
        assert runtime.source_data(keys[0]) is None
        assert runtime.source_phase(keys[0]) is ConnectionPhase.UNSUPPORTED
        # Never retried: no wait was scheduled; siblings and local run on.
        assert harness.sleeps.empty()
        assert all(runtime.source_data(key) for key in keys[1:])
        assert runtime.source_data("local")
    finally:
        await runtime.close()
