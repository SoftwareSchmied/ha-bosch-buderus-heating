"""Complete native setup with the production identity policy and local runtime."""

import pytest

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.local_identity import CONTRACT
from tests.helpers.local_runtime import running_local_owner as running_local_owner
from tests.test_local_flow import imported, submit
from tests.test_local_flow import journal as journal
from tests.test_local_flow import supported as supported
from tests.test_managed_pointt import eventually

# Local setup and the first reads take several seconds on a loaded machine.
SLOW = 30

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")


@pytest.mark.parametrize(
    "failure,error",
    [
        ("conflict", "local_identity_conflict"),
        ("rate_limit", "local_cloud_rate_limited"),
    ],
)
async def test_native_setup_explains_failed_identity_without_saving(
    hass, running_local_owner, failure, error
):
    entry, management, target, backend, clients, _ = running_local_owner
    manager = hass.config_entries.options
    result = await imported(hass, entry, target)
    before = management.current()
    if failure == "conflict":
        backend.alias = "different-gateway"
    else:
        backend.status["identity"] = 429
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["errors"] == {"base": error}
    assert management.current() == before and not clients
    assert backend.metadata_calls == 1


async def test_native_setup_then_data_status_interval_and_offline_restart(
    hass, running_local_owner
):
    entry, management, target, backend, clients, probes = running_local_owner
    manager = hass.config_entries.options
    before_ids = set(hass.states.async_entity_ids())
    result = await imported(hass, entry, target)
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["step_id"] == "local_result"
    result = await submit(hass, manager, result, {"confirm": True})
    assert result["reason"] == "local_setup_complete"
    local = next(
        key
        for key, v in management.current().bindings.items()
        if v.adapter == "k40_local"
    )
    connection = management.current().bindings[local].connection_id
    await eventually(
        lambda: (
            entry.runtime_data.connections.local_state(local) is not None
            and entry.runtime_data.connections.local_state(local).current_observations
        ),
        timeout=SLOW,
    )
    assert len(probes) == 3 and backend.metadata_calls == 2
    assert (
        management.current().bindings[local].to_data()["link_evidence"]["contract"]
        == CONTRACT
    )
    assert set(hass.states.async_entity_ids()) == before_ids
    diagnostic = await async_get_config_entry_diagnostics(hass, entry)
    local_report = next(
        v for v in diagnostic["connections"] if v["kind"] == "k40_local"
    )
    assert any(v["available"] for v in local_report["local"][0]["resources"])
    pairs = diagnostic["observation_pairs"]
    assert pairs and pairs[0]["catalog_version"] == 1
    assert pairs[0]["local_observations"] >= 1
    assert set(pairs[0]) >= {"cloud_observations", "observed_pairs", "results"}
    assert "/" not in repr(pairs)

    # Exercise the actual options flow and per-access interval validation.
    result = await manager.async_init(entry.entry_id)
    result = await submit(hass, manager, result, {"next_step_id": "connections"})
    result = await submit(hass, manager, result, {"connection": connection})
    result = await submit(hass, manager, result, {"next_step_id": "local_polling"})
    assert int(result["description_placeholders"]["available"]) >= 1
    result = await submit(hass, manager, result, {"interval": 61})
    assert result["errors"] == {"interval": "local_invalid_interval"}
    result = await submit(hass, manager, result, {"interval": 300})
    assert result["type"] == "create_entry"
    await eventually(lambda: len(probes) == 4, timeout=SLOW)

    # Existing entities may display paired local readings after explicit opt-in.
    # The interval change restarts the sources: wait until the cloud one is back.
    await eventually(
        lambda: any(
            getattr(
                entry.runtime_data.connections.source_data(key), "local_selection", None
            )
            is not None
            for key in management.current().bindings
        ),
        timeout=SLOW,
    )
    coordinator = next(
        value
        for key in management.current().bindings
        if (value := entry.runtime_data.connections.source_data(key)) is not None
        and getattr(value, "local_selection", None) is not None
    )
    # Owner decision (0.9.0-alpha.2): on by default after the first local setup.
    assert coordinator.local_selection.enabled is True
    result = await manager.async_init(entry.entry_id)
    result = await submit(hass, manager, result, {"next_step_id": "connections"})
    result = await submit(hass, manager, result, {"connection": connection})
    result = await submit(hass, manager, result, {"next_step_id": "local_values"})
    assert result["data_schema"]({})["enabled"] is True
    result = await submit(hass, manager, result, {"enabled": False})
    assert result["type"] == "create_entry"
    await eventually(lambda: not coordinator.local_selection.enabled, timeout=SLOW)
    result = await manager.async_init(entry.entry_id)
    result = await submit(hass, manager, result, {"next_step_id": "connections"})
    result = await submit(hass, manager, result, {"connection": connection})
    result = await submit(hass, manager, result, {"next_step_id": "local_values"})
    assert result["step_id"] == "local_values"
    result = await submit(hass, manager, result, {"enabled": True})
    assert result["type"] == "create_entry"
    target_options = management.current().to_options()["targets"]
    assert any(item.get("local_values") is True for item in target_options.values())
    await eventually(lambda: coordinator.local_selection.enabled, timeout=SLOW)
    diagnostic = await async_get_config_entry_diagnostics(hass, entry)
    assert any(
        item["local_values"]["enabled"] for item in diagnostic["observation_pairs"]
    )
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1

    cloud = next(
        key
        for key, v in management.current().connections.items()
        if v.kind == "singlekey_cloud"
    )
    management.set_enabled(management.edit(cloud), False)
    management.set_enabled(management.edit(connection), False)
    await hass.async_block_till_done()
    management.set_enabled(management.edit(connection), True)
    await eventually(lambda: len(probes) == 5, timeout=SLOW)
    await eventually(
        lambda: (
            entry.runtime_data.connections.local_state(local) is not None
            and entry.runtime_data.connections.local_state(local).current_observations
        ),
        timeout=SLOW,
    )
    assert backend.metadata_calls == 2 and clients
