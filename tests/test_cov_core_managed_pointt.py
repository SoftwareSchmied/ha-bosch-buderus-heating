"""Managed PointT sources against a loopback backend: a gateway the account
no longer lists, server errors on the resource reads and a source stopped
while its first read is running."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.bosch_buderus_heating.connection_runtime import ConnectionPhase
from tests.test_managed_pointt import (
    TEMP,
    Backend,
    binding_for,
    eventually,
    running,
    source_for,
)

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")


async def test_a_gateway_the_account_no_longer_lists_never_reads_and_never_blocks(
    hass,
) -> None:
    backend = Backend()
    backend.gateways = ["gateway-b"]
    async with running(hass, backend) as (_, _, owner, _):
        await eventually(lambda: source_for(owner, "gateway-b"))
        # The other gateway and the local access run; the unlisted one never
        # gets a resource request.
        assert owner.connections.source_data("local")
        assert source_for(owner, "000123") is None
        assert not any(
            call[0] == "bulk" and call[1] == "000123" for call in backend.calls
        )
        assert owner.connections.source_phase(binding_for(owner, "000123")) is not (
            ConnectionPhase.READY
        )
        # One shared gateway list served both sources.
        assert sum(call == ("gateways",) for call in backend.calls) == 1


async def test_server_errors_on_one_gateway_leave_the_other_gateway_reading(
    hass,
) -> None:
    backend = Backend()
    async with running(hass, backend) as (_, _, owner, _):
        await eventually(
            lambda: source_for(owner, "000123") and source_for(owner, "gateway-b")
        )
        a, b = source_for(owner, "000123"), source_for(owner, "gateway-b")
        backend.status["000123"] = 500
        a._next_update = dict.fromkeys(a._poll_intervals, 0)
        await a.async_refresh()
        # The failed gateway keeps its last values but is not a success.
        assert a.last_update_success is False
        assert a.data[TEMP].resource.value == 20
        backend.values["gateway-b"] = 22.5
        b._next_update = dict.fromkeys(b._poll_intervals, 0)
        await b.async_refresh()
        assert b.last_update_success is True
        assert b.data[TEMP].resource.value == 22.5
        # The error ends: the gateway recovers without a restart.
        del backend.status["000123"]
        a._next_update = dict.fromkeys(a._poll_intervals, 0)
        await a.async_refresh()
        assert a.last_update_success is True


async def test_a_source_stopped_while_the_gateway_list_is_pending_does_not_start(
    hass,
) -> None:
    backend = Backend()
    backend.gates["gateways"] = asyncio.Event()
    async with running(hass, backend) as (_, _, owner, _):
        await eventually(lambda: backend.entered.get("gateways") is not None)
        assert source_for(owner, "000123") is None
        # The runtime closes while the list is pending (running() releases
        # the gate first, then closes): nothing may start afterwards.
    assert not any(call[0] == "bulk" for call in backend.calls)
