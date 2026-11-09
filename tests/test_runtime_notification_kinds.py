"""Runtime notifications name what changed (C68), a failing listener is
logged once (C61), and a view without the cloud that failed to build is
tried again (C64)."""

import asyncio
import functools
import itertools
import logging
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.bosch_buderus_heating import (
    managed_entry,
    managed_platforms,
    managed_services,
)
from custom_components.bosch_buderus_heating.connection_runtime import (
    TARGET_RETRY_SECONDS,
    ConnectionPhase,
    ConnectionRuntime,
    RepeatedFailure,
    SourceFailure,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.gateway_identity import GatewayNamespace
from custom_components.bosch_buderus_heating.local_api.catalog import request_for_path
from custom_components.bosch_buderus_heating.local_api.decoder import decode_resource
from custom_components.bosch_buderus_heating.local_polling.inventory import (
    Freshness,
    ResourceView,
)
from custom_components.bosch_buderus_heating.local_state import (
    LocalCondition,
    LocalSourceState,
)
from tests.test_connection_runtime import Harness, build, entry_for, settle
from tests.test_local_backed import (
    CURRENT,
    GATEWAY,
    ReadingBackend,
    local_values,
    started,
    state,
)
from tests.test_local_read import payload
from tests.test_managed_pointt import eventually, running, source_for

RUNTIME = "custom_components.bosch_buderus_heating.connection_runtime"
_MONOTONIC = itertools.count(1000.0)


def bounded(test):
    """Fail within 90 s instead of hanging."""

    @functools.wraps(test)
    async def run(*args, **kwargs):
        async with asyncio.timeout(90):
            await test(*args, **kwargs)

    return run


def reading(context, value, condition=LocalCondition.READY):
    """A real local source state with one reading of the paired path."""
    identity = context.identity_context(GatewayNamespace.K40_LOCAL)
    request = request_for_path(CURRENT)
    observation = decode_resource(
        request,
        payload(CURRENT, value=value, writeable=0),
        context=identity,
        observed_at=datetime.now(UTC),
        observed_monotonic=next(_MONOTONIC),
    )
    view = ResourceView(
        request, Freshness.FRESH, True, 0.0, 60, None, None, observation
    )
    return LocalSourceState(
        identity, condition, True, (observation,), resources=(view,)
    )


# ------------------------------------------------------------------ C68


@bounded
async def test_only_value_listeners_hear_new_local_readings(hass):
    _, _, _, harness, runtime = build(hass)
    calls = {"values": 0, "state": 0}

    def values():
        calls["values"] += 1

    def phases():
        calls["state"] += 1

    runtime.subscribe(values)
    runtime.subscribe(phases, local_values=False)
    runtime.start()
    await settle(hass)
    try:
        context = harness.source("local", "local").context

        def published(value, condition=LocalCondition.READY):
            before = dict(calls)
            assert context.publish_local(reading(context, value, condition))
            return {key: calls[key] - before[key] for key in calls}

        # The first local state and every change of the condition: both.
        assert published(21.0) == {"values": 1, "state": 1}
        # New readings alone: only listeners that want them.
        assert published(21.5) == {"values": 1, "state": 0}
        assert published(22.0) == {"values": 1, "state": 0}
        assert runtime.source_data("local").current_observations
        assert published(22.0, LocalCondition.PAUSED) == {"values": 1, "state": 1}
        assert runtime.source_data("local") is None
        assert published(22.0, LocalCondition.PAUSED) == {"values": 1, "state": 0}
        # Same phase, other condition: still a change of state.
        assert published(22.0, LocalCondition.UNAVAILABLE) == {
            "values": 1,
            "state": 1,
        }
        assert published(22.5) == {"values": 1, "state": 1}
        assert runtime.source_phase("local") is ConnectionPhase.READY
        # Anything else (here a cloud publication) reaches everyone.
        cloud = runtime.snapshot.bindings
        key = next(k for k, b in cloud.items() if b.adapter == "pointt")
        source = harness.source(cloud[key].connection_id, key)
        before = dict(calls)
        assert source.context.publish({"sample": "again"})
        assert calls["values"] - before["values"] == 1
        assert calls["state"] - before["state"] == 1
    finally:
        await runtime.close()


@bounded
async def test_a_failed_local_source_reading_again_is_a_change_of_state(hass):
    _, _, _, harness, runtime = build(hass)
    seen = []
    runtime.subscribe(lambda: seen.append(1), local_values=False)
    runtime.start()
    await settle(hass)
    try:
        source = harness.source("local", "local")
        assert source.context.publish_local(reading(source.context, 21.0))
        source.messages.put_nowait(SourceFailure())
        await settle(hass)
        assert runtime.source_phase("local") is ConnectionPhase.UNAVAILABLE
        harness.retry.set()
        await settle(hass)
        source = harness.source("local", "local")
        seen.clear()
        # The new attempt's first reading is published as a change.
        assert source.context.publish_local(reading(source.context, 21.5))
        assert seen == [1]
        assert runtime.source_phase("local") is ConnectionPhase.READY
    finally:
        await runtime.close()


@pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
@bounded
async def test_local_readings_skip_the_configuration_work(hass) -> None:
    """End to end: the readings reach the entity, while the platforms, the
    repairs and the notifications are not looked at again."""
    hass.config.components.add(DOMAIN)
    backend = ReadingBackend()
    async with running(hass, backend, configure=local_values()) as (_, _, owner, _):
        await started(hass, owner)
        runtime = owner.connections
        await eventually(
            lambda: getattr(source_for(owner, GATEWAY).source, "_published", False)
        )
        await settle(hass)
        context = runtime._contexts["local"]
        # From here on real local states, as the local source publishes them.
        del runtime._local_states["local"]
        assert context.publish_local(reading(context, 23.0))
        await eventually(lambda: state(hass, owner) == "23.0")
        work = {"sources": 0, "compatibility": 0, "not_found": 0}
        sources = managed_platforms._Platform._sources

        def counted_sources(self):
            work["sources"] += 1
            return sources(self)

        def counted(name, original):
            def call(*args, **kwargs):
                work[name] += 1
                return original(*args, **kwargs)

            return call

        with (
            patch.object(managed_platforms._Platform, "_sources", counted_sources),
            patch.object(
                managed_services,
                "sync_firmware_compatibility_issue",
                counted(
                    "compatibility",
                    managed_services.sync_firmware_compatibility_issue,
                ),
            ),
            patch.object(
                managed_entry,
                "sync_not_found_issues",
                counted("not_found", managed_entry.sync_not_found_issues),
            ),
        ):
            for value in (23.5, 24.0, 24.5):
                assert context.publish_local(reading(context, value))
                await eventually(lambda value=value: state(hass, owner) == str(value))
            assert work == {"sources": 0, "compatibility": 0, "not_found": 0}
            # A change of state still reaches all of them.
            runtime._notify()
            await settle(hass)
            assert work["sources"] > 0
            assert work["compatibility"] > 0
            assert work["not_found"] == 1


# ------------------------------------------------------------------ C61


@bounded
async def test_a_failing_listener_is_logged_once_per_series(hass, caplog):
    caplog.set_level(logging.DEBUG, logger=RUNTIME)
    _, _, _, _, runtime = build(hass)
    fail = [KeyError("a private value 21.5")]

    def broken():
        if fail:
            raise fail[0]

    healthy = []
    runtime.start()
    await settle(hass)
    runtime.subscribe(broken)
    runtime.subscribe(lambda: healthy.append(1))
    try:

        def records(level):
            return [
                record
                for record in caplog.records
                if record.name == RUNTIME
                and record.levelno == level
                and "listener failed" in record.getMessage()
            ]

        caplog.clear()
        for _ in range(4):
            runtime._notify()
        assert len(healthy) == 4  # the others are still called
        errors = records(logging.ERROR)
        assert len(errors) == 1
        message = errors[0].getMessage()
        assert "KeyError" in message and "broken" in message
        assert "21.5" not in caplog.text.split("Traceback")[0]
        assert errors[0].exc_info is None
        debug = records(logging.DEBUG)
        assert any(record.exc_info for record in caplog.records if record.levelno == 10)
        assert len(debug) == 3  # the repetitions
        # Another error class is new; a success ends the series.
        fail[0] = TypeError("other")
        runtime._notify()
        assert len(records(logging.ERROR)) == 2
        fail.clear()
        runtime._notify()
        fail.append(TypeError("again"))
        runtime._notify()
        assert len(records(logging.ERROR)) == 3
    finally:
        await runtime.close()
    assert not runtime._failing_listeners or all(
        listener is broken for listener in runtime._failing_listeners
    )


def test_an_unsubscribed_listener_leaves_no_failure_record(hass):
    _, _, _, _, runtime = build(hass)

    def broken():
        raise RuntimeError

    remove = runtime.subscribe(broken)
    runtime._notify()
    assert broken in runtime._failing_listeners
    remove()
    assert broken not in runtime._failing_listeners
    runtime._notify()


def test_repeated_failures_are_kept_per_key(caplog):
    logger = logging.getLogger("tests.repeated")
    caplog.set_level(logging.DEBUG, logger="tests.repeated")
    failures = RepeatedFailure(logger, "Something failed")
    failures.failed(ValueError("value 3"), "a")
    failures.failed(ValueError("value 4"), "a")
    failures.failed(ValueError("value 5"), "b")
    failures.succeeded("b")
    failures.failed(ValueError("value 6"), "a")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.getMessage() for r in warnings] == [
        "Something failed (ValueError); further failures are logged at debug level"
    ] * 2
    assert "value" not in " ".join(r.getMessage() for r in warnings)
    failures.succeeded("a")
    failures.failed(ValueError(), "a")
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 3


