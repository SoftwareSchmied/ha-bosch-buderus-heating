"""Z1: selecting the active weekly program A/B of an EMS/iCom heating circuit."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.translation import async_get_translations

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.controls_flow import controls
from custom_components.bosch_buderus_heating.coordinator import Freshness
from custom_components.bosch_buderus_heating.discovery_inventory import (
    resource_from_data,
)
from custom_components.bosch_buderus_heating.select import (
    BoschBuderusSelect,
    build_select_descriptions,
)
from custom_components.bosch_buderus_heating.switch_program_select import (
    SwitchProgramSelect,
    descriptions,
)
from custom_components.bosch_buderus_heating.switch_program_selection import (
    ACTIVE_SWITCH_PROGRAM_POLICY,
    PROGRAM_NOT_READABLE,
)
from custom_components.bosch_buderus_heating.writes import (
    ContractRejected,
    assess_control,
)
from tests import test_dynamic_controls
from tests.switch_program_helpers import (
    ACTIVE,
    PROGRAM_A,
    PROGRAM_B,
    Gateway,
    payload,
    resource,
)
from tests.test_dynamic_controls import setup_selects

CAPTURE = (
    Path(__file__).parent / "fixtures/contracts/captures/k40-cloud-2026-11-05.json"
)


@pytest.fixture
def environment(hass, enable_custom_integrations):
    yield from test_dynamic_controls.environment.__wrapped__(hass)


async def program_select(hass, environment) -> SwitchProgramSelect | None:
    added: list[object] = []
    await setup_selects(hass, environment[2], added.extend)
    found = [item for item in added if isinstance(item, SwitchProgramSelect)]
    # Never also the generic enum select of the same path.
    assert not any(
        isinstance(item, BoschBuderusSelect)
        and item.entity_description.resource_path == ACTIVE
        for item in added
    )
    return found[0] if found else None


def readings(**changes) -> dict:
    items = {
        path: resource(payload(path))
        for path in (
            ACTIVE,
            PROGRAM_A,
            PROGRAM_B,
            "/heatingCircuits/hc1/switchPrograms/nameA",
        )
    }
    for path, item in changes.items():
        if item is None:
            items.pop(path)
        else:
            items[path] = item
    return items


def test_two_readable_programs_give_one_selection():
    (description,) = descriptions(readings())
    assert description.resource_path == ACTIVE
    assert description.key == "heatingCircuits:hc1:activeSwitchProgram"
    assert assess_control(readings()[ACTIVE]).policy is ACTIVE_SWITCH_PROGRAM_POLICY


@pytest.mark.parametrize(
    "changes",
    [
        # The own plant: only program A is allowed.
        {ACTIVE: resource(payload(ACTIVE) | {"allowedValues": ["A"]})},
        # An unknown program.
        {ACTIVE: resource(payload(ACTIVE) | {"allowedValues": ["A", "B", "C"]})},
        # The current program is not among the allowed ones.
        {ACTIVE: resource(payload(ACTIVE) | {"value": "C"})},
        {ACTIVE: resource(payload(ACTIVE) | {"writeable": 0})},
        # Program B cannot be read or fails the strict check.
        {PROGRAM_B: None},
        {PROGRAM_B: resource(payload(PROGRAM_B) | {"switchPointTimeRaster": 7})},
    ],
)
def test_no_selection_without_two_valid_programs(changes):
    assert descriptions(readings(**changes)) == ()


def test_the_real_heat_pump_capture_has_no_selection():
    data = json.loads(CAPTURE.read_text(encoding="utf-8"))
    resources = {
        path: resource_from_data(item) for path, item in data["resources"].items()
    }
    assert resources[ACTIVE].metadata.allowed_values == ("A",)
    assert descriptions(resources) == ()
    assert not any(
        item.resource_path == ACTIVE for item in build_select_descriptions(resources)
    )


async def test_the_select_switches_the_program_once_and_confirms(hass, environment):
    coordinator, client, _ = environment
    gateway = Gateway(coordinator, client)
    entity = await program_select(hass, environment)
    assert entity is not None and entity.available
    assert entity.options == ["a", "b"]
    assert entity.current_option == "a"
    assert entity.extra_state_attributes == {
        "program_names": {"a": "Home office"},
        "program_name": "Home office",
    }
    await entity.async_select_option("b")
    assert gateway.value_puts == [(ACTIVE, "B")]
    assert gateway.raw_puts == []
    assert entity.current_option == "b"
    assert coordinator.data[ACTIVE].freshness is Freshness.FRESH
    # The selection and the program were read again with the write.
    bulk = client.get_resources_bulk.await_args.args[1]
    assert set(bulk) == {ACTIVE, PROGRAM_B}


async def test_a_program_changed_meanwhile_sends_nothing(hass, environment):
    coordinator, client, _ = environment
    gateway = Gateway(coordinator, client)
    entity = await program_select(hass, environment)
    gateway.change(PROGRAM_B, switchPointTimeRaster=30)
    with pytest.raises(ServiceValidationError):
        await entity.async_select_option("b")
    assert gateway.value_puts == []


async def test_an_option_no_longer_offered_is_refused(hass, environment):
    coordinator, client, _ = environment
    Gateway(coordinator, client)
    entity = await program_select(hass, environment)
    coordinator.data[PROGRAM_B] = replace(
        coordinator.data[PROGRAM_B], freshness=Freshness.STALE
    )
    assert not entity.available
    with pytest.raises(ServiceValidationError):
        await entity.async_select_option("b")
    client.put_resource_value.assert_not_awaited()


async def test_the_write_path_refuses_a_program_without_a_reading(environment):
    coordinator, client, _ = environment
    gateway = Gateway(coordinator, client)
    del coordinator.data[PROGRAM_B]
    with pytest.raises(ContractRejected) as error:
        await coordinator.async_write_control(ACTIVE, "B", ACTIVE_SWITCH_PROGRAM_POLICY)
    assert error.value.reason == PROGRAM_NOT_READABLE
    assert gateway.value_puts == []


async def test_an_unconfirmed_switch_is_reported(hass, environment):
    coordinator, client, _ = environment
    gateway = Gateway(coordinator, client)
    entity = await program_select(hass, environment)

    async def ignored(gw, path, value):
        gateway.value_puts.append((path, value))

    client.put_resource_value.side_effect = ignored
    with pytest.raises(HomeAssistantError) as error:
        await entity.async_select_option("b")
    assert error.value.translation_key == "write_not_confirmed"
    assert gateway.value_puts == [(ACTIVE, "B")]


def test_the_operate_dialog_leaves_the_program_to_its_select(environment):
    coordinator, client, _ = environment
    Gateway(coordinator, client)
    assert ACTIVE not in controls(coordinator)


@pytest.mark.parametrize("language", ["de", "en"])
async def test_option_and_error_texts_exist(hass, enable_custom_integrations, language):
    entity = await async_get_translations(hass, language, "entity", {DOMAIN})
    prefix = f"component.{DOMAIN}.entity.select.active_switch_program.state."
    assert entity[prefix + "a"].endswith("A") and entity[prefix + "b"].endswith("B")
    errors = await async_get_translations(hass, language, "exceptions", {DOMAIN})
    assert errors[f"component.{DOMAIN}.exceptions.{PROGRAM_NOT_READABLE}.message"]
