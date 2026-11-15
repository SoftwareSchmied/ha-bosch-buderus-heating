"""The generic heating circuit settings wait for the room controller check.

A room controller (TC100.2, CT200) has its own contract settings for the
same paths. The generic summer/winter, cooling and level controls are
therefore created, offered and written only once a finished discovery got
a definite answer for the room controller evidence; a controller recognised
late never shows them, not even for a moment. Synthetic data only.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.bosch_buderus_heating.circuit_controls import (
    GENERIC_ONLY_POLICIES,
    SUMMER_WINTER_MODE_POLICY,
    room_controller_check_complete,
)
from custom_components.bosch_buderus_heating.control import track_control_entities
from custom_components.bosch_buderus_heating.controls_flow import controls
from custom_components.bosch_buderus_heating.number import (
    BoschBuderusNumber,
    build_number_descriptions,
)
from custom_components.bosch_buderus_heating.pointt import (
    Resource,
    ResourceError,
    ResourceNotFound,
    ServiceUnavailable,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.pointt.models import BatchItemResult
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.rrc import ZONE_LIST
from custom_components.bosch_buderus_heating.select import (
    BoschBuderusSelect,
    build_select_descriptions,
)
from custom_components.bosch_buderus_heating.writes import (
    UNCHECKED_CONTEXT,
    assess_control,
)
from tests.circuit_control_helpers import (
    COOLING_SETPOINT,
    SWITCH_MODE,
    THRESHOLD,
    Cloud,
    coordinator_for,
    put_reading,
    select_entity,
    variant,
)
from tests.test_write_guards import _Lifecycle

FIXTURE = "icom/season-circuit.json"
GENERIC_PATHS = {SWITCH_MODE, THRESHOLD, COOLING_SETPOINT}


def _track(coordinator):
    added: list = []
    lifecycle = _Lifecycle()
    track_control_entities(
        lifecycle,
        coordinator,
        added.extend,
        build_select_descriptions,
        BoschBuderusSelect,
    )
    track_control_entities(
        lifecycle,
        coordinator,
        added.extend,
        build_number_descriptions,
        BoschBuderusNumber,
    )
    return added, lifecycle


def _generic(added) -> set[str]:
    return {
        entity.entity_description.resource_path
        for entity in added
        if entity.entity_description.write_policy in GENERIC_ONLY_POLICIES
    }


def test_unchecked_context_offers_no_generic_control():
    resources = variant(FIXTURE, "V2")
    for path in GENERIC_PATHS:
        assessment = assess_control(resources[path], UNCHECKED_CONTEXT)
        assert assessment.policy is None
        assert assessment.rejection_reason == "room_controller_check_pending"


async def test_late_room_controller_never_shows_the_generic_controls(hass):
    """Order: readings first, then the discovery finds the zone list."""
    coordinator = coordinator_for(hass, variant(FIXTURE, "V2"), checked=False)
    cloud = Cloud(coordinator)
    assert coordinator.control_context() is UNCHECKED_CONTEXT
    added, lifecycle = _track(coordinator)
    assert _generic(added) == set()
    assert not GENERIC_PATHS & set(controls(coordinator))
    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(
            SWITCH_MODE, "forced", SUMMER_WINTER_MODE_POLICY
        )
    assert cloud.puts == []
    # The discovery finished and found a room controller.
    put_reading(
        coordinator,
        parse_resource({"id": ZONE_LIST, "type": "zoneList", "values": [{"id": 1}]}),
    )
    coordinator.resources = {
        **coordinator.resources,
        "/zones/zn1/name": Resource("/zones/zn1/name", value="x", has_value=True),
    }
    coordinator.room_controller_checked = True
    coordinator.async_update_listeners()
    assert coordinator.control_context().rrc_plant
    assert _generic(added) == set()
    lifecycle.unload()


async def test_generic_controls_follow_once_the_check_is_complete(hass):
    coordinator = coordinator_for(hass, variant(FIXTURE, "V2"), checked=False)
    cloud = Cloud(coordinator)
    added, lifecycle = _track(coordinator)
    assert _generic(added) == set()
    coordinator.room_controller_checked = True
    coordinator.async_update_listeners()
    assert _generic(added) == GENERIC_PATHS | {
        "/heatingCircuits/hc1/cooling/outdoorThreshold"
    }
    assert set(controls(coordinator)) >= GENERIC_PATHS
    await select_entity(hass, coordinator, SWITCH_MODE).async_select_option("forced")
    assert cloud.puts == [(SWITCH_MODE, "forced")]
    lifecycle.unload()


async def test_an_existing_entity_refuses_while_unchecked(hass):
    coordinator = coordinator_for(hass, variant(FIXTURE, "V2"))
    cloud = Cloud(coordinator)
    entity = select_entity(hass, coordinator, SWITCH_MODE)
    coordinator.room_controller_checked = False
    assert not entity.available
    with pytest.raises(ServiceValidationError):
        await entity.async_select_option("forced")
    assert cloud.puts == []


# The check itself --------------------------------------------------------------


def _report(completed=True, **answers):
    paths = {
        path: SimpleNamespace(
            discovered=answer == "found",
            read_policy="normal",
            bulk_result="success" if answer == "found" else answer,
            fallback_result=None,
        )
        for path, answer in answers.items()
    }
    return SimpleNamespace(completed=completed, paths=paths)


@pytest.mark.parametrize(
    ("report", "complete"),
    [
        (_report(), True),
        (_report(completed=False), False),
        (_report(**{ZONE_LIST: "found"}), True),
        (_report(**{ZONE_LIST: "http_404", "/zones": "http_404"}), True),
        (_report(**{"/zones": "http_403"}), True),
        (_report(**{ZONE_LIST: "http_503"}), False),
        (_report(**{ZONE_LIST: "http_429"}), False),
        (_report(**{"/zones": "timeout"}), False),
        (_report(**{"/gateway/productType": "malformed"}), False),
    ],
)
def test_room_controller_check(report, complete):
    assert room_controller_check_complete(report) is complete


def _client(tree: dict[str, object]) -> AsyncMock:
    client = AsyncMock()

    def answer(gateway, path):
        item = tree.get(path, 404)
        if isinstance(item, int):
            return BatchItemResult(gateway, path, item, error=ResourceError(path, item))
        return BatchItemResult(gateway, path, 200, item)

    client.get_resources_bulk.side_effect = lambda gateway, chunk, **_k: tuple(
        answer(gateway, path) for path in chunk
    )

    async def single(gateway, path, **_kwargs):
        item = tree.get(path, 404)
        if isinstance(item, int):
            if item >= 500:
                raise ServiceUnavailable(item)
            raise ResourceNotFound(path, item)
        return item

    client.get_resource.side_effect = single
    return client


def _tree(zones: object) -> dict[str, object]:
    tree: dict[str, object] = dict(variant(FIXTURE, "V1"))
    tree["/heatingCircuits"] = parse_resource(
        {
            "id": "/heatingCircuits",
            "type": "refEnum",
            "references": [{"id": "/heatingCircuits/hc1"}],
        }
    )
    tree["/heatingCircuits/hc1"] = parse_resource(
        {
            "id": "/heatingCircuits/hc1",
            "type": "refEnum",
            "references": [{"id": path} for path in variant(FIXTURE, "V1")],
        }
    )
    tree["/zones"] = zones
    return tree


@pytest.mark.parametrize(("zones", "checked"), [(404, True), (503, False)])
async def test_a_finished_discovery_settles_the_check(hass, zones, checked):
    coordinator = coordinator_for(hass, {}, checked=False)
    coordinator.client.get_resources_bulk.side_effect = None
    client = _client(_tree(zones))
    coordinator.client.get_resources_bulk.side_effect = (
        client.get_resources_bulk.side_effect
    )
    coordinator.client.get_resource.side_effect = client.get_resource.side_effect
    await coordinator._async_discover_data()
    assert SWITCH_MODE in coordinator.resources
    assert coordinator.room_controller_checked is checked


async def test_the_check_stays_settled_after_a_failed_discovery(hass):
    coordinator = coordinator_for(hass, {}, checked=False)
    client = _client(_tree(404))
    coordinator.client.get_resources_bulk.side_effect = (
        client.get_resources_bulk.side_effect
    )
    coordinator.client.get_resource.side_effect = client.get_resource.side_effect
    await coordinator._async_discover_data()
    assert coordinator.room_controller_checked
    client = _client(_tree(503))
    coordinator.client.get_resources_bulk.side_effect = (
        client.get_resources_bulk.side_effect
    )
    coordinator.client.get_resource.side_effect = client.get_resource.side_effect
    await coordinator._async_discover_data()
    assert coordinator.room_controller_checked


# Existing entities after a restart with the stored inventory ---------------------


async def test_restart_with_stored_inventory_keeps_existing_level_numbers(hass):
    """The heating and setback levels (since 0.9.0) are created at once and
    are available with the first fresh reading, before the room controller
    check; only the new generic settings wait for it."""
    from custom_components.bosch_buderus_heating.circuit_controls import (
        HEATING_LEVEL_POLICY,
    )
    from tests.circuit_control_helpers import AWAY, COMFORT, ECO

    resources = variant("icom/temperature-levels.json", "V2")
    coordinator = coordinator_for(hass, {}, checked=False)
    coordinator.start_from_inventory(
        resources, reason="cloud_unreachable", age_seconds=60, seen={}
    )
    added, lifecycle = _track(coordinator)
    levels = {
        entity.entity_description.resource_path: entity
        for entity in added
        if entity.entity_description.write_policy is HEATING_LEVEL_POLICY
    }
    assert set(levels) == {COMFORT, ECO}
    assert _generic(added) == set()
    # The first working cycle: fresh readings, no discovery yet.
    for resource in resources.values():
        put_reading(coordinator, resource)
    coordinator.last_update_success = True
    coordinator.async_update_listeners()
    assert not coordinator.room_controller_checked
    assert all(entity.available for entity in levels.values())
    assert _generic(added) == set()
    assert AWAY in coordinator.resources
    # A level write keeps its fresh reads of the guards.
    cloud = Cloud(coordinator)
    await levels[ECO].async_set_native_value(18.5)
    assert cloud.puts == [(ECO, 18.5)]
    assert ECO in cloud.bulk_calls[0] and COMFORT in cloud.bulk_calls[0]
    lifecycle.unload()