# ------------------------------------------------------------------ C64


class _Factory:
    """The harness factory plus view builds that fail a number of times."""

    def __init__(self, harness, failures):
        self.harness, self.failures = harness, failures
        self.runtime = None
        self.calls = []

    def __call__(self, key, record):
        return self.harness.factory(key, record)

    async def _view(self, binding_id, context, retain):
        self.calls.append(binding_id)
        if self.failures.get(binding_id, 0) > 0:
            self.failures[binding_id] -= 1
            raise RuntimeError("private detail 42")
        assert retain(context, SimpleNamespace(retain_for_local=lambda: True))

    async def dormant_view(self, binding_id, binding, context):
        if binding.adapter == "pointt":
            await self._view(binding_id, context, self.runtime.retain_dormant)

    async def local_plant_view(self, binding_id, binding, context):
        await self._view(binding_id, context, self.runtime.retain_startup)


def with_views(hass, failures, *, enabled=True):
    entry, cloud, _ = entry_for(hass)
    if not enabled:
        options = deepcopy(dict(entry.options))
        options["connections"][cloud]["enabled"] = False
        hass.config_entries.async_update_entry(entry, options=options)
    harness = Harness()
    factory = _Factory(harness, failures)
    runtime = ConnectionRuntime(
        hass,
        entry,
        {"singlekey_cloud": factory, "k40_local": harness.factory},
        sleep=harness.sleep,
    )
    factory.runtime = runtime
    pointt = [
        key
        for key, binding in runtime.snapshot.bindings.items()
        if binding.adapter == "pointt" and binding.connection_id == cloud
    ]
    return entry, cloud, harness, factory, runtime, pointt


