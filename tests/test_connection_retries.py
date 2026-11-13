"""B04/B08: failed source and connection attempts back off, log and count."""

import asyncio
import logging

import pytest

from custom_components.bosch_buderus_heating import connection_runtime
from custom_components.bosch_buderus_heating.connection_runtime import (
    RETRY_MAX_SECONDS,
    ConnectionPhase,
    ConnectionRuntime,
    SourceFailure,
)
from tests.test_connection_runtime import Driver, Harness, Source, entry_for, settle

PRIVATE = "private-value-4711"
GROWING = [60.0, 120.0, 240.0, 480.0, 960.0, RETRY_MAX_SECONDS, RETRY_MAX_SECONDS]


class FailingSource(Source):
    """Fails before it publishes while its driver holds a start error."""

    def __init__(self, binding_id, errors):
        super().__init__(binding_id)
        self.errors = errors

    async def run(self, context):
        if (error := self.errors.get(self.binding_id)) is not None:
            raise error
        await super().run(context)


class FailingDriver(Driver):
    def source(self, binding_id, binding):
        source = FailingSource(binding_id, self.harness.start_errors)
        self.sources.setdefault(binding_id, []).append(source)
        return source


class StepHarness(Harness):
    """Each retry pause ends only when the test lets one attempt through."""

    def __init__(self):
        super().__init__()
        self.start_errors = {}
        self.delays = []
        self.steps = asyncio.Queue()

    def factory(self, key, record):
        driver = FailingDriver(key, self)
        self.drivers.setdefault(key, []).append(driver)
        return driver

    async def sleep(self, delay):
        self.delays.append(delay)
        await self.steps.get()


def runtime_for(hass):
    entry, cloud, _ = entry_for(hass)
    harness = StepHarness()
    runtime = ConnectionRuntime(
        hass,
        entry,
        {"singlekey_cloud": harness.factory, "k40_local": harness.factory},
        sleep=harness.sleep,
    )
    binding = next(
        key
        for key, item in runtime.snapshot.bindings.items()
        if item.connection_id == cloud and item.adapter == "pointt"
    )
    return cloud, binding, harness, runtime


async def step(hass, harness):
    harness.steps.put_nowait(None)
    await settle(hass)


def records(caplog, level):
    return [
        record
        for record in caplog.records
        if record.name == connection_runtime.__name__ and record.levelno == level
    ]


@pytest.mark.parametrize("unexpected", [True, False])
async def test_failed_starts_wait_longer_until_a_start_works(hass, caplog, unexpected):
    caplog.set_level(logging.DEBUG, logger=connection_runtime.__name__)
    cloud, binding, harness, runtime = runtime_for(hass)
    error = KeyError(PRIVATE) if unexpected else SourceFailure()
    harness.start_errors[binding] = error
    runtime.start()
    await settle(hass)
    try:
        for _ in range(len(GROWING) - 1):
            await step(hass, harness)
        # B04: 1, 2, 4, 8, 16 and then at most 30 minutes, not 60 s forever.
        assert harness.delays == GROWING
        assert runtime.source_phase(binding) is ConnectionPhase.UNAVAILABLE
        assert runtime.source_failures(binding) == {
            "failures": 7,
            "consecutive_failures": 7,
            "last_failure": type(error).__name__,
            "last_failure_stage": "starting",
            "last_failure_phase": "unavailable",
            "retry_seconds": RETRY_MAX_SECONDS,
        }
        # Other sources of the same access are not affected.
        assert runtime.connection_phase(cloud) is ConnectionPhase.READY
        assert runtime.source_failures("local")["failures"] == 0

        del harness.start_errors[binding]
        await step(hass, harness)
        assert runtime.source_data(binding) == {"sample": binding}
        assert runtime.source_failures(binding)["consecutive_failures"] == 0
        assert runtime.source_failures(binding)["retry_seconds"] is None

        # A successful start resets the pause: a later failure waits 1 minute.
        harness.source(cloud, binding).messages.put_nowait(KeyError(PRIVATE))
        await settle(hass)
        assert harness.delays[-1] == 60.0
        assert runtime.source_failures(binding)["failures"] == 8
        assert runtime.source_failures(binding)["last_failure_stage"] == "running"
    finally:
        await runtime.close()

    # B08: one warning per series with the error class, never its message.
    warnings = records(caplog, logging.WARNING)
    assert [record.exc_info for record in warnings] == [None] * len(warnings)
    assert all(PRIVATE not in record.getMessage() for record in caplog.records)
    infos = [record.getMessage() for record in records(caplog, logging.INFO)]
    if unexpected:
        assert len(warnings) == 2
        assert "cloud source failed while starting (KeyError" in (
            warnings[0].getMessage()
        )
        assert "cloud source failed while running (KeyError" in (
            warnings[1].getMessage()
        )
        assert infos == ["A cloud source works again after 7 failed attempts"]
        # The traceback is available at debug level.
        assert any(
            record.exc_info is not None for record in records(caplog, logging.DEBUG)
        )
    else:
        # A failure the source reported itself; only the running KeyError warns.
        assert len(warnings) == 1
        assert "while running (KeyError" in warnings[0].getMessage()
        assert infos == []


