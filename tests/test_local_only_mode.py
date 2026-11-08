"""P49: a switched-off cloud access shows local readings also after a restart.

The managed entry runs with its real platforms and the real PointT client
against a synthetic cloud. A first run stores the inventory; a second run
starts with the cloud access switched off. The entities must come back from
that inventory with local readings, without a single cloud request.
"""

import importlib
import logging
from datetime import timedelta
from unittest.mock import patch

import pytest

from custom_components.bosch_buderus_heating.connection_runtime import (
    ConnectionPhase,
    SourceContext,
)
from tests.test_connection_runtime import settle
from tests.test_local_backed import (
    GATEWAY,
    SENSOR,
    ReadingBackend,
    local_values,
    publish_local,
    started,
    state,
)
from tests.test_managed_platforms import native
from tests.test_managed_pointt import eventually, running, source_for

INVENTORY = importlib.import_module(
    "custom_components.bosch_buderus_heating.discovery_inventory"
)
pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")


@pytest.fixture(autouse=True)
def loaded_integration(hass):
    from custom_components.bosch_buderus_heating.const import DOMAIN

    hass.config.components.add(DOMAIN)


def cloud_off(*, local_access=True, binding=True):
    base = local_values(local_access=local_access)

    def configure(data, options):
        base(data, options)
        for key, connection in data["connections"].items():
            if connection["kind"] == "singlekey_cloud":
                options["connections"][key]["enabled"] = False
        if not binding:
            for key, item in data["bindings"].items():
                if item["adapter"] == "pointt":
                    options["bindings"][key]["enabled"] = False

    return configure


async def first_run(hass, backend):
    async with running(hass, backend, configure=local_values()) as (_, _, owner, _):
        await started(hass, owner)
        assert source_for(owner, GATEWAY).inventory.resources(GATEWAY)


async def test_a_restart_with_the_cloud_switched_off_shows_local_values(
    hass, caplog
) -> None:
    backend = ReadingBackend()
    await first_run(hass, backend)
    calls = len(backend.calls)
    caplog.set_level(logging.INFO)
    async with running(hass, backend, configure=cloud_off()) as (_, cloud, owner, _):
        await owner.start_platforms()
        assert owner.connections.connection_phase(cloud) is ConnectionPhase.DISABLED
        await eventually(lambda: native(owner, "sensor", SENSOR))
        publish_local(owner, 22.5)
        await eventually(lambda: state(hass, owner) == "22.5")
        retained = next(iter(owner.connections._retained.values()))[0]
        assert retained.startup_mode == "inventory"
        assert retained.local_retained
        assert retained.update_interval is None
        # A manual refresh requests nothing either.
        await retained.async_refresh()
        await settle(hass)
        assert len(backend.calls) == calls  # not a single cloud request
    messages = [r for r in caplog.records if "switched off;" in r.getMessage()]
    assert len(messages) == 1  # announced once, after the view is kept
    assert messages[0].levelno == logging.INFO


async def test_switching_the_cloud_on_again_takes_over(hass) -> None:
    backend = ReadingBackend()
    await first_run(hass, backend)
    async with running(hass, backend, configure=cloud_off()) as (_, cloud, owner, _):
        await owner.start_platforms()
        await eventually(lambda: owner.connections._retained)
        dormant = next(iter(owner.connections._retained.values()))[0]
        await eventually(lambda: native(owner, "sensor", SENSOR))
        sensor = native(owner, "sensor", SENSOR)
        publish_local(owner, 21.0)
        await eventually(lambda: state(hass, owner) == "21.0")
        management = owner.connections.management
        management.set_enabled(management.edit(cloud), True)
        await eventually(
            lambda: (
                (item := source_for(owner, GATEWAY)) is not None and item is not dormant
            )
        )
        await eventually(lambda: not dormant.local_retained)
        assert not owner.connections._retained
        await eventually(lambda: native(owner, "sensor", SENSOR) is not None)
        assert native(owner, "sensor", SENSOR).entity_id == sensor.entity_id
        await eventually(lambda: state(hass, owner) == "21.0")  # local first


async def test_without_a_local_access_nothing_is_shown(hass) -> None:
    backend = ReadingBackend()
    await first_run(hass, backend)
    configure = cloud_off(local_access=False)
    async with running(hass, backend, configure=configure) as (_, _, owner, _):
        await owner.start_platforms()
        await settle(hass)
        assert not owner.connections._retained
        assert native(owner, "sensor", SENSOR) is None


