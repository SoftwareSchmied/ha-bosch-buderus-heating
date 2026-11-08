"""A6: "Search the system again" in the options dialog.

The cloud part runs through the managed entry with the real PointT client
against a synthetic cloud; the local part through the real poll planner.
"""

from __future__ import annotations

import asyncio
import functools
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.bosch_buderus_heating.config_flow import BoschBuderusOptionsFlow
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.local_polling.inventory import (
    ResourceInventory,
)
from custom_components.bosch_buderus_heating.local_polling.planner import PollPlanner
from custom_components.bosch_buderus_heating.local_polling.service import (
    LocalPollingService,
)
from custom_components.bosch_buderus_heating.local_state import LocalCondition
from custom_components.bosch_buderus_heating.rescan import request_rescan
from tests.test_local_probe import CONTEXT
from tests.test_managed_pointt import TEMP, Backend, eventually, running, source_for

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
HEAT = "000123"
NEW = "/heatingCircuits/hc2/currentRoomSetpoint"


def bounded(test):
    """Fail within 90 s instead of hanging."""

    @functools.wraps(test)
    async def run(*args, **kwargs):
        async with asyncio.timeout(90):
            await test(*args, **kwargs)

    return run


@pytest.fixture(autouse=True)
def loaded_integration(hass):
    hass.config.components.add(DOMAIN)


class ChangingBackend(Backend):
    """A plant that gains one reading and stops reporting another."""

    def __init__(self):
        super().__init__()
        self.changed = False

    def resource(self, gateway, path):
        if self.changed and path == "/heatingCircuits/hc2":
            return {"references": [{"id": NEW}]}
        if self.changed and path == NEW:
            return {
                "id": NEW,
                "type": "floatValue",
                "value": 21.0,
                "unitOfMeasure": "C",
                "writeable": False,
            }
        return super().resource(gateway, path)


def flow_for(hass, entry):
    flow = BoschBuderusOptionsFlow(entry)
    flow.hass = hass
    return flow


async def cycle(coordinator):
    coordinator._next_update = dict.fromkeys(coordinator._poll_intervals, 0)
    await coordinator.async_refresh()


@bounded
async def test_the_dialog_plans_one_search_within_the_limits(hass) -> None:
    backend = ChangingBackend()
    async with running(hass, backend) as (entry, _, owner, _):
        await eventually(lambda: source_for(owner, HEAT) is not None)
        coordinator = source_for(owner, HEAT)
        await eventually(lambda: TEMP in coordinator.resources)
        flow = flow_for(hass, entry)
        menu = await flow.async_step_init()
        assert "rescan" in menu["menu_options"]
        form = await flow.async_step_rescan()
        assert form["type"] is FlowResultType.FORM
        calls = len(backend.calls)
        result = await flow.async_step_rescan({})
        assert result["reason"] == "rescan_planned"
        assert len(backend.calls) == calls  # planned, nothing requested yet
        # A second request while it is planned is refused.
        again = await flow_for(hass, entry).async_step_rescan({})
        assert again["reason"] == "rescan_running"
        backend.changed = True
        discover = coordinator._async_discover_data

        async def without_temp():
            # The search does not report a reading that was there before.
            discovered = await discover()
            coordinator.resources.pop(TEMP, None)
            discovered.pop(TEMP, None)
            return discovered

        with patch.object(coordinator, "_async_discover_data", without_temp):
            await cycle(coordinator)
        assert coordinator.rescan_result == "success"
        assert NEW in coordinator.resources  # found by the search
        # Not reported, but kept and still shown: no entity is removed.
        assert TEMP in coordinator.resources
        assert TEMP in coordinator.data
        assert coordinator.rescan_state() == "ready"
        # Only the one discovery: the next cycle polls normally.
        await cycle(coordinator)
        assert coordinator.rescan_result == "success"


@bounded
async def test_a_paused_cloud_is_skipped_and_the_ready_parts_search(hass) -> None:
    backend = Backend()
    async with running(hass, backend) as (entry, _, owner, _):
        await eventually(lambda: source_for(owner, HEAT) is not None)
        await eventually(lambda: source_for(owner, "gateway-b") is not None)
        coordinator = source_for(owner, HEAT)
        other = source_for(owner, "gateway-b")
        coordinator._cloud_backoff_until = monotonic() + 600
        result = await flow_for(hass, entry).async_step_rescan({})
        assert result["reason"] == "rescan_partial"
        # The paused gateway is skipped; the other one searches.
        assert coordinator._rescan_due is None and other._rescan_due is not None
        assert request_rescan(owner) == "running"
        other._rescan_due, other.rescan_result = None, None
        # Every reachable part paused: nothing starts.
        other._circuit_open_until = monotonic() + 600
        assert request_rescan(owner) == "paused"
        assert coordinator._rescan_due is None and other._rescan_due is None
        coordinator._cloud_backoff_until = other._circuit_open_until = 0.0
        assert request_rescan(owner) == "planned"
        assert coordinator._rescan_due is not None and other._rescan_due is not None


