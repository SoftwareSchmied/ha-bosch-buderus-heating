"""Targeted reads in the discovery: trees, the K40 request plan, real cycles.

The discovery reads the paths of the rules of ``module_probes`` after every
other path, as optional paths; ``/system/info`` comes with the first batch.
The trees are synthetic; my own K40 comes from its cloud capture. Failures
run through real coordinator cycles (``async_refresh``): an error of a
targeted read keeps every reading of the discovery and raises no repair; a
rate limit pauses the cloud access as any 429 does, and the fault sources
read after the discovery follow once the pause is over. Not
yet confirmed on a real plant: a hybrid system without a listed heat pump,
a hot water circuit without a list, a cascade, and pool, ventilation or
solar found through a place.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.bosch_buderus_heating import (
    derived_store as derived_store_module,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
)
from custom_components.bosch_buderus_heating.derived_store import derived_store
from custom_components.bosch_buderus_heating.diagnostics import (
    _discovery_diagnostics,
)
from custom_components.bosch_buderus_heating.discovery import (
    MAX_EXTRA_PROBES,
    ROOT_RESOURCE_PATHS,
    DiscoveryDiagnostics,
    DiscoveryPathSource,
    async_discover_resources,
)
from custom_components.bosch_buderus_heating.module_probes import (
    CASCADE_LEAVES,
    HOT_WATER_PATHS,
    HYBRID_PATHS,
    POOL_PATHS,
    SOLAR_PATHS,
    VENTILATION_PATHS,
)
from custom_components.bosch_buderus_heating.plant_inputs import (
    existing_tracker,
    row_key,
    track_plant,
)
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    RequestTimeout,
    Resource,
    ResourceMetadata,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.pointt.transport import RateLimitBackoff
from tests.ha_test_helpers import MockConfigEntry, flush_store
from tests.helpers.rrc_cloud import RrcCloud, load_tree, with_containers

GATEWAY_ID = "gateway-one"
COORDINATOR = "custom_components.bosch_buderus_heating.coordinator"
MX300 = Gateway(GATEWAY_ID, device_type="mx300", firmware_version="16.00.01")
DHW1 = "/dhwCircuits/dhw1"
OUTDOOR = "/system/sensors/temperatures/outdoor_t1"


def _leaf(path: str, value: object, unit: str | None = None) -> Resource:
    kind = "floatValue" if isinstance(value, int | float) else "stringValue"
    return Resource(
        path,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(resource_type=kind, unit=unit),
    )


def _info(*rows: tuple[int, int, str]) -> Resource:
    return Resource(
        "/system/info",
        values=tuple({"Id": str(i), "Tok": str(t), "Ver": v} for i, t, v in rows),
        has_values=True,
        metadata=ResourceMetadata(resource_type="systeminfo"),
    )


def plant(
    leaves: dict[str, Resource], *, hidden: tuple[str, ...] = ()
) -> dict[str, Resource]:
    """A tree whose containers list every leaf except the ``hidden`` paths
    (and everything below them); those still answer when read directly."""
    tree = with_containers(leaves)
    for path, resource in list(tree.items()):
        if resource.references:
            tree[path] = Resource(
                path,
                metadata=resource.metadata,
                references=tuple(
                    reference
                    for reference in resource.references
                    if not any(
                        reference.path == item or reference.path.startswith(f"{item}/")
                        for item in hidden
                    )
                ),
            )
    return tree


def heating(**extra: Resource) -> dict[str, Resource]:
    """A heat pump with one heating circuit, hot water and the outdoor value."""
    return {
        "/heatingCircuits/hc1/operationMode": _leaf(
            "/heatingCircuits/hc1/operationMode", "auto"
        ),
        f"{DHW1}/operationMode": _leaf(f"{DHW1}/operationMode", "eco"),
        "/heatSources/hs1/type": _leaf("/heatSources/hs1/type", "Heatpump"),
        "/heatSources/hs1/numberOfStarts": _leaf("/heatSources/hs1/numberOfStarts", 10),
        OUTDOOR: _leaf(OUTDOOR, 5.0, "C"),
        "/system/info": _info((8, 8, "1.0"), (17, 72, "1.0")),
        **{resource.path: resource for resource in extra.values()},
    }


async def discover(
    cloud: RrcCloud, gateway: Gateway = MX300, **kwargs: Any
) -> tuple[dict[str, Resource], DiscoveryDiagnostics]:
    report = DiscoveryDiagnostics()
    found = await async_discover_resources(
        cloud,
        GATEWAY_ID,
        gateway=gateway,
        diagnostics=report,
        maximum_extra_probes=kwargs.pop("maximum_extra_probes", MAX_EXTRA_PROBES),
        **kwargs,
    )
    return found, report


def probe_bulks(cloud: RrcCloud, paths: set[str]) -> list[int]:
    return [index for index, bulk in enumerate(cloud.requests) if set(bulk) & paths]


def assert_last(cloud: RrcCloud, paths: set[str]) -> None:
    """The targeted reads come after every other path (their own batches)."""
    first = min(probe_bulks(cloud, paths))
    before = {path for bulk in cloud.requests[:first] for path in bulk}
    assert not before & paths
    assert all(set(bulk) <= paths | _derived(cloud) for bulk in cloud.requests[first:])


def _derived(cloud: RrcCloud) -> set[str]:
    """Paths read because a targeted read answered (the circuit's children)."""
    return {path for bulk in cloud.requests for path in bulk if path.startswith(DHW1)}


# ------------------------------------------------------------------ trees


async def test_a_hybrid_without_a_listed_heat_pump_reads_hs2() -> None:
    leaves = heating(
        hs1=_leaf("/heatSources/hs1/type", "GasBoiler"),
        outdoor=_leaf("/heatSources/hybrid/outdoorStatus", "CONNECTED"),
        info=_info((8, 8, "1.0"), (248, 83, "06.02.00")),
        defrost=_leaf("/heatSources/hs2/defrostActive", "false"),
        starts=_leaf("/heatSources/hs2/numberOfStarts", 120),
    )
    cloud = RrcCloud(plant(leaves, hidden=("/heatSources/hs2",)), missing_as_500=False)
    found, report = await discover(cloud)

    for path in HYBRID_PATHS:
        assert cloud.requested(path) == 1, path
        assert report.paths[path].source is DiscoveryPathSource.OPTIONAL
    assert "/heatSources/hs2/defrostActive" in found
    assert "/heatSources/hs2/numberOfStarts" in found
    assert_last(cloud, set(HYBRID_PATHS))
    probes = report.module_probes
    assert probes is not None
    assert probes["rules"]["R1"]["evidence"] == [  # type: ignore[index]
        "outdoor_status_connected",
        "tok83_id248",
    ]
    assert probes["rules"]["R1"]["found"] == 2  # type: ignore[index]
    assert report.completed and report.stop_reason == "complete"
    results = {
        item["path"]: item["result"] for item in report.module_probe_results or ()
    }
    assert results["/heatSources/hs2/defrostActive"] == "ok"
    assert results["/heatSources/actualHeatDemand"] == "404"


async def test_a_hot_water_circuit_without_a_list_is_read_with_its_children() -> None:
    """#131: the hot water list names no circuit, dhw1 answers anyway."""
    leaves = heating(
        temp=_leaf(f"{DHW1}/actualTemp", 48.0, "C"),
        setpoint=_leaf(f"{DHW1}/currentSetpoint", 50.0, "C"),
    )
    cloud = RrcCloud(plant(leaves, hidden=(DHW1,)), missing_as_500=False)
    found, report = await discover(cloud)

    assert cloud.requested(f"{DHW1}/operationMode") == 1
    assert cloud.requested("/dhwCircuits/dhw2/operationMode") == 1
    assert {
        f"{DHW1}/operationMode",
        f"{DHW1}/actualTemp",
        f"{DHW1}/currentSetpoint",
    } <= set(found)
    assert report.direct_probes[DHW1].reason == "probe_answered"
    assert report.paths[f"{DHW1}/actualTemp"].source is DiscoveryPathSource.OPTIONAL
    assert_last(cloud, set(HOT_WATER_PATHS))
    assert report.listing_refs["dhwCircuits"] == []


async def test_a_listed_hot_water_circuit_needs_no_probe() -> None:
    cloud = RrcCloud(plant(heating()), missing_as_500=False)
    _found, report = await discover(cloud)
    assert cloud.requested("/dhwCircuits/dhw2/operationMode") == 0
    assert report.module_probes["planned"] == 0  # type: ignore[index]
    assert report.module_probe_results == ()


async def test_a_cascade_of_three_reads_the_unlisted_heat_pumps() -> None:
    leaves = heating(
        kind=_leaf("/system/type", "heatpump_cascade"),
        count=_leaf("/heatSources/numberOfRefrigerantCircuitsInstalled", 3),
        hs2=_leaf("/heatSources/hs2/numberOfStarts", 7),
        hs3=_leaf("/heatSources/hs3/refrigerant/status", "on"),
    )
    cloud = RrcCloud(
        plant(leaves, hidden=("/heatSources/hs2", "/heatSources/hs3")),
        missing_as_500=False,
    )
    found, report = await discover(cloud)
    wanted = {
        f"/heatSources/hs{index}/{leaf}" for index in (2, 3) for leaf in CASCADE_LEAVES
    }
    assert {path for path in wanted if cloud.requested(path) == 1} == wanted
    assert {
        "/heatSources/hs2/numberOfStarts",
        "/heatSources/hs3/refrigerant/status",
    } <= set(found)
    assert not any(
        cloud.requested(f"/heatSources/hs1/{leaf}") for leaf in CASCADE_LEAVES[:2]
    )
    assert report.module_probes["rules"]["R6"]["evidence"] == [  # type: ignore[index]
        "system_type_cascade:3"
    ]


async def test_a_refused_pool_is_read_through_its_place() -> None:
    leaves = heating(
        info=_info((8, 8, "1.0"), (231, 17, "1.0")),
        enabled=_leaf("/pool/enabled", "true"),
        current=_leaf("/pool/currentTemp", 27.5, "C"),
    )
    cloud = RrcCloud(plant(leaves), missing_as_500=False)
    cloud.down["/pool"] = 403
    found, report = await discover(cloud)
    for path in POOL_PATHS:
        assert cloud.requested(path) == 1, path
    assert {"/pool/enabled", "/pool/currentTemp"} <= set(found)
    assert report.listing_refs["pool"] == "http_403"
    assert report.area_check["areas"] == [  # type: ignore[index]
        {"token": 17, "id": 231, "place": "pool", "area": "/pool", "found": True}
    ]


async def test_a_system_that_cannot_be_listed_still_shows_its_components() -> None:
    cloud = RrcCloud(plant(heating()), missing_as_500=False)
    cloud.down["/system"] = 403
    found, report = await discover(cloud)
    assert "/system" not in found
    assert "/system/info" in found
    assert "/system/info" in cloud.requests[0]
    assert report.system_info_bulk == 1


async def test_my_k40_reads_nothing_more_and_its_components_first() -> None:
    """Criterion 3: no targeted read on my own K40, every path read once and
    /system/info in the first batch (the golden master compares the rest)."""
    cloud = RrcCloud(
        load_tree("captures/k40-cloud-2026-11-05.json"),
        forbidden=set(),
        missing_as_500=False,
    )
    found, report = await discover(cloud, Gateway(GATEWAY_ID, device_type="k40"))
    assert cloud.requests[0] == (*ROOT_RESOURCE_PATHS, "/system/info")
    assert report.system_info_bulk == 1
    read = [path for bulk in cloud.requests for path in bulk]
    assert len(read) == len(set(read))
    probes = report.module_probes
    assert probes is not None and probes["active"] is True
    assert probes["planned"] == 0 and probes["rules"] == {}
    assert report.module_probe_results == ()
    assert "/system/info" in found
    assert report.listing_refs["dhwCircuits"] == ["dhw1"]


async def test_an_icom_gateway_gets_no_targeted_read() -> None:
    leaves = heating(info=_info((172, 8, "1.0"), (248, 83, "06.02.00")))
    cloud = RrcCloud(plant(leaves, hidden=(DHW1,)), missing_as_500=False)
    _found, report = await discover(cloud, Gateway(GATEWAY_ID, device_type="icom"))
    assert not any(
        cloud.requested(path) for path in (*HYBRID_PATHS, *HOT_WATER_PATHS[1:])
    )
    assert report.module_probes["active"] is False  # type: ignore[index]
    assert report.module_probes["inactive_reason"] == "icom"  # type: ignore[index]
    assert report.module_probe_results is None


async def test_a_room_controller_gets_no_targeted_read() -> None:
    leaves = heating(
        info=_info(
            (248, 83, "06.02.00"),
        )
    )
    cloud = RrcCloud(plant(leaves, hidden=(DHW1,)), missing_as_500=False)
    _found, report = await discover(cloud, rrc_evidence=("zones",))
    assert not any(cloud.requested(path) for path in HYBRID_PATHS)
    assert report.module_probes["inactive_reason"] == "room_controller"  # type: ignore[index]


async def test_the_search_for_missing_parts_reads_no_targeted_path() -> None:
    cloud = RrcCloud(plant(heating(), hidden=(DHW1,)), missing_as_500=False)
    _found, report = await discover(
        cloud, roots=("/dhwCircuits", "/heatingCircuits"), rediscovery=True
    )
    assert not any(cloud.requested(path) for path in HOT_WATER_PATHS)
    assert report.module_probes["inactive_reason"] == "search_for_missing_parts"  # type: ignore[index]


async def test_a_used_up_budget_leaves_every_other_read_as_it_was() -> None:
    tree = plant(heating(), hidden=(DHW1,))
    baseline = RrcCloud(tree, missing_as_500=False)
    with patch(
        "custom_components.bosch_buderus_heating.discovery.ModuleProbes.plan",
        lambda self, known: [],
    ):
        core, _report = await discover(baseline, maximum_extra_probes=0)
    budget = sum(len(bulk) for bulk in baseline.requests)

    # The shared budget ends with the core paths: no targeted read, the same
    # core result, and the discovery counts as complete.
    cloud = RrcCloud(tree, missing_as_500=False)
    found, report = await discover(
        cloud, maximum_resources=budget, maximum_extra_probes=0
    )
    assert set(found) == set(core)
    assert cloud.requests == baseline.requests
    assert report.completed
    assert report.module_probes["stopped"] == "resource_limit"  # type: ignore[index]
    assert {item["result"] for item in report.module_probe_results or ()} == {"skipped"}

    # With the allowance beyond the shared budget they are read after them.
    cloud = RrcCloud(tree, missing_as_500=False)
    found, report = await discover(cloud, maximum_resources=budget)
    assert set(core) < set(found)
    assert cloud.requests[: len(baseline.requests)] == baseline.requests
    assert all(cloud.requested(path) == 1 for path in HOT_WATER_PATHS)
    assert report.extra_probes_requested > 0


@pytest.mark.parametrize("info_answers", [True, False])
async def test_a_server_error_of_the_component_list_counts_towards_no_series(
    info_answers: bool,
) -> None:
    """``/system/info`` is read with the roots; like a tariff leaf, its 5xx is
    taken as missing and never moves the limit of the roots' 5xx series."""
    tree = plant(heating())
    absent = [root for root in ROOT_RESOURCE_PATHS if root not in tree][:9]
    assert len(absent) == 9
    cloud = RrcCloud(tree, missing_as_500=False)
    for root in absent:
        cloud.down[root] = 500
        cloud.get_errors[root] = ServiceUnavailable(500)
    if not info_answers:
        cloud.down["/system/info"] = 500
        cloud.get_errors["/system/info"] = ServiceUnavailable(500)
    found, report = await discover(cloud)
    assert report.completed and report.stop_reason == "complete"
    assert ("/system/info" in found) is info_answers
    assert OUTDOOR in found
    if not info_answers:
        assert (
            report.paths["/system/info"].read_policy == "optional_5xx_assumed_missing"
        )


@pytest.mark.parametrize("failing", ["observe", "plan", "circuit_answered"])
async def test_an_error_of_the_targeted_reads_ends_only_them(failing: str) -> None:
    tree = plant(heating(), hidden=(DHW1,))
    baseline = RrcCloud(tree, missing_as_500=False)
    with patch(
        "custom_components.bosch_buderus_heating.discovery.ModuleProbes.plan",
        lambda self, known: [],
    ):
        core, _report = await discover(baseline)
    cloud = RrcCloud(tree, missing_as_500=False)
    with patch(
        f"custom_components.bosch_buderus_heating.discovery.ModuleProbes.{failing}",
        side_effect=RuntimeError("probes"),
    ):
        found, report = await discover(cloud)
    assert report.completed and report.stop_reason == "complete"
    assert set(core) <= set(found)
    if failing != "circuit_answered":
        assert set(found) == set(core)
    assert report.module_probes["inactive_reason"] == "error"  # type: ignore[index]


# ------------------------------------------------------- real coordinator cycles


class Clock:
    def __init__(self) -> None:
        self.now = 10_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    clock = Clock()
    with (
        patch(f"{COORDINATOR}.monotonic", clock),
        patch(f"{COORDINATOR}.fault_resource_candidates", return_value=()),
    ):
        yield clock


class Cloud(RrcCloud):
    """The fake cloud with the client's brake (never active here)."""

    class backoff:
        @staticmethod
        def raise_if_active() -> None:
            return None


def _many_targets() -> dict[str, Resource]:
    """A hybrid system with ventilation and solar places whose containers
    answer 500: 19 targeted reads (R1 5, R3 9, R4 5), only hs2's starts
    answer."""
    leaves = heating(
        hs1=_leaf("/heatSources/hs1/type", "GasBoiler"),
        outdoor=_leaf("/heatSources/hybrid/outdoorStatus", "CONNECTED"),
        info=_info((8, 8, "1.0"), (231, 81, "1.0"), (231, 48, "1.0")),
        starts=_leaf("/heatSources/hs2/numberOfStarts", 3),
    )
    return plant(leaves, hidden=("/heatSources/hs2",))


def _targets() -> list[str]:
    return [*HYBRID_PATHS, *VENTILATION_PATHS, *SOLAR_PATHS]


def _cloud() -> RrcCloud:
    cloud = Cloud(_many_targets(), missing_as_500=False)
    cloud.down["/ventilation"] = 403
    cloud.down["/solarCircuits"] = 403
    return cloud


def _coordinator(
    hass: HomeAssistant, cloud: RrcCloud
) -> BoschBuderusDataUpdateCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, version=3)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(hass, cloud, MX300, entry)
    coordinator.update_interval = None
    return coordinator


