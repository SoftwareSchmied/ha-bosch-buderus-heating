"""Actual PointT HTTP and wallbox coordinators within independent source lifetimes."""

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.configuration import ConfigurationError
from custom_components.bosch_buderus_heating.configuration_access import (
    EntryConfiguration,
)
from custom_components.bosch_buderus_heating.connection_runtime import ConnectionPhase
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.managed_wallbox import (
    ManagedWallboxCoordinator,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthTokens,
    PointTClient,
    RefreshTokenRejected,
)
from custom_components.bosch_buderus_heating.pointt.exceptions import (
    RateLimited,
    SourceStopped,
)
from custom_components.bosch_buderus_heating.wallbox import SECTIONS
from custom_components.bosch_buderus_heating.wallbox_sensor import (
    DESCRIPTIONS,
    WallboxSensor,
)
from tests.test_connection_runtime import settle
from tests.test_managed_pointt import (
    Backend,
    binding_for,
    eventually,
    running,
    source_for,
)
from tests.test_wallbox import fixture

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
ROOT = "custom_components.bosch_buderus_heating"
WB = "gateway-b"
HEAT = "000123"


class WallboxBackend(Backend):
    def __init__(self, family="commodule2"):
        super().__init__()
        self.wallbox = WB
        self.families = {HEAT: "MX400", WB: family}
        self.objects = fixture()
        self.gateway_objects = {}
        self.object_status = {}
        self.object_calls = []
        self.authorizations = []

    async def list_gateways(self, request):
        self.calls.append(("gateways",))
        self.authorizations.append(request.headers.get("Authorization"))
        await self.wait("gateways")
        return web.json_response(
            [{"id": key, "deviceType": self.families[key]} for key in self.gateways]
        )

    async def scalar(self, request):
        gateway, path = request.match_info["gateway"], "/" + request.match_info["path"]
        if not path.startswith("/rest/v1/cp0/"):
            return await super().scalar(request)
        section = path.rsplit("/", 1)[-1]
        self.calls.append((request.method, gateway, path))
        self.object_calls.append((request.method, gateway, section))
        self.authorizations.append(request.headers.get("Authorization"))
        # Capture the response before a delay to exercise stale answers.
        status = self.object_status.get((gateway, section), 200)
        payload = self.gateway_objects.get(gateway, self.objects)[section]
        await self.wait((gateway, section))
        if status != 200:
            return web.Response(status=status, headers={"Retry-After": "900"})
        return web.json_response(payload)


def sensors(coordinator):
    return {item.key: WallboxSensor(coordinator, item) for item in DESCRIPTIONS}


def disable_other_cloud_targets(data, options):
    for key, binding in data["bindings"].items():
        if binding["adapter"] != "k40_local":
            options["bindings"][key]["enabled"] = binding["locator"]["value"] == WB


@contextmanager
def reauth_server(entry, connection, url):
    with (
        patch(
            ROOT + ".configuration_management.PointTClient",
            side_effect=lambda *args, **kwargs: PointTClient(
                *args, **kwargs, base_url=url
            ),
        ),
        patch(
            ROOT + ".configuration_management.discover",
            AsyncMock(
                return_value=(
                    ("softener",),
                    EntryConfiguration(entry, connection).cloud_data()["bacon_account"],
                )
            ),
        ),
    ):
        yield


