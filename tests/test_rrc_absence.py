"""W2: away mode, away temperature and fireplace function of an RRC 2.0
controller, as generic switches and numbers of the absence contract.

The flags are the strings "true"/"false" without ``allowedValues`` (S2,
P38); the switches write exactly these strings, once, and read back. Other
gateways keep their own away switch ("on"/"off", S11). Expected, untested
on hardware.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import (
    controls_flow,
    rrc_contract_absence,
    rrc_contracts,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.writes import (
    GENERIC_CONTEXT,
    RRC_CONTEXT,
    ControlLocked,
    assess_control,
    validate_control_value,
)
from tests.helpers.rrc_absence import (
    AWAY,
    AWAY_TEMPERATURE,
    FIREPLACE,
    absence_tree,
    entity_id,
    set_value,
    setup,
    teardown,
    wire,
)
from tests.helpers.rrc_cloud import RrcCloud, load_tree

PACKAGE = "custom_components.bosch_buderus_heating"
AWAY_SWITCH = "gateway-one:system:awayMode:enabled:control"
FIREPLACE_SWITCH = "gateway-one:system:fireplace:enabled:control"
AWAY_NUMBER = "gateway-one:system:awayMode:temperature:control"

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture(autouse=True)
def legacy_setup_path():
    with patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)):
        yield


def test_the_contract_claims_its_paths_alone() -> None:
    rrc_contracts.registry().check_paths(absence_tree())
    assert rrc_contracts.contract_for("/events") == "absence"
    assert rrc_contracts.contract_for("/heatingCircuits/hc1/boostShortcut") == "absence"
    assert rrc_contracts.extra_roots() == ("/events",)
    assert rrc_contracts.optional_children("/events") == (
        "/events/list1",
        "/events/list2",
        "/events/list3",
    )
    # The single boost leaves stay readings (W2-E1).
    for leaf in ("boostMode", "boostTemperature", "boostDuration", "boostZones"):
        assert rrc_contracts.contract_for(f"/heatingCircuits/hc1/{leaf}") is None
    # Every weekday title and description is a built text.
    assert len(rrc_contract_absence.CONTRACT.texts) == 14


@pytest.mark.parametrize(
    ("fields", "switch"),
    [
        ({"value": "false"}, True),
        ({"value": "true"}, True),
        ({"value": "false", "allowedValues": ["true", "false"]}, True),
        # Other advertised values are no switch of this contract.
        ({"value": "off", "allowedValues": ["on", "off"]}, False),
        ({"value": "false", "allowedValues": ["true", "false", "auto"]}, False),
        ({"value": "maybe"}, False),
        ({"value": "false", "writeable": 0}, False),
    ],
)
def test_flags_are_switches_of_exactly_true_and_false(fields, switch) -> None:
    resource = wire(AWAY, type="stringValue", **{"writeable": 1, **fields})
    assessment = assess_control(resource, RRC_CONTEXT)
    assert (assessment.eligible and assessment.supports_switch) is switch
    assert not assessment.supports_select or not assessment.eligible
    # S11: the generic away switch keeps its "on"/"off" contract.
    generic = assess_control(resource, GENERIC_CONTEXT)
    assert generic.policy is not rrc_contract_absence.AWAY_POLICY


async def _switch(hass: HomeAssistant, unique_id: str, on: bool) -> None:
    await hass.services.async_call(
        "switch",
        "turn_on" if on else "turn_off",
        {"entity_id": entity_id(hass, "switch", unique_id)},
        blocking=True,
    )


async def test_away_and_fireplace_write_the_strings_once(hass: HomeAssistant) -> None:
    cloud = RrcCloud(absence_tree())
    cloud.adopt_after[AWAY] = 3
    entry, coordinator, _clock, sleeps, stack = await setup(hass, cloud)
    registry = er.async_get(hass)
    try:
        away = entity_id(hass, "switch", AWAY_SWITCH)
        assert away is not None
        assert hass.states.get(away).state == "off"
        await _switch(hass, AWAY_SWITCH, True)
        assert cloud.written() == [("PUT", AWAY, "true")]
        assert sleeps[-3:] == [1.0, 2.0, 4.0]
        assert hass.states.get(away).state == "on"
        # P38: switching off sends the string "false".
        await _switch(hass, AWAY_SWITCH, False)
        assert cloud.written()[-1] == ("PUT", AWAY, "false")
        assert hass.states.get(away).state == "off"

        fireplace = registry.async_get(entity_id(hass, "switch", FIREPLACE_SWITCH))
        assert fireplace.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert fireplace.entity_category is EntityCategory.CONFIG
        assert fireplace.original_icon == "mdi:fireplace"
        assert fireplace.translation_key == "rrc_fireplace"

        number = entity_id(hass, "number", AWAY_NUMBER)
        assert hass.states.get(number).attributes["step"] == 0.5
        assert registry.async_get(number).entity_category is EntityCategory.CONFIG
        await hass.services.async_call(
            "number", "set_value", {"entity_id": number, "value": 12.5}, blocking=True
        )
        assert cloud.written()[-1] == ("PUT", AWAY_TEMPERATURE, 12.5)
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": number, "value": 12.3},
                blocking=True,
            )
        assert len(cloud.written()) == 3

        # The generic sensors become disabled diagnostic mirrors.
        for unique_id in (
            "gateway-one:system:awayMode:enabled",
            "gateway-one:system:awayMode:temperature",
            "gateway-one:system:fireplace:enabled",
        ):
            mirror = registry.async_get(entity_id(hass, "sensor", unique_id))
            assert mirror.disabled_by is er.RegistryEntryDisabler.INTEGRATION
            assert mirror.entity_category is EntityCategory.DIAGNOSTIC
        stats = coordinator.rrc_write_stats.snapshot("absence")
        assert stats["writes_sent"] == 3 and stats["writes_confirmed"] == 3
        assert stats["rejection_reasons"] == {"validation": 1}
    finally:
        await teardown(hass, entry, stack)


async def test_switching_off_without_effect_shows_the_reading(
    hass: HomeAssistant,
) -> None:
    """P38: the controller keeps away mode on; the switch is not confirmed
    and shows what the next reading says."""
    cloud = RrcCloud(absence_tree())
    set_value(cloud, AWAY, "true")
    entry, coordinator, clock, _sleeps, stack = await setup(hass, cloud)
    away = entity_id(hass, "switch", AWAY_SWITCH)
    try:
        cloud.ignore_writes.add(AWAY)
        with pytest.raises(HomeAssistantError) as unconfirmed:
            await _switch(hass, AWAY_SWITCH, False)
        assert unconfirmed.value.translation_key == "rrc_write_pending_confirmation"
        assert cloud.written() == [("PUT", AWAY, "false")]
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(away).state == "on"
        assert len(cloud.written()) == 1
    finally:
        await teardown(hass, entry, stack)


async def test_the_controls_dialog_offers_the_away_switch(hass: HomeAssistant) -> None:
    cloud = RrcCloud(absence_tree())
    entry, coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    try:
        controls = controls_flow.controls(coordinator)
        assert AWAY in controls and FIREPLACE not in controls
        flow = controls_flow.ControlsOptionsFlow()
        flow.hass = hass
        rows = await flow._control_rows(
            {AWAY: controls[AWAY]}, current=controls, context=RRC_CONTEXT
        )
        assert rows[0]["kind"] == "switch"
        assert [option["value"] for option in rows[0]["options"]] == ["true", "false"]
        validate_control_value(controls[AWAY], "true", RRC_CONTEXT)
    finally:
        await teardown(hass, entry, stack)


async def test_a_heat_pump_keeps_its_on_off_away_switch(hass: HomeAssistant) -> None:
    """S11 and T5: the K40 capture has the same entities and requests."""
    cloud = RrcCloud(
        load_tree("captures/k40-cloud-2026-11-05.json"),
        forbidden=set(),
        missing_as_500=False,
    )
    from tests.helpers.rrc_absence import EMS_GATEWAY

    entry, _coordinator, _clock, _sleeps, stack = await setup(hass, cloud, EMS_GATEWAY)
    try:
        assert not {
            path
            for path in cloud.all_requested()
            if path.startswith("/events") or path.endswith("/boostShortcut")
        }
        registry = er.async_get(hass)
        unique_ids = {
            item.unique_id
            for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        }
        assert not {
            uid for uid in unique_ids if "boostShortcut" in uid or "events" in uid
        }
    finally:
        await teardown(hass, entry, stack)


def test_a_used_but_unavailable_flag_is_shown_and_locked() -> None:
    resource = parse_resource(
        {
            "id": AWAY,
            "type": "stringValue",
            "writeable": 1,
            "recordable": 0,
            "value": "false",
            "used": "true",
            "available": "false",
        }
    )
    # S8: shown and locked.
    assert assess_control(resource, RRC_CONTEXT).supports_switch
    with pytest.raises(ControlLocked):
        validate_control_value(resource, "true", RRC_CONTEXT)