def _repairs(hass: HomeAssistant) -> list[str]:
    return [key[1] for key in ir.async_get(hass).issues if key[0] == DOMAIN]


async def _cycle(coordinator: BoschBuderusDataUpdateCoordinator, clock: Clock) -> None:
    clock.now += 60
    await coordinator.async_refresh()


def _healthy(coordinator: BoschBuderusDataUpdateCoordinator) -> None:
    """Every core reading is there and available."""
    assert coordinator.last_update_success
    for path in (OUTDOOR, "/heatSources/hs1/numberOfStarts", f"{DHW1}/operationMode"):
        assert coordinator.data[path].available, path


@pytest.mark.parametrize(
    ("failing", "stopped", "answering"),
    [
        (9, None, True),
        (14, "service_unavailable", True),
        # No targeted read answers at all: the cloud answered every other
        # path just before, so the same limit of ten applies.
        (9, None, False),
    ],
)
async def test_a_series_of_server_errors_on_targeted_reads(
    hass: HomeAssistant,
    clock: Clock,
    failing: int,
    stopped: str | None,
    answering: bool,
) -> None:
    """Fewer than ten 5xx answers in a row are taken as missing; from ten on
    the targeted reads stop. Neither touches a reading nor raises a repair."""
    cloud = _cloud()
    if not answering:
        del cloud.tree["/heatSources/hs2/numberOfStarts"]
    targets = [path for path in _targets() if path != "/heatSources/hs2/numberOfStarts"]
    for path in targets[:failing]:
        # A server error in the batch, then on the single read.
        cloud.down[path] = 500
        cloud.get_errors[path] = ServiceUnavailable(500)
    coordinator = _coordinator(hass, cloud)
    await coordinator.async_refresh()
    _healthy(coordinator)
    report = coordinator.discovery_diagnostics
    probes = report.module_probes
    assert probes is not None and probes["stopped"] == stopped
    assert report.completed and report.stop_reason == "complete"
    results = {
        item["path"]: item["result"] for item in report.module_probe_results or ()
    }
    assert [results[path] for path in targets[: min(failing, 10)]] == ["5xx"] * min(
        failing, 10
    )
    if answering:
        assert results["/heatSources/hs2/numberOfStarts"] == "ok"
        assert "/heatSources/hs2/numberOfStarts" in coordinator.data
    # The single reads after a 5xx in the batch stop at the tenth in a row.
    assert len([path for path in cloud.single if path in targets]) == min(failing, 10)
    await _cycle(coordinator, clock)
    _healthy(coordinator)
    assert not _repairs(hass)
    await coordinator.async_shutdown()