@bounded
async def test_a_search_that_meets_a_pause_waits_for_it(hass) -> None:
    backend = Backend()
    async with running(hass, backend) as (_, _, owner, _):
        await eventually(lambda: source_for(owner, HEAT) is not None)
        coordinator = source_for(owner, HEAT)
        assert coordinator.request_rescan() == "planned"
        backend.status[HEAT] = 429
        await cycle(coordinator)
        assert coordinator.rescan_result == "postponed"
        assert coordinator._rescan_due is not None  # not lost
        assert TEMP in coordinator.resources


@bounded
async def test_without_a_ready_access_nothing_is_searched(hass) -> None:
    backend = Backend()
    async with running(hass, backend) as (entry, cloud, owner, _):
        await eventually(lambda: source_for(owner, HEAT) is not None)
        management = owner.connections.management
        management.set_enabled(management.edit(cloud), False)
        await eventually(lambda: source_for(owner, HEAT) is None)
        result = await flow_for(hass, entry).async_step_rescan({})
        assert result["reason"] == "rescan_unavailable"


# ------------------------------------------------------------- local part


def local_service(condition=LocalCondition.READY, *, exhausted=True):
    planner = PollPlanner(ResourceInventory(CONTEXT.identity), 0)
    planner.exhausted, planner.rescan_at = exhausted, 86400.0
    service = SimpleNamespace(
        condition=condition, planner=planner, _clock=lambda: 100.0, wake=Mock()
    )
    service.rescan_state = lambda: LocalPollingService.rescan_state(service)
    return service


def test_the_local_search_starts_again_in_its_slots() -> None:
    service = local_service()
    planner = service.planner
    planner.cursor = 1172
    assert LocalPollingService.rescan(service) == "planned"
    assert not planner.exhausted and planner.cursor == 0
    service.wake.assert_called_once()
    # A second request before the service reads again: already searching.
    assert LocalPollingService.rescan(service) == "running"
    service.wake.assert_called_once()
    request = planner._discovery(100.0)  # within the usual search slots
    assert request is not None
    assert not planner.exhausted and planner.cursor == 1


@pytest.mark.parametrize(
    ("condition", "exhausted", "expected"),
    [
        (LocalCondition.PAUSED, True, "paused"),
        (LocalCondition.READY, False, "running"),
        (LocalCondition.UNAVAILABLE, True, "unavailable"),
        (LocalCondition.AUTH_REQUIRED, True, "unavailable"),
    ],
)
def test_the_local_search_is_refused_while_it_cannot_run(
    condition, exhausted, expected
) -> None:
    service = local_service(condition, exhausted=exhausted)
    assert LocalPollingService.rescan(service) == expected
    assert service.planner.exhausted is exhausted  # nothing restarted
    service.wake.assert_not_called()


def owner_with(cloud, local):
    runtime = SimpleNamespace(
        snapshot=SimpleNamespace(
            bindings={
                "cloud": SimpleNamespace(adapter="pointt"),
                "local": SimpleNamespace(adapter="k40_local"),
            }
        ),
        source_data=lambda key: cloud if key == "cloud" else None,
        source_driver=lambda key: local if key == "local" else None,
    )
    return SimpleNamespace(connections=runtime)


def part(state, method):
    return SimpleNamespace(
        rescan_state=lambda: state, **{method: Mock(return_value="planned")}
    )


def test_a_paused_local_part_is_skipped_and_the_cloud_searches() -> None:
    cloud, local = part("ready", "request_rescan"), part("paused", "rescan")
    assert request_rescan(owner_with(cloud, local)) == "partial"
    cloud.request_rescan.assert_called_once()
    local.rescan.assert_not_called()


def test_a_paused_cloud_is_skipped_and_the_local_part_searches() -> None:
    cloud, local = part("paused", "request_rescan"), part("ready", "rescan")
    assert request_rescan(owner_with(cloud, local)) == "partial"
    cloud.request_rescan.assert_not_called()
    local.rescan.assert_called_once()


@pytest.mark.parametrize(
    ("cloud_state", "local_state", "expected"),
    [
        ("paused", "paused", "paused"),
        ("paused", "unavailable", "paused"),
        ("unavailable", "unavailable", "unavailable"),
        ("ready", "running", "running"),
        ("paused", "running", "running"),
        ("ready", "unavailable", "planned"),
        ("ready", "ready", "planned"),
    ],
)
def test_the_whole_search_is_refused_only_without_a_ready_part(
    cloud_state, local_state, expected
) -> None:
    cloud = part(cloud_state, "request_rescan")
    local = part(local_state, "rescan")
    assert request_rescan(owner_with(cloud, local)) == expected
    planned = expected in ("planned", "partial")
    assert cloud.request_rescan.called is (planned and cloud_state == "ready")
    assert local.rescan.called is (planned and local_state == "ready")
