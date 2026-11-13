"""P44: the official examples are checked against their meaning, not only syntax.

A schema can accept an example that still contradicts what the value means: an
error marker in a temperature, a misspelled unit or a value outside its own
allowed values. Each such deviation of the pinned specification is listed here
with how the integration handles it. The comparison tool reports deviations a
new specification adds; this test keeps the list exact once the pin moves.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.bosch_buderus_heating.observations import ValueStatus
from scripts.compare_local_api_spec import example_deviations
from tests.test_observations import local

EXAMPLES = json.loads(
    (Path(__file__).parent / "fixtures/local_api_examples.json").read_text(
        encoding="utf-8"
    )
)["examples"]
VENTILATION = "/ventilation/{ventilationZoneId}/sensors/"

# Deviations of the pinned specification (commit 78e36b3) and their handling.
KNOWN = {
    # Misspelled unit; kept on the reading but never used as a measurement.
    ("unit", VENTILATION + "supplyFanRotation", 'rpm"'),
    # Example value outside its own allowedValues; text is shown as reported.
    ("allowed_value", "/devices/{deviceId}/type", "XLOW_RF"),
    # A tenth of the declared marker -32768: recognized as a marker.
    ("temperature", "/heatSources/returnTemperature", "tenth_of_marker"),
    ("temperature", "/heatSources/{heatSourceId}/returnFlowTemp", "tenth_of_marker"),
    # Marker without a declared state; the local-only ventilation sensors
    # treat it as a marker and show no value (tests/test_local_only.py).
    ("temperature", VENTILATION + "exhaustTemp", "undeclared_marker"),
    ("temperature", VENTILATION + "extractTemp", "undeclared_marker"),
    ("temperature", VENTILATION + "outdoorTemp", "undeclared_marker"),
    ("temperature", VENTILATION + "supplyTemp", "undeclared_marker"),
}


def test_official_examples_deviate_only_as_listed() -> None:
    assert example_deviations(EXAMPLES) == KNOWN


@pytest.mark.parametrize(
    "template",
    sorted(t for _, t, detail in KNOWN if detail == "tenth_of_marker"),
)
def test_tenth_of_marker_examples_are_never_shown_as_temperatures(template) -> None:
    (row,) = [r for r in EXAMPLES if r["template"] == template]
    assert local(row["response"]).status is ValueStatus.STATE_MARKER


def test_misspelled_unit_is_never_used_as_a_measurement() -> None:
    ((_, template, _),) = [item for item in KNOWN if item[0] == "unit"]
    (row,) = [r for r in EXAMPLES if r["template"] == template]
    assert local(row["response"]).status is ValueStatus.UNKNOWN_UNIT