@pytest.mark.parametrize("family", ["commodule", "CoMmOdUlE2"])
@pytest.mark.parametrize("friendly", [False, True])
async def test_actual_wallbox_http_and_native_sensors_share_heating_account(
    hass, family, friendly
):
    backend = WallboxBackend(family)
    with patch(ROOT + ".wallbox.monotonic", return_value=100) as now:
        async with running(hass, backend, friendly=friendly) as (_, cloud, owner, _):
            await eventually(lambda: source_for(owner, WB) and source_for(owner, HEAT))
            wallbox, heating = source_for(owner, WB), source_for(owner, HEAT)
            assert isinstance(wallbox, ManagedWallboxCoordinator)
            assert wallbox.connection_id == cloud
            assert backend.object_calls == [("GET", WB, part) for part in SECTIONS]
            assert sum(call == ("gateways",) for call in backend.calls) == 1
            assert not any(
                call[0] == "bulk" and call[1] == WB for call in backend.calls
            )
            assert wallbox.client._token_provider is heating.client._token_provider
            assert wallbox.client.backoff is heating.client.backoff
            assert wallbox.client.metrics is heating.client.metrics
            assert (
                wallbox.client._transport._semaphore
                is heating.client._transport._semaphore
            )
            entities = sensors(wallbox)
            assert len(entities) == 8
            assert entities["state"].native_value == "charging"
            assert entities["power"].native_value == 7.2
            assert entities["energy"].native_value == 1.25
            assert entities["current_limit"].native_value == 16
            assert all(
                item.unique_id == f"{WB}:wallbox:cp0:{key}"
                for key, item in entities.items()
            )
            assert entities["energy"].state_class is None
            before = len(backend.calls)
            report = wallbox.diagnostics()
            assert not report["write_support"]
            assert len(backend.calls) == before
            assert all(
                text not in json.dumps(report)
                for text in (
                    WB,
                    "private-label",
                    "private-serial",
                    "Synthetic wallbox",
                    "2026-10-24",
                    "7.2",
                )
            )
            fast = 120 if friendly else 60
            assert wallbox.intervals == {
                "telemetry": fast,
                "conf": 10 * fast // 2,
                "info": 15 * fast,
            }
            await wallbox.async_refresh()
            assert len(backend.calls) == before
            now.return_value += fast
            await wallbox.async_refresh()
            assert backend.object_calls[3:] == [("GET", WB, "telemetry")]
            now.return_value += 2 * fast + 1
            assert not entities["power"].available
            assert entities["power"].native_value is None
            assert entities["role"].available


async def test_wallbox_only_cloud_source_needs_no_heating_discovery(hass):
    backend = WallboxBackend()
    async with running(hass, backend, configure=disable_other_cloud_targets) as (
        _,
        _,
        owner,
        _,
    ):
        await eventually(lambda: source_for(owner, WB))
        assert source_for(owner, HEAT) is None
        assert not any(call[0] == "bulk" for call in backend.calls)
        assert backend.object_calls == [("GET", WB, part) for part in SECTIONS]
        assert owner.connections.source_data("local")


async def test_slow_wallbox_does_not_hold_heating_supplementary_startup(hass):
    backend = WallboxBackend()
    backend.gates[(WB, "telemetry")] = asyncio.Event()
    async with running(hass, backend) as (_, _, owner, _):
        await eventually(lambda: source_for(owner, HEAT))
        assert source_for(owner, WB) is None
        assert source_for(owner, HEAT).startup_session.setup_complete
        assert owner.connections.source_data("local")
        backend.gates[(WB, "telemetry")].set()
        await eventually(lambda: source_for(owner, WB))


@pytest.mark.parametrize("part", SECTIONS)
@pytest.mark.parametrize("failure", [403, 404, 406, "malformed"])
async def test_section_failure_and_scheduled_recovery_keep_other_readings(
    hass, part, failure
):
    backend = WallboxBackend()
    with patch(ROOT + ".wallbox.monotonic", return_value=100) as now:
        async with running(hass, backend) as (_, _, owner, _):
            await eventually(lambda: source_for(owner, WB))
            wallbox = source_for(owner, WB)
            entities = sensors(wallbox)
            ids = {key: item.unique_id for key, item in entities.items()}
            original = backend.objects[part]
            if failure == "malformed":
                backend.objects[part] = {"value": "invalid"}
            else:
                backend.object_status[(WB, part)] = failure
            wallbox._next[part] = 0
            await wallbox.async_refresh()
            assert wallbox.last_update_success
            assert not wallbox.section_fresh(part)
            assert all(
                wallbox.section_fresh(other) for other in SECTIONS if other != part
            )
            count = len(backend.object_calls)
            await wallbox.async_refresh()
            assert len(backend.object_calls) == count
            backend.objects[part] = original
            backend.object_status.clear()
            now.return_value += wallbox.intervals[part]
            await wallbox.async_refresh()
            assert wallbox.section_fresh(part)
            assert source_for(owner, WB) is wallbox
            assert ids == {key: item.unique_id for key, item in entities.items()}
            assert all(item.available for item in entities.values())


