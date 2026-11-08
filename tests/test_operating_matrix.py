"""P23: operating cases that combine several rules in one real cycle.

Each test runs the managed entry with its real platforms, the real PointT
HTTP client against a synthetic loopback cloud and, where needed, the real
local scheduler. They cover combinations that the topic tests check only in
isolation: registry identities, user choices and source changes.
"""

import asyncio
import itertools
import json
import logging
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import label_registry as lr
from homeassistant.util import dt as dt_util

from custom_components.bosch_buderus_heating import coordinator as coordinator_module
from custom_components.bosch_buderus_heating import managed_local
from custom_components.bosch_buderus_heating.cloud_pauses import (
    _rows,
    cloud_pause_store,
)
from custom_components.bosch_buderus_heating.connection_runtime import ConnectionPhase
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.gateway_identity import GatewayNamespace
from custom_components.bosch_buderus_heating.local_api.catalog import request_for_path
from custom_components.bosch_buderus_heating.local_budget import LocalReadBudget
from custom_components.bosch_buderus_heating.local_credentials import (
    StoredLocalAccess,
    import_local_token,
)
from custom_components.bosch_buderus_heating.local_only_sensor import (
    local_only_unique_id,
)
from custom_components.bosch_buderus_heating.local_polling import (
    service as service_module,
)
from custom_components.bosch_buderus_heating.local_probe import LocalEndpoint, ProbePath
from custom_components.bosch_buderus_heating.managed_entry import ManagedEntry
from tests.ha_test_helpers import MockConfigEntry
from tests.test_configuration import OWNER
from tests.test_connection_runtime import settle
from tests.test_entry_dispatch import legacy_entry, loopback
from tests.test_gateway_exchange import (
    NEW,
    OLD,
    SETPOINT,
    ExchangeBackend,
    _detected,
    _flow,
    _real_cycle,
)
from tests.test_local_backed import CURRENT, SENSOR, ReadingBackend, local_values
from tests.test_local_polling import no_discovery
from tests.test_local_probe import response
from tests.test_local_selection import local
from tests.test_local_transport import certificates as certificates
from tests.test_local_transport import gateways as gateways
from tests.test_local_transport import wire_reply
from tests.test_managed_inventory import _restart as inventory_restart
from tests.test_managed_platforms import NUMBER, available, native, registered
from tests.test_managed_pointt import (
    TEMP,
    Backend,
    binding_for,
    eventually,
    running,
    source_for,
)
from tests.test_source_isolation import paired_target
from tests.test_storage_migration import stored

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")

HEAT, PEER = "000123", "gateway-b"
PEER_NUMBER = PEER + ":heatingCircuits:hc2:manualRoomSetpoint:control"
SUPPLY = "/heatSources/currentSupplySetpoint"  # only the local access reads it
LOCAL_ONLY = local_only_unique_id(HEAT, SUPPLY)
STARTS = "/heatSources/numberOfStarts"
COUNTER = HEAT + ":heatSources:numberOfStarts"
_READ_TIMES = itertools.count(10_000.0)


@pytest.fixture(autouse=True)
def loaded_integration(hass):
    # These transport fixtures start after the integration's normal setup phase.
    hass.config.components.add(DOMAIN)


def entry_registry(hass, entry):
    """Everything the registries hold for one entry, by identity."""
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    return (
        {
            item.id: (
                item.entity_id,
                item.unique_id,
                item.device_id,
                item.area_id,
                frozenset(item.labels),
                item.disabled_by,
            )
            for item in entities
        },
        {
            item.id: (
                frozenset(item.identifiers),
                item.area_id,
                frozenset(item.labels),
                item.disabled_by,
            )
            for item in devices
        },
    )


def assert_no_duplicates(hass, entry):
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    keys = [(item.domain, item.unique_id) for item in entities]
    assert len(keys) == len(set(keys))
    entity_ids = [item.entity_id for item in entities]
    assert len(entity_ids) == len(set(entity_ids))
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    identifiers = [identifier for item in devices for identifier in item.identifiers]
    assert len(identifiers) == len(set(identifiers))


