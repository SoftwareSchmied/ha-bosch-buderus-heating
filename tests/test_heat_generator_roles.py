"""The role of a module without a hardware type, from its type number (E3).

A module whose hardware type is missing (or has no table entry) gets the role
the component table gives its type number at its place, only from an entry
whose basis is not a name. The hardware type and the product name come
first; nothing is derived from a name; the kept data of a restart stay as
they were.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.heat_generator import (
    ModuleRow,
    component_role,
    interpret,
    interpret_row,
    rows_from_cloud,
    with_places,
)
from custom_components.bosch_buderus_heating.heat_source_kind import classify
from custom_components.bosch_buderus_heating.outdoor_unit_software import (
    from_cloud,
    from_data,
    track_outdoor_unit_software_sensor,
)
from tests.ha_test_helpers import mock_restore_cache_with_extra_data
from tests.system_components_variants import (
    each_name_entry_alone,
    each_name_entry_removed,
)
from tests.test_heat_generator import OWN_CLOUD
from tests.test_heat_generator_contract import _sensor, _state
from tests.test_plant_inputs import _coordinator, _restart, _start

OUTDOOR = "Logatherm WLW MB-5 AR"
# Shaped like #138: the outdoor unit reports no hardware type.
CASE_138: dict[str, Any] = {
    "gateway": {"device_type": "MX300", "firmware": "16.00.09"},
    "cloud": [
        {"Id": "65281", "Tok": "8", "Ver": "9.10.0", "ProductName": OUTDOOR},
        {"Id": "248", "Tok": "83", "Ver": "06.02", "ModuleHwIdentStr": "HM200.2"},
        {"Id": "17", "Tok": "72", "Ver": "16.00.01", "ModuleHwIdentStr": "MX300"},
    ],
    "local": None,
    "values": {"/heatSources/hs1/type": "GasBoiler"},
}


@pytest.mark.parametrize(
    ("row", "role"),
    [
        (ModuleRow("Unit", None, 65281, 8), "outdoor_unit"),
        (ModuleRow("Unit", None, 8, 8), "heat_source"),
        (ModuleRow("Unit", None, 248, 83), "hybrid_manager"),
        (ModuleRow("Unit", None, 65281, 16), "other"),
        (ModuleRow("Unit", None, 172, 8), "other"),
        (ModuleRow("Unit", None, 253, 16), "other"),
        (ModuleRow("Unit", None, 17, 72), "other"),
        (ModuleRow("Unit", None), "other"),
        # A name never gives a role.
        (ModuleRow("WLW-10 MB AR", None, 172, 8), "other"),
        (ModuleRow("Logatherm WLW186i-12 E", None, 189, 8), "other"),
        # The hardware type comes first.
        (ModuleRow("Unit", "XCU_THH", 65281, 8), "heat_source"),
        (ModuleRow("Unit", "XCU_SRH", 8, 8), "outdoor_unit"),
    ],
)
def test_the_role_from_the_type_number(row: ModuleRow, role: str) -> None:
    assert interpret_row(row).role == role


def test_without_a_component_table_no_role_from_the_type_number() -> None:
    row = ModuleRow("Unit", None, 65281, 8)
    assert component_role(row, None) is None
    assert interpret_row(row, components=None).role == "other"


def test_the_type_number_and_place_are_never_compared_or_kept() -> None:
    """M16: the kept data are those of 0.9.3-beta.5."""
    assert ModuleRow("A", None, 8, 8) == ModuleRow("A", None)
    shown = interpret(rows_from_cloud(OWN_CLOUD), "cloud")
    assert shown is not None
    assert [row.type_id for row in shown.rows] == [8, 65281]
    assert json.dumps(shown.to_data()) == json.dumps(
        {
            "rows": [
                ["Logatherm WLW176i-12 TP70", "XCU_THH"],
                ["WLW-10 MB AR", "XCU_SRH"],
            ],
            "source": "cloud",
        }
    )


@pytest.mark.parametrize(
    "variants",
    [each_name_entry_removed, each_name_entry_alone],
    ids=["removed", "alone"],
)
def test_a_name_entry_never_gives_a_role(variants: Any) -> None:
    """M42 (I5): the roles come from role entries, never from names."""
    rows = [
        ModuleRow("Unit", None, type_id, tok)
        for type_id in (8, 16, 17, 172, 189, 240, 248, 253, 65281)
        for tok in (8, 16, 72, 83)
    ]
    expected = [interpret_row(row).role for row in rows]
    for type_id, table in variants():
        assert [interpret_row(row, components=table).role for row in rows] == (
            expected
        ), type_id


async def test_an_outdoor_unit_without_a_hardware_type(hass: HomeAssistant) -> None:
    """#138: the outdoor unit is named; the software sensor is created; the
    heat source type never comes from the type number (M22)."""
    coordinator = _coordinator(hass, CASE_138)
    await _start(hass, coordinator)
    entity = await _sensor(hass, coordinator)
    assert entity is not None
    attributes = _state(hass, entity).attributes
    assert attributes["outdoor_unit"] == OUTDOOR
    assert [item["role"] for item in attributes["heat_generators"]] == ["outdoor_unit"]
    assert attributes["heat_source_type"] is None
    assert classify(coordinator).kind is None
    units = from_cloud(CASE_138["cloud"])
    assert units is not None
    assert units.state == "9.10.0"
    assert units.attributes()["hardware_type"] is None
    assert from_data(units.to_data()) is not None
    added: list[Any] = []
    track_outdoor_unit_software_sensor(
        SimpleNamespace(async_on_unload=Mock()), coordinator, added.extend
    )
    assert len(added) == 1
    await coordinator.async_shutdown()


async def test_a_restart_keeps_the_role_from_the_stored_row(
    hass: HomeAssistant,
) -> None:
    """The restore data hold no type numbers; the plant model's stored row
    gives them back, so a restart changes nothing."""
    coordinator = _coordinator(hass, CASE_138)
    await _start(hass, coordinator)
    entity = await _sensor(hass, coordinator)
    assert entity is not None
    before = _state(hass, entity)
    restore = entity.extra_restore_state_data.as_dict()
    assert restore["heat_generators"]["rows"] == [[OUTDOOR, None]]
    await entity.async_remove()
    await coordinator.async_shutdown()
    await _restart(hass)
    mock_restore_cache_with_extra_data(hass, [(before, restore)])
    again = _coordinator(hass, CASE_138, info=False)
    await _start(hass, again)
    entity = await _sensor(hass, again)
    assert entity is not None
    after = _state(hass, entity)
    assert (after.state, after.attributes) == (before.state, before.attributes)
    await again.async_shutdown()


def test_places_are_given_back_only_when_unique() -> None:
    shown = interpret([ModuleRow(OUTDOOR, None)], "cloud")
    rows = [
        SimpleNamespace(type_id=65281, tok=8, product_name=OUTDOOR, hardware_type=None)
    ]
    placed = with_places(shown, rows)
    assert placed is not None and placed.generators[0].role == "outdoor_unit"
    twice = [
        *rows,
        SimpleNamespace(type_id=8, tok=8, product_name=OUTDOOR, hardware_type=None),
    ]
    assert with_places(shown, twice) is shown
    assert with_places(None, rows) is None


def test_the_own_k40_is_unchanged() -> None:
    """With hardware types the roles come from them as before."""
    shown = interpret(rows_from_cloud(OWN_CLOUD), "cloud")
    assert shown is not None
    plain = interpret(
        [ModuleRow(row.product_name, row.hardware_type) for row in shown.rows],
        "cloud",
    )
    assert plain is not None
    assert shown == plain
    assert shown.attributes() == plain.attributes()