async def test_failed_connections_wait_longer_and_log_once(hass, caplog):
    caplog.set_level(logging.DEBUG, logger=connection_runtime.__name__)
    cloud, _, harness, runtime = runtime_for(hass)
    harness.errors[cloud] = RuntimeError(PRIVATE)
    runtime.start()
    await settle(hass)
    try:
        for _ in range(3):
            await step(hass, harness)
        assert harness.delays == GROWING[:4]
        assert runtime.connection_phase(cloud) is ConnectionPhase.UNAVAILABLE
        # The local access keeps running.
        assert runtime.source_data("local")
        assert runtime.connection_failures(cloud) == {
            "failures": 4,
            "consecutive_failures": 4,
            "last_failure": "RuntimeError",
            "last_failure_stage": "connecting",
            "last_failure_phase": "unavailable",
            "retry_seconds": 480.0,
        }
        del harness.errors[cloud]
        await step(hass, harness)
        assert runtime.connection_phase(cloud) is ConnectionPhase.READY
        assert runtime.connection_failures(cloud)["consecutive_failures"] == 0
    finally:
        await runtime.close()

    warnings = [record.getMessage() for record in records(caplog, logging.WARNING)]
    assert len(warnings) == 1
    assert "cloud access failed while connecting (RuntimeError" in warnings[0]
    assert "next attempt follows in 60 seconds" in warnings[0]
    assert all(PRIVATE not in record.getMessage() for record in caplog.records)
    assert [record.getMessage() for record in records(caplog, logging.INFO)] == [
        "A cloud access works again after 4 failed attempts"
    ]


async def test_cloud_pause_is_kept_when_longer_than_the_growing_pause(hass):
    _, binding, harness, runtime = runtime_for(hass)
    harness.start_errors[binding] = SourceFailure(retry_after=7200)
    runtime.start()
    await settle(hass)
    try:
        await step(hass, harness)
        assert harness.delays == [7200.0, 7200.0]
        assert runtime.source_failures(binding)["retry_seconds"] == 7200.0
    finally:
        await runtime.close()


async def test_removed_access_forgets_its_failures(hass):
    cloud, binding, harness, runtime = runtime_for(hass)
    harness.start_errors[binding] = SourceFailure()
    runtime.start()
    await settle(hass)
    try:
        assert runtime.source_failures(binding)["failures"] == 1
        assert cloud in runtime._connection_failures
        runtime.management.remove(runtime.management.edit(cloud))
        await settle(hass)
        assert binding not in runtime._source_failures
        assert cloud not in runtime._connection_failures
        assert runtime.source_failures(binding)["failures"] == 0
    finally:
        await runtime.close()
