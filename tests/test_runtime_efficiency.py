"""Runtime notifications reuse the parsed configuration and notify once; an
unloaded runtime leaves no view build or fault baseline load behind."""

import asyncio
import functools
from copy import deepcopy
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.bosch_buderus_heating import configuration_management
from custom_components.bosch_buderus_heating.connection_runtime import (
    CachedConfiguration,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.managed_pointt import (
    ManagedPointTCoordinator,
)
from tests.test_connection_runtime import build, settle
from tests.test_local_backed import (
    GATEWAY,
    ReadingBackend,
    local_values,
    publish_local,
    started,
    state,
)
from tests.test_managed_pointt import eventually, running, source_for


def bounded(test):
    """Fail within 90 s instead of hanging."""

    @functools.wraps(test)
    async def run(*args, **kwargs):
        async with asyncio.timeout(90):
            await test(*args, **kwargs)

    return run


def counted_parses(monkeypatch):
    original, calls = configuration_management.load_configuration, []

    def load(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(configuration_management, "load_configuration", load)
    return calls


@bounded
async def test_notifications_reuse_the_parsed_configuration(hass, monkeypatch):
    _, _, _, harness, runtime = build(hass)
    checks = []

    def listener():
        # What entities and sources ask on every notification.
        for key in runtime.snapshot.bindings:
            checks.append(runtime.source_phase(key))
            context = runtime._contexts.get(key)
            if context is not None:
                checks.append((context.current, context.configuration_current))

    runtime.subscribe(listener)
    runtime.start()
    await settle(hass)
    try:
        local = harness.source(
            runtime.snapshot.bindings["local"].connection_id, "local"
        )
        parses = counted_parses(monkeypatch)
        checks.clear()
        began = perf_counter()
        for index in range(20):
            assert local.context.publish({"sample": index})
        elapsed = perf_counter() - began
        assert len(checks) >= 20 * len(runtime.snapshot.bindings)
        assert not parses  # nothing parsed again while the entry is unchanged
        assert elapsed < 1.0
    finally:
        await runtime.close()


@bounded
async def test_an_options_change_is_seen_at_once(hass, monkeypatch):
    entry, _, _, _, runtime = build(hass)
    runtime.start()
    await settle(hass)
    try:
        snapshot = runtime.management.current()
        assert runtime.management.current() is snapshot
        # Also without the update notice: the changed entry is parsed again.
        unnotified = CachedConfiguration(hass, entry)
        assert unnotified.current() == snapshot
        parses = counted_parses(monkeypatch)
        options = deepcopy(dict(entry.options))
        options["bindings"]["local"]["enabled"] = False
        hass.config_entries.async_update_entry(entry, options=options)
        assert not unnotified.current().to_options()["bindings"]["local"]["enabled"]
        changed = runtime.management.current()
        assert changed is not snapshot and len(parses) == 2
        assert not changed.to_options()["bindings"]["local"]["enabled"]
        assert runtime.management.current() is changed
        await settle(hass)
        assert runtime.snapshot == changed
        assert runtime.source_data("local") is None
    finally:
        await runtime.close()


@pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
@bounded
async def test_a_paired_local_reading_notifies_once(hass) -> None:
    hass.config.components.add(DOMAIN)
    backend = ReadingBackend()
    async with running(hass, backend, configure=local_values()) as (_, _, owner, _):
        await started(hass, owner)
        runtime = owner.connections
        # The cloud source has finished its own start and first publication.
        await eventually(
            lambda: getattr(source_for(owner, GATEWAY).source, "_published", False)
        )
        await settle(hass)
        notified = []
        remove = runtime.subscribe(lambda: notified.append(1))
        try:
            publish_local(owner, 24.5)
            await settle(hass)
            assert len(notified) == 1
            assert state(hass, owner) == "24.5"
        finally:
            remove()


class _View:
    """A target view whose start blocks until the runtime unloads."""

    def __init__(self):
        self.local_retained = False
        self.faults = SimpleNamespace(discard=Mock())
        self.calls = []
        self.entered = asyncio.Event()

    async def async_stop_source(self, *, retain=False):
        self.calls.append("stop")
        if retain:
            self.local_retained = True
            self.entered.set()
            await asyncio.Event().wait()

    async def async_release_retention(self):
        self.calls.append("release")
        self.local_retained = False


@bounded
async def test_unload_cancels_a_view_build_and_ends_its_view(hass, monkeypatch):
    _, _, _, _, runtime = build(hass)
    runtime.start()
    await settle(hass)
    view = _View()

    async def build_view(target_id, gateway_id):
        return view

    monkeypatch.setattr(runtime, "_target_current", lambda target, generation: True)
    task = runtime._build(runtime._build_target_view(build_view, "target", "gw", 1))
    await view.entered.wait()
    await runtime.close()
    assert task.cancelled() and not runtime._builds
    assert view.calls == ["stop", "release"]
    view.faults.discard.assert_called_once()
    assert not runtime._target_views


async def test_ending_a_view_cancels_its_fault_baseline_load() -> None:
    loading = asyncio.create_task(asyncio.Event().wait())
    view = SimpleNamespace(
        _hold_task=loading, _inventory_expiry=None, local_retained=False
    )
    await ManagedPointTCoordinator.async_release_retention(view)
    with pytest.raises(asyncio.CancelledError):
        await loading
    assert view._hold_task is None
    # A coordinator that never loaded a baseline for a view ends as before.
    plain = SimpleNamespace(_inventory_expiry=None, local_retained=False)
    await ManagedPointTCoordinator.async_release_retention(plain)


@pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
@bounded
async def test_the_same_local_answer_read_again_updates_no_entity(hass) -> None:
    """G1: a new read with an unchanged answer starts no full update pass."""
    hass.config.components.add(DOMAIN)
    backend = ReadingBackend()
    async with running(hass, backend, configure=local_values()) as (_, _, owner, _):
        await started(hass, owner)
        coordinator = source_for(owner, GATEWAY)
        await eventually(lambda: getattr(coordinator.source, "_published", False))
        await settle(hass)
        passes = []
        original = coordinator.async_update_listeners

        def counted():
            passes.append(1)
            original()

        coordinator.async_update_listeners = counted
        try:
            for _ in range(5):
                publish_local(owner, 23.5)
                await settle(hass)
            assert passes == []
            publish_local(owner, 24.5)
            await settle(hass)
            assert len(passes) == 1 and state(hass, owner) == "24.5"
        finally:
            del coordinator.async_update_listeners
