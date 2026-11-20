"""A first discovery with a slow cloud shows its entities step by step.

A new installation without an inventory discovers the plant with one request
after the other. These tests use real time: the fake cloud needs a tenth of a
second per request, the time limit for the first partial result is shortened,
and the cloud can be held at a request. They check that entities appear before
the discovery ends, that the end state equals the one of a fast cloud (same
unique IDs, no duplicates, no orphans) and how an error, a stop and a restart
in the middle behave.
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import discovery_partial
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.device import is_rrc_plant
from custom_components.bosch_buderus_heating.discovery import (
    async_discover_resources,
)
from custom_components.bosch_buderus_heating.managed_pointt import (
    ManagedPointTCoordinator,
)
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Gateway,
    Resource,
    ResourceNotFound,
    ResourceReference,
)
from custom_components.bosch_buderus_heating.pointt.exceptions import (
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.rrc import PRODUCT_TYPE_PATH, ZONE_LIST
from tests.helpers.rrc_cloud import (
    Clock,
    RrcCloud,
    cloud_patches,
    load_tree,
    with_containers,
)
from tests.test_managed_pointt import (
    Backend,
    binding_for,
    eventually,
    running,
    source_for,
)

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
GATEWAY = "000123"
DELAY = 0.1
LIMIT = 0.2
PLANTS = ("k40", "ct200")


@pytest.fixture(autouse=True)
def loaded_integration(hass):
    hass.config.components.add(DOMAIN)


@pytest.fixture(autouse=True)
def short_limit(monkeypatch):
    monkeypatch.setattr(discovery_partial, "PARTIAL_AFTER", LIMIT)


class RealClock(Clock):
    def __call__(self) -> float:
        return time.monotonic()


class SlowCloud(RrcCloud):
    """Every bulk read takes ``delay`` seconds; reads can be held or fail."""

    def __init__(self, *args, delay=DELAY, **kwargs):
        super().__init__(*args, **kwargs)
        self.delay = delay
        self.hold_at: int | None = None
        self.release = asyncio.Event()
        self.held = asyncio.Event()
        self.fail_at: int | None = None
        self.first_at: float | None = None

    async def get_resources_bulk(self, gateway_id, paths, **kwargs):
        number = len(self.requests)
        if self.first_at is None:
            self.first_at = time.monotonic()
        await asyncio.sleep(self.delay)
        if self.hold_at is not None and number >= self.hold_at:
            self.held.set()
            await self.release.wait()
        if self.fail_at is not None and number == self.fail_at:
            self.fail_at = None
            raise ServiceUnavailable(503)
        return await super().get_resources_bulk(gateway_id, paths, **kwargs)


def plant(name: str, **kwargs) -> tuple[SlowCloud, Gateway]:
    if name == "k40":
        tree = load_tree("captures/k40-cloud-2026-11-05.json")
        cloud = SlowCloud(tree, forbidden=set(), missing_as_500=False, **kwargs)
        return cloud, Gateway(GATEWAY, device_type="k40")
    tree = with_containers(load_tree("rrc/ct200-shapes.json"))
    return SlowCloud(tree, **kwargs), Gateway(GATEWAY)


def only_first(data, options):
    for key, binding in data["bindings"].items():
        if binding["adapter"] == "pointt" and binding["locator"]["value"] != GATEWAY:
            options["bindings"][key]["enabled"] = False


def entries(hass, entry):
    registry = er.async_get(hass)
    return {
        (item.domain, item.unique_id.replace(entry.entry_id, "<entry>")): item
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }


def final(hass, entry):
    return {
        key: (item.entity_id, item.disabled_by)
        for key, item in sorted(entries(hass, entry).items())
    }


async def settled(hass, entry, owner, cloud, *, quiet=1.0):
    """The discovery is finished and the entities are no longer changing."""
    await eventually(lambda: source_for(owner, GATEWAY), timeout=30)
    await eventually(
        lambda: source_for(owner, GATEWAY).discovery_diagnostics.completed, timeout=30
    )
    previous = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        await asyncio.sleep(quiet)
        await hass.async_block_till_done()
        current = final(hass, entry)
        if current == previous:
            return current
        previous = current
    raise AssertionError("the entities did not settle")


async def baseline(hass, name):
    """The entities of a fast cloud: no partial result is ever shown."""
    cloud, gateway = plant(name, delay=0)
    with cloud_patches(cloud, RealClock(), gateway):
        async with running(hass, Backend(), configure=only_first) as (
            entry,
            _,
            owner,
            _,
        ):
            await owner.start_platforms()
            result = await settled(hass, entry, owner, cloud)
            coordinator = source_for(owner, GATEWAY)
            assert coordinator.discovery_diagnostics.partial_publications == 0
    await forget(hass, entry)
    assert result
    return result


async def forget(hass, entry):
    """Start the next plant run of a test from nothing."""
    await hass.config_entries.async_remove(entry.entry_id)
    assert not er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)


@pytest.mark.parametrize("name", PLANTS)
async def test_entities_appear_before_a_slow_discovery_ends(hass, name):
    expected = await baseline(hass, name)
    cloud, gateway = plant(name)
    cloud.hold_at = 8
    with cloud_patches(cloud, RealClock(), gateway):
        async with running(hass, Backend(), configure=only_first) as (
            entry,
            _,
            owner,
            _,
        ):
            await owner.start_platforms()
            await asyncio.wait_for(cloud.held.wait(), 30)
            await eventually(lambda: source_for(owner, GATEWAY), timeout=30)
            coordinator = source_for(owner, GATEWAY)
            report = coordinator.discovery_diagnostics
            # The discovery is still under way ...
            assert not report.completed
            assert report.partial_publications >= 1
            # ... and the entities of what was found already exist and have values.
            await eventually(
                lambda: any(
                    state.state not in ("unavailable", "unknown")
                    for item in entries(hass, entry).values()
                    if (state := hass.states.get(item.entity_id)) is not None
                ),
                timeout=30,
            )
            early = set(entries(hass, entry))
            assert early
            assert all(
                unique_id.startswith(GATEWAY) or unique_id for _, unique_id in early
            )
            cloud.release.set()
            end = await settled(hass, entry, owner, cloud)
            assert early <= set(end)
            assert len(end) > len(early)
            # The end state is the one of a fast cloud, entity for entity.
            assert end == expected
            assert source_for(owner, GATEWAY) is coordinator
            assert coordinator.discovery_diagnostics.completed


def watch(owner, seen):
    """Remember each coordinator the source publishes, the first one first."""
    coordinator = source_for(owner, GATEWAY)
    if coordinator is not None and coordinator not in seen:
        seen.append(coordinator)
    return coordinator


async def partial_shown(owner, cloud, seen):
    await asyncio.wait_for(cloud.held.wait(), 30)
    await eventually(lambda: watch(owner, seen), timeout=30)
    assert seen[0].discovery_diagnostics.partial_publications >= 1


@pytest.mark.parametrize("name", PLANTS)
async def test_an_error_in_the_middle_ends_the_attempt_and_the_retry_completes(
    hass, name
):
    expected = await baseline(hass, name)
    cloud, gateway = plant(name)
    cloud.hold_at = 8
    seen: list = []
    with cloud_patches(cloud, RealClock(), gateway):
        async with running(hass, Backend(), configure=only_first) as (
            entry,
            _,
            owner,
            harness,
        ):
            await owner.start_platforms()
            await partial_shown(owner, cloud, seen)
            first = seen[0]
            cloud.fail_at = len(cloud.requests) + 1
            cloud.release.set()
            harness.retry.set()  # the source retries without waiting
            await eventually(
                lambda: source_for(owner, GATEWAY) not in (None, first), timeout=60
            )
            end = await settled(hass, entry, owner, cloud)
            # Nothing of the partial result is left in the failed attempt.
            assert not first.resources
            assert not any(item.available for item in (first.data or {}).values())
            assert end == expected
            assert not first.discovery_diagnostics.completed


@pytest.mark.parametrize("name", PLANTS)
async def test_stopping_the_access_in_the_middle_keeps_nothing_and_starts_again(
    hass, name
):
    expected = await baseline(hass, name)
    cloud, gateway = plant(name)
    cloud.hold_at = 8
    seen: list = []
    with cloud_patches(cloud, RealClock(), gateway):
        async with running(hass, Backend(), configure=only_first) as (
            entry,
            connection,
            owner,
            _,
        ):
            await owner.start_platforms()
            await partial_shown(owner, cloud, seen)
            first = seen[0]
            shown = first.discovery_diagnostics.partial_publications
            management = owner.connections.management
            management.set_binding_enabled(
                management.edit(connection), binding_for(owner, GATEWAY), False
            )
            cloud.release.set()
            await eventually(lambda: source_for(owner, GATEWAY) is None, timeout=30)
            await asyncio.sleep(1)
            # The stopped discovery shows nothing more.
            assert first.discovery_diagnostics.partial_publications == shown
            assert not first.discovery_diagnostics.completed
            management.set_binding_enabled(
                management.edit(connection), binding_for(owner, GATEWAY), True
            )
            await eventually(
                lambda: source_for(owner, GATEWAY) not in (None, first), timeout=60
            )
            assert await settled(hass, entry, owner, cloud) == expected


@pytest.mark.parametrize("name", PLANTS)
async def test_a_restart_in_the_middle_ends_like_a_complete_first_start(hass, name):
    expected = await baseline(hass, name)
    cloud, gateway = plant(name)
    cloud.hold_at = 8
    seen: list = []
    with cloud_patches(cloud, RealClock(), gateway):
        async with running(hass, Backend(), configure=only_first) as (
            entry,
            _,
            owner,
            _,
        ):
            await owner.start_platforms()
            await partial_shown(owner, cloud, seen)
            early = set(entries(hass, entry))
            assert early
        # Home Assistant stops while the discovery is held; nothing was stored.
        cloud.hold_at = None
        async with running(hass, Backend(), restart=entry) as (_, _, owner, _):
            await owner.start_platforms()
            end = await settled(hass, entry, owner, cloud)
            assert early <= set(end)
            assert end == expected


async def test_a_start_with_an_inventory_shows_no_partial_result(hass):
    cloud, gateway = plant("k40")
    with cloud_patches(cloud, RealClock(), gateway):
        async with running(hass, Backend(), configure=only_first) as (
            entry,
            _,
            owner,
            _,
        ):
            await owner.start_platforms()
            await settled(hass, entry, owner, cloud)
        cloud.delay = DELAY
        async with running(hass, Backend(), restart=entry) as (_, _, owner, _):
            await owner.start_platforms()
            await settled(hass, entry, owner, cloud)
            coordinator = source_for(owner, GATEWAY)
            assert not coordinator.progressive_discovery
            assert coordinator.discovery_diagnostics.partial_publications == 0


@pytest.mark.parametrize("name", PLANTS)
async def test_a_later_discovery_never_shows_a_partial_result(hass, monkeypatch, name):
    cloud, gateway = plant(name, delay=0)
    with cloud_patches(cloud, RealClock(), gateway):
        async with running(hass, Backend(), configure=only_first) as (
            entry,
            _,
            owner,
            _,
        ):
            await owner.start_platforms()
            await settled(hass, entry, owner, cloud)
            coordinator = source_for(owner, GATEWAY)
            before = set(coordinator.resources)
            monkeypatch.setattr(discovery_partial, "PARTIAL_AFTER", 0.0)
            shown = []
            with patch.object(
                ManagedPointTCoordinator,
                "_publish_partial",
                lambda self: shown.append(1),
            ):
                coordinator.firmware.due = 0.0
                requests = len(cloud.requests)
                await coordinator.async_refresh()
            assert len(cloud.requests) > requests
            assert coordinator.discovery_diagnostics.completed
            assert not shown
            assert coordinator.discovery_diagnostics.partial_publications == 0
            assert before <= set(coordinator.resources)


@pytest.mark.parametrize("name", PLANTS)
async def test_a_fast_cloud_shows_nothing_before_the_time_limit(
    hass, monkeypatch, name
):
    monkeypatch.setattr(discovery_partial, "PARTIAL_AFTER", 3600.0)
    cloud, gateway = plant(name, delay=0)
    shown = []
    with (
        cloud_patches(cloud, RealClock(), gateway),
        patch.object(
            ManagedPointTCoordinator, "_publish_partial", lambda self: shown.append(1)
        ),
    ):
        async with running(hass, Backend(), configure=only_first) as (
            entry,
            _,
            owner,
            _,
        ):
            await owner.start_platforms()
            await settled(hass, entry, owner, cloud)
    assert not shown


async def test_the_room_controller_is_known_before_anything_is_shown(hass, monkeypatch):
    """Entities of a room controller carry other names than those of a heating
    system: nothing is shown until the evidence is answered."""
    monkeypatch.setattr(discovery_partial, "PARTIAL_AFTER", 0.0)
    cloud, gateway = plant("ct200", delay=0.02)
    kinds: list[bool] = []

    def record(self):
        kinds.append(is_rrc_plant(self))

    with (
        cloud_patches(cloud, RealClock(), gateway),
        patch.object(ManagedPointTCoordinator, "_publish_partial", record),
    ):
        async with running(hass, Backend(), configure=only_first) as (
            entry,
            _,
            owner,
            _,
        ):
            await owner.start_platforms()
            await settled(hass, entry, owner, cloud)
    assert kinds
    assert all(kinds)


async def test_discovery_reports_its_progress_after_each_answered_block() -> None:
    resources = {
        "/heatingCircuits": Resource(
            path="/heatingCircuits",
            references=(ResourceReference("/heatingCircuits/hc1"),),
        ),
        "/heatingCircuits/hc1": Resource(
            path="/heatingCircuits/hc1",
            references=(ResourceReference("/heatingCircuits/hc1/operationMode"),),
        ),
        "/heatingCircuits/hc1/operationMode": Resource(
            path="/heatingCircuits/hc1/operationMode", value="auto", has_value=True
        ),
    }
    client = AsyncMock()
    client.get_resources_bulk.side_effect = lambda gateway, paths: tuple(
        BatchItemResult(gateway, path, 200, resource=resources[path])
        if path in resources
        else BatchItemResult(gateway, path, 404, error=ResourceNotFound(path, 404))
        for path in paths
    )
    sizes: list[int] = []
    result = await async_discover_resources(
        client,
        "gateway",
        roots=("/heatingCircuits",),
        on_progress=lambda found: sizes.append(len(found)),
    )
    assert len(sizes) == client.get_resources_bulk.await_count
    assert sizes == sorted(sizes) and sizes[0] < sizes[-1] == len(result)


def _item(**values):
    return SimpleNamespace(
        **{"discovered": False, "read_policy": "normal", "bulk_result": ""} | values
    )


def test_the_kind_of_plant_is_settled_by_evidence_or_by_definite_answers() -> None:
    settle = discovery_partial.profile_settled
    # Not asked yet: undecided.
    assert not settle(SimpleNamespace(paths={}), {}, None)
    # The container answered and does not list the evidence: no room controller.
    both = {"/zones": _item(discovered=True), "/gateway": _item(discovered=True)}
    assert settle(SimpleNamespace(paths=both), {}, None)
    # An evidence path asked but still open (or failed with a server error).
    pending = {**both, ZONE_LIST: _item(bulk_result="pending")}
    assert not settle(SimpleNamespace(paths=pending), {}, None)
    server = {**both, PRODUCT_TYPE_PATH: _item(bulk_result="http_500")}
    assert not settle(SimpleNamespace(paths=server), {}, None)
    # A definite answer (not found) settles it.
    missing = {**both, ZONE_LIST: _item(bulk_result="http_404")}
    assert settle(SimpleNamespace(paths=missing), {}, None)
    # A container that failed leaves the evidence open.
    broken = {**both, "/zones": _item(bulk_result="http_500")}
    assert not settle(SimpleNamespace(paths=broken), {}, None)
    # Positive evidence needs nothing else.
    zone = Resource(path="/zones/zn1", value=1, has_value=True)
    assert settle(SimpleNamespace(paths={}), {"/zones/zn1": zone}, None)
