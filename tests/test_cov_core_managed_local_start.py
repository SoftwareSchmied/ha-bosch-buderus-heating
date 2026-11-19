# ruff: noqa: F811
"""Local source start: stored-pause failures, a changed identity and a
source replaced while it starts, with the real scheduler and synthetic
transports of the managed local tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.bosch_buderus_heating import managed_local as module
from custom_components.bosch_buderus_heating.configuration_management import (
    ConfigurationManagement,
)
from custom_components.bosch_buderus_heating.connection_runtime import (
    ConnectionPhase,
)
from custom_components.bosch_buderus_heating.local_budget import LocalBudgetError
from custom_components.bosch_buderus_heating.local_polling.service import (
    LocalPollingService,
)
from custom_components.bosch_buderus_heating.local_state import LocalCondition
from tests.test_connection_runtime import settle
from tests.test_managed_local import (  # noqa: F401
    prepared,
    ready,
    wait_until,
)


async def test_unreadable_stored_pauses_are_retried_after_a_pause(
    hass, prepared, monkeypatch
) -> None:
    p = prepared
    real = module.local_budget_manager(hass)
    calls = []

    async def budget(context, **kwargs):
        calls.append(context)
        if len(calls) == 1:
            raise LocalBudgetError("local_budget_storage_failed")
        return await real.budget(context, **kwargs)

    monkeypatch.setattr(
        module, "local_budget_manager", lambda hass: SimpleNamespace(budget=budget)
    )
    monkeypatch.setattr(module, "START_RETRY", (0.01,))
    state = await ready(hass, p)
    assert len(calls) == 2
    # The failed start left no client behind and read nothing; the second try
    # went through the full identity check.
    assert state.identity_confirmed
    assert len(p.probes) == 1 and len(p.clients) == 1
    assert p.runtime.connection_phase(p.cloud) is ConnectionPhase.READY


async def test_a_failed_service_start_closes_what_was_half_started(
    hass, prepared, monkeypatch
) -> None:
    p = prepared
    monkeypatch.setattr(module, "START_RETRY", (0.01,))
    started = []
    real_start = LocalPollingService.start

    async def start(self):
        started.append(self)
        if len(started) == 1:
            raise LocalBudgetError("local_budget_storage_failed")
        await real_start(self)

    closed = []
    real_close = LocalPollingService.close

    async def close(self):
        closed.append(self)
        await real_close(self)

    monkeypatch.setattr(LocalPollingService, "start", start)
    monkeypatch.setattr(LocalPollingService, "close", close)
    await ready(hass, p)
    # The first service was closed before the second one started.
    assert len(started) == 2
    assert closed[0] is started[0]
    assert started[0] is not started[1]
    assert p.clients[0].closed


async def test_an_access_of_another_gateway_than_its_binding_needs_a_new_signin(
    hass, prepared, monkeypatch
) -> None:
    p = prepared
    real = module.load_local_access

    def load(data):
        return replace(real(data), gateway_id="999999")

    monkeypatch.setattr(module, "load_local_access", load)
    p.runtime.start()
    await wait_until(
        hass,
        lambda: (
            p.runtime.local_state("local") is not None
            and p.runtime.local_state("local").condition is LocalCondition.AUTH_REQUIRED
        ),
    )
    state = p.runtime.local_state("local")
    assert state.problem == "local_revalidation_required"
    # Nothing is sent to a gateway that is not the verified one.
    assert not p.probes and not p.clients


async def test_a_source_replaced_while_it_checks_the_gateway_never_reads(
    hass, prepared
) -> None:
    p = prepared
    p.control.gate = asyncio.Event()
    p.runtime.start()
    await wait_until(hass, lambda: len(p.probes) == 1)
    management = ConfigurationManagement(hass, p.entry)
    management.set_enabled(management.edit("local"), False)
    await settle(hass)
    p.control.gate.set()
    await settle(hass)
    await asyncio.sleep(0.05)
    await settle(hass)
    assert not p.clients
    assert p.runtime.local_state("local") is None
    assert p.runtime.connection_phase(p.cloud) is ConnectionPhase.READY


async def test_a_pause_of_the_access_is_waited_out_before_the_check(
    hass, prepared
) -> None:
    p = prepared
    waits = []
    real_sleep = asyncio.sleep

    async def sleep(seconds, *args):
        # Only the pause of the start check is skipped over.
        if seconds == 120:
            waits.append(seconds)
            p.clock.advance(seconds)
            seconds = 0
        await real_sleep(seconds, *args)

    real = module.local_budget_manager(hass)

    async def budget(context, **kwargs):
        value = await real.budget(context, **kwargs)
        await value.pause(120, "rate_limited")
        return value

    with (
        patch.object(
            module, "local_budget_manager", lambda h: SimpleNamespace(budget=budget)
        ),
        patch.object(module.asyncio, "sleep", sleep),
    ):
        state = await ready(hass, p)
    assert waits == [120]
    # The pause is shown while it lasts and the check follows after it.
    assert state.identity_confirmed
    assert len(p.probes) == 1