async def test_a_rate_limit_among_the_targeted_reads_keeps_the_readings(
    hass: HomeAssistant, clock: Clock
) -> None:
    cloud = _cloud()
    cloud.down[VENTILATION_PATHS[0]] = 429
    coordinator = _coordinator(hass, cloud)
    await coordinator.async_refresh()
    _healthy(coordinator)
    probes = coordinator.discovery_diagnostics.module_probes
    assert probes is not None and probes["stopped"] == "rate_limited"
    results = {
        item["path"]: item["result"]
        for item in coordinator.discovery_diagnostics.module_probe_results or ()
    }
    assert results[VENTILATION_PATHS[0]] == "429"
    # The other answers of the same batch are kept.
    assert results["/heatSources/hs2/numberOfStarts"] == "ok"
    assert "/heatSources/hs2/numberOfStarts" in coordinator.data
    assert not _repairs(hass)
    await coordinator.async_shutdown()


class BrakedCloud(RrcCloud):
    """The fake cloud behind the client's real brake: a 429 in a bulk answer
    pauses every further request of the access, as the client does."""

    def __init__(self, tree: dict[str, Resource], clock: Clock) -> None:
        super().__init__(tree, missing_as_500=False)
        self.backoff = RateLimitBackoff(clock=clock)

    async def get_resources_bulk(self, gateway_id, paths, **kwargs):
        self.backoff.raise_if_active()
        results = await super().get_resources_bulk(gateway_id, paths, **kwargs)
        if any(result.status == 429 for result in results):
            self.backoff.activate(None)
        return results

    async def get_resource(self, gateway_id, path, **kwargs):
        self.backoff.raise_if_active()
        return await super().get_resource(gateway_id, path, **kwargs)


