"""Real HA setup and cloud HTTP with synthetic certificate and local reads."""

import json
from dataclasses import replace

import pytest

from custom_components.bosch_buderus_heating import local_setup, managed_local
from custom_components.bosch_buderus_heating.local_api.catalog import request_for_path
from custom_components.bosch_buderus_heating.local_probe import ProbePath
from custom_components.bosch_buderus_heating.local_read import (
    ReadMetrics,
    ReadResult,
    ReadState,
)
from custom_components.bosch_buderus_heating.local_transport import LocalProbeExchange
from custom_components.bosch_buderus_heating.pointt import PointTClient
from tests.test_local_flow import supported as supported
from tests.test_local_identity import IdentityBackend, cloud_system
from tests.test_local_polling import success
from tests.test_local_probe import response
from tests.test_local_setup import registered
from tests.test_managed_pointt import eventually, source_for
from tests.test_pointt_transport_and_client import serve


@pytest.fixture
async def running_local_owner(
    hass, monkeypatch, supported, enable_custom_integrations, journal, tmp_path
):
    entry, management, cloud, target = registered(hass)
    data = management.current().to_data()
    data["connections"][cloud]["credentials"]["expires_at"] = 10**12
    # This test covers the initially retained gateway only.
    for binding in data["bindings"].values():
        assert binding["adapter"] == "pointt"
    hass.config_entries.async_update_entry(entry, data=data)
    backend = IdentityBackend()
    backend.gateways = ["000123"]
    clients, runtime_probes = [], []

    async def probe(context, token, binding, *, is_current, budget):
        assert is_current()
        runtime_probes.append(context)
        system = {
            **cloud_system(),
            "id": ProbePath.SYSTEM.value,
            "timeStamp": "",
            "writeable": 0,
        }
        system["values"][0]["Ver"] = "1.0"
        return LocalProbeExchange(
            tuple(
                replace(
                    response(path, context=context, gatewayId="000123"),
                    body=json.dumps(system).encode(),
                )
                if path is ProbePath.SYSTEM
                else response(path, context=context)
                for path in ProbePath
            )
        )

    monkeypatch.setattr(local_setup, "read_local_probe", probe)
    monkeypatch.setattr(managed_local, "read_local_probe", probe)
    original = managed_local.LocalPollingService

    def service(*args, **kwargs):
        result = original(*args, **kwargs)
        result.store._path = tmp_path / result.store._path.name
        result.planner.hints(
            (request_for_path("/heatingCircuits/hc2/currentRoomSetpoint"),),
            result._clock(),
        )
        result.planner.epoch = result._clock()
        return result

    monkeypatch.setattr(managed_local, "LocalPollingService", service)

    class Client:
        metrics = ReadMetrics()

        def __init__(self, context, token, pin, **kwargs):
            self.context, self.calls, self.closed = context, [], False
            clients.append(self)

        async def read(self, request):
            from time import monotonic

            self.calls.append(request)
            if request.path != "/heatingCircuits/hc2/currentRoomSetpoint":
                return ReadResult(
                    request, ReadState.PATH_ERROR, 404, "not_found", http_requests=1
                )
            return success(request, monotonic(), context=self.context.identity)

        async def close(self):
            self.closed = True

    monkeypatch.setattr(managed_local, "LocalReadClient", Client)
    async with serve(backend.routes) as url:
        for module in ("managed_cloud", "managed_pointt"):
            monkeypatch.setattr(
                "custom_components.bosch_buderus_heating." + module + ".PointTClient",
                lambda *args, **kwargs: PointTClient(*args, **kwargs, base_url=url),
            )
        assert await hass.config_entries.async_setup(entry.entry_id)
        await eventually(lambda: source_for(entry.runtime_data, "000123"))
        try:
            yield entry, management, target, backend, clients, runtime_probes
        finally:
            await hass.config_entries.async_unload(entry.entry_id)
