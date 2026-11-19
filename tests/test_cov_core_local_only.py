"""Local-only readings: which unit and value a reading may show. Text and
flags are never shown as numbers, an unverified signal follows the unit its
own gateway reports."""

from __future__ import annotations

import re
from decimal import Decimal
from types import SimpleNamespace

import pytest
from homeassistant.components.sensor import SensorDeviceClass

from custom_components.bosch_buderus_heating.local_only import (
    GENERIC,
    MODE,
    READING,
    STATE,
    TEMPERATURE,
    LocalOnlyDefinition,
    native_value,
    numeric,
    unit_and_class,
)
from custom_components.bosch_buderus_heating.observations import ValueStatus


def _definition(kind, **extra) -> LocalOnlyDefinition:
    return LocalOnlyDefinition(
        re.compile(r"/signals/x"), "x", kind, True, ("x", "x"), **extra
    )


def _observation(value, **extra):
    data = {
        "status": ValueStatus.VALID,
        "parts": (),
        "value": value,
        "resource_type": "floatValue",
        "matched": (),
        "state_names": (),
        "declared": (),
    }
    data.update(extra)
    return SimpleNamespace(**data)


# ------------------------------------------------------ unit and class


def test_a_state_never_carries_a_unit() -> None:
    assert unit_and_class(_definition(STATE), "°C") == (None, SensorDeviceClass.ENUM)


def test_an_unverified_signal_follows_the_unit_of_its_gateway() -> None:
    definition = _definition(GENERIC, unverified=True, default_unit="°C")
    assert unit_and_class(definition, "K") == ("K", SensorDeviceClass.TEMPERATURE)
    # The expected unit only stands in while the answer declares none.
    assert unit_and_class(definition, None) == (
        "°C",
        SensorDeviceClass.TEMPERATURE,
    )
    # A unit the integration does not map is kept as reported, without class.
    assert unit_and_class(definition, "rad") == ("rad", None)
    # Neither a declared nor an expected unit: shown without any.
    assert unit_and_class(_definition(GENERIC, unverified=True), None) == (None, None)


def test_an_unverified_reading_keeps_the_unit_but_never_the_class() -> None:
    definition = _definition(READING, unverified=True)
    assert unit_and_class(definition, "°C") == ("°C", None)


# ------------------------------------------------------- shown values


def test_a_value_that_is_a_flag_or_missing_is_never_a_number() -> None:
    definition = _definition(GENERIC)
    assert native_value(definition, _observation(True)) is None
    assert native_value(definition, _observation(None)) is None
    assert native_value(definition, _observation(5)) == 5


def test_text_is_never_shown_for_a_measurement() -> None:
    assert native_value(_definition(GENERIC), _observation("SERIAL-12345")) is None
    assert native_value(_definition(TEMPERATURE), _observation("warm")) is None


def test_decimal_values_follow_the_reported_type() -> None:
    assert (
        native_value(
            _definition(GENERIC),
            _observation(Decimal("12"), resource_type="integerValue"),
        )
        == 12
    )
    shown = native_value(_definition(GENERIC), _observation(Decimal("12.5")))
    assert shown == 12.5
    assert isinstance(shown, float)


@pytest.mark.parametrize("status", [ValueStatus.UNKNOWN_UNIT, ValueStatus.VALID])
def test_an_unknown_unit_still_shows_the_number_of_a_generic_signal(status) -> None:
    assert native_value(_definition(GENERIC), _observation(7, status=status)) == 7
    assert native_value(_definition(TEMPERATURE), _observation(7, status=status)) == (
        7 if status is ValueStatus.VALID else None
    )


def test_a_smart_grid_state_map_without_a_match_shows_nothing() -> None:
    definition = _definition(MODE)
    observation = _observation(3, state_names=("normal", "boost"), matched=())
    assert native_value(definition, observation) is None


def test_a_smart_grid_without_a_state_map_reads_the_number_or_name() -> None:
    definition = _definition(MODE, options=("not_used", "mode_1"))
    assert native_value(definition, _observation(0)) == "not_used"
    # Out of range, fractional and boolean readings are no mode.
    assert native_value(definition, _observation(9)) is None
    assert native_value(definition, _observation(1.5)) is None
    assert native_value(definition, _observation("nonsense")) is None


# --------------------------------------------------------------- numeric


def test_only_a_valid_number_offers_a_generic_entry() -> None:
    assert numeric(None) is False
    assert numeric(_observation(5)) is True
    assert numeric(_observation(Decimal("1.5"))) is True
    assert numeric(_observation(True)) is False
    assert numeric(_observation("5")) is False
    assert numeric(_observation(5, status=ValueStatus.UNKNOWN_UNIT)) is False