async def test_a_deselected_system_gets_no_view(hass) -> None:
    backend = ReadingBackend()
    await first_run(hass, backend)
    configure = cloud_off(binding=False)
    async with running(hass, backend, configure=configure) as (_, _, owner, _):
        await owner.start_platforms()
        await settle(hass)
        assert not owner.connections._retained


async def test_after_its_age_limit_the_local_readings_take_over(hass, caplog) -> None:
    """S1 (P49b): the stored inventory for 30 days, then the local readings."""
    from custom_components.bosch_buderus_heating import managed_pointt
    from custom_components.bosch_buderus_heating.managed_local_plant import (
        LocalPlantCoordinator,
    )

    managed_pointt._OLD_INVENTORY_REPORTED.clear()
    backend = ReadingBackend()
    await first_run(hass, backend)
    calls = len(backend.calls)
    caplog.set_level(logging.INFO)
    with patch.object(INVENTORY, "MAX_AGE", timedelta(0)):
        async with running(hass, backend, configure=cloud_off()) as (_, _, owner, _):
            await owner.start_platforms()
            await eventually(lambda: owner.connections._retained)
            view = next(iter(owner.connections._retained.values()))[0]
            assert isinstance(view, LocalPlantCoordinator)
            await eventually(lambda: "local" in owner.connections._contexts)
            publish_local(owner, 22.5)
            await eventually(lambda: state(hass, owner) == "22.5")
            await settle(hass)
            assert len(backend.calls) == calls  # not a single cloud request
    assert not [r for r in caplog.records if "last 30 days" in r.getMessage()]
    shown = [r for r in caplog.records if "no recent inventory" in r.getMessage()]
    assert len(shown) == 1 and shown[0].levelno == logging.INFO


async def test_with_the_switch_off_an_old_inventory_is_not_used(hass, caplog) -> None:
    from custom_components.bosch_buderus_heating import managed_pointt

    managed_pointt._OLD_INVENTORY_REPORTED.clear()
    backend = ReadingBackend()
    await first_run(hass, backend)
    with (
        patch.object(managed_pointt, "LOCAL_PLANT_VIEW", False),
        patch.object(INVENTORY, "MAX_AGE", timedelta(0)),
    ):
        async with running(hass, backend, configure=cloud_off()) as (_, _, owner, _):
            await owner.start_platforms()
            await settle(hass)
            assert not owner.connections._retained
            assert native(owner, "sensor", SENSOR) is None
            # Said once, also when the settings change again.
            owner.connections.synchronize(owner.connections.snapshot)
            await settle(hass)
    reported = [r for r in caplog.records if "last 30 days" in r.getMessage()]
    assert len(reported) == 1
    assert reported[0].levelno == logging.WARNING


async def test_without_a_local_access_nothing_is_built(hass) -> None:
    from custom_components.bosch_buderus_heating import managed_pointt

    backend = ReadingBackend()
    await first_run(hass, backend)
    configure = cloud_off(local_access=False)
    with patch.object(managed_pointt.PointTSource, "_retain_from_inventory") as retain:
        async with running(hass, backend, configure=configure) as (_, _, owner, _):
            await owner.start_platforms()
            await settle(hass)
    retain.assert_not_called()


async def test_a_dormant_connection_leaves_the_sign_in_untouched(hass) -> None:
    from custom_components.bosch_buderus_heating import managed_cloud

    backend = ReadingBackend()
    await first_run(hass, backend)
    with patch.object(
        managed_cloud, "entry_auth", side_effect=AssertionError("sign-in used")
    ):
        async with running(hass, backend, configure=cloud_off()) as (_, _, owner, _):
            await owner.start_platforms()
            await eventually(lambda: owner.connections._retained)


async def test_switching_the_local_access_on_later_builds_the_view(hass) -> None:
    backend = ReadingBackend()
    await first_run(hass, backend)
    configure = cloud_off(local_access=False)
    async with running(hass, backend, configure=configure) as (_, _, owner, _):
        await owner.start_platforms()
        await settle(hass)
        assert not owner.connections._retained
        management = owner.connections.management
        local_connection = owner.connections.snapshot.bindings["local"].connection_id
        management.set_binding_enabled(management.edit(local_connection), "local", True)
        await eventually(lambda: owner.connections._retained)
        await eventually(lambda: native(owner, "sensor", SENSOR))


