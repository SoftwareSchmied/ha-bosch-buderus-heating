"""P47: a cloud failure at start uses the saved inventory (#104).

These tests run the real config-entry setup with every platform against a
synthetic cloud. Entities must exist and be unavailable during the failure,
fill in once the cloud answers, and have the same identities as after a
normal start.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.discovery_inventory import (
    MAX_AGE,
    discovery_inventory,
)
from custom_components.bosch_buderus_heating.pointt import (
    AccessTokenRejected,
    Gateway,
    PointTClient,
    RateLimited,
    RefreshTokenRejected,
    RequestTimeout,
    ResourceReference,
)
from tests.test_discovery_recovery import Clock, Cloud, container, leaf
from tests.test_entry import _entry
from tests.test_heating_controls import MODE, TARGET, scalar

GATEWAY = Gateway("gateway-one", model="MX400")
COORDINATOR = "custom_components.bosch_buderus_heating.coordinator"
PACKAGE = "custom_components.bosch_buderus_heating"
CIRCUIT = MODE.rsplit("/", 1)[0]
MODULATION = "/heatSources/actualModulation"

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture(autouse=True)
def legacy_setup_path():
    """Feature branch: reach the legacy (format 3) setup without migrating.

    Entries in format 4 start through managed sources; their inventory start
    is covered in test_managed_inventory.py.
    """
    with patch(
        "custom_components.bosch_buderus_heating.async_migrate_entry",
        AsyncMock(return_value=True),
    ):
        yield


def plant() -> dict:
    mode = scalar(MODE, "manual", options=("off", "manual", "auto"))
    return {
        "/heatingCircuits": container("/heatingCircuits", CIRCUIT),
        CIRCUIT: container(CIRCUIT, MODE, TARGET),
        MODE: mode,
        TARGET: scalar(TARGET, 19),
        "/heatSources": container("/heatSources", MODULATION),
        MODULATION: leaf(MODULATION, 40.0, "%"),
    }


class Setup:
    """The cloud, the clock and the patched client calls for one test."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.cloud = Cloud(plant())
        self.clock = Clock()
        self.gateway_list: list[Gateway] | Exception = [GATEWAY]
        self.gateway_calls = 0

    async def gateways(self):
        self.gateway_calls += 1
        if isinstance(self.gateway_list, Exception):
            raise self.gateway_list
        return tuple(self.gateway_list)

    def patches(self):
        return (
            patch(f"{COORDINATOR}.monotonic", self.clock),
            patch(f"{COORDINATOR}.fault_resource_candidates", return_value=()),
            patch.object(PointTClient, "get_gateways", side_effect=self.gateways),
            patch.object(
                PointTClient,
                "get_resources_bulk",
                AsyncMock(side_effect=self.cloud.get_resources_bulk),
            ),
            patch.object(
                PointTClient,
                "get_resource",
                AsyncMock(side_effect=self.cloud.get_resource),
            ),
            patch(f"{PACKAGE}.async_setup_notifications", AsyncMock(return_value=())),
        )


async def _run(setup: Setup, entry, body) -> None:
    patches = setup.patches()
    for item in patches:
        item.start()
    try:
        await body()
    finally:
        if entry.state is ConfigEntryState.LOADED:
            await setup.hass.config_entries.async_unload(entry.entry_id)
            await setup.hass.async_block_till_done()
        for item in reversed(patches):
            item.stop()


def _gateway_entities(hass: HomeAssistant, entry) -> dict[str, er.RegistryEntry]:
    return {
        item.unique_id: item
        for item in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
        if item.unique_id.startswith(f"{GATEWAY.gateway_id}:")
    }