def customize(hass, entry, gateway, unique_id, platform="number"):
    """A user's areas and labels on the device and the entity of one system."""
    areas = ar.async_get(hass)
    room, office = (
        areas.async_get_or_create("Basement"),
        areas.async_get_or_create("Office"),
    )
    label = lr.async_get(hass).async_create("Heating " + gateway)
    devices, entities = dr.async_get(hass), er.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, gateway)}
    )
    devices.async_update_device(device.id, area_id=room.id, labels={label.label_id})
    item = entities.async_get_or_create(
        platform, DOMAIN, unique_id, config_entry=entry, device_id=device.id
    )
    # HA only accepts an own area for an entity with an own name.
    entities.async_update_entity(
        item.entity_id, name="My room", area_id=office.id, labels={label.label_id}
    )
    return (
        devices.async_get(device.id),
        entities.async_get(item.entity_id),
        (room.id, office.id),
        label.label_id,
    )


def assert_customized(hass, device, item, areas, label, *, unique_id=None):
    devices, entities = dr.async_get(hass), er.async_get(hass)
    kept = entities.async_get(item.entity_id)
    assert kept is not None and kept.id == item.id
    assert kept.unique_id == (unique_id or item.unique_id)
    assert kept.device_id == device.id and kept.name == "My room"
    assert kept.area_id == areas[1] and kept.labels == {label}
    current = devices.async_get(device.id)
    assert current is not None
    assert current.area_id == areas[0] and current.labels == {label}


async def replace_source(hass, owner, cloud, gateway):
    """Stop and start one system's cloud source: a new coordinator."""
    previous = source_for(owner, gateway)
    management = owner.connections.management
    management.set_binding_enabled(
        management.edit(cloud), binding_for(owner, gateway), False
    )
    await settle(hass)
    management.set_binding_enabled(
        management.edit(cloud), binding_for(owner, gateway), True
    )
    await eventually(
        lambda: (
            (current := source_for(owner, gateway)) is not None
            and current is not previous
        )
    )
    return source_for(owner, gateway)


async def exchanged(hass, entry, cloud, owner):
    """P50: the confirmed exchange of the old gateway for the new one."""
    binding = await _detected(hass, entry, cloud, owner)
    flow = await _flow(hass, entry, cloud, binding)
    assert (await flow.async_step_init())["step_id"] == "evidence_unknown"
    await flow.async_step_continue()
    result = await flow.async_step_continue_unverified(
        {"confirm": True, "confirm_unverified": True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, ManagedEntry)
    assert entry.runtime_data is not owner and owner._closed
    assert entry.data["bindings"][binding]["locator"]["value"] == NEW
    return binding


# ------------------------------------------- 1: areas, labels and registry ids


async def test_areas_labels_and_ids_survive_a_source_replacement(hass) -> None:
    backend = Backend()
    async with running(hass, backend) as (entry, cloud, owner, _):
        customized = customize(hass, entry, HEAT, NUMBER)
        await owner.start_platforms()
        await eventually(lambda: available(owner, "number", NUMBER))
        before = native(owner, "number", NUMBER)
        await replace_source(hass, owner, cloud, HEAT)
        await eventually(
            lambda: (
                (current := available(owner, "number", NUMBER)) is not None
                and current is not before
            )
        )
        assert_customized(hass, *customized)
        assert native(owner, "number", NUMBER).entity_id == customized[1].entity_id
        assert_no_duplicates(hass, entry)


# ------------------------------------------------- 2: a device I disabled


def assert_device_disabled(hass, owner, device, item):
    assert dr.async_get(hass).async_get(device.id).disabled_by is (
        dr.DeviceEntryDisabler.USER
    )
    entities = er.async_get(hass)
    assert entities.async_get(item.entity_id).disabled_by is (
        er.RegistryEntryDisabler.DEVICE
    )
    on_device = er.async_entries_for_device(
        entities, device.id, include_disabled_entities=True
    )
    assert on_device and all(entry.disabled_by is not None for entry in on_device)
    assert all(hass.states.get(entry.entity_id) is None for entry in on_device)
    assert native(owner, "number", NUMBER) is None
    # The other system is not affected.
    assert available(owner, "number", PEER_NUMBER)


async def test_a_device_i_disabled_stays_disabled_across_replacement_and_restart(
    hass, caplog
) -> None:
    backend = Backend()
    async with running(hass, backend) as (entry, cloud, owner, _):
        devices, entities = dr.async_get(hass), er.async_get(hass)
        device = devices.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, HEAT)}
        )
        item = entities.async_get_or_create(
            "number", DOMAIN, NUMBER, config_entry=entry, device_id=device.id
        )
        devices.async_update_device(device.id, disabled_by=dr.DeviceEntryDisabler.USER)
        await hass.async_block_till_done()
        await owner.start_platforms()
        await eventually(lambda: available(owner, "number", PEER_NUMBER))
        await eventually(lambda: source_for(owner, HEAT))
        await settle(hass)
        assert_device_disabled(hass, owner, device, item)

        await replace_source(hass, owner, cloud, HEAT)
        await settle(hass)
        assert_device_disabled(hass, owner, device, item)
        # The accesses keep running (documented): the source still reads.
        assert source_for(owner, HEAT).last_update_success

    # A restart with the same registry.
    async with running(hass, backend) as (entry, cloud, owner, _):
        await owner.start_platforms()
        await eventually(lambda: available(owner, "number", PEER_NUMBER))
        await eventually(lambda: source_for(owner, HEAT))
        await settle(hass)
        assert_device_disabled(hass, owner, device, item)
    # No crash: the integration logs no error. (Re-adding the synthetic entry
    # for the restart logs one in HA itself.)
    assert not [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR
        and record.name.startswith("custom_components.")
    ]


