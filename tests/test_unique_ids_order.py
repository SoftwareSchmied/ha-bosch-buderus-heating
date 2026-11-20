"""Unique IDs must not depend on the order of the cloud's answers.

The same plants as the entity baseline are set up with the resource tree and
every list of references in the original, the reverse and a shuffled order.
A position-based unique ID would register other entities in each run.
"""

from __future__ import annotations

import random
from contextlib import nullcontext
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.pointt import Resource
from tests.helpers.rrc_cloud import Clock, cloud_patches
from tests.test_entry import _entry
from tests.test_rrc_w0_baseline import PACKAGE, PLANTS

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture(autouse=True)
def legacy_setup_path():
    with patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)):
        yield


def _reordered(tree: dict[str, Resource], order: str) -> dict[str, Resource]:
    rng = random.Random(7)
    items = list(tree.items())
    if order == "reverse":
        items.reverse()
    elif order == "shuffle":
        rng.shuffle(items)
    result: dict[str, Resource] = {}
    for path, resource in items:
        references = list(resource.references)
        if order == "reverse":
            references.reverse()
        elif order == "shuffle":
            rng.shuffle(references)
        result[path] = replace(resource, references=tuple(references))
    return result


async def _unique_ids(hass: HomeAssistant, plant: str, order: str) -> list[str]:
    build, gateway = PLANTS[plant]
    cloud = build()
    cloud.tree = _reordered(cloud.tree, order)
    entry = _entry(hass)
    faults = (
        patch(f"{PACKAGE}.coordinator.fault_resource_candidates", return_value=())
        if plant == "tc100"
        else nullcontext()
    )
    with cloud_patches(cloud, Clock(), gateway), faults:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        found = sorted(
            f"{item.domain}:{item.unique_id.replace(entry.entry_id, '<entry>')}"
            for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        )
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    return found


@pytest.mark.parametrize("plant", sorted(PLANTS))
async def test_unique_ids_do_not_depend_on_the_answer_order(
    hass: HomeAssistant, plant: str
) -> None:
    original = await _unique_ids(hass, plant, "original")
    assert len(original) > 20
    assert await _unique_ids(hass, plant, "reverse") == original
    assert await _unique_ids(hass, plant, "shuffle") == original