ACTIVE_FAILURE = "/heatSources/hs1/activefailure"


@pytest.fixture
def brake_clock():
    """The coordinator's clock only: the fault sources are really looked for."""
    clock = Clock()
    with patch(f"{COORDINATOR}.monotonic", clock):
        yield clock


def _braked_cloud(clock: Clock) -> BrakedCloud:
    tree = _many_targets()
    # Not listed by its heat source: read only as a fault source.
    tree[ACTIVE_FAILURE] = Resource(
        ACTIVE_FAILURE, value={"ccd": 6249, "fc": "12"}, has_value=True
    )
    cloud = BrakedCloud(tree, clock)
    cloud.down["/ventilation"] = 403
    cloud.down["/solarCircuits"] = 403
    return cloud


async def test_fault_sources_are_read_without_a_rate_limit(
    hass: HomeAssistant, brake_clock: Clock
) -> None:
    cloud = _braked_cloud(brake_clock)
    coordinator = _coordinator(hass, cloud)
    await coordinator.async_refresh()
    _healthy(coordinator)
    assert cloud.requested(ACTIVE_FAILURE) == 1
    assert ACTIVE_FAILURE in coordinator.resources
    assert [fault.component_id for fault in coordinator.faults.active] == ["hs1"]
    await coordinator.async_shutdown()


