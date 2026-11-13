"""Managed local startup through a real loopback TLS server and read client."""

import pytest

from custom_components.bosch_buderus_heating import managed_local as module
from custom_components.bosch_buderus_heating.connection_runtime import (
    ConnectionPhase,
    ConnectionRuntime,
)
from custom_components.bosch_buderus_heating.local_api.catalog import request_for_path
from custom_components.bosch_buderus_heating.local_budget import local_budget_manager
from custom_components.bosch_buderus_heating.local_credentials import (
    StoredLocalAccess,
    import_local_token,
)
from custom_components.bosch_buderus_heating.local_polling import service as polling
from custom_components.bosch_buderus_heating.local_probe import LocalEndpoint, ProbePath
from custom_components.bosch_buderus_heating.local_state import LocalCondition
from tests.test_connection_runtime import Harness, entry_for
from tests.test_local_polling import REQUEST, no_discovery
from tests.test_local_probe import response
from tests.test_local_read import payload
from tests.test_local_transport import certificates as certificates
from tests.test_local_transport import gateways as gateways
from tests.test_local_transport import wire_reply
from tests.test_managed_local import wait_until


@pytest.mark.usefixtures("socket_enabled")
@pytest.mark.parametrize(
    "outcome", ["ready", "certificate_changed", "auth_required", "identity_changed"]
)
async def test_managed_source_real_tls_uses_confirmed_trust_before_reading(
    hass, monkeypatch, gateways, outcome, tmp_path
):
    manager = local_budget_manager(hass)
    manager._path = tmp_path / "budget.json"

    async def reply(raw, writer):
        path = raw.split(b" ", 2)[1].decode()
        if path == REQUEST.path:
            body = payload()
        else:
            gateway_id = (
                "other-gateway"
                if outcome == "identity_changed" and len(seen) > 3
                else "000123"
            )
            body = response(ProbePath(path), gatewayId=gateway_id).body
        writer.write(
            wire_reply(body, status=401 if outcome == "auth_required" else 200)
        )
        await writer.drain()

    _, seen, _, pin = await gateways(reply)
    entry, cloud, _ = entry_for(hass)
    data = dict(entry.data)
    data["connections"] = dict(data["connections"])
    access = StoredLocalAccess(
        import_local_token("synthetic-local-token"),
        LocalEndpoint("gateway.example.invalid"),
        pin.sha256.hex() if outcome != "certificate_changed" else "61" * 32,
        None,
        "000123",
        1,
    )
    data["connections"]["local"] = {**data["connections"]["local"], **access.to_data()}
    hass.config_entries.async_update_entry(entry, data=data, minor_version=2)
    # No start offset: the hinted reading is due at once and must start
    # without waiting for another occasion (next_wake).
    monkeypatch.setattr(polling, "start_offset", lambda scope: 0)
    original = module.LocalPollingService

    def service(*args, **kwargs):
        value = original(*args, **kwargs)
        value.store._path = tmp_path / value.store._path.name
        no_discovery(value.planner)
        request = (
            request_for_path(ProbePath.SYSTEM.value)
            if outcome == "identity_changed"
            else REQUEST
        )
        value.planner.hints((request,), value._clock())
        value.planner.epoch = value._clock()
        return value

    monkeypatch.setattr(module, "LocalPollingService", service)
    harness = Harness()
    runtime = ConnectionRuntime(
        hass,
        entry,
        {
            "singlekey_cloud": harness.factory,
            "k40_local": module.LocalDriverFactory(hass, entry, lambda: runtime),
        },
        sleep=harness.sleep,
    )
    runtime.start()
    try:
        expected = (
            LocalCondition.UNAVAILABLE
            if outcome == "identity_changed"
            else LocalCondition(outcome)
        )
        await wait_until(
            hass,
            lambda: (
                runtime.local_state("local") is not None
                and runtime.local_state("local").condition is expected
                and (
                    outcome != "ready"
                    or runtime.local_state("local").current_observations
                )
            ),
            limit_seconds=10,
        )
        state = runtime.local_state("local")
        assert runtime.connection_phase(cloud) is ConnectionPhase.READY
        if outcome == "ready":
            assert [r.split(b" ")[1].decode() for r in seen] == [
                p.value for p in ProbePath
            ] + [REQUEST.path]
            assert len(state.current_observations) == 1
            assert state.current_observations[0].request.path == REQUEST.path
        else:
            assert not state.current_observations
            assert (
                len(seen)
                == {
                    "auth_required": 1,
                    "certificate_changed": 0,
                    "identity_changed": 4,
                }[outcome]
            )
            if outcome == "identity_changed":
                assert state.problem == "local_identity_conflict"
        assert all(
            r.startswith(b"GET ")
            and b"Authorization: Bearer synthetic-local-token\r\n" in r
            for r in seen
        )
    except TimeoutError:
        source = runtime.connection_driver("local").sources["local"]
        pytest.fail(str(source.diagnostics()))
    finally:
        await runtime.close()
        await manager.close()
