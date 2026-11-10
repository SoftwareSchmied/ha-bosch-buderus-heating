"""W4 addendum to W0: placeholder options, a minimum of options, P41.

A contract selection may leave advertised placeholders such as "" out and
ask for at least two usable options; an unavailable contract setting that is
not marked as used gives no control at all (S8/P41). The generic policies
keep their behaviour.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from custom_components.bosch_buderus_heating import rrc_contracts
from custom_components.bosch_buderus_heating.pointt import Resource
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.rrc_contract_base import RrcContract
from custom_components.bosch_buderus_heating.rrc_contracts import Registry
from custom_components.bosch_buderus_heating.writes import (
    GENERIC_CONTEXT,
    RRC_CONTEXT,
    EnumWritePolicy,
    NumberWritePolicy,
    assess_control,
    validate_control_value,
)

MODE = "/system/w4probe/mode"
LEVEL = "/system/w4probe/level"
MODES = frozenset({"one", "two", "three"})
MODE_POLICY = EnumWritePolicy(
    rf"^{MODE}$",
    frozenset({"stringValue"}),
    MODES,
    known_only=True,
    ignored_options=frozenset({""}),
    min_options=2,
)
LEVEL_POLICY = NumberWritePolicy(rf"^{LEVEL}$", "C", 1.0, strict_grid=True)
PROBE = RrcContract(
    name="w_four_probe",
    enum_policies=(MODE_POLICY,),
    number_policies=(LEVEL_POLICY,),
)


@contextmanager
def installed() -> Iterator[None]:
    original = rrc_contracts._registry
    rrc_contracts._registry = Registry((*rrc_contracts.CONTRACTS, PROBE))
    try:
        yield
    finally:
        rrc_contracts._registry = original


def mode(*allowed: str, value: str = "one", **flags: object) -> Resource:
    return parse_resource(
        {
            "id": MODE,
            "type": "stringValue",
            "writeable": 1,
            "value": value,
            "allowedValues": list(allowed),
            **flags,
        }
    )


def level(**flags: object) -> Resource:
    return parse_resource(
        {
            "id": LEVEL,
            "type": "floatValue",
            "writeable": 1,
            "unitOfMeasure": "C",
            "minValue": 10,
            "maxValue": 30,
            "stepSize": 1,
            "value": 20,
            **flags,
        }
    )


def test_placeholder_options_are_left_out() -> None:
    with installed():
        assessment = assess_control(mode("one", "two", "three", ""), RRC_CONTEXT)
        assert assessment.eligible
        assert assessment.options == ("one", "two", "three")
        with pytest.raises(Exception, match="not currently advertised"):
            validate_control_value(mode("one", "two", "three", ""), "", RRC_CONTEXT)


@pytest.mark.parametrize(
    ("allowed", "reason"),
    [
        (("one",), "too_few_options"),
        (("one", ""), "too_few_options"),
        (("one", "unknown"), "too_few_options"),
        (("",), "missing_options"),
        ((), "missing_options"),
    ],
)
def test_fewer_usable_options_leave_no_control(
    allowed: tuple[str, ...], reason: str
) -> None:
    with installed():
        assert assess_control(mode(*allowed), RRC_CONTEXT).rejection_reason == reason


def test_default_policies_need_only_one_option() -> None:
    """Without the new fields one advertised option stays a control."""
    single = EnumWritePolicy(
        rf"^{MODE}$", frozenset({"stringValue"}), MODES, known_only=True
    )
    contract = RrcContract(name="w_four_single", enum_policies=(single,))
    original = rrc_contracts._registry
    rrc_contracts._registry = Registry((*rrc_contracts.CONTRACTS, contract))
    try:
        assessment = assess_control(mode("one"), RRC_CONTEXT)
        assert assessment.eligible and assessment.options == ("one",)
        # An empty option still makes the list invalid without ignored_options.
        assert (
            assess_control(mode("one", "two", ""), RRC_CONTEXT).rejection_reason
            == "invalid_options"
        )
    finally:
        rrc_contracts._registry = original


@pytest.mark.parametrize(
    ("flags", "reason"),
    [
        ({"available": "false"}, "rrc_unavailable"),
        ({"available": False, "used": "false"}, "rrc_unavailable"),
        ({"available": "maybe"}, "rrc_unavailable"),
        ({"used": "true", "available": "false"}, None),  # S8: shown, locked
        ({"used": "true", "available": "true"}, None),
        ({}, None),
    ],
)
def test_p41_unavailable_contract_settings_give_no_control(
    flags: dict[str, object], reason: str | None
) -> None:
    with installed():
        assert (
            assess_control(mode("one", "two", **flags), RRC_CONTEXT).rejection_reason
            == reason
        )
        assert assess_control(level(**flags), RRC_CONTEXT).rejection_reason == reason


def test_p41_needs_a_contract_policy_and_an_rrc_plant() -> None:
    """S11: the same flags elsewhere keep today's assessment."""
    with installed():
        assert (
            assess_control(
                mode("one", "two", available="false"), GENERIC_CONTEXT
            ).rejection_reason
            == "no_scalar_control_policy"
        )
    away = parse_resource(
        {
            "id": "/system/awayMode/enabled",
            "type": "stringValue",
            "writeable": 1,
            "value": "off",
            "allowedValues": ["on", "off"],
            "available": "false",
        }
    )
    assert assess_control(away, RRC_CONTEXT).eligible