# ------------------------------------------- 7: two systems with the same name


async def test_two_systems_with_the_same_name_keep_apart(hass) -> None:
    backend = Backend()
    async with running(hass, backend) as (entry, _, owner, _):
        await owner.start_platforms()
        await eventually(
            lambda: (
                available(owner, "number", NUMBER)
                and available(owner, "number", PEER_NUMBER)
            )
        )
        first, second = (
            native(owner, "number", NUMBER),
            native(owner, "number", PEER_NUMBER),
        )
        devices = dr.async_get(hass)
        first_device = devices.async_get(registered(hass, "number", NUMBER).device_id)
        second_device = devices.async_get(
            registered(hass, "number", PEER_NUMBER).device_id
        )
        # Both systems report the same model: the same device name.
        assert first_device.name == second_device.name
        assert first_device.id != second_device.id
        assert (DOMAIN, HEAT) in first_device.identifiers
        assert (DOMAIN, PEER) in second_device.identifiers
        assert first.entity_id != second.entity_id
        assert_no_duplicates(hass, entry)

        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": second.entity_id, "value": 18},
            blocking=True,
        )
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": first.entity_id, "value": 23},
            blocking=True,
        )
        assert backend.values == {HEAT: 23, PEER: 18}
        assert [call for call in backend.calls if call[0] == "PUT"] == [
            ("PUT", PEER, TEMP),
            ("PUT", HEAT, TEMP),
        ]
        await settle(hass)
        assert hass.states.get(first.entity_id).state == "23.0"
        assert hass.states.get(second.entity_id).state == "18.0"


async def test_areas_labels_and_ids_survive_the_storage_migration(hass) -> None:
    entry = legacy_entry(hass)
    assert entry.version == 3
    customized = customize(hass, entry, HEAT, NUMBER)
    before = entry_registry(hass, entry)
    backend = Backend()
    async with loopback(backend):
        assert await hass.config_entries.async_setup(entry.entry_id)
        assert entry.version == 4
        owner = entry.runtime_data
        await eventually(lambda: available(owner, "number", NUMBER))
        await settle(hass)
        assert_customized(hass, *customized)
        # Nothing the user set up is replaced; the platforms only add.
        after = entry_registry(hass, entry)
        for registry_before, registry_after in zip(before, after, strict=True):
            assert {key: registry_after[key] for key in registry_before} == (
                registry_before
            )
        assert_no_duplicates(hass, entry)
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_areas_labels_and_ids_survive_the_gateway_exchange(hass) -> None:
    backend = ExchangeBackend()
    async with running(hass, backend) as (entry, cloud, owner, _):
        device, number, areas, label = customize(hass, entry, OLD, OLD + SETPOINT)
        await exchanged(hass, entry, cloud, owner)
        assert_customized(hass, device, number, areas, label, unique_id=NEW + SETPOINT)
        assert dr.async_get(hass).async_get(device.id).identifiers == {(DOMAIN, NEW)}
        await _real_cycle(hass, entry, backend, number)
        assert_customized(hass, device, number, areas, label, unique_id=NEW + SETPOINT)
        assert_no_duplicates(hass, entry)
        assert await hass.config_entries.async_unload(entry.entry_id)


