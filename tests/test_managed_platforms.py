"""Native HA platforms retain identities across independent source changes."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import Event
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers.trigger import TriggerConfig

from custom_components.bosch_buderus_heating.bacon.models import device_key
from tests.ha_test_helpers import MockConfigEntry

try:
    from homeassistant.components.event.trigger import EventReceivedTrigger
except ModuleNotFoundError:  # older Home Assistant releases have no event trigger
    EventReceivedTrigger = None
from custom_components.bosch_buderus_heating.configuration_access import (
    EntryConfiguration,
)
from custom_components.bosch_buderus_heating.const import DOMAIN, PLATFORMS
from custom_components.bosch_buderus_heating.managed_entry import (
    ManagedEntry,
    async_setup_managed_entry,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthTokens,
    PointTClient,
    Resource,
)
from tests.test_connection_runtime import Harness, entry_for, settle
from tests.test_managed_bacon import A, B, Broker, binding, connected, ready
from tests.test_managed_pointt import (
    TEMP,
    Backend,
    binding_for,
    eventually,
    running,
    source_for,
)
from tests.test_managed_wallbox import WallboxBackend

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
ROOT = "custom_components.bosch_buderus_heating"
HEAT = "000123"
NUMBER = HEAT + ":heatingCircuits:hc2:manualRoomSetpoint:control"


@pytest.fixture(autouse=True)
def loaded_integration(hass):
    # These transport fixtures start after the integration's normal setup phase.
    hass.config.components.add(DOMAIN)


def registered(hass, platform, unique_id):
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
    return registry.async_get(entity_id) if entity_id else None


def native(owner, platform, unique_id):
    item = registered(owner.hass, platform, unique_id)
    controller = owner.platforms._platforms.get(Platform(platform))
    return (
        controller.platform.entities.get(item.entity_id)
        if controller and item
        else None
    )


def available(owner, platform, unique_id):
    entity = native(owner, platform, unique_id)
    return entity if entity and entity.available else None


async def test_number_replacement_preserves_registry_and_writes_to_same_gateway(hass):
    backend = Backend()
    async with running(hass, backend) as (entry, cloud, owner, _):
        registry = er.async_get(hass)
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, HEAT)},
        )
        dr.async_get(hass).async_update_device(
            device.id, name_by_user="My heating system"
        )
        item = registry.async_get_or_create(
            "number",
            DOMAIN,
            NUMBER,
            config_entry=entry,
            device_id=device.id,
            suggested_object_id="my_room_temperature",
        )
        registry.async_update_entity(
            item.entity_id, name="My room", icon="mdi:home-thermometer"
        )
        await owner.start_platforms()
        await eventually(lambda: available(owner, "number", NUMBER))
        previous = native(owner, "number", NUMBER)
        peer_id = "gateway-b:heatingCircuits:hc2:manualRoomSetpoint:control"
        await eventually(lambda: available(owner, "number", peer_id))
        peer = native(owner, "number", peer_id)
        assert len(owner.platforms._platforms) == len(PLATFORMS)
        manager = owner.connections.management
        manager.set_binding_enabled(
            manager.edit(cloud), binding_for(owner, HEAT), False
        )
        await eventually(lambda: native(owner, "number", NUMBER) is None)
        assert hass.states.get(item.entity_id).state == "unavailable"
        assert native(owner, "number", peer_id) is peer and peer.available
        with pytest.raises(HomeAssistantError):
            await previous.async_set_native_value(21)
        manager.set_binding_enabled(manager.edit(cloud), binding_for(owner, HEAT), True)
        await eventually(lambda: available(owner, "number", NUMBER))
        current = native(owner, "number", NUMBER)
        assert current is not previous and current.entity_id == item.entity_id
        retained = registry.async_get(item.entity_id)
        assert retained.name == "My room" and retained.icon == "mdi:home-thermometer"
        assert retained.device_id == device.id
        assert (
            dr.async_get(hass).async_get(device.id).name_by_user == "My heating system"
        )
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": item.entity_id, "value": 22},
            blocking=True,
        )
        assert backend.values[HEAT] == 22 and backend.values["gateway-b"] == 20
        assert any(record.unique_id == NUMBER for record in owner.inventory.entities)


@pytest.mark.parametrize(
    "name,platform,suffix",
    [
        ("midea", "water_heater", "water_heater"),
        ("rwh", "water_heater", "water_heater"),
        ("cl_7000i", "climate", "climate"),
        ("air_6001i", "fan", "purifier_fan"),
        ("dry_8000i", "humidifier", "dry_dehumidifier"),
    ],
)
async def test_cloud_family_reconnects_native_entities_without_restarting_peer(
    hass, name, platform, suffix
):
    broker = Broker(name)
    async with connected(hass, broker) as (_, cloud, owner, _):
        await owner.start_platforms()
        uid = device_key("account-A", A) + ":" + suffix
        peer_uid = device_key("account-A", B) + ":" + suffix
        await eventually(lambda: ready(owner, A) and available(owner, platform, uid))
        await eventually(lambda: available(owner, platform, peer_uid))
        previous, peer = native(owner, platform, uid), native(owner, platform, peer_uid)
        entity_id = previous.entity_id
        manager = owner.connections.management
        manager.set_binding_enabled(manager.edit(cloud), binding(owner, A), False)
        await eventually(lambda: native(owner, platform, uid) is None)
        assert hass.states.get(entity_id).state == "unavailable"
        assert native(owner, platform, peer_uid) is peer and peer.available
        manager.set_binding_enabled(manager.edit(cloud), binding(owner, A), True)
        await eventually(lambda: available(owner, platform, uid))
        current = native(owner, platform, uid)
        assert current is not previous and current.entity_id == entity_id
        assert native(owner, platform, peer_uid) is peer
        assert len(broker.connections) == 1
        if platform == "water_heater":
            await hass.services.async_call(
                platform,
                "set_temperature",
                {"entity_id": entity_id, "temperature": 55},
                blocking=True,
            )
        else:
            await hass.services.async_call(
                platform, "turn_off", {"entity_id": entity_id}, blocking=True
            )
        assert len(broker.writes) == 1 and broker.writes[0][0] == A


async def test_wallbox_sensor_keeps_customization_and_peer(hass):
    backend = WallboxBackend()
    uid = "gateway-b:wallbox:cp0:power"
    async with running(hass, backend) as (entry, cloud, owner, _):
        registry = er.async_get(hass)
        item = registry.async_get_or_create("sensor", DOMAIN, uid, config_entry=entry)
        await owner.start_platforms()
        await eventually(lambda: available(owner, "sensor", uid))
        old = native(owner, "sensor", uid)
        registry.async_update_entity(item.entity_id, name="Garage power")
        registry.async_update_entity_options(
            item.entity_id, "sensor", {"display_precision": 2}
        )
        manager = owner.connections.management
        manager.set_binding_enabled(
            manager.edit(cloud), binding_for(owner, "gateway-b"), False
        )
        await eventually(lambda: native(owner, "sensor", uid) is None)
        await eventually(lambda: source_for(owner, HEAT))
        manager.set_binding_enabled(
            manager.edit(cloud), binding_for(owner, "gateway-b"), True
        )
        await eventually(lambda: available(owner, "sensor", uid))
        assert native(owner, "sensor", uid) is not old
        assert native(owner, "sensor", uid).entity_id == item.entity_id
        retained = registry.async_get(item.entity_id)
        assert (
            retained.name == "Garage power"
            and retained.options["sensor"]["display_precision"] == 2
        )


async def test_statistics_keep_legacy_ids_and_separate_cloud_connections(hass):
    backend = Backend()
    async with running(hass, backend, second_cloud=True) as (entry, cloud, owner, _):
        uid = entry.entry_id + ":request_metrics:pointt_api_requests_total"
        second_uid = (
            entry.entry_id
            + ":connection:second:request_metrics:pointt_api_requests_total"
        )
        for identity in (uid, second_uid):
            er.async_get(hass).async_get_or_create(
                "sensor", DOMAIN, identity, config_entry=entry
            )
        await owner.start_platforms()
        await eventually(
            lambda: (
                available(owner, "sensor", uid)
                and available(owner, "sensor", second_uid)
            )
        )
        first, second = (
            native(owner, "sensor", uid),
            native(owner, "sensor", second_uid),
        )
        assert first._metrics is not second._metrics
        other_count = second.native_value
        await source_for(owner, HEAT).client.get_resource(HEAT, TEMP)
        assert second.native_value == other_count
        assert first.unique_id == uid and second.unique_id == second_uid
        manager = owner.connections.management
        manager.set_enabled(manager.edit(cloud), False)
        await eventually(lambda: native(owner, "sensor", uid) is None)
        assert native(owner, "sensor", second_uid) is second and second.available


async def test_disabled_entity_stays_disabled_after_source_replacement(hass):
    backend = Backend()
    async with running(hass, backend) as (entry, cloud, owner, _):
        registry = er.async_get(hass)
        item = registry.async_get_or_create(
            "number",
            DOMAIN,
            NUMBER,
            config_entry=entry,
            disabled_by=er.RegistryEntryDisabler.USER,
        )
        await owner.start_platforms()
        await eventually(
            lambda: any(
                record.unique_id == NUMBER for record in owner.inventory.entities
            )
        )
        assert native(owner, "number", NUMBER) is None

        manager = owner.connections.management
        manager.set_binding_enabled(
            manager.edit(cloud), binding_for(owner, HEAT), False
        )
        await settle(hass)
        manager.set_binding_enabled(manager.edit(cloud), binding_for(owner, HEAT), True)
        await eventually(lambda: source_for(owner, HEAT))
        await settle(hass)
        assert (
            registry.async_get(item.entity_id).disabled_by
            is er.RegistryEntryDisabler.USER
        )
        assert hass.states.get(item.entity_id) is None
        assert native(owner, "number", NUMBER) is None


async def test_new_login_replaces_native_access_without_changing_entity_ids(hass):
    backend = Backend()
    async with running(hass, backend) as (entry, cloud, owner, _):
        await owner.start_platforms()
        await eventually(lambda: available(owner, "number", NUMBER))
        old = native(owner, "number", NUMBER)
        url = old.coordinator.client._transport._base_url
        renewed = AuthTokens(
            "replacement-access", "replacement-refresh", 10**12, scope=("bacon",)
        )
        manager = owner.connections.management
        with (
            patch(
                ROOT + ".configuration_management.PointTClient",
                side_effect=lambda *a, **kw: PointTClient(*a, **kw, base_url=url),
            ),
            patch(
                ROOT + ".configuration_management.discover",
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
                available(owner, "number", NUMBER)
                and native(owner, "number", NUMBER) is not old
            )
        )
        current = native(owner, "number", NUMBER)
        assert current.entity_id == old.entity_id
        assert current.coordinator.client._token_provider.tokens == renewed
        with pytest.raises(HomeAssistantError):
            await old.async_set_native_value(24)
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": current.entity_id, "value": 23},
            blocking=True,
        )
        assert backend.values[HEAT] == 23


async def test_last_connection_removed_then_offline_restart_retains_registry(hass):
    backend = Backend()
    async with running(hass, backend) as (entry, _, owner, _):
        await owner.start_platforms()
        await eventually(lambda: available(owner, "number", NUMBER))
        item = registered(hass, "number", NUMBER)
        manager = owner.connections.management
        for key in tuple(manager.current().connections):
            manager.remove(manager.edit(key))
        await eventually(lambda: native(owner, "number", NUMBER) is None)
        await owner.close()
        assert manager.current().targets
        saved = await owner.inventory._store.async_load()
        assert saved and any(
            record["unique_id"] == NUMBER for record in saved["entities"]
        )
        assert all(
            "value" not in record and "state" not in record
            for record in saved["entities"]
        )
        calls = len(backend.calls)
        restored = ManagedEntry(hass, entry, {})
        entry.runtime_data = restored
        try:
            await restored.start()
            await restored.start_platforms()
            assert registered(hass, "number", NUMBER).entity_id == item.entity_id
            assert hass.states.get(item.entity_id).state == "unavailable"
            assert hass.states.get(item.entity_id).attributes["supported_features"] == 0
            assert native(restored, "number", NUMBER) is None
            assert len(backend.calls) == calls
        finally:
            await restored.close()


async def test_disable_during_entity_addition_removes_late_instance_and_keeps_peer(
    hass,
):
    backend = Backend()
    entered, release = asyncio.Event(), asyncio.Event()
    original = EntityPlatform.async_add_entities

    async def delayed(platform, entities, *args, **kwargs):
        entities = list(entities)
        if any(entity.unique_id == NUMBER for entity in entities):
            entered.set()
            await release.wait()
        await original(platform, entities, *args, **kwargs)

    async with running(hass, backend) as (_, cloud, owner, _):
        with patch.object(EntityPlatform, "async_add_entities", delayed):
            try:
                await owner.start_platforms()
                await eventually(entered.is_set)
                peer_id = "gateway-b:heatingCircuits:hc2:manualRoomSetpoint:control"
                await eventually(lambda: available(owner, "number", peer_id))
                peer = native(owner, "number", peer_id)
                manager = owner.connections.management
                manager.set_binding_enabled(
                    manager.edit(cloud), binding_for(owner, HEAT), False
                )
            finally:
                release.set()
            await eventually(
                lambda: (
                    registered(hass, "number", NUMBER)
                    and native(owner, "number", NUMBER) is None
                )
            )
            assert not native(owner, "number", NUMBER)
            assert native(owner, "number", peer_id) is peer and peer.available
            assert (
                hass.states.get(registered(hass, "number", NUMBER).entity_id).state
                == "unavailable"
            )


class RichBackend(Backend):
    """Offer a synthetic gateway covering every existing PointT platform."""

    def __init__(self):
        super().__init__()
        self.extra = {
            "/heatingCircuits/hc2/operationMode": {
                "value": "manual",
                "allowedValues": ["off", "manual", "auto"],
            },
            "/dhwCircuits/dhw1/operationMode": {
                "value": "high",
                "allowedValues": ["Off", "low", "high"],
            },
            "/system/awayMode/enabled": {
                "value": "off",
                "allowedValues": ["on", "off"],
            },
            "/holidayMode/list": {"values": []},
            "/swCircuits/sw1/regeneration/enabled": {
                "value": "off",
                "allowedValues": ["on", "off"],
            },
            "/notifications": {"values": []},
        }

    def resource(self, gateway, path):
        if path in self.extra:
            return {
                "id": path,
                "type": "stringValue",
                "writeable": True,
                **self.extra[path],
            }
        result = super().resource(gateway, path)
        children = [p for p in self.extra if p.startswith(path.rstrip("/") + "/")]
        if children:
            result = result or {"references": []}
            result.setdefault("references", []).extend({"id": p} for p in children)
        if path == "/system":
            result.setdefault("references", []).append({"id": "/swCircuits/sw1"})
        return result


@pytest.mark.parametrize(
    "platform",
    [
        "binary_sensor",
        "button",
        "calendar",
        "climate",
        "event",
        "number",
        "select",
        "sensor",
        "switch",
        "water_heater",
    ],
)
async def test_each_pointt_platform_reattaches_only_its_source(hass, platform):
    backend = RichBackend()
    async with running(hass, backend) as (entry, cloud, owner, _):
        button_id = HEAT + ":swCircuits:sw1:regeneration:enabled:start"
        er.async_get(hass).async_get_or_create(
            "button", DOMAIN, button_id, config_entry=entry
        )
        await owner.start_platforms()

        def entities():
            controller = owner.platforms._platforms.get(Platform(platform))
            return (
                tuple(
                    entity
                    for entity in controller.platform.entities.values()
                    if entity.unique_id.startswith(HEAT + ":")
                )
                if controller
                else ()
            )

        await eventually(lambda: entities())
        before = {entity.unique_id: entity for entity in entities()}
        manager = owner.connections.management
        manager.set_binding_enabled(
            manager.edit(cloud), binding_for(owner, HEAT), False
        )
        await eventually(lambda: not entities())
        manager.set_binding_enabled(manager.edit(cloud), binding_for(owner, HEAT), True)
        await eventually(
            lambda: set(before).issubset(entity.unique_id for entity in entities())
        )
        for current in entities():
            if current.unique_id in before:
                old = before[current.unique_id]
                assert current is not old and current.entity_id == old.entity_id
                assert current.coordinator is not old.coordinator


@pytest.mark.parametrize("action", ["binding", "connection"])
@pytest.mark.parametrize("has_event", [False, True])
async def test_notification_event_survives_repeated_source_replacement(
    hass, action, has_event
):
    backend = RichBackend()
    uid = HEAT + ":system_notifications"
    active = Resource(
        path="/notifications", values=({"ccd": 6249, "fc": "12", "orig": "8"},)
    )
    empty = Resource(path="/notifications", values=())
    async with running(hass, backend, second_cloud=True) as (_, cloud, owner, _):
        await owner.start_platforms()
        await eventually(lambda: available(owner, "event", uid))
        peer_uid = "gateway-b:system_notifications"
        await eventually(lambda: available(owner, "event", peer_uid))
        peer = native(owner, "event", peer_uid)
        entity = native(owner, "event", uid)
        if has_event:
            entity.coordinator.faults.process_resources({active.path: active})
        entity_id = entity.entity_id
        previous = hass.states.get(entity_id)
        assert (previous.state == "unknown") is not has_event
        await hass.async_block_till_done()
        action_runner = Mock()
        remove_trigger = Mock()
        if EventReceivedTrigger is not None:
            trigger = EventReceivedTrigger(
                hass,
                TriggerConfig(
                    "event.received",
                    target={"entity_id": [entity_id]},
                    options={"event_type": ["appeared", "resolved"]},
                ),
            )
            remove_trigger = await trigger.async_attach_runner(action_runner)
        changes = []

        def changed(event: Event) -> None:
            if event.data["entity_id"] == entity_id:
                changes.append(event.data["new_state"])

        unsubscribe = hass.bus.async_listen("state_changed", changed)
        try:
            manager = owner.connections.management
            for _ in range(2):
                old = native(owner, "event", uid)

                def enable(value):
                    edit = manager.edit(cloud)
                    if action == "binding":
                        manager.set_binding_enabled(
                            edit, binding_for(owner, HEAT), value
                        )
                    else:
                        manager.set_enabled(edit, value)

                enable(False)
                await eventually(lambda: native(owner, "event", uid) is None)
                assert hass.states.get(entity_id).state == "unavailable"
                # A stopped source cannot supply an event to a replacement entity.
                old.coordinator.faults.process_resources({empty.path: empty})
                old.coordinator.faults.process_resources({empty.path: empty})
                backend.extra["/notifications"] = {
                    "values": list(active.values) if has_event else []
                }
                enable(True)
                await eventually(lambda: available(owner, "event", uid))
                current = native(owner, "event", uid)
                state = hass.states.get(current.entity_id)
                assert current is not old and current.entity_id == entity_id
                assert state.state == previous.state
                assert state.attributes == previous.attributes
                assert native(owner, "event", peer_uid) is peer
                old.coordinator.faults.process_resources({active.path: active})
                await hass.async_block_till_done()
                assert hass.states.get(entity_id) == state

            await hass.async_block_till_done()
            action_runner.assert_not_called()
            assert all(
                state is not None and state.state in {"unavailable", previous.state}
                for state in changes
            )
            current = native(owner, "event", uid)
            replacement_fault = Resource(
                path="/notifications", values=({"ccd": 1038, "fc": "12"},)
            )
            with patch.object(
                current, "_trigger_event", wraps=current._trigger_event
            ) as trigger:
                current.coordinator.faults.process_resources(
                    {replacement_fault.path: replacement_fault}
                )
                assert trigger.call_count == 1
            fresh = hass.states.get(entity_id)
            assert fresh.state not in {"unknown", "unavailable", previous.state}
            assert fresh.attributes["code"] == "1038"
            await hass.async_block_till_done()
            if EventReceivedTrigger is not None:
                action_runner.assert_called_once()
            assert not any(call[0] == "PUT" for call in backend.calls)
        finally:
            unsubscribe()
            remove_trigger()


async def test_partial_entity_addition_cleans_up_and_recovers_on_source_update(
    hass, caplog
):
    original = EntityPlatform.async_add_entities
    failed = []

    async def fail_once(platform, entities, *args, **kwargs):
        entities = list(entities)
        await original(platform, entities, *args, **kwargs)
        if not failed and any(entity.unique_id == NUMBER for entity in entities):
            failed.extend(entities)
            raise RuntimeError("Simulated interrupted entity setup")

    async with running(hass, Backend()) as (_, _, owner, _):
        with patch.object(EntityPlatform, "async_add_entities", fail_once):
            await owner.start_platforms()
            await eventually(
                lambda: "native entities could not be attached" in caplog.text
            )
            assert native(owner, "number", NUMBER) not in failed
            peer_id = "gateway-b:heatingCircuits:hc2:manualRoomSetpoint:control"
            await eventually(lambda: available(owner, "number", peer_id))
            source = source_for(owner, HEAT)
            context = owner.connections.source_context(binding_for(owner, HEAT))
            assert context.publish(source)
            await eventually(lambda: available(owner, "number", NUMBER))
            current = native(owner, "number", NUMBER)
            old = next(entity for entity in failed if entity.unique_id == NUMBER)
            assert current is not old and current.entity_id == old.entity_id
            assert available(owner, "number", peer_id)


async def test_registry_owned_by_another_entry_is_never_claimed(hass, caplog):
    other = MockConfigEntry(domain=DOMAIN, entry_id="other-owner")
    other.add_to_hass(hass)
    registry = er.async_get(hass)
    item = registry.async_get_or_create("number", DOMAIN, NUMBER, config_entry=other)
    async with running(hass, Backend()) as (_, _, owner, _):
        await owner.start_platforms()
        await eventually(lambda: "native entities could not be attached" in caplog.text)
        peer_id = "gateway-b:heatingCircuits:hc2:manualRoomSetpoint:control"
        await eventually(lambda: available(owner, "number", peer_id))
        assert native(owner, "number", NUMBER) is None
        assert registry.async_get(item.entity_id).config_entry_id == other.entry_id
        assert not any(
            record.unique_id == NUMBER for record in owner.inventory.entities
        )


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_platform_setup_failure_closes_started_connections(hass, error):
    entry, _, _ = entry_for(hass)
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    harness = Harness()
    original = hass.config_entries.async_forward_entry_setups

    async def fail_after_setup(*args, **kwargs):
        await original(*args, **kwargs)
        raise error()

    with (
        patch.object(
            hass.config_entries, "async_forward_entry_setups", fail_after_setup
        ),
        pytest.raises(error),
    ):
        await async_setup_managed_entry(
            hass,
            entry,
            {"singlekey_cloud": harness.factory, "k40_local": harness.factory},
        )
    owner = entry.runtime_data
    assert owner.connections._closed and not owner.connections._tasks
    assert owner.platforms._closed
    assert all(
        not controller.platform.entities and not controller._jobs
        for controller in owner.platforms._platforms.values()
    )