def view_warnings(caplog):
    return [
        record
        for record in caplog.records
        if record.name == RUNTIME
        and record.levelno >= logging.WARNING
        and "without the cloud" in record.getMessage()
    ]


@bounded
async def test_a_failed_dormant_view_warns_once_and_is_built_again(hass, caplog):
    caplog.set_level(logging.DEBUG, logger=RUNTIME)
    failures: dict[str, int] = {}
    _, cloud, harness, factory, runtime, pointt = with_views(
        hass, failures, enabled=False
    )
    failures.update(dict.fromkeys(pointt, 2))
    runtime.start()
    await settle(hass)
    try:
        assert runtime.connection_phase(cloud) is ConnectionPhase.DISABLED
        assert not any(runtime.retained_data(key) for key in pointt)
        warnings = view_warnings(caplog)
        assert len(warnings) == len(pointt)
        assert all("RuntimeError" in r.getMessage() for r in warnings)
        assert "private detail" not in " ".join(r.getMessage() for r in warnings)
        assert sorted(runtime._dormant_retries) == sorted(pointt)
        assert [harness.sleeps.get_nowait() for _ in pointt] == [
            TARGET_RETRY_SECONDS
        ] * len(pointt)
        caplog.clear()
        harness.retry.set()  # the pauses end: fails once more, then succeeds
        await eventually(lambda: all(runtime.retained_data(key) for key in pointt))
        assert factory.calls.count(pointt[0]) == 3
        assert not view_warnings(caplog)  # repetitions at debug level
        assert any("built again" in r.getMessage() for r in caplog.records)
        assert not runtime._dormant_warned and not runtime._dormant_retries
    finally:
        await runtime.close()