@pytest.mark.parametrize("limited", ["targeted read", "fault source"])
async def test_a_rate_limit_among_the_targeted_reads_keeps_the_fault_sources(
    hass: HomeAssistant, brake_clock: Clock, limited: str
) -> None:
    """The 429 of a targeted read pauses the access; the fault sources the
    discovery reads afterwards are held back, kept and read in the first
    cycle after the pause, not only after the next start. So is a fault
    source that answers 429 itself."""
    cloud = _braked_cloud(brake_clock)
    first = VENTILATION_PATHS[0] if limited == "targeted read" else ACTIVE_FAILURE
    cloud.down[first] = 429
    coordinator = _coordinator(hass, cloud)
    await coordinator.async_refresh()
    _healthy(coordinator)
    assert coordinator.discovery_diagnostics.completed
    assert cloud.requested(ACTIVE_FAILURE) == (1 if limited == "fault source" else 0)
    assert ACTIVE_FAILURE not in coordinator.resources
    assert cloud.backoff.remaining > 0
    cloud.down.pop(ACTIVE_FAILURE, None)
    asked = cloud.requested(ACTIVE_FAILURE)

    # Within the pause no request leaves (the brake of every 429).
    brake_clock.now += 60
    await coordinator.async_refresh()
    assert cloud.requested(ACTIVE_FAILURE) == asked

    # After the pause: the first cycle that returns data reads them.
    for _ in range(6):
        brake_clock.now += 301
        await coordinator.async_refresh()
        if coordinator.last_update_success:
            break
    _healthy(coordinator)
    assert cloud.requested(ACTIVE_FAILURE) == asked + 1
    assert ACTIVE_FAILURE in coordinator.resources
    assert [fault.component_id for fault in coordinator.faults.active] == ["hs1"]
    # Once read, they are not read again outside the regular polls.
    requested = cloud.requested(ACTIVE_FAILURE)
    brake_clock.now += 60
    await coordinator.async_refresh()
    assert cloud.requested(ACTIVE_FAILURE) <= requested + 1
    await coordinator.async_shutdown()


