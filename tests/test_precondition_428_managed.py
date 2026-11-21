"""A managed cloud source whose gateway data the cloud refuses with 428 (#146).

Real HTTP backend, real coordinator and the real source retry of the runtime:
the next start waits for the growing pause instead of a minute.
"""

import asyncio

import pytest
from aiohttp import web
from homeassistant.helpers import issue_registry as ir

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from tests.test_managed_pointt import Backend, running

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
CORE = frozenset({"/system", "/gateway", "/heatingCircuits", "/notifications"})


class RefusingBackend(Backend):
    """The cloud refuses the core paths with 428, the rest is a gateway 404."""

    def __init__(self):
        super().__init__()
        self.gateways = ["000123"]
        self.refusing = True

    async def bulk(self, request):
        if not self.refusing:
            return await super().bulk(request)
        body = (await request.json())[0]
        gateway, paths = body["gatewayId"], body["resourcePaths"]
        self.calls.append(("bulk", gateway, tuple(paths)))
        items = [
            {"resourcePath": path, "serverStatus": 428, "errorId": "e-1"}
            if path in CORE
            else {
                "resourcePath": path,
                "serverStatus": 200,
                "gatewayResponse": {"status": 404},
            }
            for path in paths
        ]
        return web.json_response([{"gatewayId": gateway, "resourcePaths": items}])

    @property
    def routes(self):
        routes = super().routes
        routes[1] = ("POST", "/bulk", self.bulk)
        return routes


async def _next_pause(harness):
    async with asyncio.timeout(30):
        while True:
            delay = await harness.sleeps.get()
            if delay > 100:  # not the short waits of other parts of the runtime
                return delay


async def test_the_next_start_waits_for_the_pause_not_a_minute(hass):
    backend = RefusingBackend()
    async with running(hass, backend) as (entry, _, _owner, harness):
        first = await _next_pause(harness)
        bulks = sum(call[0] == "bulk" for call in backend.calls)
        assert bulks == 1
        assert 290 <= first <= 300  # the first pause of five minutes
        issues = [
            issue
            for (domain, _), issue in ir.async_get(hass).issues.items()
            if domain == DOMAIN and issue.translation_key == "gateway_precondition"
        ]
        assert len(issues) == 1 and issues[0].is_fixable is False

        # The source retries inside the pause: no request, same pause again.
        harness.retry.set()
        await asyncio.sleep(0.1)
        harness.retry.clear()
        await asyncio.sleep(0.2)
        seen = []
        while not harness.sleeps.empty():
            seen.append(harness.sleeps.get_nowait())

        assert sum(call[0] == "bulk" for call in backend.calls) == bulks
        # Another part of the runtime retries with its own growing waits; the
        # refused gateway waits for its pause every time.
        assert any(280 <= delay <= 300 for delay in seen)

        report = await async_get_config_entry_diagnostics(hass, entry)
        access = report["cloud_data_access"]
        assert len(access) == 1
        assert access[0]["precondition"]["refusals_in_a_row"] == 1
        assert access[0]["bulk_item_errors"]["status_counts"]["428"] == len(
            [
                path
                for path in next(call for call in backend.calls if call[0] == "bulk")[2]
                if path in CORE
            ]
        )
        assert access[0]["bulk_item_errors"]["recent"][0]["details"] == {
            "errorId": "e-1"
        }