async def _normal_start(hass: HomeAssistant, setup: Setup, entry) -> set[str]:
    """A previous start with a working cloud saves the inventory."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    identities = set(_gateway_entities(hass, entry))
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    return identities


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (RequestTimeout(), "gateway_list_timeout"),
        (RateLimited(retry_after=600), "gateway_list_rate_limited"),
    ],
)
async def test_cloud_outage_at_start_uses_the_saved_inventory(
    hass: HomeAssistant, caplog, failure, reason
) -> None:
    entry = _entry(hass)
    setup = Setup(hass)

    async def body() -> None:
        live = await _normal_start(hass, setup, entry)
        assert {"select", "number", "climate", "sensor"} <= {
            item.domain for item in _gateway_entities(hass, entry).values()
        }
        # Remove every registry entry: identities now come only from the build.
        registry = er.async_get(hass)
        for item in list(_gateway_entities(hass, entry).values()):
            registry.async_remove(item.entity_id)

        setup.gateway_list = failure
        setup.cloud.bulk_error = RequestTimeout()  # the whole cloud is down
        with caplog.at_level(logging.INFO):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert f"start-up failed temporarily ({reason}); starting with" in (caplog.text)
        assert GATEWAY.gateway_id not in caplog.text
        stored = _gateway_entities(hass, entry)
        assert set(stored) == live  # same identities as a normal start (N3)
        states = {
            item.entity_id: hass.states.get(item.entity_id)
            for item in stored.values()
            if item.disabled_by is None
        }
        assert {item.split(".")[0] for item in states} >= {"select", "climate"}
        assert all(state is not None for state in states.values())
        assert {state.state for state in states.values()} == {"unavailable"}
        assert not any(state.attributes.get("restored") for state in states.values())

        coordinator = entry.runtime_data.coordinators[0]
        report = coordinator.startup_inventory_diagnostics(setup.clock.now)
        assert report["mode"] == "inventory" and report["reason"] == reason
        assert report["inventory_age_hours"] == 0

        # A write sends nothing while nothing is confirmed. Home Assistant does
        # not call an unavailable entity; the coordinator rejects it as well.
        select = next(item for item in stored.values() if item.domain == "select")
        sent = len(setup.cloud.requests)
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": select.entity_id, "option": "auto"},
            blocking=True,
        )
        assert len(setup.cloud.requests) == sent
        assert hass.states.get(select.entity_id).state == "unavailable"
        entity = hass.data["entity_components"]["select"].get_entity(select.entity_id)
        # Not current (or rate limited): a translated error, no request.
        with pytest.raises(HomeAssistantError):
            await entity.async_select_option("auto")
        assert len(setup.cloud.requests) == sent

        if isinstance(failure, RateLimited):
            # The account brake from start-up holds: no request at all.
            await coordinator.async_refresh()
            assert len(setup.cloud.requests) == sent
            setup.clock.now = coordinator._cloud_backoff_until + 1

        # Still down: a cycle fails, entities stay unavailable, and the cycle
        # sends one bulk read plus the bounded core fallback, nothing more.
        before = len(setup.cloud.requests)
        await coordinator.async_refresh()
        assert not coordinator.last_update_success
        assert len(setup.cloud.requests) - before <= 1
        report = coordinator.startup_inventory_diagnostics(setup.clock.now)
        assert report["discovery_attempts"] == 0

        # The cloud answers again: values within one cycle, then discovery.
        setup.cloud.bulk_error = None
        setup.clock.now += 60
        with caplog.at_level(logging.INFO):
            await coordinator.async_refresh()
            await hass.async_block_till_done()
        assert coordinator.last_update_success
        modulation = next(
            item for item in stored.values() if "modulation" in item.unique_id.lower()
        )
        assert float(hass.states.get(modulation.entity_id).state) == 40.0
        assert hass.states.get(select.entity_id).state == "manual"
        report = coordinator.startup_inventory_diagnostics(setup.clock.now)
        assert report["discovery_attempts"] == 0  # values first

        # The next cycle discovers instead of polling: each reading once.
        setup.clock.now += 60
        with (
            caplog.at_level(logging.INFO),
            patch.object(
                coordinator.faults,
                "process_resources",
                wraps=coordinator.faults.process_resources,
            ) as faults,
        ):
            await coordinator.async_refresh()
            await hass.async_block_till_done()
        assert faults.call_count == 1
        assert coordinator.last_update_success
        report = coordinator.startup_inventory_diagnostics(setup.clock.now)
        assert report["discovery_attempts"] == 1
        assert report["discovery_result"] == "success"
        assert report["next_discovery_in_seconds"] is None
        assert "discovery completed after starting with the saved inventory" in (
            caplog.text
        )
        assert set(_gateway_entities(hass, entry)) == live
        assert setup.gateway_calls == 2  # no extra gateway-list retries

    await _run(setup, entry, body)


async def test_discovery_failure_at_start_uses_the_saved_inventory(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    setup = Setup(hass)

    async def body() -> None:
        live = await _normal_start(hass, setup, entry)
        setup.cloud.bulk_error = RequestTimeout()  # gateway list works
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        coordinator = entry.runtime_data.coordinators[0]
        report = coordinator.startup_inventory_diagnostics(setup.clock.now)
        assert report["reason"] == "discovery_timeout"
        assert set(_gateway_entities(hass, entry)) == live

        # The discovery after the first working cycle fails: retried later,
        # and the values from the working cycle stay.
        setup.cloud.bulk_error = None
        setup.cloud.request_errors["/system"] = RequestTimeout()
        setup.clock.now += 60
        await coordinator.async_refresh()  # poll works
        assert coordinator.last_update_success
        setup.clock.now += 60
        await coordinator.async_refresh()  # discovery fails
        assert coordinator.last_update_success
        assert coordinator.data[MODULATION].available
        report = coordinator.startup_inventory_diagnostics(setup.clock.now)
        assert report["discovery_result"] == "failed"
        assert report["next_discovery_in_seconds"] == 300
        assert coordinator.stored_resources()

        # A rate limit during the next discovery postpones it, no attempt used.
        del setup.cloud.request_errors["/system"]
        setup.cloud.down["/system"] = 429
        setup.clock.now += 300
        await coordinator.async_refresh()
        report = coordinator.startup_inventory_diagnostics(setup.clock.now)
        assert report["discovery_attempts"] == 1
        assert report["discovery_postponed"] == 1
        assert report["discovery_result"] == "postponed"
        assert report["next_discovery_in_seconds"] == math.ceil(
            coordinator._cloud_backoff_until - setup.clock.now
        )

        # After the brake: poll first, then the discovery succeeds.
        del setup.cloud.down["/system"]
        setup.clock.now = coordinator._cloud_backoff_until + 1
        await coordinator.async_refresh()
        setup.clock.now += 60
        await coordinator.async_refresh()
        report = coordinator.startup_inventory_diagnostics(setup.clock.now)
        assert report["discovery_result"] == "success"
        assert report["discovery_attempts"] == 2
        assert not coordinator.stored_resources()
        assert coordinator.startup_mode == "inventory"  # how it started
        assert coordinator.discovery_recovery.state == "complete"

    await _run(setup, entry, body)


async def test_without_a_usable_inventory_setup_retries_as_before(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    setup = Setup(hass)

    async def body() -> None:
        setup.gateway_list = RequestTimeout()
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_RETRY
        await hass.config_entries.async_unload(entry.entry_id)

        # Too old: also as before.
        inventory = discovery_inventory(hass, entry.entry_id)
        inventory.store.async_delay_save = lambda data, delay: None
        inventory.record(GATEWAY, plant())
        inventory.gateway(GATEWAY.gateway_id).saved_at = datetime.now(UTC) - (
            MAX_AGE + timedelta(days=1)
        )
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_RETRY
        await hass.config_entries.async_unload(entry.entry_id)

        # Known gateway, but no resources saved for it: as before.
        inventory.gateway(GATEWAY.gateway_id).saved_at = datetime.now(UTC)
        inventory.gateway(GATEWAY.gateway_id).resources.clear()
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_RETRY

    await _run(setup, entry, body)


async def test_rejected_sign_in_at_start_still_asks_for_sign_in(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    setup = Setup(hass)

    async def body() -> None:
        await _normal_start(hass, setup, entry)
        setup.gateway_list = RefreshTokenRejected("rejected")
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_ERROR
        flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        assert any(flow["context"]["source"] == "reauth" for flow in flows)

    await _run(setup, entry, body)


async def _inventory_start(hass: HomeAssistant, setup: Setup, entry):
    """Normal start, then a start during a cloud outage; returns the identities."""
    live = await _normal_start(hass, setup, entry)
    before = _gateway_entities(hass, entry)
    setup.gateway_list = RequestTimeout()
    setup.cloud.bulk_error = RequestTimeout()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data.coordinators[0]
    assert coordinator.startup_mode == "inventory"
    assert set(_gateway_entities(hass, entry)) == live
    # The cloud answers again: the first working cycle reads the values.
    setup.cloud.bulk_error = None
    setup.clock.now += 60
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.last_update_success
    report = coordinator.startup_inventory_diagnostics(setup.clock.now)
    assert report["discovery_attempts"] == 0  # the discovery comes next
    return coordinator, before


@pytest.mark.parametrize(
    "rejection",
    [AccessTokenRejected("rejected"), RefreshTokenRejected("rejected")],
    ids=["http_401", "refresh_rejected"],
)
async def test_rejected_sign_in_during_the_deferred_discovery_asks_for_sign_in(
    hass: HomeAssistant, rejection
) -> None:
    entry = _entry(hass)
    setup = Setup(hass)

    async def body() -> None:
        coordinator, before = await _inventory_start(hass, setup, entry)
        inventory = discovery_inventory(hass, entry.entry_id)
        stored = dict(inventory.resources(GATEWAY.gateway_id))
        # Only the discovery meets the rejection; the poll does not read /system.
        setup.cloud.request_errors["/system"] = rejection

        setup.clock.now += 60
        sent = len(setup.cloud.requests)
        await coordinator.async_refresh()  # the deferred discovery
        await hass.async_block_till_done()
        # The discovery stops at its first batch; nothing else is sent.
        assert len(setup.cloud.requests) - sent == 1
        assert "/system" in setup.cloud.requests[-1]
        assert not coordinator.last_update_success
        flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        assert any(flow["context"]["source"] == "reauth" for flow in flows)
        # No crash and no deletion: the entry stays loaded with every entity.
        assert entry.state is ConfigEntryState.LOADED
        current = _gateway_entities(hass, entry)
        assert set(current) == set(before)
        assert all(
            current[unique_id].entity_id == item.entity_id
            for unique_id, item in before.items()
        )
        states = {
            item.unique_id: hass.states.get(item.entity_id)
            for item in current.values()
            if item.disabled_by is None
        }
        assert states and all(state is not None for state in states.values())
        # Every control is unavailable, so nothing can be written.
        controls = {
            unique_id: state.state
            for unique_id, state in states.items()
            if current[unique_id].domain in {"climate", "number", "select", "switch"}
        }
        assert {"climate", "number", "select"} <= {
            current[unique_id].domain for unique_id in controls
        }
        assert set(controls.values()) == {"unavailable"}
        # Read-only readings follow the documented display rule: a reading of
        # the working cycle stays shown within its validity after a failed
        # cycle; everything else is unavailable. Nothing comes from storage.
        modulation = next(uid for uid in states if "modulation" in uid.lower())
        assert states[modulation].state == "40.0"
        assert {state.state for uid, state in states.items() if uid != modulation} == {
            "unavailable"
        }
        report = coordinator.startup_inventory_diagnostics(setup.clock.now)
        assert report["discovery_result"] != "success"
        assert inventory.resources(GATEWAY.gateway_id) == stored
        assert coordinator.discovery_recovery.state == "none"  # nothing compared

    await _run(setup, entry, body)


async def test_changed_plant_adds_the_new_part_after_an_inventory_start(
    hass: HomeAssistant,
) -> None:
    """The deferred discovery finds a circuit the inventory did not know."""
    entry = _entry(hass)
    setup = Setup(hass)
    added = "/heatingCircuits/hc4"
    added_mode, added_target = f"{added}/operationMode", f"{added}/manualRoomSetpoint"
    prefix = f"{GATEWAY.gateway_id}:heatingCircuits:hc4:"

    async def body() -> None:
        coordinator, before = await _inventory_start(hass, setup, entry)
        inventory = discovery_inventory(hass, entry.entry_id)
        assert added_mode not in inventory.resources(GATEWAY.gateway_id)
        # Meanwhile the plant gained a second circuit.
        tree = setup.cloud.tree
        tree["/heatingCircuits"] = container("/heatingCircuits", CIRCUIT, added)
        tree[added] = container(added, added_mode, added_target)
        tree[added_mode] = scalar(
            added_mode, "manual", options=("off", "manual", "auto")
        )
        tree[added_target] = scalar(added_target, 21)

        setup.clock.now += 60
        await coordinator.async_refresh()  # the deferred discovery
        await hass.async_block_till_done()
        assert coordinator.last_update_success
        report = coordinator.startup_inventory_diagnostics(setup.clock.now)
        assert report["discovery_result"] == "success"
        assert entry.runtime_data.coordinators[0] is coordinator  # no reload

        after = _gateway_entities(hass, entry)
        new = {uid: item for uid, item in after.items() if uid not in before}
        assert new and all(uid.startswith(prefix) for uid in new)
        assert {"climate", "number", "select"} <= {item.domain for item in new.values()}
        # Existing entities keep their identities and entity IDs.
        assert set(before) <= set(after)
        assert all(
            after[unique_id].entity_id == item.entity_id
            for unique_id, item in before.items()
        )
        components = hass.data["entity_components"]
        for item in new.values():
            if item.disabled_by is not None:
                continue
            assert components[item.domain].get_entity(item.entity_id) is not None
            state = hass.states.get(item.entity_id)
            assert state is not None and state.state != "unavailable"
        by_domain = {item.domain: item.entity_id for item in new.values()}
        assert hass.states.get(by_domain["select"]).state == "manual"
        assert float(hass.states.get(by_domain["number"]).state) == 21
        assert added_mode in inventory.resources(GATEWAY.gateway_id)
        # Another cycle adds nothing twice.
        setup.clock.now += 60
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert set(_gateway_entities(hass, entry)) == set(after)

    await _run(setup, entry, body)


async def test_normal_start_remembers_the_gateways_without_extra_requests(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    setup = Setup(hass)

    async def body() -> None:
        await _normal_start(hass, setup, entry)
        first = len(setup.cloud.requests)
        await _normal_start(hass, setup, entry)
        assert len(setup.cloud.requests) == 2 * first  # same discovery, no extra
        inventory = discovery_inventory(hass, entry.entry_id)
        assert inventory.startup_gateways([GATEWAY.gateway_id]) == (GATEWAY,)
        assert set(inventory.resources(GATEWAY.gateway_id)) == set(plant())
        assert inventory.age_seconds(GATEWAY.gateway_id) == 0
        assert inventory.startup_gateways(["unknown"]) is None
        assert inventory.age_seconds("unknown") is None
        # A changed gateway (for example new firmware) is kept up to date.
        newer = Gateway(GATEWAY.gateway_id, model="MX400", firmware_version="2")
        inventory.remember_gateways([newer])
        assert inventory.startup_gateways([GATEWAY.gateway_id]) == (newer,)
        assert inventory.gateway(GATEWAY.gateway_id).resources  # kept

    await _run(setup, entry, body)


def test_references_are_part_of_the_plant() -> None:
    # Guard for the synthetic plant used above.
    assert plant()[CIRCUIT].references == (
        ResourceReference(MODE),
        ResourceReference(TARGET),
    )


async def test_inventory_start_shows_no_stored_value_as_current(
    hass: HomeAssistant,
) -> None:
    from tests.test_discovery_recovery import _coordinator

    coordinator = _coordinator(hass, Cloud({}))
    seen = datetime(2026, 11, 1, tzinfo=UTC)
    coordinator.start_from_inventory(
        plant(),
        reason="gateway_list_timeout",
        age_seconds=7200,
        seen=dict.fromkeys(plant(), seen),
    )
    assert not coordinator.last_update_success
    assert set(coordinator.data) == set(plant())
    for snapshot in coordinator.data.values():
        assert not snapshot.available
        assert snapshot.freshness.value == "stale"
        assert snapshot.last_error_category == "startup_inventory"
        assert snapshot.last_success == seen
    assert coordinator.stored_resources() == plant()
    report = coordinator.startup_inventory_diagnostics(0.0)
    assert report["inventory_age_hours"] == 2
    assert report["mode"] == "inventory"


async def test_setup_remembers_listed_gateways_even_without_discovery(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    setup = Setup(hass)

    async def body() -> None:
        with patch(
            f"{COORDINATOR}.BoschBuderusDataUpdateCoordinator."
            "async_config_entry_first_refresh",
            AsyncMock(),
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
        inventory = discovery_inventory(hass, entry.entry_id)
        assert inventory.startup_gateways([GATEWAY.gateway_id]) == (GATEWAY,)
        assert inventory.resources(GATEWAY.gateway_id) == {}

    await _run(setup, entry, body)


async def test_start_up_value_batch_is_skipped_while_the_cloud_is_down(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    setup = Setup(hass)

    async def body() -> None:
        with patch(f"{PACKAGE}._async_load_startup_data", AsyncMock()) as startup_batch:
            await _normal_start(hass, setup, entry)
            assert startup_batch.await_count == 1
            setup.gateway_list = RequestTimeout()
            setup.cloud.bulk_error = RequestTimeout()
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            assert startup_batch.await_count == 1  # not again during the outage

    await _run(setup, entry, body)


async def test_regular_polls_keep_the_inventory_current(hass: HomeAssistant) -> None:
    """A month of uptime without discovery must not age the inventory out."""
    from tests.test_discovery_recovery import _coordinator, _inventory

    clock = Clock()
    inventory = _inventory(hass)
    cloud = Cloud(plant())
    with (
        patch(f"{COORDINATOR}.monotonic", clock),
        patch(f"{COORDINATOR}.fault_resource_candidates", return_value=()),
    ):
        coordinator = _coordinator(hass, cloud, inventory)
        await coordinator.async_refresh()  # discovery records the inventory
        gateway = coordinator.gateway
        stored = inventory.gateway(gateway.gateway_id)
        old = datetime.now(UTC) - MAX_AGE - timedelta(days=1)
        stored.saved_at = old
        for item in stored.resources.values():
            item.seen = old
        assert inventory.startup_gateways([gateway.gateway_id]) is None
        # Throttled: polls on the same day do not touch the store.
        clock.now += 3600
        await coordinator.async_refresh()
        assert stored.saved_at == old
        # A day later a successful poll confirms what it read.
        clock.now += 86400
        await coordinator.async_refresh()
        assert inventory.startup_gateways([gateway.gateway_id]) == (gateway,)
        assert MODULATION in inventory.resources(gateway.gateway_id)


async def test_a_listed_gateway_without_resources_stays_usable(
    hass: HomeAssistant,
) -> None:
    """For example a wallbox: listing it confirms it, even after 30 days."""
    inventory = discovery_inventory(hass, "wallbox-entry")
    inventory._loaded = True
    inventory.store.async_delay_save = lambda data, delay: None
    wallbox = Gateway("wallbox-one", device_type="wallbox")
    inventory.remember_gateways([wallbox])
    inventory.gateway("wallbox-one").saved_at = datetime.now(UTC) - (
        MAX_AGE + timedelta(days=1)
    )
    assert inventory.startup_gateways(["wallbox-one"]) is None
    inventory.remember_gateways([wallbox])
    assert inventory.startup_gateways(["wallbox-one"]) == (wallbox,)
    inventory.confirm("unknown", ["/x"])  # unknown gateway: nothing happens
    inventory.confirm("wallbox-one", ["/not-stored"])
    assert inventory.resources("wallbox-one") == {}


async def test_inventory_start_adds_climate_only_where_a_normal_start_would(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    setup = Setup(hass)
    cooling = "/heatingCircuits/hc3"
    heat_cool = cooling + "/heatCoolMode"
    tree = plant()
    tree["/heatingCircuits"] = container("/heatingCircuits", CIRCUIT, cooling)
    tree[cooling] = container(cooling, cooling + "/operationMode", heat_cool)
    tree[cooling + "/operationMode"] = scalar(
        cooling + "/operationMode", "manual", options=("off", "manual", "auto")
    )
    tree[heat_cool] = scalar(heat_cool, "cool", options=("heat", "cool"))
    setup.cloud.tree = tree

    async def body() -> None:
        live = await _normal_start(hass, setup, entry)
        climates = {uid for uid in live if uid.endswith(":climate")}
        assert len(climates) == 1  # only the heating circuit
        registry = er.async_get(hass)
        for item in list(_gateway_entities(hass, entry).values()):
            registry.async_remove(item.entity_id)
        setup.gateway_list = RequestTimeout()
        setup.cloud.bulk_error = RequestTimeout()
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert set(_gateway_entities(hass, entry)) == live

    await _run(setup, entry, body)


async def test_one_gateway_from_the_inventory_keeps_start_up_values_for_others(
    hass: HomeAssistant,
) -> None:
    from homeassistant.helpers import entity_registry as entity_registry_helper

    from custom_components.bosch_buderus_heating.const import CONF_GATEWAY_IDS

    del entity_registry_helper
    entry = _entry(hass)
    second = Gateway("gateway-two", model="MX400")
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_GATEWAY_IDS: ["gateway-one", "gateway-two"]}
    )
    setup = Setup(hass)
    setup.gateway_list = [GATEWAY, second]

    async def body() -> None:
        await _normal_start(hass, setup, entry)
        setup.cloud.gateway_errors["gateway-two"] = RequestTimeout()
        with patch(f"{PACKAGE}._async_load_startup_data", AsyncMock()) as batch:
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
        modes = {
            item.gateway.gateway_id: item.startup_mode
            for item in entry.runtime_data.coordinators
        }
        assert modes == {"gateway-one": "live", "gateway-two": "inventory"}
        ((started,), _kwargs) = batch.await_args
        assert [item.gateway.gateway_id for item in started] == ["gateway-one"]

    await _run(setup, entry, body)


@pytest.mark.parametrize("cause", ["gateway_list_403", "discovery_404"])
async def test_permanent_answers_are_retried_as_before(
    hass: HomeAssistant, cause: str
) -> None:
    """Only temporary failures use the inventory; real answers do not."""
    from custom_components.bosch_buderus_heating.pointt import ResourceError

    entry = _entry(hass)
    setup = Setup(hass)

    async def body() -> None:
        await _normal_start(hass, setup, entry)
        if cause == "gateway_list_403":
            setup.gateway_list = ResourceError("/gateways/", 403)
        else:
            setup.cloud.tree = {}  # every root answers 404
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_RETRY

    await _run(setup, entry, body)


async def test_first_poll_without_answer_keeps_stored_values_hidden(
    hass: HomeAssistant,
) -> None:
    """Feature branch (P12): a no-answer result must not revive stored values."""
    from tests.test_discovery_recovery import _coordinator

    clock = Clock()
    cloud = Cloud(plant())
    cloud.down[MODULATION] = RequestTimeout()  # one item does not answer
    with (
        patch(f"{COORDINATOR}.monotonic", clock),
        patch(f"{COORDINATOR}.fault_resource_candidates", return_value=()),
    ):
        coordinator = _coordinator(hass, cloud)
        coordinator.start_from_inventory(
            plant(),
            reason="gateway_list_timeout",
            age_seconds=0,
            seen=dict.fromkeys(plant(), datetime.now(UTC)),
        )
        await coordinator.async_refresh()
        assert coordinator.last_update_success  # other items answered
        shown = coordinator.display_snapshot(MODULATION)
        assert shown is not None and not shown.available
        assert coordinator.display_snapshot(MODE).available  # it answered


async def test_discovery_waits_for_the_coordinator_brake(hass: HomeAssistant) -> None:
    from tests.test_discovery_recovery import _coordinator

    clock = Clock()
    cloud = Cloud(plant())
    cloud.down[TARGET] = 429  # partial rate limit in the first working cycle
    with (
        patch(f"{COORDINATOR}.monotonic", clock),
        patch(f"{COORDINATOR}.fault_resource_candidates", return_value=()),
    ):
        coordinator = _coordinator(hass, cloud)
        coordinator.start_from_inventory(
            plant(), reason="gateway_list_timeout", age_seconds=0, seen={}
        )
        await coordinator.async_refresh()
        assert coordinator._cloud_backoff_until > clock.now
        del cloud.down[TARGET]
        sent = len(cloud.requests)
        clock.now += 60  # still within the brake
        await coordinator.async_refresh()
        report = coordinator.startup_inventory_diagnostics(clock.now)
        assert report["discovery_attempts"] == 0
        assert len(cloud.requests) == sent
        clock.now = coordinator._cloud_backoff_until + 1
        await coordinator.async_refresh()
        await coordinator.async_refresh()
        report = coordinator.startup_inventory_diagnostics(clock.now)
        assert report["discovery_result"] == "success"


async def test_reported_age_is_the_age_of_the_confirmed_resources(
    hass: HomeAssistant,
) -> None:
    from custom_components.bosch_buderus_heating import _start_from_inventory
    from tests.test_discovery_recovery import _coordinator, _inventory

    inventory = _inventory(hass)
    inventory.record(GATEWAY, plant())
    stored = inventory.gateway(GATEWAY.gateway_id)
    for item in stored.resources.values():
        item.seen = datetime.now(UTC) - timedelta(hours=5)
    inventory.remember_gateways([GATEWAY])  # a fresh listing, resources older
    coordinator = _coordinator(hass, Cloud({}))
    coordinator.gateway = GATEWAY
    assert _start_from_inventory(coordinator, inventory, "gateway_list_timeout")
    report = coordinator.startup_inventory_diagnostics(0)
    assert report["inventory_age_hours"] == 5