@pytest.mark.parametrize("when", ["startup", "polling"])
async def test_wallbox_service_failure_is_local_and_recovers(hass, when):
    backend = WallboxBackend()
    if when == "startup":
        backend.object_status[(WB, "telemetry")] = 503
    async with running(hass, backend) as (_, _, owner, _):
        await eventually(lambda: source_for(owner, HEAT) and source_for(owner, WB))
        wallbox = source_for(owner, WB)
        entities = sensors(wallbox)
        ids = {key: item.unique_id for key, item in entities.items()}
        if when == "startup":
            assert backend.object_calls == [("GET", WB, "telemetry")] * 2
        else:
            backend.object_status[(WB, "telemetry")] = 503
            wallbox._next["telemetry"] = 0
            await wallbox.async_refresh()
        assert not wallbox.last_update_success
        assert all(not item.available for item in entities.values())
        assert source_for(owner, HEAT).last_update_success
        assert owner.connections.source_data("local")
        backend.object_status.clear()
        await wallbox.async_refresh()
        assert source_for(owner, WB) is wallbox
        assert all(item.available for item in entities.values())
        assert ids == {key: item.unique_id for key, item in sensors(wallbox).items()}
        assert wallbox.reading("telemetry", "power") == 7.2


@pytest.mark.parametrize("when", ["startup", "polling"])
@pytest.mark.parametrize("separate_account", [False, True])
async def test_wallbox_authentication_only_stops_its_account(
    hass, when, separate_account
):
    backend = WallboxBackend()
    if when == "startup":
        backend.object_status[(WB, "telemetry")] = 401
    with patch(
        ROOT + ".pointt.auth.OAuthClient.refresh",
        AsyncMock(side_effect=RefreshTokenRejected()),
    ):
        async with running(hass, backend, second_cloud=separate_account) as (
            entry,
            cloud,
            owner,
            _,
        ):
            affected = "second" if separate_account else cloud
            if when == "polling":
                await eventually(
                    lambda: source_for(owner, WB) and source_for(owner, HEAT)
                )
                wallbox = source_for(owner, WB)
                backend.object_status[(WB, "telemetry")] = 401
                wallbox._next["telemetry"] = 0
                await wallbox.async_refresh()
            await eventually(
                lambda: (
                    owner.connections.connection_phase(affected)
                    is ConnectionPhase.AUTH_REQUIRED
                )
            )
            await settle(hass)
            assert source_for(owner, WB) is None
            if separate_account:
                await eventually(lambda: source_for(owner, HEAT))
                assert source_for(owner, HEAT).last_update_success
            else:
                assert source_for(owner, HEAT) is None
            assert owner.connections.source_data("local")
            flows = list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
            assert len(flows) == 1 and flows[0]["context"]["connection_id"] == affected


@pytest.mark.parametrize("origin", ["wallbox", "heating"])
async def test_heating_and_wallbox_obey_the_same_rate_limit(hass, origin):
    backend = WallboxBackend()
    async with running(hass, backend) as (_, _, owner, _):
        await eventually(lambda: source_for(owner, WB) and source_for(owner, HEAT))
        wallbox, heating = source_for(owner, WB), source_for(owner, HEAT)
        if origin == "wallbox":
            backend.object_status[(WB, "telemetry")] = 429
            wallbox._next["telemetry"] = 0
            await wallbox.async_refresh()
            count = len(backend.calls)
            heating._next_update = dict.fromkeys(heating._poll_intervals, 0)
            await heating.async_refresh()
        else:
            backend.status[HEAT] = 429
            heating._next_update = dict.fromkeys(heating._poll_intervals, 0)
            await heating.async_refresh()
            count = len(backend.calls)
            wallbox._next["telemetry"] = 0
            await wallbox.async_refresh()
        assert len(backend.calls) == count
        with pytest.raises(RateLimited):
            wallbox.client.backoff.raise_if_active()
        assert owner.connections.source_data("local")