@bounded
async def test_a_pending_view_retry_ends_with_the_runtime(hass):
    failures: dict[str, int] = {}
    _, _, harness, factory, runtime, pointt = with_views(hass, failures, enabled=False)
    failures.update(dict.fromkeys(pointt, 10))
    runtime.start()
    await settle(hass)
    retries = tuple(runtime._dormant_retries.values())
    assert retries
    await runtime.close()
    assert all(task.done() for task in retries)
    calls = len(factory.calls)
    harness.retry.set()
    await settle(hass)
    assert len(factory.calls) == calls  # nothing is built after the close


@bounded
async def test_a_retry_after_the_access_was_switched_on_builds_nothing(hass):
    failures: dict[str, int] = {}
    entry, cloud, harness, factory, runtime, pointt = with_views(
        hass, failures, enabled=False
    )
    failures.update(dict.fromkeys(pointt, 1))
    runtime.start()
    await settle(hass)
    try:
        assert runtime._dormant_retries
        calls = len(factory.calls)
        options = deepcopy(dict(entry.options))
        options["connections"][cloud]["enabled"] = True
        hass.config_entries.async_update_entry(entry, options=options)
        await settle(hass)
        assert runtime.connection_phase(cloud) is ConnectionPhase.READY
        harness.retry.set()
        await settle(hass)
        assert len(factory.calls) == calls
        assert not any(runtime.retained_data(key) for key in pointt)
    finally:
        await runtime.close()


@bounded
async def test_a_failed_local_plant_view_is_built_again_while_sign_in_is_due(
    hass, caplog
):
    caplog.set_level(logging.DEBUG, logger=RUNTIME)
    failures: dict[str, int] = {}
    _, cloud, harness, factory, runtime, pointt = with_views(hass, failures)
    key = pointt[0]
    failures[key] = 1
    runtime.start()
    await settle(hass)
    try:
        expired = SimpleNamespace(retain_for_local=lambda: True)
        runtime._data[key] = expired
        source = harness.source(cloud, key)
        # A sign-in is due: the source stops without a retry and keeps its view.
        source.messages.put_nowait(SourceFailure(ConnectionPhase.AUTH_REQUIRED))
        await settle(hass)
        assert runtime.source_phase(key) is ConnectionPhase.AUTH_REQUIRED
        assert runtime.retained_data(key) is expired
        assert harness.sleeps.empty()
        # 2A: the expired view is replaced by a local plant view; it fails.
        assert runtime.expire_dormant(key, expired)
        await settle(hass)
        assert factory.calls == [key]
        assert runtime.retained_data(key) is None
        assert runtime._dormant_retries.keys() == {key}
        assert harness.sleeps.get_nowait() == TARGET_RETRY_SECONDS
        assert len(view_warnings(caplog)) == 1
        harness.retry.set()
        await eventually(lambda: runtime.retained_data(key) is not None)
        assert factory.calls == [key, key]
        assert not runtime._dormant_warned
    finally:
        await runtime.close()


@bounded
async def test_a_local_plant_retry_after_the_cloud_returned_builds_nothing(hass):
    failures: dict[str, int] = {}
    _, cloud, harness, factory, runtime, pointt = with_views(hass, failures)
    key = pointt[0]
    failures[key] = 1
    runtime.start()
    await settle(hass)
    try:
        expired = SimpleNamespace(retain_for_local=lambda: True)
        runtime._data[key] = expired
        harness.source(cloud, key).messages.put_nowait(
            SourceFailure(ConnectionPhase.AUTH_REQUIRED)
        )
        await settle(hass)
        assert runtime.expire_dormant(key, expired)
        await settle(hass)
        retry = runtime._dormant_retries[key]
        # The cloud answers again before the pause ends.
        runtime._connection_phases[cloud] = ConnectionPhase.READY
        runtime._source_phases[key] = ConnectionPhase.READY
        harness.retry.set()
        await asyncio.wait((retry,), timeout=5)
        await settle(hass)
        assert factory.calls == [key]
        assert runtime.retained_data(key) is None
    finally:
        await runtime.close()
