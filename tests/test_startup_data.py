"""Startup metadata must save requests without making outages permanent."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.bosch_buderus_heating import (
    _async_load_startup_data,
    _restore_registered_startup_paths,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
    SnapshotSource,
)
from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.discovery import (
    DiscoveryDiagnostics,
    async_discover_resources,
)
from custom_components.bosch_buderus_heating.entry_sources import pointt_coordinators
from custom_components.bosch_buderus_heating.inventory import (
    InventoryError,
    build_anonymized_inventory,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    BatchItemResult,
    Gateway,
    InvalidPayload,
    RateLimited,
    RequestMetrics,
    Resource,
    ResourceForbidden,
    ResourceMetadata,
    ResourceReference,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.pointt.transport import RateLimitBackoff
from custom_components.bosch_buderus_heating.resource_catalog import (
    PollGroup,
    poll_group,
)
from custom_components.bosch_buderus_heating.runtime import BoschBuderusRuntimeData
from custom_components.bosch_buderus_heating.sensor import (
    BoschBuderusSensor,
)
from custom_components.bosch_buderus_heating.sensor import (
    async_setup_entry as setup_sensors,
)
from custom_components.bosch_buderus_heating.startup_data import (
    CIRCUIT_COUNT_PATH,
    LICENSE_PATH,
    StartupGatewayData,
    StartupRead,
    StartupSession,
    is_startup_path,
    is_static_startup_resource,
    safe_technical_value,
    startup_placeholder,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.helpers.managed_runtime import pointt_client, wait_for_sources

PRODUCT = "/devices/synthetic-device/productName"
FAST = "/heatSources/actualModulation"
STAMP = datetime(2026, 10, 11, tzinfo=UTC)


@contextmanager
def at_time(now):
    """Advance coordinator cadence and supplementary deadlines together."""
    with (
        patch(
            "custom_components.bosch_buderus_heating.coordinator.monotonic",
            return_value=now,
        ),
        patch(
            "custom_components.bosch_buderus_heating.startup_reader.monotonic",
            return_value=now,
        ),
    ):
        yield


def scalar(path=PRODUCT, value="Synthetic product", **metadata):
    return Resource(
        path,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="stringValue" if isinstance(value, str) else "floatValue",
            **metadata,
        ),
    )


@pytest.fixture
def environment(hass):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    client = Mock()
    client.metrics = RequestMetrics()
    client.backoff = RateLimitBackoff()
    client.get_resources_bulk = AsyncMock()
    client.get_resource = AsyncMock()
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, client, Gateway("synthetic"), entry
    )
    coordinator.resources = {FAST: scalar(FAST, 20)}
    coordinator.data = {
        FAST: ResourceSnapshot(coordinator.resources[FAST], True, STAMP)
    }
    coordinator._paths_by_group = {PollGroup.FAST: (FAST,)}
    coordinator._next_update = {group: 1e20 for group in coordinator._poll_intervals}
    coordinator.startup_session.mark_setup_complete()
    coordinator.startup_data.record_main_cycle(successful=True)
    coordinator.update_interval = None
    entry.runtime_data = BoschBuderusRuntimeData(
        client,
        Mock(),
        (coordinator.gateway,),
        (coordinator,),
        startup=coordinator.startup_session,
    )
    return entry, client, coordinator


@pytest.mark.parametrize(
    "value, valid",
    [
        (0, True),
        (2, True),
        (65535, True),
        (65536, False),
        (-1, False),
        (1.5, False),
        (True, False),
        (None, False),
        (float("nan"), False),
        (float("inf"), False),
    ],
)
def test_only_usable_circuit_counts_become_static(value, valid):
    resource = scalar(CIRCUIT_COUNT_PATH, value)
    assert is_static_startup_resource(resource) is valid
    if valid:
        assert poll_group(resource) is PollGroup.STATIC


@pytest.mark.parametrize(
    "change",
    [
        {"has_value": False},
        {"value": ""},
        {"value": "   "},
        {"has_values": True},
        {"references": (ResourceReference("/system"),)},
        {"metadata": ResourceMetadata(resource_type="stringValue", writable=True)},
        {"metadata": ResourceMetadata(resource_type="unknown")},
    ],
)
def test_product_schema_guards(change):
    assert is_static_startup_resource(scalar())
    assert not is_static_startup_resource(replace(scalar(), **change))


def test_reviewed_paths_are_exact_and_private_paths_have_no_placeholders():
    for path in (
        PRODUCT,
        CIRCUIT_COUNT_PATH,
        "/system/dateTime",
        "/gateway/tosAccepted",
        "/gateway/wifi/mac",
        "/devices/reported/inclusionTime",
        "/devices/reported/sgtin",
    ):
        assert is_startup_path(path)
    for path in (
        LICENSE_PATH,
        "/gateway",
        "/devices/list/productName",
        "/devices/x/name",
        "/system/dateTime/child",
        "/gateway/wifi/ssid",
    ):
        assert not is_startup_path(path)
    assert not startup_placeholder(PRODUCT).has_value
    assert (
        startup_placeholder(CIRCUIT_COUNT_PATH).metadata.resource_type == "floatValue"
    )
    with pytest.raises(ValueError):
        startup_placeholder("/gateway/wifi/mac")
    assert is_static_startup_resource(
        Resource("/gateway/tosAccepted", value=True, has_value=True)
    )


def test_transient_reads_keep_retrying_then_stop_only_after_success():
    state = StartupGatewayData()
    state.schedule(PRODUCT, 2, advertised=True)
    now = 100.0
    for delay in (60, 120, 300, 900, 1800, 1800, 1800):
        paths = state.due(now)
        assert paths == (PRODUCT,)
        state.start(paths, STAMP)
        state.fail(paths, "service_unavailable", now)
        read = state.reads[PRODUCT]
        assert read.next_attempt == now + delay
        assert not state.due(now + delay - 0.01)
        now += delay
    assert state.receive(scalar(), now, STAMP)
    read = state.reads[PRODUCT]
    assert not state.due(now + 86400)
    assert read.last_success == STAMP
    assert read.attempts == 7


@pytest.mark.parametrize("status", [403, 404, 406])
@pytest.mark.parametrize(
    "advertised,registered,expected",
    [(False, False, False), (True, False, True), (False, True, True)],
)
def test_local_metadata_errors_recover_only_when_the_sensor_is_expected(
    status, advertised, registered, expected
):
    read = StartupRead(PRODUCT, 2, advertised=advertised, registered=registered)
    read = read.failed(f"http_{status}", 100)
    assert read.pending is expected
    assert read.next_attempt == (1900 if expected else 0)
    diagnostic = StartupRead("/gateway/wifi/mac", 2, advertised=True)
    diagnostic = diagnostic.failed(f"http_{status}", 100)
    assert not diagnostic.pending


def test_depth_resource_bounds_and_unattempted_budget_are_distinct():
    state = StartupGatewayData(maximum_resources=2, maximum_depth=2)
    state.schedule("/not-reviewed", 0)
    assert not state.reads
    state.schedule(PRODUCT, 3)
    assert state.reads[PRODUCT].result == "depth_limit"
    state.schedule(PRODUCT, 2, advertised=True)
    state.schedule(CIRCUIT_COUNT_PATH, 2)
    state.reserve_discovery(("/system",))
    assert state.due(0) == (PRODUCT,)
    assert state.reads[CIRCUIT_COUNT_PATH].result == "resource_limit"
    state.budget_expired(0)
    read = state.reads[PRODUCT]
    assert read.result == "budget_wait"
    assert read.attempts == read.failures == 0
    assert read.next_attempt == 60
    state.start((PRODUCT,), STAMP)
    state.receive(scalar(), 60, STAMP)
    state.begin_discovery()
    assert not state.cached_reads()
    state.schedule(PRODUCT, 2)
    assert tuple(read.resource for read in state.cached_reads()) == (scalar(),)
    assert not state.due(100000)


def test_session_rotates_gateways_and_does_not_block_on_an_unhealthy_one():
    session = StartupSession(("one", "two"))
    session.mark_setup_complete()
    for gateway in session.selected_ids:
        state = session.gateway(gateway)
        state.record_main_cycle(successful=True)
        state.schedule(PRODUCT, 1)
    assert not session.claim("two", 100)
    assert session.claim("one", 100)
    assert not session.claim("two", 200)
    session.release()
    assert not session.claim("two", 159)
    assert session.claim("two", 160)
    session.release()
    session.gateway("one").record_main_cycle(successful=False)
    assert session.claim("two", 220)
    session.release()
    session.close()
    assert not session.claim("two", 1000)


async def test_license_omission_and_startup_deferral_do_not_hide_functional_paths():
    client = AsyncMock()
    resources = {
        "/gateway": Resource(
            "/gateway",
            references=(
                ResourceReference(LICENSE_PATH),
                ResourceReference("/gateway/tosAccepted"),
                ResourceReference("/gateway/versionFirmware"),
            ),
        ),
        "/gateway/versionFirmware": scalar("/gateway/versionFirmware", "1.2.3"),
    }
    client.get_resources_bulk.side_effect = lambda gateway, paths: tuple(
        BatchItemResult(
            gateway,
            path,
            200 if path in resources else 404,
            resource=resources.get(path),
        )
        for path in paths
    )
    state = StartupGatewayData()
    report = DiscoveryDiagnostics()
    found = await async_discover_resources(
        client, "one", roots=("/gateway",), diagnostics=report, startup_data=state
    )
    assert found == resources
    requested = [
        p for call in client.get_resources_bulk.call_args_list for p in call.args[1]
    ]
    assert LICENSE_PATH not in requested
    assert "/gateway/tosAccepted" not in requested
    assert state.omitted_license
    assert state.reads["/gateway/tosAccepted"].advertised
    assert report.paths[LICENSE_PATH].read_policy == "omitted_license"
    assert report.paths[LICENSE_PATH].bulk_result == "not_attempted"
    assert not report.paths[LICENSE_PATH].failed
    client.get_resource.assert_not_awaited()


async def test_supplementary_failure_does_not_fail_poll_and_recovers_without_restart(
    environment,
):
    entry, client, coordinator = environment
    coordinator.startup_data.schedule(PRODUCT, 2, advertised=True)
    coordinator.startup_data.schedule(CIRCUIT_COUNT_PATH, 2, registered=True)
    added = []
    await setup_sensors(coordinator.hass, entry, added.extend)
    old = next(
        entity
        for entity in added
        if isinstance(entity, BoschBuderusSensor)
        and entity.entity_description.resource_path == CIRCUIT_COUNT_PATH
    )
    identity = old.unique_id
    assert not old.available
    assert all(
        not isinstance(entity, BoschBuderusSensor)
        or entity.entity_description.resource_path != PRODUCT
        for entity in added
    )
    client.get_resources_bulk.side_effect = ServiceUnavailable(504)
    with at_time(100):
        coordinator.data = await coordinator._async_update_data()
    assert coordinator._poll_failures == 0
    assert coordinator.data[FAST].last_success == STAMP
    assert coordinator.startup_data.reads[PRODUCT].next_attempt == 160
    with at_time(159):
        await coordinator._async_update_data()
    assert client.get_resources_bulk.await_count == 1
    client.get_resources_bulk.side_effect = lambda gateway, paths, **kwargs: tuple(
        BatchItemResult(
            gateway,
            path,
            200,
            resource=scalar(path, 2 if path == CIRCUIT_COUNT_PATH else "MX400"),
        )
        for path in paths
    )
    with at_time(160):
        coordinator.async_set_updated_data(await coordinator._async_update_data())
    assert old.available and old.unique_id == identity and old.native_value == 2
    product = [
        entity
        for entity in added
        if isinstance(entity, BoschBuderusSensor)
        and entity.entity_description.resource_path == PRODUCT
    ]
    assert len(product) == 1 and product[0].available
    coordinator.async_update_listeners()
    assert (
        sum(
            isinstance(entity, BoschBuderusSensor)
            and entity.entity_description.resource_path == PRODUCT
            for entity in added
        )
        == 1
    )
    with at_time(86560):
        await coordinator._async_update_data()
    assert client.get_resources_bulk.await_count == 2
    client.get_resource.assert_not_awaited()


@pytest.mark.parametrize(
    "outcome",
    [
        ServiceUnavailable(504),
        InvalidPayload(),
        ResourceForbidden(PRODUCT, 403),
        TimeoutError(),
        RateLimited(90),
    ],
)
async def test_request_errors_remain_local_to_supplementary_data(environment, outcome):
    _, client, coordinator = environment
    coordinator.startup_data.schedule(PRODUCT, 2, advertised=True)
    client.get_resources_bulk.side_effect = outcome
    assert await coordinator.async_read_startup_batch(coordinator.data)
    read = coordinator.startup_data.reads[PRODUCT]
    assert read.pending and read.attempts == 1 and read.resource is None
    assert coordinator.data[FAST].last_success == STAMP
    client.get_resources_bulk.assert_awaited_once_with(
        "synthetic", (PRODUCT,), retryable=False, request_type="startup_metadata"
    )
    client.get_resource.assert_not_awaited()


@pytest.mark.parametrize("status", [200, 403, 404, 406, 429, 503])
async def test_bulk_failures_are_not_false_successes(environment, status):
    _, client, coordinator = environment
    coordinator.startup_data.schedule(PRODUCT, 2, advertised=True)
    client.get_resources_bulk.return_value = (
        BatchItemResult("synthetic", PRODUCT, status),
    )
    await coordinator.async_read_startup_batch(coordinator.data)
    assert coordinator.startup_data.reads[PRODUCT].pending
    assert coordinator.startup_data.reads[PRODUCT].result != "success"


@pytest.mark.parametrize(
    "resource", [Resource(PRODUCT), scalar(value=""), scalar(value=True)]
)
async def test_http_success_with_unusable_metadata_stays_pending_until_a_valid_read(
    environment, resource
):
    _, client, coordinator = environment
    coordinator.startup_data.schedule(PRODUCT, 2, advertised=True)
    client.get_resources_bulk.return_value = (
        BatchItemResult("synthetic", PRODUCT, 200, resource=resource),
    )
    with at_time(200):
        await coordinator.async_read_startup_batch(coordinator.data)
    read = coordinator.startup_data.reads[PRODUCT]
    assert read.result == "invalid_value" and read.pending
    assert read.last_success is None and PRODUCT not in coordinator.data
    client.get_resources_bulk.return_value = (
        BatchItemResult("synthetic", PRODUCT, 200, resource=scalar()),
    )
    with at_time(260):
        await coordinator.async_read_startup_batch(coordinator.data)
    assert not coordinator.startup_data.reads[PRODUCT].pending
    assert coordinator.data[PRODUCT].available


def test_pending_queue_is_bounded_even_when_a_gateway_advertises_too_many_paths():
    state = StartupGatewayData()
    for number in range(1024):
        state.schedule(f"/devices/synthetic-{number}/productName", 2, advertised=True)
    assert len(state.reads) == 512
    assert len(state.due(0)) == 30


@pytest.mark.parametrize("bulk_item", [False, True])
async def test_real_authentication_failure_requests_reauth(environment, bulk_item):
    _, client, coordinator = environment
    coordinator.startup_data.schedule(PRODUCT, 2)
    if bulk_item:
        client.get_resources_bulk.return_value = (
            BatchItemResult("synthetic", PRODUCT, 401),
        )
    else:
        client.get_resources_bulk.side_effect = AuthenticationError()
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()
    assert coordinator.startup_session.closed
    assert not coordinator.startup_session.busy


async def test_main_failure_has_priority_and_cannot_be_healed_by_metadata(environment):
    _, client, coordinator = environment
    coordinator.startup_data.schedule(PRODUCT, 2)
    with (
        patch.object(
            coordinator,
            "_async_update_data_locked",
            AsyncMock(side_effect=UpdateFailed("offline")),
        ),
        pytest.raises(UpdateFailed),
    ):
        await coordinator._async_update_data()
    client.get_resources_bulk.assert_not_awaited()
    assert not coordinator.startup_data.healthy
    coordinator.last_update_success = False
    await coordinator._async_update_data()
    client.get_resources_bulk.assert_not_awaited()


async def test_cancel_and_deadline_release_reservations(environment):
    _, client, coordinator = environment
    coordinator.startup_data.schedule(PRODUCT, 2)
    entered = asyncio.Event()

    async def wait(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    client.get_resources_bulk.side_effect = wait
    task = asyncio.create_task(coordinator._async_update_data())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not coordinator.startup_session.busy
    read = coordinator.startup_data.reads[PRODUCT]
    assert read.pending and read.result == "cancelled" and read.last_success is None
    with at_time(read.next_attempt):
        assert await coordinator.async_read_startup_batch(
            coordinator.data, budget=0.001
        )
    assert coordinator.startup_data.reads[PRODUCT].result == "timeout"
    assert coordinator.startup_data.budget_exhaustions == 1


async def test_initial_phase_shares_deadline_and_keeps_unattempted_paths_pending(
    environment,
):
    _, client, coordinator = environment
    coordinator.startup_data.schedule(PRODUCT, 2)
    other = BoschBuderusDataUpdateCoordinator(
        coordinator.hass,
        client,
        Gateway("other"),
        coordinator.config_entry,
        startup_session=coordinator.startup_session,
    )
    other.startup_data.schedule(CIRCUIT_COUNT_PATH, 2)
    other.data = {}
    now = 100.0

    async def slow(gateway, paths, **kwargs):
        nonlocal now
        now += 31
        return (BatchItemResult(gateway, PRODUCT, 503),)

    client.get_resources_bulk.side_effect = slow
    with patch(
        "custom_components.bosch_buderus_heating.monotonic", side_effect=lambda: now
    ):
        await _async_load_startup_data((coordinator, other))
    read = other.startup_data.reads[CIRCUIT_COUNT_PATH]
    assert read.attempts == read.failures == 0
    assert read.pending and read.result == "budget_wait"
    assert client.get_resources_bulk.await_count == 1


async def test_registered_disabled_sensor_identity_and_gateway_selection_survive(
    environment,
):
    entry, _, coordinator = environment
    registry = er.async_get(coordinator.hass)
    sensor = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"synthetic:{PRODUCT.strip('/').replace('/', ':')}",
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    registry.async_get_or_create(
        "sensor", DOMAIN, "other:devices:x:productName", config_entry=entry
    )
    registry.async_get_or_create(
        "sensor", DOMAIN, "synthetic:devices:x:name", config_entry=entry
    )
    registry.async_get_or_create(
        "number", DOMAIN, "synthetic:devices:x:productName", config_entry=entry
    )
    _restore_registered_startup_paths(coordinator.hass, entry)
    assert set(coordinator.startup_data.reads) == {PRODUCT}
    assert coordinator.startup_data.reads[PRODUCT].registered
    added = []
    await setup_sensors(coordinator.hass, entry, added.extend)
    recovered = next(
        item
        for item in added
        if isinstance(item, BoschBuderusSensor) and item.unique_id == sensor.unique_id
    )
    assert not recovered.available
    assert (
        registry.async_get(sensor.entity_id).disabled_by
        is er.RegistryEntryDisabler.USER
    )


async def test_unexpected_writable_or_structural_metadata_keeps_normal_handling(
    environment,
):
    _, client, coordinator = environment
    coordinator.startup_data.schedule(PRODUCT, 2)
    child = "/heatSources/hs2/actualPower"
    container = replace(
        scalar(),
        references=(
            ResourceReference(child),
            ResourceReference(LICENSE_PATH),
            ResourceReference("/outside"),
        ),
    )
    client.get_resources_bulk.return_value = (
        BatchItemResult("synthetic", PRODUCT, 200, resource=container),
    )
    await coordinator.async_read_startup_batch(coordinator.data)
    assert coordinator.startup_data.omitted_license
    assert (
        child in coordinator.startup_data.reads
        and "/outside" not in coordinator.startup_data.reads
    )
    assert coordinator.startup_data.reads[child].depth == 3
    client.get_resources_bulk.return_value = (
        BatchItemResult(
            "synthetic", child, 200, resource=scalar(child, 5, writable=True, unit="kW")
        ),
    )
    await coordinator.async_read_startup_batch(coordinator.data)
    assert child in coordinator._paths_by_group[PollGroup.FAST]
    newer = ResourceSnapshot(
        scalar(child, 8),
        True,
        datetime.now(UTC) + timedelta(seconds=1),
        source=SnapshotSource.BATCH,
    )
    coordinator.data[child] = newer
    coordinator._merge_startup_resources(coordinator.data)
    assert coordinator.data[child] is newer


async def test_diagnostics_are_local_and_inventory_drops_technical_values(environment):
    entry, client, coordinator = environment
    for resource in (
        scalar(PRODUCT, "Private house label"),
        scalar(CIRCUIT_COUNT_PATH, 2),
        scalar("/gateway/wifi/mac", "11:22:33:44:55:66"),
        scalar("/gateway/versionFirmware", "1.2.3"),
    ):
        coordinator.resources[resource.path] = resource
    coordinator.startup_data.schedule(PRODUCT, 2, advertised=True)
    coordinator.startup_data.schedule(
        "/devices/synthetic-other/productName", 2, advertised=True
    )
    coordinator.startup_data.omit_license()
    diagnostics = await async_get_config_entry_diagnostics(coordinator.hass, entry)
    assert diagnostics["diagnostics_schema"] == 43
    # O94: the same masked path twice is told apart by its number.
    products = [
        item
        for item in diagnostics["gateways"][0]["startup_data"]["paths"]
        if item["path"].endswith("/productName")
    ]
    assert len({item["path"] for item in products}) == 1
    assert sorted(item["index"] for item in products) == [1, 2]
    assert all(
        "index" not in item
        for item in diagnostics["gateways"][0]["startup_data"]["paths"]
        if not item["path"].endswith("/productName")
    )
    assert diagnostics["privacy"]["contains_raw_resource_values"] is True
    metadata = diagnostics["gateways"][0]["technical_metadata"]
    assert {item["path"] for item in metadata} == {
        CIRCUIT_COUNT_PATH,
        "/gateway/versionFirmware",
    }
    for private in ("Private house label", "11:22:33:44:55:66", "synthetic-device"):
        assert private not in repr(diagnostics)
    inventory = build_anonymized_inventory(diagnostics)
    assert inventory["privacy"]["contains_raw_resource_values"] is False
    assert "technical_metadata" not in repr(inventory)
    assert "1.2.3" not in repr(inventory)
    diagnostics["privacy"]["raw_resource_values_scope"] = "anything"
    with pytest.raises(InventoryError):
        build_anonymized_inventory(diagnostics)
    client.get_resources_bulk.assert_not_awaited()


@pytest.mark.parametrize(
    "path,value,expected",
    [
        (PRODUCT, "MX400", "MX400"),
        (PRODUCT, "private", None),
        ("/system/brand", "Buderus", "buderus"),
        ("/system/brand", "Other", None),
        ("/gateway/versionFirmware", "1.2.3", "1.2.3"),
        ("/gateway/versionFirmware", "123456789", None),
        ("/gateway/wifi/mac", "12:34:56:78:90:ab", None),
        (CIRCUIT_COUNT_PATH, 2, 2),
        (CIRCUIT_COUNT_PATH, 1.5, None),
    ],
)
def test_technical_metadata_allowlist(path, value, expected):
    assert safe_technical_value(scalar(path, value)) == expected
    assert safe_technical_value(scalar(path, value, writable=True)) is None


def test_private_network_shapes_and_extreme_numbers_are_never_exported():
    assert (
        safe_technical_value(scalar("/gateway/versionFirmware", "192.168.1.1")) is None
    )
    assert (
        safe_technical_value(replace(scalar(), values=({"secret": "value"},))) is None
    )
    assert not is_static_startup_resource(scalar(CIRCUIT_COUNT_PATH, 10**1000))
    assert not is_static_startup_resource(replace(scalar(), values=({"field": 1},)))


def test_live_or_structural_surprises_are_not_reused_as_static_cache():
    state = StartupGatewayData()
    state.schedule(PRODUCT, 2)
    state.receive(scalar(writable=True), 0, STAMP)
    assert state.cached_reads()
    state.begin_discovery()
    state.schedule(PRODUCT, 2)
    assert not state.cached_reads()
    assert state.due(0) == (PRODUCT,)


async def test_removing_an_entry_drops_only_its_own_account_pause(environment):
    from custom_components.bosch_buderus_heating import async_remove_entry
    from custom_components.bosch_buderus_heating.auth_lifecycle import ACCOUNT_BACKOFFS
    from custom_components.bosch_buderus_heating.configuration_migration import (
        legacy_cloud_connection_id,
    )

    entry, client, coordinator = environment
    other = RateLimitBackoff()
    coordinator.hass.data[ACCOUNT_BACKOFFS] = {
        (entry.entry_id, legacy_cloud_connection_id(entry.entry_id)): client.backoff,
        ("other", "cloud"): other,
    }
    await async_remove_entry(coordinator.hass, entry)
    assert coordinator.hass.data[ACCOUNT_BACKOFFS] == {("other", "cloud"): other}
    await async_remove_entry(coordinator.hass, entry)


async def test_recovery_batches_are_bounded_and_unattempted_paths_are_not_failures(
    environment,
):
    _, client, coordinator = environment
    for number in range(65):
        coordinator.startup_data.schedule(f"/devices/synthetic-{number}/productName", 2)
    client.get_resources_bulk.side_effect = ServiceUnavailable(504)
    with at_time(100):
        await coordinator._async_update_data()
    assert client.get_resources_bulk.await_count == 1
    assert len(client.get_resources_bulk.call_args.args[1]) == 30
    assert sum(read.attempts for read in coordinator.startup_data.reads.values()) == 30
    assert (
        sum(
            read.result == "pending" for read in coordinator.startup_data.reads.values()
        )
        == 35
    )
    with at_time(160):
        await coordinator._async_update_data()
    second = set(client.get_resources_bulk.call_args.args[1])
    first = set(client.get_resources_bulk.call_args_list[0].args[1])
    assert first.isdisjoint(second)
    assert all(read.attempts <= 1 for read in coordinator.startup_data.reads.values())


async def test_local_account_backoff_sends_no_metadata_request(environment):
    _, client, coordinator = environment
    coordinator.startup_data.schedule(PRODUCT, 2)
    client.backoff.activate(3600)
    assert not await coordinator.async_read_startup_batch(coordinator.data)
    assert coordinator.startup_data.reads[PRODUCT].attempts == 0
    client.get_resources_bulk.assert_not_awaited()
    assert not await coordinator.async_read_startup_batch(coordinator.data, budget=0)


async def test_combined_discovery_path_limit_survives_setup_retries():
    client = AsyncMock()
    client.get_resources_bulk.side_effect = lambda gateway, paths: tuple(
        BatchItemResult(gateway, path, 200, resource=Resource(path)) for path in paths
    )
    state = StartupGatewayData()
    state.reserve_discovery(("/already/one", "/already/two"))
    report = DiscoveryDiagnostics()
    await async_discover_resources(
        client,
        "gateway",
        roots=("/root", "/other"),
        maximum_resources=3,
        startup_data=state,
        diagnostics=report,
    )
    assert len(state.requested) == 3
    assert client.get_resources_bulk.call_args.args[1] == ("/root",)
    assert report.stop_reason == "resource_limit"
    state.schedule(PRODUCT, 2)
    assert not state.due(0)
    assert state.reads[PRODUCT].result == "resource_limit"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_real_ha_registry_and_entities_recover_during_a_refresh(hass):
    from custom_components.bosch_buderus_heating.const import (
        CONF_BRAND,
        CONF_GATEWAY_IDS,
    )
    from custom_components.bosch_buderus_heating.data import tokens_to_data
    from custom_components.bosch_buderus_heating.pointt import AuthTokens

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={
            CONF_BRAND: "buderus",
            CONF_GATEWAY_IDS: ["synthetic"],
            **tokens_to_data(AuthTokens("access", "refresh", expires_at=4e9)),
        },
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    existing = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"synthetic:{CIRCUIT_COUNT_PATH.strip('/').replace('/', ':')}",
        config_entry=entry,
    )
    client = Mock()
    client.metrics = RequestMetrics()
    client.backoff = RateLimitBackoff()
    client.get_gateways = AsyncMock(return_value=(Gateway("synthetic"),))
    client.get_resources_bulk = AsyncMock(side_effect=ServiceUnavailable(504))

    async def discover(*args, startup_data, **kwargs):
        startup_data.begin_discovery()
        startup_data.schedule(PRODUCT, 2, advertised=True)
        startup_data.schedule(CIRCUIT_COUNT_PATH, 2, advertised=True)
        return {FAST: scalar(FAST, 20)}

    with (
        pointt_client(client),
        patch(
            "custom_components.bosch_buderus_heating.coordinator.async_discover_resources",
            side_effect=discover,
        ),
        patch(
            "custom_components.bosch_buderus_heating.coordinator.fault_resource_candidates",
            return_value=(),
        ),
        patch(
            "custom_components.bosch_buderus_heating.async_setup_notifications",
            AsyncMock(return_value=()),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await wait_for_sources(hass, entry, 1)
        await hass.async_block_till_done()
        assert hass.states.get(existing.entity_id).state == "unavailable"
        product_id = f"synthetic:{PRODUCT.strip('/').replace('/', ':')}"
        assert registry.async_get_entity_id("sensor", DOMAIN, product_id) is None
        coordinator = pointt_coordinators(entry)[0]
        recovery_at = (
            max(read.next_attempt for read in coordinator.startup_data.reads.values())
            + 60
        )
        coordinator._next_update[PollGroup.FAST] = 0
        client.get_resources_bulk.side_effect = lambda gateway, paths, **kwargs: tuple(
            BatchItemResult(
                gateway,
                path,
                200,
                resource=scalar(path, "MX400" if path == PRODUCT else 2),
            )
            for path in paths
        )
        with at_time(recovery_at):
            await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(existing.entity_id).state == "2"
        product_entity_id = registry.async_get_entity_id("sensor", DOMAIN, product_id)
        assert product_entity_id is not None
        registered_product = registry.async_get(product_entity_id)
        assert registered_product.unique_id == product_id
        # Default-disabled metadata still gets a registry entry without overriding
        # Home Assistant's activation policy.
        if registered_product.disabled_by is None:
            assert hass.states.get(product_entity_id).state == "MX400"
        assert await hass.config_entries.async_unload(entry.entry_id)