async def test_disabling_wallbox_invalidates_sensors_and_retired_clients(hass):
    backend = WallboxBackend()
    async with running(hass, backend) as (_, cloud, owner, _):
        await eventually(lambda: source_for(owner, WB) and source_for(owner, HEAT))
        previous, heating = source_for(owner, WB), source_for(owner, HEAT)
        entities = sensors(previous)
        cached = dict(previous.data)
        manager = owner.connections.management
        manager.set_binding_enabled(manager.edit(cloud), binding_for(owner, WB), False)
        assert all(
            not item.available and item.native_value is None
            for item in entities.values()
        )
        with pytest.raises(SourceStopped):
            await previous.client.get_wallbox_section(WB, "telemetry")
        await settle(hass)
        previous.async_set_updated_data(cached)
        assert not previous.last_update_success
        assert all(not snapshot.values for snapshot in previous.data.values())
        assert source_for(owner, HEAT) is heating
        manager.set_binding_enabled(manager.edit(cloud), binding_for(owner, WB), True)
        await eventually(lambda: source_for(owner, WB))
        current = source_for(owner, WB)
        assert current is not previous
        assert {key: item.unique_id for key, item in sensors(current).items()} == {
            key: item.unique_id for key, item in entities.items()
        }
        assert not entities["power"].available
        with pytest.raises(SourceStopped):
            await previous.client.get_wallbox_section(WB, "info")


@pytest.mark.parametrize("status", [200, 401])
async def test_late_wallbox_response_cannot_refresh_tokens_or_revive_old_source(
    hass, status
):
    backend = WallboxBackend()
    with patch(ROOT + ".pointt.auth.OAuthClient.refresh", AsyncMock()) as refresh:
        async with running(hass, backend) as (_, cloud, owner, _):
            await eventually(lambda: source_for(owner, WB))
            previous = source_for(owner, WB)
            gate = backend.gates[(WB, "telemetry")] = asyncio.Event()
            backend.entered.pop((WB, "telemetry"), None)
            backend.object_status[(WB, "telemetry")] = status
            pending = asyncio.create_task(
                previous.client.get_wallbox_section(WB, "telemetry")
            )
            await eventually(lambda: (WB, "telemetry") in backend.entered)
            manager = owner.connections.management
            manager.set_binding_enabled(
                manager.edit(cloud), binding_for(owner, WB), False
            )
            await settle(hass)
            backend.object_status.clear()
            backend.objects["telemetry"] = {
                "values": {"meter": "500;3.1", "wbState": "Available"}
            }
            manager.set_binding_enabled(
                manager.edit(cloud), binding_for(owner, WB), True
            )
            gate.set()
            with pytest.raises(SourceStopped):
                await pending
            await eventually(lambda: source_for(owner, WB))
            assert source_for(owner, WB).reading("telemetry", "power") == 3.1
            assert previous.reading("telemetry", "power") is None
            refresh.assert_not_awaited()


async def test_stopping_wallbox_cancels_active_poll_and_scheduled_work(hass):
    backend = WallboxBackend()
    async with running(hass, backend) as (_, cloud, owner, _):
        await eventually(lambda: source_for(owner, WB))
        previous = source_for(owner, WB)
        gate = backend.gates[(WB, "telemetry")] = asyncio.Event()
        backend.entered.pop((WB, "telemetry"), None)
        previous._next["telemetry"] = 0
        pending = asyncio.create_task(previous.async_refresh())
        await eventually(lambda: (WB, "telemetry") in backend.entered)
        manager = owner.connections.management
        manager.set_binding_enabled(manager.edit(cloud), binding_for(owner, WB), False)
        await eventually(lambda: pending.done())
        await asyncio.gather(pending, return_exceptions=True)
        assert not previous._updates._active
        before = len(backend.object_calls)
        gate.set()
        await previous.async_refresh()
        assert len(backend.object_calls) == before
        assert not previous.last_update_success


@pytest.mark.parametrize("missing_disabled_wallbox", [False, True])
async def test_reauthentication_verifies_wallbox_and_replaces_only_valid_sources(
    hass, missing_disabled_wallbox
):
    backend = WallboxBackend()
    async with running(hass, backend) as (entry, cloud, owner, _):
        await eventually(lambda: source_for(owner, WB) and source_for(owner, HEAT))
        previous, heating = source_for(owner, WB), source_for(owner, HEAT)
        manager = owner.connections.management
        old_tokens = previous.client._token_provider.tokens
        new_tokens = AuthTokens("replacement", "new-refresh", 10**12, scope=("bacon",))
        if missing_disabled_wallbox:
            manager.set_binding_enabled(
                manager.edit(cloud), binding_for(owner, WB), False
            )
            backend.gateways.remove(WB)
        with reauth_server(entry, cloud, previous.client._transport._base_url):
            if missing_disabled_wallbox:
                with pytest.raises(ConfigurationError, match="reauth_targets_missing"):
                    await manager.async_reauthenticate(manager.edit(cloud), new_tokens)
                assert source_for(owner, HEAT) is heating
                assert EntryConfiguration(entry, cloud).tokens() == old_tokens
            else:
                await manager.async_reauthenticate(manager.edit(cloud), new_tokens)
                await eventually(
                    lambda: (
                        source_for(owner, WB) is not None
                        and source_for(owner, WB) is not previous
                    )
                )
                current = source_for(owner, WB)
                assert current.client.metrics is previous.client.metrics
                assert current.client._token_provider.tokens == new_tokens
                assert backend.authorizations[-1] == "Bearer replacement"
                assert not previous.section_fresh("telemetry")
                with pytest.raises(SourceStopped):
                    await previous.client.get_wallbox_section(WB, "telemetry")
        assert owner.connections.source_data("local")


