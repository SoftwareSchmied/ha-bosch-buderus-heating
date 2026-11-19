"""Display and outcome details of device writes (0.9.2, synthetic).

- A program name longer than the advertised maximum is still shown.
- An air conditioner timer whose minimum lies off the 30-minute grid offers
  steps that reach the unit's values.
- A room controller's hot water week or day accepted with an answer that
  cannot be read is decided by the read-back, never sent again.
- The clear button of a zone is named in the profile language.

Nothing here confirms behaviour on a real device.
"""

from __future__ import annotations

import copy

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.bosch_buderus_heating import text as text_platform
from custom_components.bosch_buderus_heating.aircon_entities import _timers
from custom_components.bosch_buderus_heating.aircon_policies import TIMER_OFF
from custom_components.bosch_buderus_heating.pointt import InvalidPayload
from custom_components.bosch_buderus_heating.rrc_programs import day_change
from custom_components.bosch_buderus_heating.zone_climate import (
    ZoneTemporaryClearButton,
)
from tests import test_rrc_dhw_program as dhw
from tests import test_rrc_point_add_delete as rrc_day
from tests.aircon_helpers import (  # noqa: F401
    FakeUnit,
    aircon_cleanup,
    make_coordinator,
)
from tests.test_program_names import NAME, case, gateway_for
from tests.test_program_names import environment as environment
from tests.test_zone_climate import zone_plant as zone_plant

# Program names ---------------------------------------------------------------


def test_a_name_longer_than_its_maximum_is_still_shown(environment):
    gateway = gateway_for(environment, "ascii_plain")
    gateway.change(NAME, value="Office and workshop")
    gateway.refresh()
    (description,) = text_platform.descriptions({NAME: gateway.read(NAME)})
    entity = text_platform.ProgramNameText(gateway.coordinator, description)
    entity.hass = gateway.coordinator.hass
    entity.entity_id = "text.program_a_name"
    assert entity.native_value == "Office and workshop"
    assert entity.state == "Office and workshop"
    assert entity.native_max == len("Office and workshop")


def test_a_short_name_keeps_the_advertised_maximum(environment):
    gateway = gateway_for(environment, "ascii_plain")
    (description,) = text_platform.descriptions({NAME: gateway.read(NAME)})
    entity = text_platform.ProgramNameText(gateway.coordinator, description)
    assert entity.native_max == case("ascii_plain")["valueConfig"]["maxLength"]


async def test_a_name_longer_than_its_maximum_is_not_written(environment):
    gateway = gateway_for(environment, "ascii_plain")
    (description,) = text_platform.descriptions({NAME: gateway.read(NAME)})
    entity = text_platform.ProgramNameText(gateway.coordinator, description)
    entity.hass = gateway.coordinator.hass
    with pytest.raises(ServiceValidationError):
        await entity.async_set_value("Office and workshop")
    assert gateway.puts == 0


# Air conditioner timer -------------------------------------------------------


def _timer(coordinator):
    return dict(_timers(coordinator))["airConditioning:timers:off"]()


async def test_timer_on_the_grid_keeps_thirty_minutes(hass):
    coordinator, _unit, _ = await make_coordinator(hass)
    assert _timer(coordinator).native_step == 30.0


@pytest.mark.parametrize(("minimum", "step"), [(15.0, 15.0), (20.0, 10.0)])
async def test_timer_minimum_off_the_grid_gets_a_reaching_step(hass, minimum, step):
    unit = FakeUnit()
    unit.set(TIMER_OFF, minValue=minimum)
    coordinator, unit, _ = await make_coordinator(hass, unit)
    timer = _timer(coordinator)
    timer.hass = hass
    assert timer.native_step == step
    # 0 stops the timer; the unit's own values lie on the offered steps.
    for value in (0.0, minimum, minimum + 30):
        assert value % step == 0
    await timer.async_set_native_value(minimum + 30)
    assert unit.puts == [(TIMER_OFF, minimum + 30)]


# Room controller programs: an unreadable answer to an accepted write ---------


@pytest.fixture
def dhw_gateway(environment):
    yield dhw.gateway.__wrapped__(environment)


@pytest.fixture
def day_gateway(environment):
    yield rrc_day.gateway.__wrapped__(environment)


def _unreadable_answer(gateway, *, applied: bool = True) -> None:
    original = gateway.put_value

    async def put_value(gw, path, value):
        if applied:
            await original(gw, path, value)
        else:
            gateway.value_puts.append((path, copy.deepcopy(value)))
        raise InvalidPayload("answer cannot be read")

    gateway.client.put_resource_value.side_effect = put_value


async def test_hot_water_week_with_an_unreadable_answer_is_read_back(dhw_gateway):
    _unreadable_answer(dhw_gateway)
    week = dhw.with_point(dhw.program().points, 0, 600, "off")
    await dhw_gateway.coordinator.async_write_rrc_dhw_program(
        dhw_gateway.read(dhw.PATH), week
    )
    assert len(dhw_gateway.value_puts) == 1


async def test_hot_water_week_not_stored_stays_unconfirmed(dhw_gateway):
    from custom_components.bosch_buderus_heating.pointt import WriteNotConfirmed

    _unreadable_answer(dhw_gateway, applied=False)
    week = dhw.with_point(dhw.program().points, 0, 600, "off")
    with pytest.raises(WriteNotConfirmed):
        await dhw_gateway.coordinator.async_write_rrc_dhw_program(
            dhw_gateway.read(dhw.PATH), week
        )
    assert len(dhw_gateway.value_puts) == 1


async def test_day_with_an_unreadable_answer_is_read_back(day_gateway):
    _unreadable_answer(day_gateway)
    points = day_change(rrc_day.schedule(), "Mo", add=(720, 15.0))
    await day_gateway.coordinator.async_write_rrc_day(
        day_gateway.read(rrc_day.WEEK), "Mo", points
    )
    assert len(day_gateway.value_puts) == 1


# Zone clear button -----------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "name"),
    [
        ("de", "Vorübergehenden Sollwert aufheben"),
        ("en", "Clear temporary setpoint"),
        ("fr", "Annuler la consigne temporaire"),
        ("cs", "Zrušit dočasnou požadovanou hodnotu"),
    ],
)
def test_zone_clear_button_is_named_in_the_profile_language(
    hass, plant, language, name
):
    coordinator, _cloud = plant
    hass.config.language = language
    button = ZoneTemporaryClearButton(coordinator, "/zones/zone1")
    assert button.name is not None and button.name.endswith(name)
    # The icon of the heating circuit's clear button (icons.json).
    assert button.translation_key == "heating_temporary_setpoint_clear"