# --------------------------- 8: reloads and a restart after the migration


async def rebuilt(hass, entry):
    """A restart: HA builds a new entry object from what is on disk."""
    assert await hass.config_entries.async_unload(entry.entry_id)
    fresh = MockConfigEntry(
        domain=DOMAIN,
        entry_id=entry.entry_id,
        unique_id=entry.unique_id,
        title=entry.title,
        version=entry.version,
        minor_version=entry.minor_version,
        data=deepcopy(dict(entry.data)),
        options=deepcopy(dict(entry.options)),
    )
    fresh.add_to_hass(hass)  # replaces the old object under the same id
    assert hass.config_entries.async_get_entry(entry.entry_id) is fresh
    assert await hass.config_entries.async_setup(fresh.entry_id)
    return fresh


async def test_reloads_and_a_restart_after_the_migration_keep_one_registry(
    hass,
) -> None:
    entry = legacy_entry(hass)
    backend = Backend()
    async with loopback(backend):
        assert await hass.config_entries.async_setup(entry.entry_id)
        assert entry.version == 4
        await eventually(lambda: available(entry.runtime_data, "number", NUMBER))
        await settle(hass)
        registry = entry_registry(hass, entry)
        assert registry[0] and registry[1]
        migrated = stored(entry)
        owners = [entry.runtime_data]
        for _ in range(2):
            assert await hass.config_entries.async_reload(entry.entry_id)
            owners.append(entry.runtime_data)
            await eventually(lambda: available(entry.runtime_data, "number", NUMBER))
            await settle(hass)
            assert entry_registry(hass, entry) == registry
            assert_no_duplicates(hass, entry)
        entry = await rebuilt(hass, entry)
        owners.append(entry.runtime_data)
        await eventually(lambda: available(entry.runtime_data, "number", NUMBER))
        await settle(hass)
        assert entry_registry(hass, entry) == registry
        assert_no_duplicates(hass, entry)
        assert stored(entry) == migrated
        assert len({id(owner) for owner in owners}) == 4
        assert all(owner._closed for owner in owners[:-1])
        assert not any(call[0] == "PUT" for call in backend.calls)
        assert await hass.config_entries.async_unload(entry.entry_id)


# ------------------------- 9: a write after the exchange goes to the new id


async def test_a_write_after_the_gateway_exchange_goes_to_the_new_gateway(
    hass,
) -> None:
    backend = ExchangeBackend()
    async with running(hass, backend) as (entry, cloud, owner, _):
        _, number, _, _ = customize(hass, entry, OLD, OLD + SETPOINT)
        await exchanged(hass, entry, cloud, owner)
        await _real_cycle(hass, entry, backend, number)
        since = len(backend.calls)
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": number.entity_id, "value": 23},
            blocking=True,
        )
        sent = backend.calls[since:]
        assert [call for call in sent if call[0] == "PUT"] == [("PUT", NEW, TEMP)]
        assert not any(OLD in call for call in sent)
        assert backend.values[NEW] == 23 and backend.values[OLD] == 20.0
        await settle(hass)
        assert hass.states.get(number.entity_id).state == "23.0"
        assert await hass.config_entries.async_unload(entry.entry_id)


# ------------- 3 and 4: local-only sensors and counters when the cloud stops