async def test_a_timeout_of_the_targeted_reads_keeps_the_readings(
    hass: HomeAssistant, clock: Clock
) -> None:
    cloud = _cloud()
    cloud.request_errors[HYBRID_PATHS[0]] = RequestTimeout("PointT request timed out")
    coordinator = _coordinator(hass, cloud)
    await coordinator.async_refresh()
    _healthy(coordinator)
    probes = coordinator.discovery_diagnostics.module_probes
    assert probes is not None and probes["stopped"] == "timeout"
    assert not _repairs(hass)
    await coordinator.async_shutdown()


async def test_only_428_on_targeted_reads_raises_no_repair(
    hass: HomeAssistant, clock: Clock
) -> None:
    cloud = _cloud()
    for path in _targets():
        cloud.down[path] = 428
    coordinator = _coordinator(hass, cloud)
    await coordinator.async_refresh()
    _healthy(coordinator)
    snapshot = coordinator.discovery_diagnostics.snapshot()
    assert snapshot["access_reason"] != "precondition_required"
    results = coordinator.discovery_diagnostics.module_probe_results or ()
    assert {item["result"] for item in results} == {"other"}
    await _cycle(coordinator, clock)
    _healthy(coordinator)
    assert not _repairs(hass)
    await coordinator.async_shutdown()


