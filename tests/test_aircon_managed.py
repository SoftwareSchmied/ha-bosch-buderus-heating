"""An air conditioner on a PointT gateway within a managed cloud source (HTTP)."""

import json

import pytest
from aiohttp import web
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.aircon import DETECTION_PATHS
from custom_components.bosch_buderus_heating.aircon_policies import (
    FAN_SPEED,
    POLL_PATHS,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.managed_aircon import (
    ManagedAirconCoordinator,
)
from custom_components.bosch_buderus_heating.managed_pointt import (
    ManagedPointTCoordinator,
)
from tests.aircon_helpers import Sleeps, fixture
from tests.test_managed_pointt import Backend, eventually, running, source_for

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
AC = "gateway-b"
HEAT = "000123"


@pytest.fixture(autouse=True)
def loaded_integration(hass):
    # The transport fixture starts after the integration's normal setup phase.
    hass.config.components.add(DOMAIN)


class AirconBackend(Backend):
    def __init__(self):
        super().__init__()
        self.device_types = {AC: "rac"}
        data = fixture()
        self.unit = dict(data["resources"])
        for path, value in data["detection"]["midea"].items():
            self.unit[path] = {"id": path, "type": "stringValue", "value": value}
        self.puts = []

    def resource(self, gateway, path):
        if gateway == AC:
            return self.unit.get(path)
        return super().resource(gateway, path)

    async def scalar(self, request):
        gateway, path = request.match_info["gateway"], "/" + request.match_info["path"]
        if gateway != AC:
            return await super().scalar(request)
        self.calls.append((request.method, gateway, path))
        if path not in self.unit:
            return web.Response(status=404)
        if request.method == "PUT":
            body = await request.json()
            self.puts.append((path, body))
            self.unit[path] = {**self.unit[path], "value": body["value"]}
            return web.Response(status=204)
        return web.json_response(self.unit[path])


async def test_air_conditioner_gateway_runs_its_own_coordinator(hass):
    backend = AirconBackend()
    async with running(hass, backend) as (entry, _, owner, _):
        await eventually(
            lambda: source_for(owner, AC) and source_for(owner, HEAT), timeout=10
        )
        coordinator = source_for(owner, AC)
        assert isinstance(coordinator, ManagedAirconCoordinator)
        assert isinstance(source_for(owner, HEAT), ManagedPointTCoordinator)
        await eventually(coordinator.current)
        assert coordinator.system == "midea"
        # No heating discovery: only the detection and the fixed read list.
        requested = [call[2] for call in backend.calls if call[:2] == ("bulk", AC)]
        assert set(requested) <= {DETECTION_PATHS, POLL_PATHS}
        assert not any(call[0] == "GET" and call[1] == AC for call in backend.calls)
        assert coordinator.client._token_provider is (
            source_for(owner, HEAT).client._token_provider
        )
        assert coordinator.value(FAN_SPEED) == "auto"

        await owner.start_platforms()
        registry = er.async_get(hass)

        def entity_id(platform, key):
            return registry.async_get_entity_id(platform, DOMAIN, f"{AC}:{key}")

        await eventually(
            lambda: entity_id("climate", "airConditioning:climate"), timeout=10
        )
        await eventually(
            lambda: entity_id("switch", "airConditioning:sleepMode"), timeout=10
        )
        climate = registry.async_get(entity_id("climate", "airConditioning:climate"))
        assert climate.disabled_by is None
        for platform, key in (
            ("switch", "airConditioning:sleepMode"),
            ("select", "airConditioning:quickAirFlows"),
            ("number", "airConditioning:timers:on"),
            ("button", "airConditioning:switchPrograms:temporarySetting:enabled"),
            ("binary_sensor", "pv:enable"),
        ):
            await eventually(lambda p=platform, k=key: entity_id(p, k))
            entry_ = registry.async_get(entity_id(platform, key))
            assert entry_.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        await eventually(
            lambda: hass.states.get(climate.entity_id) is not None, timeout=10
        )
        state = hass.states.get(climate.entity_id)
        assert state.state == "cool"
        assert state.attributes["current_temperature"] == 21.5

        coordinator._write_service._sleep = Sleeps()
        await hass.services.async_call(
            "climate",
            "set_fan_mode",
            {"entity_id": climate.entity_id, "fan_mode": "medium"},
            blocking=True,
        )
        assert backend.puts == [(FAN_SPEED, {"value": "mid"})]
        assert ("GET", AC, FAN_SPEED) in backend.calls

        report = await async_get_config_entry_diagnostics(hass, entry)
        [aircon] = report["pointt_air_conditioners"]
        assert aircon["system_type"] == "midea"
        assert aircon["writes_sent"] == 1
        assert AC not in json.dumps(aircon)
