"""Exercise the real PointT coordinator and HTTP client in managed HA access."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.configuration_access import (
    EntryConfiguration,
)
from custom_components.bosch_buderus_heating.connection_runtime import ConnectionPhase
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    POLL_INTERVALS,
    POLL_INTERVALS_CLOUD_FRIENDLY,
)
from custom_components.bosch_buderus_heating.managed_cloud import CloudDriverFactory
from custom_components.bosch_buderus_heating.managed_entry import ManagedEntry
from custom_components.bosch_buderus_heating.managed_pointt import (
    ManagedPointTCoordinator,
)
from custom_components.bosch_buderus_heating.managed_wallbox import (
    ManagedWallboxCoordinator,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthTokens,
    PointTClient,
    PointTError,
    RefreshTokenRejected,
    Resource,
)
from custom_components.bosch_buderus_heating.pointt.exceptions import SourceStopped
from custom_components.bosch_buderus_heating.writes import number_policy_for_resource
from tests.test_connection_runtime import Harness, entry_for, settle
from tests.test_pointt_transport_and_client import serve

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
MODULE = "custom_components.bosch_buderus_heating.managed_pointt"
TEMP = "/heatingCircuits/hc2/manualRoomSetpoint"


class Backend:
    def __init__(self):
        self.calls = []
        self.gateways = ["000123", "gateway-b"]
        self.values = {key: 20.0 for key in self.gateways}
        self.status = {}
        self.gates = {}
        self.entered = {}
        self.wallbox = None

    def resource(self, gateway, path):
        if path == "/heatingCircuits":
            return {"references": [{"id": "/heatingCircuits/hc2"}]}
        if path == "/heatingCircuits/hc2":
            return {"references": [{"id": TEMP}]}
        if path == TEMP:
            return {
                "id": TEMP,
                "type": "floatValue",
                "value": self.values[gateway],
                "minValue": 5,
                "maxValue": 30,
                "stepSize": 0.5,
                "unitOfMeasure": "C",
                "writeable": True,
            }
        return None

    async def wait(self, key):
        self.entered.setdefault(key, asyncio.Event()).set()
        if key in self.gates:
            await self.gates[key].wait()

    async def list_gateways(self, request):
        self.calls.append(("gateways",))
        await self.wait("gateways")
        if status := self.status.get("gateways"):
            return web.Response(status=status, headers={"Retry-After": "900"})
        return web.json_response(
            [
                {
                    "id": key,
                    "deviceType": "Commodule2" if key == self.wallbox else "MX400",
                }
                for key in self.gateways
            ]
        )

    async def bulk(self, request):
        body = (await request.json())[0]
        gateway, paths = body["gatewayId"], body["resourcePaths"]
        self.calls.append(("bulk", gateway, tuple(paths)))
        await self.wait(gateway)
        if status := self.status.get(gateway):
            return web.Response(status=status, headers={"Retry-After": "900"})
        items = []
        for path in paths:
            resource = self.resource(gateway, path)
            items.append(
                {
                    "resourcePath": path,
                    "serverStatus": 200,
                    "gatewayResponse": {
                        "status": 200 if resource else 404,
                        "payload": resource,
                    },
                }
            )
        return web.json_response([{"gatewayId": gateway, "resourcePaths": items}])

    async def scalar(self, request):
        gateway, path = request.match_info["gateway"], "/" + request.match_info["path"]
        self.calls.append((request.method, gateway, path))
        if status := self.status.get(gateway):
            return web.Response(status=status)
        resource = self.resource(gateway, path)
        if resource is None:
            return web.Response(status=404)
        if request.method == "PUT":
            self.values[gateway] = (await request.json())["value"]
            await self.wait("PUT")
            return web.Response(status=204)
        return web.json_response(resource)

    @property
    def routes(self):
        return [
            ("GET", "/gateways/", self.list_gateways),
            ("POST", "/bulk", self.bulk),
            ("GET", "/gateways/{gateway}/resource/{path:.*}", self.scalar),
            ("PUT", "/gateways/{gateway}/resource/{path:.*}", self.scalar),
        ]


async def eventually(predicate, *, timeout=30):  # noqa: ASYNC109
    # Background HA jobs and loopback HTTP handlers have separate schedulers.
    async with asyncio.timeout(timeout):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.005)


@asynccontextmanager
async def running(
    hass, backend, *, friendly=False, second_cloud=False, defaults=False, configure=None
):
    entry, cloud, _ = entry_for(hass, second_cloud=second_cloud)
    data, options = deepcopy(dict(entry.data)), deepcopy(dict(entry.options))
    for key, connection in data["connections"].items():
        if connection["kind"] == "singlekey_cloud":
            connection["credentials"]["expires_at"] = 10**12
            options["connections"][key]["polling_profile"] = (
                "cloud_friendly" if friendly else "standard"
            )
    for key, binding in data["bindings"].items():
        if binding["adapter"] == "bacon":
            options["bindings"][key]["enabled"] = False
    if configure is not None:
        configure(data, options)
    hass.config_entries.async_update_entry(entry, data=data, options=options)
    harness = Harness()
    factory = CloudDriverFactory(hass, entry, lambda: owner.connections)
    owner = ManagedEntry(
        hass,
        entry,
        None
        if defaults
        else {"singlekey_cloud": factory, "k40_local": harness.factory},
    )
    owner.connections._sleep = harness.sleep
    failures = []
    classify = owner.connections._failure

    def failed(error):
        failures.append((type(error).__name__, str(error)))
        return classify(error)

    owner.connections._failure = failed
    entry.runtime_data = owner
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    async with serve(backend.routes) as url:
        with (
            patch(
                "custom_components.bosch_buderus_heating.managed_cloud.PointTClient",
                side_effect=lambda *args, **kwargs: PointTClient(
                    *args, **kwargs, base_url=url
                ),
            ),
            patch(
                MODULE + ".PointTClient",
                side_effect=lambda *args, **kwargs: PointTClient(
                    *args, **kwargs, base_url=url
                ),
            ),
        ):
            await owner.start()
            try:
                yield entry, cloud, owner, harness
            except TimeoutError as error:
                raise AssertionError(
                    f"Managed sources did not settle: {failures}"
                ) from error
            finally:
                for event in backend.gates.values():
                    event.set()
                await owner.close()


def binding_for(owner, gateway):
    return next(
        key
        for key, item in owner.connections.snapshot.bindings.items()
        if item.adapter == "pointt" and item.to_data()["locator"]["value"] == gateway
    )


def source_for(owner, gateway):
    return owner.connections.source_data(binding_for(owner, gateway))


@pytest.mark.parametrize("friendly", [False, True])
async def test_real_discovery_polling_and_confirmed_write_share_account_resources(
    hass, friendly
):
    backend = Backend()
    async with running(hass, backend, friendly=friendly) as (entry, cloud, owner, _):
        await eventually(
            lambda: source_for(owner, "000123") and source_for(owner, "gateway-b")
        )
        a, b = source_for(owner, "000123"), source_for(owner, "gateway-b")
        assert isinstance(a, ManagedPointTCoordinator)
        assert a.data[TEMP].resource.value == b.data[TEMP].resource.value == 20
        assert a.client._token_provider is b.client._token_provider
        assert a.client.backoff is b.client.backoff
        assert a.client.metrics is b.client.metrics
        assert a.client._transport._semaphore is b.client._transport._semaphore
        assert sum(call == ("gateways",) for call in backend.calls) == 1
        assert a._poll_intervals == (
            POLL_INTERVALS_CLOUD_FRIENDLY if friendly else POLL_INTERVALS
        )
        policy = number_policy_for_resource(a.data[TEMP].resource)
        assert policy is not None
        assert (await a.async_write_control(TEMP, 21.5, policy)).value == 21.5
        assert backend.values == {"000123": 21.5, "gateway-b": 20.0}
        assert a.data[TEMP].resource.value == 21.5
        assert backend.calls[-1] == ("GET", "000123", TEMP)
        backend.values["gateway-b"] = 19
        b._next_update = dict.fromkeys(b._poll_intervals, 0)
        await b.async_refresh()
        assert b.data[TEMP].resource.value == 19
        assert a.data[TEMP].resource.value == 21.5
        assert a.connection_id == cloud
        assert entry.version == 4


async def test_slow_pointt_gateway_does_not_block_another_gateway_or_local_source(hass):
    backend = Backend()
    backend.gates["000123"] = asyncio.Event()
    async with running(hass, backend) as (_, _, owner, _):
        await eventually(lambda: source_for(owner, "gateway-b"))
        assert source_for(owner, "000123") is None
        assert owner.connections.source_data("local")
        backend.gates["000123"].set()
        await eventually(lambda: source_for(owner, "000123"))


async def test_disabling_one_gateway_stops_its_reads_and_writes_only(hass):
    backend = Backend()
    async with running(hass, backend) as (_, cloud, owner, _):
        await eventually(
            lambda: source_for(owner, "000123") and source_for(owner, "gateway-b")
        )
        a, b = source_for(owner, "000123"), source_for(owner, "gateway-b")
        management = owner.connections.management
        management.set_binding_enabled(
            management.edit(cloud), binding_for(owner, "000123"), False
        )
        with pytest.raises(SourceStopped):
            await a.client.get_resource("000123", TEMP)
        await settle(hass)
        calls = len(backend.calls)
        policy = number_policy_for_resource(a.data[TEMP].resource)
        with pytest.raises(PointTError):
            await a.async_write_control(TEMP, 22, policy)
        assert len(backend.calls) == calls
        assert not a.last_update_success and not a.data[TEMP].available
        assert not a._manual_setpoint_tasks
        assert source_for(owner, "gateway-b") is b
        assert owner.connections.source_data("local")
        management.set_binding_enabled(
            management.edit(cloud), binding_for(owner, "000123"), True
        )
        await eventually(lambda: source_for(owner, "000123"))
        fresh = source_for(owner, "000123")
        assert fresh is not a and fresh.gateway.gateway_id == a.gateway.gateway_id
        with pytest.raises(SourceStopped):
            await a.client.get_resource("000123", TEMP)
        assert fresh.client._token_provider is b.client._token_provider


async def test_in_flight_put_is_not_retried_or_confirmed_after_disabling_access(hass):
    backend = Backend()
    backend.gates["PUT"] = asyncio.Event()
    async with running(hass, backend) as (_, cloud, owner, _):
        await eventually(lambda: source_for(owner, "000123"))
        coordinator = source_for(owner, "000123")
        policy = number_policy_for_resource(coordinator.data[TEMP].resource)
        task = asyncio.create_task(coordinator.async_write_control(TEMP, 23, policy))
        await eventually(
            lambda: backend.entered.get("PUT") and backend.entered["PUT"].is_set()
        )
        management = owner.connections.management
        management.set_binding_enabled(
            management.edit(cloud), binding_for(owner, "000123"), False
        )
        backend.gates["PUT"].set()
        with pytest.raises(PointTError):
            await task
        await settle(hass)
        assert backend.values["000123"] == 23
        assert [call for call in backend.calls if call[0] == "PUT"] == [
            ("PUT", "000123", TEMP)
        ]
        last_put = backend.calls.index(("PUT", "000123", TEMP))
        assert not any(
            call[:2] == ("GET", "000123") for call in backend.calls[last_put + 1 :]
        )
        assert not coordinator.data[TEMP].available
        assert coordinator.data[TEMP].resource.value == 20


@pytest.mark.parametrize("when", ["discovery", "polling"])
async def test_pointt_auth_error_requests_only_its_own_cloud_login(hass, when):
    backend = Backend()
    if when == "discovery":
        backend.status["000123"] = 401
    with patch(
        "custom_components.bosch_buderus_heating.pointt.auth.OAuthClient.refresh",
        AsyncMock(side_effect=RefreshTokenRejected()),
    ):
        async with running(hass, backend, second_cloud=True) as (
            entry,
            cloud,
            owner,
            _,
        ):
            if when == "polling":
                await eventually(lambda: source_for(owner, "000123"))
                coordinator = source_for(owner, "000123")
                backend.status["000123"] = 401
                coordinator._next_update = dict.fromkeys(coordinator._poll_intervals, 0)
                await coordinator.async_refresh()
            await eventually(
                lambda: (
                    owner.connections.connection_phase(cloud)
                    is ConnectionPhase.AUTH_REQUIRED
                )
            )
            await eventually(lambda: source_for(owner, "gateway-b"))
            await settle(hass)
            assert owner.connections.source_data("local")
            flows = list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
            assert len(flows) == 1
            assert flows[0]["context"]["connection_id"] == cloud


async def test_wallbox_uses_its_own_coordinator_and_disabled_adapters_stay_idle(
    hass,
):
    backend = Backend()
    backend.wallbox = "gateway-b"
    async with running(hass, backend) as (_, _, owner, _):
        await eventually(
            lambda: source_for(owner, "000123") and source_for(owner, "gateway-b")
        )
        await settle(hass)
        assert isinstance(source_for(owner, "gateway-b"), ManagedWallboxCoordinator)
        additional = next(
            key
            for key, binding in owner.connections.snapshot.bindings.items()
            if binding.adapter == "bacon"
        )
        assert owner.connections.source_phase(additional) is ConnectionPhase.DISABLED
        assert not any(
            call[0] == "bulk" and call[1] == "gateway-b" for call in backend.calls
        )
        assert source_for(owner, "000123").startup_session.setup_complete


async def test_known_registry_recovery_paths_are_preserved_without_cleanup(hass):
    backend = Backend()
    backend.gates["gateways"] = asyncio.Event()
    async with running(hass, backend) as (entry, _, owner, _):
        registered = er.async_get(hass).async_get_or_create(
            "sensor",
            DOMAIN,
            "000123:heatSources:hs1:numberOfStarts",
            config_entry=entry,
        )
        backend.gates["gateways"].set()
        await eventually(lambda: source_for(owner, "000123"))
        assert (
            "/heatSources/hs1/numberOfStarts"
            in source_for(owner, "000123").registered_start_counter_paths
        )
        assert er.async_get(hass).async_get(registered.entity_id) == registered


async def test_gateway_list_failure_is_shared_and_local_access_still_starts(hass):
    backend = Backend()
    backend.status["gateways"] = 503
    async with running(hass, backend) as (_, _, owner, harness):
        await eventually(lambda: harness.sleeps.qsize() == 2)
        assert (
            sum(call == ("gateways",) for call in backend.calls) == 2
        )  # One logical read, two bounded HTTP attempts.
        assert owner.connections.source_data("local")
        assert not any(call[0] == "bulk" for call in backend.calls)


async def test_pointt_gateway_rate_limit_stops_other_account_requests(hass):
    backend = Backend()
    async with running(hass, backend) as (_, _, owner, _):
        await eventually(
            lambda: source_for(owner, "000123") and source_for(owner, "gateway-b")
        )
        a, b = source_for(owner, "000123"), source_for(owner, "gateway-b")
        backend.status["000123"] = 429
        a._next_update = dict.fromkeys(a._poll_intervals, 0)
        await a.async_refresh()
        count = len(backend.calls)
        b._next_update = dict.fromkeys(b._poll_intervals, 0)
        await b.async_refresh()
        assert len(backend.calls) == count
        assert owner.connections.source_data("local")


@pytest.mark.parametrize("change", ["disable", "remove"])
async def test_late_rotation_is_saved_only_while_the_same_access_exists(hass, change):
    backend = Backend()
    async with running(hass, backend) as (entry, cloud, owner, _):
        await eventually(
            lambda: source_for(owner, "000123") and source_for(owner, "gateway-b")
        )
        coordinator = source_for(owner, "000123")
        entered, release = asyncio.Event(), asyncio.Event()
        renewed = AuthTokens("rotated-access", "rotated-refresh", 10**12)

        async def refresh(token):
            entered.set()
            await release.wait()
            return renewed

        backend.status["000123"] = 401
        with patch.object(
            coordinator.client._token_provider._oauth_client,
            "refresh",
            side_effect=refresh,
        ):
            task = asyncio.create_task(coordinator.client.get_resource("000123", TEMP))
            async with asyncio.timeout(3):
                await entered.wait()
            manager = owner.connections.management
            if change == "disable":
                manager.set_enabled(manager.edit(cloud), False)
            else:
                manager.remove(manager.edit(cloud))
            count = len(backend.calls)
            release.set()
            with pytest.raises(SourceStopped):
                await task
            await settle(hass)
        assert len(backend.calls) == count
        assert owner.connections.source_data("local")
        if change == "disable":
            assert EntryConfiguration(entry, cloud).tokens() == renewed
            assert entry.options["connections"][cloud]["enabled"] is False
        else:
            assert cloud not in entry.data["connections"]


async def test_default_factory_requires_revalidation_of_legacy_local_access(hass):
    backend = Backend()
    async with running(hass, backend, defaults=True) as (_, _, owner, _):
        await eventually(
            lambda: source_for(owner, "000123") and source_for(owner, "gateway-b")
        )
        assert owner.connections.source_phase("local") is ConnectionPhase.AUTH_REQUIRED
        assert owner.connections.source_data("local") is None
        assert (
            owner.connections.local_state("local").problem
            == "local_revalidation_required"
        )


async def test_reauthentication_replaces_pointt_sources_and_keeps_account_metrics(hass):
    from custom_components.bosch_buderus_heating.configuration_management import (
        ConfigurationManagement,
    )

    backend = Backend()
    async with running(hass, backend) as (entry, cloud, owner, _):
        await eventually(
            lambda: source_for(owner, "000123") and source_for(owner, "gateway-b")
        )
        previous = source_for(owner, "000123")
        manager = ConfigurationManagement(hass, entry)
        renewed = AuthTokens("replacement", "new-refresh", 10**12, scope=("bacon",))
        old_provider = previous.client._token_provider
        url = previous.client._transport._base_url
        with (
            patch(
                "custom_components.bosch_buderus_heating.configuration_management.PointTClient",
                side_effect=lambda *args, **kwargs: PointTClient(
                    *args, **kwargs, base_url=url
                ),
            ),
            patch(
                "custom_components.bosch_buderus_heating.configuration_management.discover",
                AsyncMock(
                    return_value=(
                        ("softener",),
                        EntryConfiguration(entry, cloud).cloud_data()["bacon_account"],
                    )
                ),
            ),
        ):
            await manager.async_reauthenticate(manager.edit(cloud), renewed)
        await eventually(
            lambda: (
                source_for(owner, "000123") is not None
                and source_for(owner, "000123") is not previous
            )
        )
        current = source_for(owner, "000123")
        assert current.client._token_provider is not old_provider
        assert current.client._token_provider.tokens == renewed
        assert current.client.metrics is previous.client.metrics
        assert not previous.last_update_success
        with pytest.raises(SourceStopped):
            await previous.client.get_resource("000123", TEMP)
        assert owner.connections.source_data("local")


async def test_disabling_pending_gateway_releases_startup_recovery_for_other_gateway(
    hass,
):
    backend = Backend()
    backend.gates["000123"] = asyncio.Event()
    async with running(hass, backend) as (_, cloud, owner, _):
        await eventually(lambda: source_for(owner, "gateway-b"))
        other = source_for(owner, "gateway-b")
        assert not other.startup_session.setup_complete
        manager = owner.connections.management
        manager.set_binding_enabled(
            manager.edit(cloud), binding_for(owner, "000123"), False
        )
        await eventually(lambda: other.startup_session.setup_complete)
        assert source_for(owner, "gateway-b") is other


async def test_replacement_waits_for_fault_baseline_flush(hass):
    backend = Backend()
    async with running(hass, backend) as (_, cloud, owner, _):
        await eventually(
            lambda: source_for(owner, "000123") and source_for(owner, "gateway-b")
        )
        previous = source_for(owner, "000123")
        other = source_for(owner, "gateway-b")
        fault = Resource(
            path="/notifications",
            values=({"ccd": "6249", "fc": "12"},),
            has_values=True,
        )
        previous.faults.process_resources({fault.path: fault})
        fingerprint = previous.faults.active[0].fingerprint
        entered, release = asyncio.Event(), asyncio.Event()
        save = previous.faults._store.async_save

        async def delayed_save(data):
            entered.set()
            await release.wait()
            await save(data)

        manager = owner.connections.management
        with patch.object(previous.faults._store, "async_save", delayed_save):
            manager.set_binding_enabled(
                manager.edit(cloud), binding_for(owner, "000123"), False
            )
            async with asyncio.timeout(5):
                await entered.wait()
            manager.set_binding_enabled(
                manager.edit(cloud), binding_for(owner, "000123"), True
            )
            try:
                await settle(hass)
                assert source_for(owner, "000123") is None
                assert source_for(owner, "gateway-b") is other
            finally:
                release.set()
            await eventually(lambda: source_for(owner, "000123"))
        current = source_for(owner, "000123")
        assert current is not previous
        assert current.faults.active[0].fingerprint == fingerprint
        assert not current.faults.current_state_confirmed