# The frozen clock starts before the patched one: freezegun remembers per
# module which names are clocks, and a module scanned while its clock is
# patched would stay unfrozen in later tests.
async def test_the_last_run_is_kept_and_shown_after_a_restart(
    hass: HomeAssistant, freezer: Any, clock: Clock
) -> None:
    """B12: the results go into the plant model's row once the model starts
    (the first discovery runs before it), only when they change; after a
    restart the diagnostics show them as restored. They never skip a read."""
    cloud = _cloud()
    coordinator = _coordinator(hass, cloud)
    await coordinator.async_refresh()
    assert existing_tracker(coordinator) is None
    tracker = track_plant(coordinator.config_entry, coordinator)
    assert tracker is not None
    await hass.async_block_till_done()
    row = derived_store(hass).get(row_key(GATEWAY_ID))
    assert row is not None
    stored = {item["path"]: item["result"] for item in row["probes"]}
    assert stored["/heatSources/hs2/numberOfStarts"] == "ok"
    assert stored["/heatSources/hs2/defrostActive"] == "404"
    assert len(row["probes"]) == len(_targets())
    report = _discovery_diagnostics(coordinator)["module_probes"]
    assert report["restored"] is False  # type: ignore[index]
    writes = tracker.writes

    # The same answers again (a requested search): nothing is written, and
    # every targeted read is asked again, the stored 404 included.
    asked = cloud.requested("/heatSources/hs2/defrostActive")
    freezer.tick(3600)
    assert coordinator.request_rescan() == "planned"
    await _cycle(coordinator, clock)
    assert cloud.requested("/heatSources/hs2/defrostActive") == asked + 1
    assert tracker.writes == writes

    # A changed answer is written.
    cloud.tree["/heatSources/hs2/defrostActive"] = _leaf(
        "/heatSources/hs2/defrostActive", "false"
    )
    clock.now += 3600
    freezer.tick(3600)
    assert coordinator.request_rescan() == "planned"
    await _cycle(coordinator, clock)
    assert tracker.writes == writes + 1
    kept = {item["path"]: item["at"] for item in tracker.probes}
    # Only the changed result has a new time.
    assert (
        kept["/heatSources/hs2/defrostActive"] > kept["/heatSources/actualHeatDemand"]
    )
    await coordinator.async_shutdown()

    # A restart that starts without a discovery shows the last run.
    await flush_store(derived_store(hass)._store)
    hass.data.pop(derived_store_module._STORE)
    restarted = _coordinator(hass, cloud)
    again = track_plant(restarted.config_entry, restarted)
    assert again is not None
    await hass.async_block_till_done()
    shown = _discovery_diagnostics(restarted)["module_probes"]
    assert shown["restored"] is True  # type: ignore[index]
    results = {item["path"]: item["result"] for item in shown["results"]}  # type: ignore[index]
    assert results["/heatSources/hs2/defrostActive"] == "ok"
    assert all(item["seconds_ago"] >= 0 for item in shown["results"])  # type: ignore[index]
    await restarted.async_shutdown()


async def test_a_plant_without_targeted_reads_writes_nothing(
    hass: HomeAssistant, clock: Clock
) -> None:
    cloud = RrcCloud(plant(heating()), missing_as_500=False)
    coordinator = _coordinator(hass, cloud)
    await coordinator.async_refresh()
    tracker = track_plant(coordinator.config_entry, coordinator)
    assert tracker is not None
    await hass.async_block_till_done()
    assert tracker.probes == ()
    row = derived_store(hass).get(row_key(GATEWAY_ID))
    assert row is not None and row["probes"] == []
    writes = tracker.writes
    assert coordinator.request_rescan() == "planned"
    await _cycle(coordinator, clock)
    assert tracker.writes == writes
    await coordinator.async_shutdown()