class CounterBackend(ReadingBackend):
    """Adds a start counter that only the cloud supplies so far."""

    def __init__(self):
        super().__init__()
        self.starts = {key: 1234 for key in self.gateways}

    def resource(self, gateway, path):
        if path == "/heatSources":
            return {"references": [{"id": STARTS}]}
        if path == STARTS:
            return {
                "id": STARTS,
                "type": "floatValue",
                "value": self.starts[gateway],
                "unitOfMeasure": "",
                "writeable": False,
            }
        return super().resource(gateway, path)


def publish(owner, readings):
    """Current local readings, as the local source publishes them."""
    runtime = owner.connections
    context = runtime._contexts["local"]
    runtime._local_states["local"] = SimpleNamespace(
        context=context.identity_context(GatewayNamespace.K40_LOCAL),
        current_observations=tuple(
            # The start counter has no unit, like the cloud's.
            local(
                path,
                value,
                monotonic=next(_READ_TIMES),
                **({"unitOfMeasure": ""} if path == STARTS else {}),
            )
            for path, value in readings.items()
        ),
    )
    runtime._notify()


@contextmanager
def calendar_jump(hass, seconds):
    """Move Home Assistant's calendar time forward; the steady clock stays."""
    offset = timedelta(seconds=seconds)

    class JumpedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + offset

    class JumpedDt:
        def __getattr__(self, name):
            return getattr(dt_util, name)

        @staticmethod
        def utcnow():
            return dt_util.utcnow() + offset

    pauses = cloud_pause_store(hass)
    wall = pauses._wall_clock
    with (
        patch.object(coordinator_module, "dt_util", JumpedDt()),
        patch.object(coordinator_module, "datetime", JumpedDatetime),
        patch.object(pauses, "_wall_clock", lambda: wall() + seconds),
    ):
        yield


def shown(hass, owner, unique_id):
    entity = native(owner, "sensor", unique_id)
    state = hass.states.get(entity.entity_id) if entity else None
    return state.state if state else None


async def stop_cloud(hass, backend, owner, cloud, case):
    """The cloud of the paired system stops answering in one of four ways."""
    coordinator = source_for(owner, HEAT)
    if case == "sign_in":
        coordinator._request_reauthentication()
        await eventually(
            lambda: (
                owner.connections.connection_phase(cloud)
                is ConnectionPhase.AUTH_REQUIRED
            )
        )
        await eventually(lambda: coordinator.local_retained)
    elif case == "cloud_off":
        management = owner.connections.management
        management.set_enabled(management.edit(cloud), False)
        await eventually(lambda: coordinator.local_retained)
    else:
        backend.status[HEAT] = 503 if case == "outage" else 429
        coordinator._next_update = dict.fromkeys(coordinator._poll_intervals, 0)
        await coordinator.async_refresh()
        assert not coordinator.last_update_success
    await settle(hass)
    return coordinator


@pytest.mark.parametrize("case", ["sign_in", "outage", "rate_limit", "cloud_off"])
async def test_local_only_sensors_keep_current_local_readings(hass, case) -> None:
    backend = ReadingBackend()
    async with running(hass, backend, configure=local_values()) as (
        _,
        cloud,
        owner,
        _,
    ):
        await owner.start_platforms()
        await eventually(lambda: native(owner, "sensor", SENSOR))
        publish(owner, {CURRENT: 23.5, SUPPLY: 34.5})
        await eventually(lambda: shown(hass, owner, LOCAL_ONLY) == "34.5")
        sensor = native(owner, "sensor", LOCAL_ONLY)
        await stop_cloud(hass, backend, owner, cloud, case)
        assert native(owner, "sensor", LOCAL_ONLY) is sensor
        assert shown(hass, owner, LOCAL_ONLY) == "34.5"
        calls = len(backend.calls)
        publish(owner, {CURRENT: 24.0, SUPPLY: 36.0})
        await eventually(lambda: shown(hass, owner, LOCAL_ONLY) == "36.0")
        assert shown(hass, owner, SENSOR) == "24.0"  # the paired reading too
        await settle(hass)
        assert len(backend.calls) == calls  # local readings start no cloud request
        # Without a current local reading there is nothing to show.
        publish(owner, {CURRENT: 24.5})
        await eventually(lambda: shown(hass, owner, LOCAL_ONLY) == "unavailable")
        assert shown(hass, owner, SENSOR) == "24.5"