async def test_separate_accounts_keep_their_wallbox_polling_profiles(hass):
    backend = WallboxBackend()

    def configure(data, options):
        options["connections"]["second"]["polling_profile"] = "cloud_friendly"

    async with running(hass, backend, second_cloud=True, configure=configure) as (
        _,
        _,
        owner,
        _,
    ):
        await eventually(lambda: source_for(owner, WB) and source_for(owner, HEAT))
        wallbox, heating = source_for(owner, WB), source_for(owner, HEAT)
        assert wallbox.intervals == {"telemetry": 120, "conf": 600, "info": 1800}
        assert wallbox.connection_id == "second"
        assert wallbox.client._token_provider is not heating.client._token_provider
        assert wallbox.client.backoff is not heating.client.backoff
        assert wallbox.client.metrics is not heating.client.metrics


async def test_two_wallboxes_keep_distinct_values_and_source_lifetimes(hass):
    backend = WallboxBackend()
    backend.families[HEAT] = "commodule"
    backend.gateway_objects[HEAT] = fixture()
    backend.gateway_objects[HEAT]["telemetry"]["values"]["meter"] = "250;1.1"
    async with running(hass, backend) as (_, cloud, owner, _):
        await eventually(lambda: source_for(owner, WB) and source_for(owner, HEAT))
        first, second = source_for(owner, HEAT), source_for(owner, WB)
        assert isinstance(first, ManagedWallboxCoordinator)
        assert first.reading("telemetry", "power") == 1.1
        assert second.reading("telemetry", "power") == 7.2
        first_ids = {item.unique_id for item in sensors(first).values()}
        second_ids = {item.unique_id for item in sensors(second).values()}
        assert first_ids.isdisjoint(second_ids)
        assert first.client._token_provider is second.client._token_provider
        assert not any(call[0] == "bulk" for call in backend.calls)
        manager = owner.connections.management
        manager.set_binding_enabled(
            manager.edit(cloud), binding_for(owner, HEAT), False
        )
        await settle(hass)
        assert first.reading("telemetry", "power") is None
        assert source_for(owner, WB) is second
        assert second.reading("telemetry", "power") == 7.2
        with pytest.raises(ValueError, match="Gateway does not match"):
            await second.client.get_wallbox_section(HEAT, "telemetry")


async def test_removing_cloud_retains_wallbox_target_and_registry_preferences(hass):
    backend = WallboxBackend()
    async with running(hass, backend) as (entry, cloud, owner, _):
        await eventually(lambda: source_for(owner, WB))
        previous = source_for(owner, WB)
        registry = er.async_get(hass)
        entity = registry.async_get_or_create(
            "sensor", DOMAIN, sensors(previous)["power"].unique_id, config_entry=entry
        )
        entity = registry.async_update_entity(
            entity.entity_id,
            name="My charging power",
            disabled_by=er.RegistryEntryDisabler.USER,
        )
        binding = binding_for(owner, WB)
        target = owner.connections.snapshot.bindings[binding].target_id
        manager = owner.connections.management
        manager.remove(manager.edit(cloud))
        await settle(hass)
        assert target in owner.connections.snapshot.targets
        assert binding not in owner.connections.snapshot.bindings
        assert registry.async_get(entity.entity_id) == entity
        assert not sensors(previous)["power"].available
        with pytest.raises(SourceStopped):
            await previous.client.get_wallbox_section(WB, "telemetry")
        assert owner.connections.source_data("local")