async def test_a_cascade_uses_the_count_read_after_the_discovery(
    hass: HomeAssistant, clock: Clock
) -> None:
    """The number of refrigerant circuits is read once after the discovery:
    the cascade's heat pumps are read from the next discovery on."""
    leaves = heating(
        kind=_leaf("/system/type", "heatpump_cascade"),
        count=_leaf("/heatSources/numberOfRefrigerantCircuitsInstalled", 3),
        hs2=_leaf("/heatSources/hs2/numberOfStarts", 7),
    )
    cloud = Cloud(
        plant(leaves, hidden=("/heatSources/hs2", "/heatSources/hs3")),
        missing_as_500=False,
    )
    coordinator = _coordinator(hass, cloud)
    await coordinator.async_refresh()
    assert not cloud.requested("/heatSources/hs2/numberOfStarts")
    # The setup is complete: the start-up reads follow in the next cycles.
    coordinator.startup_session.mark_setup_complete()
    for _ in range(3):
        clock.now += 600
        await _cycle(coordinator, clock)
    count = "/heatSources/numberOfRefrigerantCircuitsInstalled"
    assert coordinator.resources[count].value == 3
    assert coordinator.request_rescan() == "planned"
    await _cycle(coordinator, clock)
    assert cloud.requested("/heatSources/hs2/numberOfStarts") == 1
    assert cloud.requested("/heatSources/hs3/workingTime") == 1
    assert "/heatSources/hs2/numberOfStarts" in coordinator.resources
    probes = coordinator.discovery_diagnostics.module_probes
    assert probes is not None
    assert probes["rules"]["R6"]["evidence"] == ["system_type_cascade:3"]  # type: ignore[index]
    _healthy(coordinator)
    await coordinator.async_shutdown()


async def test_a_targeted_read_takes_over_the_local_only_entity(
    hass: HomeAssistant,
) -> None:
    """Cloud and local access: the heat pump at hs2, found by a targeted
    read, continues the local-only entity of the same reading instead of
    adding a second one (the same for a hot water circuit)."""
    from homeassistant.helpers import entity_registry as er

    from custom_components.bosch_buderus_heating import sensor
    from custom_components.bosch_buderus_heating.coordinator import ResourceSnapshot
    from custom_components.bosch_buderus_heating.local_only_sensor import (
        local_only_unique_id,
    )
    from tests.test_validity import _coordinator as plain_coordinator

    leaves = heating(
        hs1=_leaf("/heatSources/hs1/type", "GasBoiler"),
        outdoor=_leaf("/heatSources/hybrid/outdoorStatus", "CONNECTED"),
        starts=_leaf("/heatSources/hs2/numberOfStarts", 120),
        temp=_leaf(f"{DHW1}/actualTemp", 48.0, "C"),
    )
    cloud = RrcCloud(
        plant(leaves, hidden=("/heatSources/hs2", DHW1)), missing_as_500=False
    )
    found, _report = await discover(cloud)
    shapes = {
        "/heatSources/hs2/numberOfStarts": (None, "total_increasing"),
        f"{DHW1}/actualTemp": ("°C", "measurement"),
    }
    paths = tuple(shapes)
    assert set(paths) <= set(found)

    coordinator = plain_coordinator(hass)
    registry = er.async_get(hass)
    local_ids = {}
    for path, (unit, state_class) in shapes.items():
        local_ids[path] = local_only_unique_id("gateway-one", path)
        entity_id = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            local_ids[path],
            config_entry=coordinator.config_entry,
            unit_of_measurement=unit,
            capabilities={"state_class": state_class},
        ).entity_id
        registry.async_update_entity_options(entity_id, DOMAIN, {"native_unit": unit})
    added: list[Any] = []
    scope = type("Scope", (), {"async_on_unload": staticmethod(lambda _: None)})()
    sensor._track_resource_sensors(scope, coordinator, added.extend)
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    coordinator.resources = {path: found[path] for path in paths}
    coordinator.data = {
        path: ResourceSnapshot(found[path], True, now) for path in paths
    }
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    for path in paths:
        assert registry.async_get_entity_id("sensor", DOMAIN, local_ids[path]) is None
    entries = [
        item
        for item in er.async_entries_for_config_entry(
            registry, coordinator.config_entry.entry_id
        )
        if item.domain == "sensor"
    ]
    assert len(entries) == len(paths) == len({entry.unique_id for entry in entries})
    await coordinator.async_shutdown()