@pytest.mark.parametrize("case", ["sign_in", "outage"])
async def test_counters_without_a_current_cloud_reading_show_nothing(
    hass, case
) -> None:
    backend = CounterBackend()
    async with running(hass, backend, configure=local_values()) as (
        _,
        cloud,
        owner,
        _,
    ):
        await owner.start_platforms()
        await eventually(lambda: shown(hass, owner, COUNTER) == "1234")
        entity = native(owner, "sensor", COUNTER)
        states = []
        remove = hass.bus.async_listen(
            "state_changed",
            lambda event: (
                states.append(event.data["new_state"].state)
                if event.data["entity_id"] == entity.entity_id
                and event.data["new_state"] is not None
                else None
            ),
        )
        try:
            coordinator = await stop_cloud(hass, backend, owner, cloud, case)
            if case == "sign_in":
                # A stopped connection ends its readings at once; counters
                # are unavailable even with a current local count.
                assert shown(hass, owner, COUNTER) == "unavailable"
                for value in (1200, 1240):
                    publish(owner, {CURRENT: 23.5, STARTS: value})
                    await settle(hass)
                    assert shown(hass, owner, COUNTER) == "unavailable"
                    current = native(owner, "sensor", COUNTER)
                    assert current is None or current.native_value is None
            else:
                # A failed cycle keeps the last good count within its limit.
                assert shown(hass, owner, COUNTER) == "1234"
                publish(owner, {CURRENT: 23.5, STARTS: 1200})  # lower: held
                await settle(hass)
                assert shown(hass, owner, COUNTER) == "1234"
                # Past the limit the count ends; the lower local count is
                # still no replacement.
                with calendar_jump(hass, 2 * 3600):
                    coordinator.async_update_listeners()
                    await settle(hass)
                    assert shown(hass, owner, COUNTER) == "unavailable"
                    current = native(owner, "sensor", COUNTER)
                    assert current.native_value is None
            numbers = [float(state) for state in states if state[:1].isdigit()]
            assert all(number >= 1234 for number in numbers)
            assert numbers == sorted(numbers)
        finally:
            remove()


# -------------------------- 6: a local certificate change while running


class SteadyClock:
    """The process' steady clock, moved forward on demand.

    The real read client stamps readings with the process clock; the polling
    schedule must never run behind it, or a reading would lie in its future.
    """

    def __init__(self):
        self.offset = 0.0

    def __call__(self):
        return monotonic() + self.offset

    def advance(self, seconds):
        self.offset += seconds


@pytest.fixture
async def tls_local(hass, monkeypatch, gateways, tmp_path):
    """The local access reads a real loopback TLS gateway on a steady test clock."""
    readings = {CURRENT: 23.5, SUPPLY: 34.5}

    async def reply(raw, writer):
        path = raw.split(b" ", 2)[1].decode()
        if path in readings:
            body = json.dumps(
                {
                    "id": path,
                    "type": "floatValue",
                    "writeable": 0,
                    "value": readings[path],
                    "unitOfMeasure": "C",
                }
            ).encode()
        else:
            body = response(ProbePath(path), gatewayId=HEAT).body
        writer.write(wire_reply(body))
        await writer.drain()

    _, seen, _, pin = await gateways(reply)
    clock = SteadyClock()

    async def budget(context, **kwargs):
        # Request spacing (0.25 s) on the real clock; polling on the test clock.
        return LocalReadBudget(context)

    monkeypatch.setattr(
        managed_local,
        "local_budget_manager",
        lambda hass: SimpleNamespace(budget=budget),
    )
    monkeypatch.setattr(service_module, "start_offset", lambda scope: 0)
    original = managed_local.LocalPollingService

    def service(*args, **kwargs):
        value = original(*args, **kwargs, clock=clock)
        value.store._path = tmp_path / value.store._path.name
        no_discovery(value.planner)
        value.planner.hints(tuple(request_for_path(path) for path in readings), clock())
        return value

    monkeypatch.setattr(managed_local, "LocalPollingService", service)

    def configure(data, options):
        target, _ = paired_target(data, options)
        options["targets"][target]["local_values"] = True
        access = StoredLocalAccess(
            import_local_token("synthetic-local-token"),
            LocalEndpoint("gateway.example.invalid"),
            pin.sha256.hex(),
            None,
            HEAT,
            1,
        )
        data["connections"]["local"] = {
            **data["connections"]["local"],
            **access.to_data(),
        }
        # Format of a confirmed local access (not a legacy one).
        hass.config_entries.async_update_entry(
            hass.config_entries.async_get_entry(OWNER), minor_version=2
        )

    return SimpleNamespace(configure=configure, seen=seen, clock=clock)