async def test_a_dormant_view_is_refused_while_the_access_is_on(hass) -> None:
    backend = ReadingBackend()
    async with running(hass, backend, configure=local_values()) as (_, cloud, owner, _):
        await started(hass, owner)
        runtime = owner.connections
        binding_id = next(
            key
            for key, item in runtime.snapshot.bindings.items()
            if item.adapter == "pointt"
        )
        context = SourceContext(runtime, runtime._generations.peek(cloud, binding_id))
        coordinator = source_for(owner, GATEWAY)
        assert not runtime.retain_dormant(context, coordinator)  # access is on
        # A generation from before a change is refused as well.
        management = runtime.management
        management.set_enabled(management.edit(cloud), False)
        await settle(hass)
        runtime._retained.clear()
        assert not runtime.retain_dormant(context, coordinator)
        # A current generation is kept only while the access is switched off,
        # not after the configuration was rejected.
        fresh = SourceContext(runtime, runtime._generations.peek(cloud, binding_id))
        runtime._connection_phases[cloud] = ConnectionPhase.UNAVAILABLE
        assert not runtime.retain_dormant(fresh, coordinator)
        runtime._connection_phases[cloud] = ConnectionPhase.DISABLED
        assert runtime.retain_dormant(fresh, coordinator)
        runtime._retained.clear()


async def test_a_dormant_connection_never_requests(hass) -> None:
    from custom_components.bosch_buderus_heating.managed_cloud import CloudConnection
    from custom_components.bosch_buderus_heating.pointt.exceptions import (
        SourceStopped,
    )

    backend = ReadingBackend()
    async with running(hass, backend, configure=cloud_off()) as (
        entry,
        cloud,
        owner,
        _,
    ):
        runtime = owner.connections
        from custom_components.bosch_buderus_heating.auth_lifecycle import (
            connection_resources,
        )

        connection = CloudConnection(
            hass,
            entry,
            runtime,
            cloud,
            connection_resources(hass, entry.entry_id, cloud),
            dormant=runtime._generations.peek(cloud, "x"),
        )
        with pytest.raises(SourceStopped):
            connection.ensure_current()
        with pytest.raises(SourceStopped):
            await connection.gateways()


async def test_only_the_cloud_remove_dialog_recommends_switching_off(
    hass, enable_custom_integrations
) -> None:
    from homeassistant.data_entry_flow import FlowResultType

    from tests.test_managed_connections import choose_connection, entry_for

    entry, cloud, _ = entry_for(hass)
    for connection, step in (
        (cloud, "connection_remove_cloud"),
        ("local", "connection_remove"),
    ):
        result = await choose_connection(hass, entry, connection)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "connection_remove"}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == step
        await hass.config_entries.options.async_configure(
            result["flow_id"], {"confirm": False}
        )


def test_the_cloud_remove_dialog_is_translated() -> None:
    import json
    from pathlib import Path

    root = Path("custom_components/bosch_buderus_heating")
    for name in ("strings.json", "translations/en.json", "translations/de.json"):
        steps = json.loads((root / name).read_text(encoding="utf-8"))["options"]["step"]
        cloud = steps["connection_remove_cloud"]["description"]
        # R1 (6C-a): local readings stay; switching off is for a pause.
        assert "30" not in cloud
        if name.endswith("de.json"):
            assert "lokalen Werte" in cloud and "nur lesend" in cloud
            assert "schalte den Zugang" in cloud
        else:
            assert "local readings" in cloud and "read-only" in cloud
            assert "switch the access off" in cloud


async def test_a_change_during_the_build_builds_again(hass) -> None:
    from custom_components.bosch_buderus_heating.managed_cloud import (
        CloudDriverFactory,
    )

    backend = ReadingBackend()
    await first_run(hass, backend)
    original = CloudDriverFactory.dormant_view
    builds = []

    async def changing(self, binding_id, binding, context):
        builds.append(context)
        if len(builds) == 1:
            # Switched on and off again while this build is still running.
            runtime = self.runtime()
            runtime._generations._bindings[binding_id] = (
                runtime._generations._bindings.get(binding_id, 0) + 1
            )
        await original(self, binding_id, binding, context)

    with patch.object(CloudDriverFactory, "dormant_view", changing):
        async with running(hass, backend, configure=cloud_off()) as (_, _, owner, _):
            await owner.start_platforms()
            await eventually(lambda: owner.connections._retained)
            assert len(builds) >= 2
