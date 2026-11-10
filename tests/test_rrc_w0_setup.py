"""W0 through the real setup: RRC write patience, counters and hooks.

An RRC 2.0 controller (``ct200-shapes.json``) and a K40 heat pump gateway
(anonymized capture) run in real coordinator cycles against the fake cloud
of ``tests/helpers/rrc_cloud.py``. The RRC variants reach only the room
controller (S11); the heat pump keeps its schedule, messages and entities.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from types import MappingProxyType
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_platform
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from custom_components.bosch_buderus_heating import rrc_platforms
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.control import track_rrc_entities
from custom_components.bosch_buderus_heating.coordinator import Freshness
from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.pointt import Gateway
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.rrc_contract_base import RrcContract
from custom_components.bosch_buderus_heating.rrc_write import RrcWritePending
from custom_components.bosch_buderus_heating.writes import (
    GENERIC_CONTEXT,
    RRC_CONTEXT,
    EnumWritePolicy,
    RrcWriteNotConfirmed,
)
from tests.helpers.rrc_cloud import Clock, RrcCloud, cloud_patches, load_tree
from tests.helpers.rrc_w0_state import w0_state
from tests.test_entry import _entry

PACKAGE = "custom_components.bosch_buderus_heating"
MANUAL = "/zones/zn1/manualTemperatureHeating"
AWAY = "/system/awayMode/enabled"
ECO = "/heatingCircuits/hc1/temperatureLevels/eco"
RRC_GATEWAY = Gateway("gateway-one", device_type="rrc2")
EMS_GATEWAY = Gateway("gateway-one", device_type="k40")

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture(autouse=True)
def legacy_setup_path():
    """Reach the format 3 setup without migrating, as test_startup_inventory."""
    with patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)):
        yield


def ct200() -> RrcCloud:
    return RrcCloud(load_tree("rrc/ct200-shapes.json"))


def k40() -> RrcCloud:
    return RrcCloud(
        load_tree("captures/k40-cloud-2026-11-05.json"),
        forbidden=set(),
        missing_as_500=False,
    )


@contextmanager
def installed(*contracts: RrcContract) -> Iterator[None]:
    """The W0 contracts (whatever the packages added since) and ``contracts``."""
    with w0_state(*contracts):
        yield


async def _setup(hass: HomeAssistant, cloud: RrcCloud, gateway: Gateway, clock):
    entry = _entry(hass)
    stack = cloud_patches(cloud, clock, gateway)
    stack.__enter__()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinators[0]
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    coordinator._write_service._sleep = sleep
    return entry, coordinator, sleeps, stack


async def _teardown(hass: HomeAssistant, entry, stack) -> None:
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stack.close()


def _entity_id(hass: HomeAssistant, domain: str, unique_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)
    assert entity_id is not None, unique_id
    return entity_id


async def _set_number(hass: HomeAssistant, entity_id: str, value: float) -> None:
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
    )


async def test_rrc_write_waits_longer_and_counts_a_late_confirmation(
    hass: HomeAssistant,
) -> None:
    """S7, S12, CT-C07: confirmed at the third read; unconfirmed after the
    fourth with exactly one PUT; the next regular poll confirms it late."""
    cloud, clock = ct200(), Clock()
    entry, coordinator, sleeps, stack = await _setup(hass, cloud, RRC_GATEWAY, clock)
    try:
        assert coordinator.control_context() is RRC_CONTEXT
        number = _entity_id(
            hass, "number", "gateway-one:zones:zn1:manualTemperatureHeating:control"
        )
        cloud.adopt_after[MANUAL] = 3
        await _set_number(hass, number, 21.5)
        assert sleeps == [1.0, 2.0, 4.0]
        assert cloud.calls["PUT", MANUAL] == 1
        assert hass.states.get(number).state == "21.5"

        sleeps.clear()
        cloud.adopt_after[MANUAL] = 5
        with pytest.raises(HomeAssistantError) as unconfirmed:
            await _set_number(hass, number, 22.0)
        assert unconfirmed.value.translation_key == "rrc_write_pending_confirmation"
        assert sleeps == [1.0, 2.0, 4.0, 8.0]
        assert cloud.calls["PUT", MANUAL] == 2
        assert coordinator.data[MANUAL].freshness is Freshness.STALE
        assert hass.states.get(number).state == "unavailable"

        clock.now += 1200
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(number).state == "22.0"
        assert cloud.calls["PUT", MANUAL] == 2  # never sent again

        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        features = diagnostics["gateways"][0]["rrc_features"]
        assert features["confirmation"] == "unconfirmed"
        assert features["read_back_delays"] == [1.0, 2.0, 4.0, 8.0]
        assert features["awaiting_late_confirmation"] == 0
        assert set(features["contracts"]) == {
            "zone",
            "absence",
            "dhw",
            "circuit",
            "system",
            "devices",
            "core",
        }
        writes = features["contracts"]["core"]["writes"]
        assert writes == {
            "writes_sent": 2,
            "writes_confirmed": 1,
            "writes_unconfirmed": 1,
            "late_confirmed": 1,
            "writes_rejected": 0,
            "awaiting_late_confirmation": 0,
            "rejection_reasons": {},
        }
        # W1 adds value-free counts of its zones next to the counters.
        zone = features["contracts"]["zone"]
        assert zone["confirmation"] == "unconfirmed"
        assert zone["writes"] == {
            **dict.fromkeys(writes, 0),
            "rejection_reasons": {},
        }
    finally:
        await _teardown(hass, entry, stack)


async def test_rrc_write_failure_paths_in_real_cycles(hass: HomeAssistant) -> None:
    """429 with Retry-After, 403, a timeout after the PUT and a 204 without
    effect: never a second PUT, the brake stays, counters without values."""
    cloud, clock = ct200(), Clock()
    entry, coordinator, sleeps, stack = await _setup(hass, cloud, RRC_GATEWAY, clock)
    number = _entity_id(
        hass, "number", "gateway-one:zones:zn1:manualTemperatureHeating:control"
    )
    try:
        # A timeout after an applied PUT: confirmed by reading alone.
        cloud.timeout_after_write[MANUAL] = True
        await _set_number(hass, number, 20.5)
        assert cloud.calls["PUT", MANUAL] == 1
        del cloud.timeout_after_write[MANUAL]

        # 204 without effect: unconfirmed, and no later reading confirms it.
        cloud.ignore_writes.add(MANUAL)
        with pytest.raises(HomeAssistantError) as ignored:
            await _set_number(hass, number, 23.0)
        assert ignored.value.translation_key == "rrc_write_pending_confirmation"
        assert cloud.calls["PUT", MANUAL] == 2
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(number).state == "20.5"
        assert coordinator.rrc_write_stats.snapshot("core")["late_confirmed"] == 0
        assert coordinator.rrc_write_stats.awaiting() == 1
        cloud.ignore_writes.clear()

        # 403 on the PUT: refused, never repeated.
        cloud.forbidden.add(MANUAL)
        with pytest.raises(HomeAssistantError) as refused:
            await _set_number(hass, number, 21.0)
        assert refused.value.translation_key == "write_failed"
        assert cloud.calls["PUT", MANUAL] == 3
        cloud.forbidden.discard(MANUAL)
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        # 429 with Retry-After on the PUT: the account brake stops the next
        # write before anything is sent.
        cloud.rate_limit_writes[MANUAL] = 120
        with pytest.raises(HomeAssistantError) as limited:
            await _set_number(hass, number, 21.0)
        assert limited.value.translation_key == "write_rate_limited"
        assert cloud.calls["PUT", MANUAL] == 4
        del cloud.rate_limit_writes[MANUAL]
        with pytest.raises(HomeAssistantError) as braked:
            await _set_number(hass, number, 21.0)
        assert braked.value.translation_key == "write_rate_limited"
        assert cloud.calls["PUT", MANUAL] == 4
        # After the pause, a 429 on the fresh read sends nothing at all.
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        cloud.rate_limit[MANUAL] = None
        with pytest.raises(HomeAssistantError):
            await _set_number(hass, number, 21.0)
        assert cloud.calls["PUT", MANUAL] == 4
        del cloud.rate_limit[MANUAL]

        core = coordinator.rrc_write_stats.snapshot("core")
        assert core["writes_confirmed"] == 1
        assert core["writes_unconfirmed"] == 1
        assert core["writes_rejected"] == 2
        assert core["rejection_reasons"] == {"rate_limited": 1, "request_failed": 1}
        assert sleeps.count(8.0) == 1  # only the ignored write waited it all
    finally:
        await _teardown(hass, entry, stack)


async def test_heat_pump_gateway_keeps_its_write_schedule_and_message(
    hass: HomeAssistant,
) -> None:
    cloud, clock = k40(), Clock()
    entry, coordinator, sleeps, stack = await _setup(hass, cloud, EMS_GATEWAY, clock)
    try:
        assert coordinator.control_context() is GENERIC_CONTEXT
        number = _entity_id(
            hass,
            "number",
            "gateway-one:heatingCircuits:hc1:temperatureLevels:eco:control",
        )
        cloud.ignore_writes.add(ECO)
        current = float(hass.states.get(number).state)
        with pytest.raises(HomeAssistantError) as unconfirmed:
            await _set_number(hass, number, current - 1)
        assert unconfirmed.value.translation_key == "write_not_confirmed"
        assert sleeps == [0.5, 1.0, 2.0]
        assert cloud.calls["PUT", ECO] == 1
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        assert "rrc_features" not in diagnostics["gateways"][0]
        assert coordinator.rrc_write_stats.snapshot("core")["writes_sent"] == 0
    finally:
        await _teardown(hass, entry, stack)


AWAY_VARIANT = EnumWritePolicy(
    rf"^{AWAY}$",
    frozenset({"stringValue"}),
    frozenset({"true", "false"}),
    known_only=True,
    fallback_options=("true", "false"),
    on_value="true",
    off_value="false",
)
CONTRACT = RrcContract(
    name="w_zero_probe",
    enum_policies=(AWAY_VARIANT,),
    names=MappingProxyType({rf"^{AWAY}$": ("Abwesenheit", "Away")}),
    entity_keys=MappingProxyType({rf"^{AWAY}$": "away_mode"}),
)


class ProbeSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, description: SensorEntityDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:w0_probe"
        self._attr_name = "Probe"


@pytest.mark.parametrize("plant", ["rrc", "ems"])
async def test_contract_variants_and_trackers_reach_only_room_controllers(
    hass: HomeAssistant, plant: str
) -> None:
    calls: Counter[Platform] = Counter()

    def tracker(platform: Platform):
        def track(entry, coordinator, add) -> None:
            calls[platform] += 1
            if platform is Platform.SENSOR:
                track_rrc_entities(
                    entry,
                    coordinator,
                    add,
                    lambda resources, context: (
                        (SensorEntityDescription(key="w0_probe"),)
                        if "/zones/zn1/temperatureActual" in resources
                        else ()
                    ),
                    ProbeSensor,
                )

        return track

    table = {platform: tracker(platform) for platform in rrc_platforms.PLATFORMS}
    cloud = ct200() if plant == "rrc" else k40()
    gateway = RRC_GATEWAY if plant == "rrc" else EMS_GATEWAY
    with (
        installed(CONTRACT),
        patch.object(rrc_platforms, "trackers", lambda: (("probe", table),)),
    ):
        entry, _coordinator, _sleeps, stack = await _setup(
            hass, cloud, gateway, Clock()
        )
        try:
            # Every platform hook runs once for the coordinator.
            assert calls == dict.fromkeys(rrc_platforms.PLATFORMS, 1)
            platforms = {
                item.domain
                for item in entity_platform.async_get_platforms(hass, DOMAIN)
            }
            assert "time" in platforms
            # The same unique ID: the contract switch or the generic one.
            switch = _entity_id(
                hass, "switch", "gateway-one:system:awayMode:enabled:control"
            )
            await hass.services.async_call(
                "switch", "turn_on", {"entity_id": switch}, blocking=True
            )
            probe = er.async_get(hass).async_get_entity_id(
                "sensor", DOMAIN, "gateway-one:w0_probe"
            )
            if plant == "rrc":
                # The contract's string switch, named by the contract.
                assert cloud.written("PUT")[-1] == ("PUT", AWAY, "true")
                assert hass.states.get(switch).state == "on"
                assert probe is not None
                assert (
                    hass.states.get(switch).attributes["friendly_name"].endswith("Away")
                )
            else:
                # The heat pump keeps its generic away switch ("on"/"off").
                assert cloud.written("PUT")[-1] == ("PUT", AWAY, "on")
                assert probe is None
        finally:
            await _teardown(hass, entry, stack)


async def test_structured_write_runs_once_with_its_guard_in_real_cycles(
    hass: HomeAssistant,
) -> None:
    """A creation (W2's events): one POST, an unknown outcome blocks a second
    creation, the counters follow; the fake's list shows the entry late."""
    events = "/events/list1"
    cloud, clock = ct200(), Clock()
    cloud.tree[events] = parse_resource(
        {"id": events, "type": "eventArray", "writeable": 1, "value": []}
    )
    entry, coordinator, _sleeps, stack = await _setup(hass, cloud, RRC_GATEWAY, clock)
    client = coordinator.client
    struct_sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        struct_sleeps.append(delay)

    coordinator.rrc_struct_writes._sleep = sleep
    coordinator.rrc_struct_writes._clock = clock

    def add(value) -> None:
        listed = cloud.tree[events]
        cloud._settle(events, [*listed.value, *value])

    cloud.post_handlers["/events"] = add
    cloud.adopt_after[events] = 6

    def create(name: str):
        async def preflight():
            return await client.get_resource("gateway-one", events)

        async def send(_state) -> None:
            await client.post_resource("gateway-one", "/events", [{"name": name}])

        async def confirm(_state):
            current = await client.get_resource("gateway-one", events)
            return (current,) if {"name": name} in current.value else None

        async def operation():
            return await coordinator.rrc_struct_writes.async_write(
                preflight=preflight,
                send=send,
                confirm=confirm,
                guard="events",
                pending_reason="rrc_event_pending",
                on_sent=lambda: coordinator.note_rrc_write_sent(events),
            )

        return operation

    try:
        with pytest.raises(RrcWriteNotConfirmed):
            await coordinator.async_run_rrc_operation(
                events, create("first"), require_snapshot=False
            )
        assert cloud.calls["POST", "/events"] == 1
        assert struct_sleeps == [1.0, 2.0, 4.0, 8.0]
        with pytest.raises(RrcWritePending):
            await coordinator.async_run_rrc_operation(
                events, create("second"), require_snapshot=False
            )
        assert cloud.calls["POST", "/events"] == 1
        # The first entry shows at the sixth read: the guard opens.
        cloud.adopt_after[events] = 1
        await coordinator.async_run_rrc_operation(
            events, create("second"), require_snapshot=False
        )
        assert cloud.calls["POST", "/events"] == 2
        assert coordinator.data[events].resource.value == [
            {"name": "first"},
            {"name": "second"},
        ]
        # W2's absence contract owns the events paths.
        core = coordinator.rrc_write_stats.snapshot("absence")
        assert core["writes_sent"] == 2
        assert core["writes_confirmed"] == 1
        assert core["writes_unconfirmed"] == 1
        assert core["rejection_reasons"] == {"pending": 1}
    finally:
        await _teardown(hass, entry, stack)