async def polled(owner, clock, predicate):
    """Let the local scheduler run on its test clock until ``predicate``.

    The test clock moves 20 s per real second: a local reading stays valid
    for 180 s, so a slow run cannot expire the first reading before the
    second one arrives.
    """
    async with asyncio.timeout(30):
        while not predicate():
            clock.advance(1)
            driver = owner.connections.connection_driver("local")
            source = driver.sources.get("local") if driver is not None else None
            if source is not None and source.service is not None:
                source.service.wake()
            await asyncio.sleep(0.05)


async def test_a_certificate_change_at_runtime_stops_only_the_local_source(
    hass, tls_local, certificates, tmp_path
) -> None:
    backend = ReadingBackend()
    async with running(hass, backend, configure=tls_local.configure, defaults=True) as (
        _,
        cloud,
        owner,
        _,
    ):
        await owner.start_platforms()
        await polled(
            owner,
            tls_local.clock,
            lambda: (
                shown(hass, owner, SENSOR) == "23.5"
                and shown(hass, owner, LOCAL_ONLY) == "34.5"
            ),
        )
        assert tls_local.seen and all(
            b"Authorization: Bearer synthetic-local-token\r\n" in request
            for request in tls_local.seen
        )
        source = owner.connections.connection_driver("local").sources["local"]
        # The gateway now presents another certificate (same key).
        certificates[0][0].load_cert_chain(
            tmp_path / "synthetic-2.pem", tmp_path / "synthetic-2.key"
        )
        sent = len(tls_local.seen)
        await polled(
            owner,
            tls_local.clock,
            lambda: (
                owner.connections.source_phase("local") is ConnectionPhase.AUTH_REQUIRED
            ),
        )
        issue = ir.async_get(hass).async_get_issue(
            DOMAIN, source._certificate_issue_id()
        )
        assert issue is not None
        assert issue.translation_key == "local_certificate_changed"
        # The paired reading falls back to the cloud, the local-only one ends.
        await eventually(lambda: shown(hass, owner, SENSOR) == "20.0")
        await eventually(lambda: shown(hass, owner, LOCAL_ONLY) == "unavailable")
        assert owner.connections.connection_phase(cloud) is ConnectionPhase.READY
        # No request (and so no token) reaches the new certificate.
        for _ in range(3):
            tls_local.clock.advance(600)
            source.service.wake()
            await settle(hass)
        assert len(tls_local.seen) == sent
        # The cloud carries on.
        coordinator = source_for(owner, HEAT)
        backend.values[HEAT] = 21.0
        coordinator._next_update = dict.fromkeys(coordinator._poll_intervals, 0)
        await coordinator.async_refresh()
        await eventually(lambda: shown(hass, owner, SENSOR) == "21.0")


# --------------------- 5: no write after a start from the stored inventory


