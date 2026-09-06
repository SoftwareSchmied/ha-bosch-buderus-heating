"""Control rejection evidence agrees with entity creation and remains redacted."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from custom_components.bosch_buderus_heating.diagnostics import (
    _capability_diagnostics,
    _control_diagnostics,
)
from custom_components.bosch_buderus_heating.number import build_number_descriptions
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata
from custom_components.bosch_buderus_heating.select import build_select_descriptions
from custom_components.bosch_buderus_heating.switch import build_switch_descriptions

MODE = Resource(
    path="/heatingCircuits/hc2/operationMode",
    value="manual",
    has_value=True,
    metadata=ResourceMetadata(
        resource_type="stringValue",
        writable=True,
        allowed_values=("manual", "auto"),
    ),
)
TEMPERATURE = Resource(
    path="/heatingCircuits/hc2/manualRoomSetpoint",
    value=21.5,
    has_value=True,
    metadata=ResourceMetadata(
        resource_type="floatValue", writable=True, unit="C", minimum=5, maximum=30
    ),
)


@pytest.mark.parametrize(
    ("resource", "reason"),
    [
        (MODE, None),
        (
            replace(MODE, metadata=replace(MODE.metadata, writable=False)),
            "not_writable",
        ),
        (
            replace(MODE, metadata=replace(MODE.metadata, resource_type="floatValue")),
            "unsupported_resource_type",
        ),
        (
            replace(MODE, metadata=replace(MODE.metadata, allowed_values=())),
            "no_supported_options",
        ),
        (replace(MODE, has_value=False), "missing_value"),
        (replace(MODE, value=None), "missing_value"),
        (replace(MODE, value=12.5), "invalid_value_type"),
        (replace(MODE, value="private-mode"), "unsupported_current_option"),
        (replace(MODE, value="off"), "current_option_not_advertised"),
        (
            replace(MODE, path="/system/silentMode/enabled", value="auto"),
            "incomplete_options",
        ),
    ],
)
def test_select_diagnostics_explain_the_actual_creation_decision(
    resource: Resource, reason: str | None
) -> None:
    report = _control_diagnostics(resource)
    descriptions = build_select_descriptions({resource.path: resource})
    assert report["platform"] == "select"
    assert report["rejection_reason"] == reason
    assert report["eligible"] == bool(descriptions) == (reason is None)
    assert report["enabled_by_default"]


@pytest.mark.parametrize(
    ("resource", "reason"),
    [
        (TEMPERATURE, None),
        (
            replace(
                TEMPERATURE, metadata=replace(TEMPERATURE.metadata, writable=False)
            ),
            "not_writable",
        ),
        (
            replace(
                TEMPERATURE,
                metadata=replace(TEMPERATURE.metadata, resource_type="stringValue"),
            ),
            "unsupported_resource_type",
        ),
        (
            replace(TEMPERATURE, metadata=replace(TEMPERATURE.metadata, unit="bar")),
            "unsupported_unit",
        ),
        (
            replace(TEMPERATURE, metadata=replace(TEMPERATURE.metadata, minimum=None)),
            "missing_bounds",
        ),
        (
            replace(TEMPERATURE, metadata=replace(TEMPERATURE.metadata, maximum=None)),
            "missing_bounds",
        ),
        (
            replace(
                TEMPERATURE,
                metadata=replace(TEMPERATURE.metadata, minimum=float("nan")),
            ),
            "non_finite_bounds",
        ),
        (
            replace(
                TEMPERATURE,
                metadata=replace(TEMPERATURE.metadata, maximum=float("inf")),
            ),
            "non_finite_bounds",
        ),
        (
            replace(
                TEMPERATURE,
                metadata=replace(TEMPERATURE.metadata, minimum=30, maximum=5),
            ),
            "inverted_bounds",
        ),
        (
            replace(TEMPERATURE, metadata=replace(TEMPERATURE.metadata, minimum=0)),
            "unsafe_bounds",
        ),
        (
            replace(TEMPERATURE, metadata=replace(TEMPERATURE.metadata, maximum=35)),
            "unsafe_bounds",
        ),
        (replace(TEMPERATURE, has_value=False), "missing_value"),
        (replace(TEMPERATURE, value=None), "missing_value"),
        (replace(TEMPERATURE, value=True), "invalid_value_type"),
        (replace(TEMPERATURE, value="private-setpoint"), "invalid_value_type"),
        (replace(TEMPERATURE, value=float("nan")), "non_finite_value"),
        (replace(TEMPERATURE, value=31), "value_out_of_bounds"),
        (replace(TEMPERATURE, value=21.25), "value_off_step"),
    ],
)
def test_number_diagnostics_explain_the_actual_creation_decision(
    resource: Resource, reason: str | None
) -> None:
    report = _control_diagnostics(resource)
    descriptions = build_number_descriptions({resource.path: resource})
    assert report["platform"] == "number"
    assert report["rejection_reason"] == reason
    assert report["eligible"] == bool(descriptions) == (reason is None)
    # Even malformed metadata produces standard JSON without NaN or infinity.
    json.dumps(report, allow_nan=False)


def test_heating_mode_diagnostics_reveal_only_known_options() -> None:
    resource = replace(
        MODE,
        value="private-current-value",
        metadata=replace(
            MODE.metadata,
            allowed_values=("manual", "auto", "private-option", 987654321, None),
        ),
    )
    report = _capability_diagnostics(resource, None, {}, 0)
    assert report["path"] == MODE.path
    assert report["control"] == {
        "platform": "select",
        "eligible": False,
        "rejection_reason": "unsupported_current_option",
        "enabled_by_default": True,
        "advertised_known_options": ["auto", "manual"],
        "unrecognized_option_count": 3,
        "requires_all_options": False,
    }
    serialized = json.dumps(report)
    for private in ("private-current-value", "private-option", "987654321"):
        assert private not in serialized


def test_number_diagnostics_distinguish_metadata_from_current_settings() -> None:
    report = _control_diagnostics(TEMPERATURE)
    assert report == {
        "platform": "number",
        "eligible": True,
        "rejection_reason": None,
        "enabled_by_default": True,
        "minimum": 5,
        "maximum": 30,
        "policy_minimum": 5,
        "policy_maximum": 30,
        "policy_step": 0.5,
        "policy_unit": "C",
    }
    assert "21.5" not in json.dumps(report)


def test_installer_control_is_eligible_but_disabled_by_default() -> None:
    resource = replace(
        TEMPERATURE,
        path="/heatingCircuits/hc2/maxFlowTemp",
        value=40,
        metadata=replace(TEMPERATURE.metadata, minimum=30, maximum=60),
    )
    report = _control_diagnostics(resource)
    description = build_number_descriptions({resource.path: resource})[0]
    assert report["eligible"]
    assert not report["enabled_by_default"]
    assert not description.entity_registry_enabled_default
    assert report["minimum"] == 30
    assert report["maximum"] == 60
    assert report["policy_step"] == 1


def test_switch_still_requires_both_options() -> None:
    resource = replace(
        MODE,
        path="/dhwCircuits/dhw2/charge",
        value="stop",
        metadata=replace(MODE.metadata, allowed_values=("start", "stop")),
    )
    assert _control_diagnostics(resource)["platform"] == "switch"
    assert _control_diagnostics(resource)["eligible"]
    assert build_switch_descriptions({resource.path: resource})
    incomplete = replace(
        resource, metadata=replace(resource.metadata, allowed_values=("stop",))
    )
    assert _control_diagnostics(incomplete)["rejection_reason"] == "incomplete_options"
    assert not build_switch_descriptions({incomplete.path: incomplete})


def test_unreleased_paths_do_not_publish_metadata_or_gain_controls() -> None:
    resource = replace(
        TEMPERATURE,
        path="/devices/private-device/private-setting",
        metadata=replace(TEMPERATURE.metadata, minimum=123456789, maximum=987654321),
    )
    report = _control_diagnostics(resource)
    assert report == {
        "platform": None,
        "eligible": False,
        "rejection_reason": "no_scalar_control_policy",
        "enabled_by_default": None,
    }
    assert not build_number_descriptions({resource.path: resource})
    assert not build_select_descriptions({resource.path: resource})
    assert not build_switch_descriptions({resource.path: resource})
