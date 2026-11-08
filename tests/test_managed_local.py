"""Native source ownership with a real scheduler and synthetic local transports."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from custom_components.bosch_buderus_heating import managed_local as module
from custom_components.bosch_buderus_heating.configuration_management import (
    ConfigurationManagement,
)
from custom_components.bosch_buderus_heating.connection_runtime import (
    ConnectionPhase,
    ConnectionRuntime,
)
from custom_components.bosch_buderus_heating.local_budget import LocalReadBudget
from custom_components.bosch_buderus_heating.local_credentials import (
    StoredLocalAccess,
    import_local_token,
)
from custom_components.bosch_buderus_heating.local_polling import (
    service as service_module,
)
from custom_components.bosch_buderus_heating.local_polling.inventory import Freshness
from custom_components.bosch_buderus_heating.local_probe import (
    LocalEndpoint,
    ProbePath,
    TransportFailure,
)
from custom_components.bosch_buderus_heating.local_read import (
    ReadMetrics,
    ReadResult,
    ReadState,
)
from custom_components.bosch_buderus_heating.local_state import LocalCondition
from custom_components.bosch_buderus_heating.local_transport import LocalProbeExchange
from tests.test_connection_runtime import Harness, entry_for, settle
from tests.test_local_budget import Clock
from tests.test_local_polling import REQUEST, no_discovery, success
from tests.test_local_probe import response


@pytest.fixture
async def prepared(hass, monkeypatch, tmp_path):
    entry, cloud, target = entry_for(hass)
    data = dict(entry.data)
    data["connections"] = dict(data["connections"])
    access = StoredLocalAccess(
        import_local_token("synthetic-local-token"),
        LocalEndpoint("gateway.example.invalid"),
        "61" * 32,
        None,
        "000123",
        1,
    )
    data["connections"]["local"] = {**data["connections"]["local"], **access.to_data()}
    hass.config_entries.async_update_entry(entry, data=data, minor_version=2)
    clock, harness = Clock(), Harness()
    probes, clients = [], []
    control = SimpleNamespace(
        gate=None,
        gateway="000123",
        failure=None,
        status=200,
        result=None,
        hold=None,
        retry_after=None,
    )

    async def budget(context, **kwargs):
        return LocalReadBudget(context, clock=clock)

    monkeypatch.setattr(
        module, "local_budget_manager", lambda hass: SimpleNamespace(budget=budget)
    )
    monkeypatch.setattr(service_module, "start_offset", lambda scope: 0)
    original_service = module.LocalPollingService

    def service(*args, **kwargs):
        value = original_service(*args, **kwargs, clock=clock)
        value.store._path = tmp_path / value.store._path.name
        no_discovery(value.planner)
        value.planner.hints((REQUEST,), clock())
        return value

    monkeypatch.setattr(module, "LocalPollingService", service)

    async def probe(context, token, pin, *, is_current, budget):
        probes.append(context)
        assert token == "synthetic-local-token" and is_current()
        if control.gate is not None:
            await control.gate.wait()
        return LocalProbeExchange(
            tuple(
                replace(
                    response(path, context=context, gatewayId=control.gateway),
                    status=None if control.failure else control.status,
                    body=b""
                    if control.failure
                    else response(
                        path, context=context, gatewayId=control.gateway
                    ).body,
                    failure=control.failure,
                )
                for path in ProbePath
            ),
            retry_after_seconds=control.retry_after,
        )

    monkeypatch.setattr(module, "read_local_probe", probe)

    class Client:
        metrics = ReadMetrics()

        def __init__(self, context, token, pin, **kwargs):
            self.context, self.calls, self.closed = context, [], False
            clients.append(self)

        async def read(self, request):
            self.calls.append(request)
            if control.hold is not None:
                await control.hold.wait()
            return control.result or success(
                request, clock(), context=self.context.identity
            )

        async def close(self):
            self.closed = True

    monkeypatch.setattr(module, "LocalReadClient", Client)
    factory = module.LocalDriverFactory(hass, entry, lambda: runtime)
    runtime = ConnectionRuntime(
        hass,
        entry,
        {"singlekey_cloud": harness.factory, "k40_local": factory},
        sleep=harness.sleep,
    )
    yield SimpleNamespace(
        entry=entry,
        cloud=cloud,
        target=target,
        clock=clock,
        runtime=runtime,
        harness=harness,
        control=control,
        probes=probes,
        clients=clients,
    )
    await runtime.close()


async def ready(hass, p):
    p.runtime.start()
    for _ in range(100):
        await settle(hass)
        state = p.runtime.local_state("local")
        if state is not None and state.current_observations:
            return state
        await asyncio.sleep(0.01)
    raise AssertionError("Local data did not become ready")


async def wait_until(hass, predicate, *, limit_seconds=3):
    async with asyncio.timeout(limit_seconds):
        while not predicate():
            await settle(hass)
            await asyncio.sleep(0.01)


async def test_local_reads_start_only_after_fresh_identity_while_cloud_is_independent(
    hass, prepared
):
    p = prepared
    p.control.gate = asyncio.Event()
    p.runtime.start()
    await settle(hass)
    assert p.runtime.connection_phase(p.cloud) is ConnectionPhase.READY
    assert p.runtime.local_state("local").condition is LocalCondition.INITIALIZING
    assert not p.clients
    p.control.gate.set()
    for _ in range(100):
        await settle(hass)
        if p.runtime.local_state("local").current_observations:
            break
        await asyncio.sleep(0.01)
    state = p.runtime.local_state("local")
    assert state.identity_confirmed and len(state.current_observations) == 1
    assert state.current_observations[0].context == p.probes[0].identity
    assert len(p.probes) == 1


async def test_independent_expiry_retains_observation_but_excludes_it_from_supply(
    hass, prepared
):
    p = prepared
    state = await ready(hass, p)
    original = state.current_observations[0]
    source = p.runtime.connection_driver("local").sources["local"]
    await source.service.budget.pause(7200)
    p.clock.advance(180)
    source.service.wake()
    await settle(hass)
    state = p.runtime.local_state("local")
    assert state.condition is LocalCondition.PAUSED
    assert state.last_observations == (original,) and not state.current_observations
    assert state.resources[0].freshness is Freshness.EXPIRED
    assert not state.resources[0].available
    assert p.runtime.connection_phase(p.cloud) is ConnectionPhase.READY


@pytest.mark.parametrize("action", ["disable", "remove", "binding", "close"])
async def test_lifecycle_cancels_late_reads_and_preserves_other_sources(
    hass, prepared, action
):
    p = prepared
    await ready(hass, p)
    source = p.runtime.connection_driver("local").sources["local"]
    old_context = source.context
    p.control.hold = asyncio.Event()
    p.clock.advance(60)
    source.service.wake()
    await wait_until(hass, lambda: len(p.clients[0].calls) == 2)
    management = ConfigurationManagement(hass, p.entry)
    if action == "disable":
        management.set_enabled(management.edit("local"), False)
    elif action == "remove":
        management.remove(management.edit("local"))
    elif action == "binding":
        management.set_targets_enabled(management.edit("local"), set())
    else:
        await p.runtime.close()
    await settle(hass)
    p.control.hold.set()
    assert not old_context.configuration_current
    assert p.runtime.local_state("local") is None
    assert p.clients[0].closed
    if action != "close":
        assert p.runtime.connection_phase(p.cloud) is ConnectionPhase.READY
        assert p.target in management.current().targets


async def test_reenable_rechecks_identity_and_does_not_restore_old_measurements(
    hass, prepared
):
    p = prepared
    old = await ready(hass, p)
    management = ConfigurationManagement(hass, p.entry)
    management.set_enabled(management.edit("local"), False)
    await settle(hass)
    p.control.gate = asyncio.Event()
    management.set_enabled(management.edit("local"), True)
    await wait_until(hass, lambda: len(p.probes) == 2)
    assert len(p.probes) == 2 and len(p.clients) == 1
    assert not p.runtime.local_state("local").last_observations
    p.control.gate.set()
    for _ in range(100):
        await settle(hass)
        if p.runtime.local_state("local").current_observations:
            break
        await asyncio.sleep(0.01)
    assert (
        p.runtime.local_state("local").current_observations[0]
        is not old.current_observations[0]
    )


async def test_wrong_gateway_never_starts_polling(hass, prepared):
    p = prepared
    p.control.gateway = "different-gateway"
    p.runtime.start()
    await settle(hass)
    assert not p.clients
    assert p.runtime.local_state("local").problem == "local_identity_conflict"
    assert p.runtime.connection_phase(p.cloud) is ConnectionPhase.READY


@pytest.mark.parametrize(
    "status,failure,condition,problem",
    [
        (401, None, LocalCondition.AUTH_REQUIRED, "auth_required"),
        (
            200,
            TransportFailure.TLS_CHANGED,
            LocalCondition.CERTIFICATE_CHANGED,
            "certificate_changed",
        ),
        (404, None, LocalCondition.UNAVAILABLE, "required_resource_missing"),
        (503, None, LocalCondition.PAUSED, None),
        (429, None, LocalCondition.PAUSED, None),
        (200, TransportFailure.CONNECT, LocalCondition.PAUSED, None),
        (200, TransportFailure.TIMEOUT, LocalCondition.PAUSED, None),
    ],
)
async def test_startup_failures_never_start_polling_or_restart_cloud(
    hass, prepared, status, failure, condition, problem
):
    p = prepared
    p.control.status, p.control.failure = status, failure
    p.control.retry_after = 7200 if status == 429 else None
    p.runtime.start()
    await wait_until(
        hass,
        lambda: (
            p.runtime.local_state("local") is not None
            and p.runtime.local_state("local").condition is condition
        ),
    )
    state = p.runtime.local_state("local")
    assert state.problem == problem and not state.current_observations
    assert len(p.probes) == 1 and not p.clients
    assert p.runtime.connection_phase(p.cloud) is ConnectionPhase.READY
    assert len(p.harness.drivers[p.cloud]) == 1


@pytest.mark.parametrize(
    "problem,condition",
    [
        ("auth_required", LocalCondition.AUTH_REQUIRED),
        ("certificate_changed", LocalCondition.CERTIFICATE_CHANGED),
        ("local_identity_conflict", LocalCondition.UNAVAILABLE),
    ],
)
async def test_polling_access_errors_remain_recoverable_without_cloud_restart(
    hass, prepared, problem, condition
):
    p = prepared
    await ready(hass, p)
    p.control.result = ReadResult(REQUEST, ReadState.ACCESS_ERROR, problem=problem)
    source = p.runtime.connection_driver("local").sources["local"]
    p.clock.advance(60)
    source.service.wake()
    await wait_until(
        hass, lambda: p.runtime.local_state("local").condition is condition
    )
    state = p.runtime.local_state("local")
    assert state.condition is condition and not state.current_observations
    assert source.context.configuration_current and len(p.probes) == 1
    assert p.runtime.connection_phase(p.cloud) is ConnectionPhase.READY
    assert len(p.harness.drivers[p.cloud]) == 1


async def test_legacy_unverified_access_is_not_used(hass, prepared):
    p = prepared
    data = dict(p.entry.data)
    data["connections"] = dict(data["connections"])
    data["connections"]["local"] = dict(data["connections"]["local"])
    data["connections"]["local"].pop("local_access")
    data["connections"]["local"]["credentials"] = {
        "token": "synthetic",
        "scope": ["open_api.read"],
    }
    hass.config_entries.async_update_entry(p.entry, data=data)
    p.runtime.start()
    await settle(hass)
    assert p.runtime.local_state("local").condition is LocalCondition.AUTH_REQUIRED
    assert not p.clients and not p.probes


@pytest.mark.parametrize("interval", [60, 300, 900])
async def test_interval_change_restarts_only_local_source(hass, prepared, interval):
    p = prepared
    await ready(hass, p)
    management = ConfigurationManagement(hass, p.entry)
    management.set_local_interval(management.edit("local"), interval)
    await wait_until(
        hass,
        lambda: (
            p.runtime.connection_driver("local") is not None
            and p.runtime.connection_driver("local").sources.get("local") is not None
            and p.runtime.connection_driver("local").sources["local"].service
            is not None
        ),
    )
    source = p.runtime.connection_driver("local").sources["local"]
    assert source.service.inventory.interval == interval
    assert len(p.harness.drivers[p.cloud]) == 1


async def test_running_local_source_caps_paired_readings_at_the_cloud_interval(
    hass, prepared
):
    from datetime import timedelta
    from types import SimpleNamespace

    from custom_components.bosch_buderus_heating.local_api.catalog import (
        request_for_path,
    )

    p = prepared
    await ready(hass, p)
    management = ConfigurationManagement(hass, p.entry)
    management.set_local_values(management.edit("local"), True)
    management.set_local_interval(management.edit("local"), 900)
    # The running cloud reports its actual plan for the paired reading.
    plan = SimpleNamespace(
        scheduled_interval=lambda path: (True, timedelta(seconds=120))
    )
    original = p.runtime.source_data
    cloud_bindings = {
        key
        for key, binding in management.current().bindings.items()
        if binding.connection_id == p.cloud
    }
    p.runtime.source_data = lambda key: plan if key in cloud_bindings else original(key)
    await wait_until(
        hass,
        lambda: (
            p.runtime.connection_driver("local") is not None
            and p.runtime.connection_driver("local").sources.get("local") is not None
            and p.runtime.connection_driver("local").sources["local"].service
            is not None
            and p.runtime.connection_driver("local")
            .sources["local"]
            .service.inventory.interval
            == 900
        ),
    )
    inventory = p.runtime.connection_driver("local").sources["local"].service.inventory
    # Paired with the running cloud: min(local 900, cloud plan 120).
    assert inventory.period(request_for_path("/heatSources/returnTemperature")) == 120
    # Not paired: the configured local interval applies.
    assert (
        inventory.period(request_for_path("/heatSources/currentSupplySetpoint")) == 900
    )


@pytest.mark.parametrize("interval", [0, 30, 61, 960, True, 60.0, "60", None])
async def test_invalid_interval_never_changes_configuration(hass, prepared, interval):
    p = prepared
    management = ConfigurationManagement(hass, p.entry)
    before = management.current()
    with pytest.raises(ValueError, match="invalid_local_interval"):
        management.set_local_interval(management.edit("local"), interval)
    assert management.current() == before


async def test_local_diagnostics_have_freshness_and_no_values_or_credentials(
    hass, prepared
):
    p = prepared
    await ready(hass, p)
    source = p.runtime.connection_driver("local").sources["local"]
    report = source.diagnostics()
    assert report["identity_confirmed"]
    assert report["resources"][0]["available"]
    assert report["resources"][0]["path"] == REQUEST.path
    text = json.dumps(report)
    for private in (
        "synthetic-local-token",
        "gateway.example.invalid",
        "000123",
        "private-owner",
        '"value"',
        '"data"',
    ):
        assert private not in text
    assert len(p.clients[0].calls) == 1


def test_refresh_after_write_uses_bounded_explicit_reads() -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from custom_components.bosch_buderus_heating.local_transport import (
        StaleLocalRequest,
    )
    from custom_components.bosch_buderus_heating.managed_local import LocalSource

    source = LocalSource(SimpleNamespace(closed=False), "b", SimpleNamespace())
    paths = (
        "/heatingCircuits/hc1/currentRoomSetpoint",
        "/heatingCircuits/hc1/maxFlowTemp",
        "/heatingCircuits/hc1/overallStatus",
        "/heatingCircuits/hc1/currentSuWiMode",
        "/dhwCircuits/dhw1/actualTemp",
        "/dhwCircuits/dhw1/currentSetpoint",
        "/not/a/local/path",
    )
    assert source.refresh(paths) == 0  # not running yet
    source.context = SimpleNamespace(configuration_current=True)
    source.service = Mock()
    assert source.refresh(paths) == 6
    sizes = [len(call.args[0]) for call in source.service.request.call_args_list]
    assert sizes == [4, 2]
    source.service.request.side_effect = ValueError("local_explicit_queue_full")
    assert source.refresh(paths) == 0
    source.service.request.side_effect = StaleLocalRequest()
    assert source.refresh(paths) == 0


# ------------------------------------------- health history (O80, O93, O94)


def test_condition_changes_are_bounded_and_a_recovery_is_logged_once(
    caplog, monkeypatch
):
    import logging

    source = module.LocalSource(SimpleNamespace(closed=False), "local", None)
    now = [100.0]
    monkeypatch.setattr(module, "monotonic", lambda: now[0])
    caplog.set_level(logging.INFO, logger=module.__name__)
    source._note(LocalCondition.INITIALIZING, None)
    source._note(LocalCondition.READY, None)
    assert not caplog.records  # a normal start is no recovery
    source._note(LocalCondition.PAUSED, "rate_limited")
    now[0] += 30
    source._note(LocalCondition.PAUSED, "rate_limited")  # unchanged: not kept
    source._note(LocalCondition.INITIALIZING, None)
    now[0] += 15
    source._note(LocalCondition.READY, None)
    assert [record.getMessage() for record in caplog.records] == [
        "The local access is available again after 45 s"
    ]
    assert [item["condition"] for item in source._changes] == [
        "initializing",
        "ready",
        "paused",
        "initializing",
        "ready",
    ]
    assert source._changes[2]["reason"] == "rate_limited"
    assert set(source._changes[0]) == {"at", "condition", "reason"}
    for _ in range(30):
        source._note(LocalCondition.UNAVAILABLE, "local_poll_failed")
        source._note(LocalCondition.READY, None)
    assert len(source._changes) == module.CONDITION_CHANGES
    assert len(caplog.records) == 31


def test_diagnostics_show_the_read_signal_list_and_bounded_health() -> None:
    from custom_components.bosch_buderus_heating.local_api.catalog import (
        request_for_path,
    )
    from custom_components.bosch_buderus_heating.local_polling.inventory import (
        ResourceView,
    )

    def view(path, freshness, observation):
        return ResourceView(
            request_for_path(path), freshness, False, None, 900, None, None, observation
        )

    snapshot = SimpleNamespace(
        resources=(
            view("/signals", Freshness.UNCONFIRMED, object()),
            view("/gateway/brand", Freshness.UNCONFIRMED, None),
            view("/gateway/versionFirmware", Freshness.LATE, object()),
        ),
        discovery_cursor=3,
        discovery_exhausted=False,
        discovery_pause_seconds=0,
        gateway_pause_seconds=0,
    )
    source = module.LocalSource(SimpleNamespace(closed=False), "local", None)
    source.context = SimpleNamespace(
        configuration_current=True, runtime=SimpleNamespace(local_state=lambda _: None)
    )
    source.service = SimpleNamespace(snapshot=lambda: snapshot)
    report = source.diagnostics()
    assert [item["freshness"] for item in report["resources"]] == [
        "read",
        "unconfirmed",
        "late",
    ]
    assert report["condition_changes"] == []
    assert report["failures_by_class"] == {} and report["deferred_reasons"] == {}
    assert report["response_times"] == {"samples": 0}