async def test_a_write_after_an_inventory_start_is_refused_without_a_request(
    hass,
) -> None:
    backend = Backend()
    async with running(hass, backend) as (_, cloud, owner, _):
        await owner.start_platforms()
        await eventually(lambda: available(owner, "number", NUMBER))
        first = source_for(owner, HEAT)
        backend.status["gateways"] = 503
        backend.status[HEAT] = 503
        fresh = await inventory_restart(hass, owner, cloud, first)
        assert fresh.startup_mode == "inventory"
        await eventually(
            lambda: (
                (number := native(owner, "number", NUMBER)) is not None
                and number.coordinator is fresh
            )
        )
        await settle(hass)
        number = native(owner, "number", NUMBER)
        assert not number.available
        assert hass.states.get(number.entity_id).state == "unavailable"
        # The cloud could answer now, but nothing is confirmed yet.
        backend.status.clear()
        calls = len(backend.calls)
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": number.entity_id, "value": 22},
            blocking=True,
        )
        assert len(backend.calls) == calls
        with pytest.raises(ServiceValidationError) as error:
            await number.async_set_native_value(22)
        assert error.value.translation_key == "write_validation_failed"
        assert len(backend.calls) == calls
        assert backend.values[HEAT] == 20.0
        assert hass.states.get(number.entity_id).state == "unavailable"


# ------------------------------------- 10: a forward clock jump at runtime


async def test_a_forward_clock_jump_shortens_no_cloud_pause(hass) -> None:
    backend = Backend()
    async with running(hass, backend) as (entry, cloud, owner, _):
        await eventually(lambda: source_for(owner, HEAT) and source_for(owner, PEER))
        coordinator, peer = source_for(owner, HEAT), source_for(owner, PEER)
        backend.status[HEAT] = 429  # Retry-After: 900
        coordinator._next_update = dict.fromkeys(coordinator._poll_intervals, 0)
        await coordinator.async_refresh()
        backoff = coordinator.client.backoff
        before = backoff.remaining
        assert 800 < before <= 900
        backend.status.clear()
        pauses = cloud_pause_store(hass)
        key = (entry.entry_id, cloud)
        with calendar_jump(hass, 86400):
            calls = len(backend.calls)
            for item in (coordinator, peer):
                item._next_update = dict.fromkeys(item._poll_intervals, 0)
                await item.async_refresh()
            assert len(backend.calls) == calls  # the account pause holds
            assert before - 30 < backoff.remaining <= before
            assert coordinator.control_status()[0] == "rate_limited"
            # The remaining time a restart restores is not shortened either.
            saved = {
                (row["owner"], row["connection"]): row
                for row in pauses._snapshot()["pauses"]
            }
            assert before - 30 < saved[key]["remaining"] <= before
            restored = dict(_rows({"pauses": [saved[key]]}, pauses._wall_clock()))
            assert before - 30 < restored[key] <= before


async def test_a_forward_clock_jump_makes_no_counter_jump(hass) -> None:
    backend = CounterBackend()
    async with running(hass, backend, configure=local_values()) as (_, _, owner, _):
        await owner.start_platforms()
        await eventually(lambda: shown(hass, owner, COUNTER) == "1234")
        coordinator = source_for(owner, HEAT)
        publish(owner, {CURRENT: 23.5, STARTS: 1240})  # local first, not lower
        await eventually(lambda: shown(hass, owner, COUNTER) == "1240")
        entity_id = native(owner, "sensor", COUNTER).entity_id
        states = []
        remove = hass.bus.async_listen(
            "state_changed",
            lambda event: (
                states.append(event.data["new_state"].state)
                if event.data["entity_id"] == entity_id
                and event.data["new_state"] is not None
                else None
            ),
        )
        try:
            with calendar_jump(hass, 2 * 3600):
                # The old cloud count expires; the local count runs on the
                # steady clock and stays.
                coordinator.async_update_listeners()
                await settle(hass)
                assert shown(hass, owner, COUNTER) == "1240"
                for cloud_count in (1238, 1250):
                    backend.starts[HEAT] = cloud_count
                    coordinator._next_update = dict.fromkeys(
                        coordinator._poll_intervals, 0
                    )
                    await coordinator.async_refresh()
                    assert coordinator.last_update_success
                    await settle(hass)
                    assert shown(hass, owner, COUNTER) == "1240"
                publish(owner, {CURRENT: 23.5, STARTS: 1251})
                await eventually(lambda: shown(hass, owner, COUNTER) == "1251")
            numbers = [float(state) for state in states if state[:1].isdigit()]
            assert numbers == sorted(numbers) and all(n >= 1240 for n in numbers)
        finally:
            remove()
