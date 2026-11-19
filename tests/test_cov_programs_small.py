"""Small guards of the program and zone helpers: readings that must not be
taken for valid ones."""

from __future__ import annotations

import copy

import pytest

from custom_components.bosch_buderus_heating import (
    rrc,
    rrc_contract_zone,
    rrc_energy_statistics,
)
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata
from custom_components.bosch_buderus_heating.switch_program_selection import (
    program_readable,
)
from custom_components.bosch_buderus_heating.switch_program_sensor import attributes
from custom_components.bosch_buderus_heating.switch_program_service import (
    program_shows,
)
from custom_components.bosch_buderus_heating.switch_programs import (
    assess_switch_program,
)
from tests.program_kind_helpers import resource
from tests.switch_program_helpers import PROGRAM_A, PROGRAM_B, payload
from tests.test_switch_program_kinds import (
    WDDW2,
    WDDW2_RANGE,
    items,
    readings,
)

MANUAL = "/zones/zn1/manualTemperatureHeating"


def zone_leaf(
    path: str, value: object, unit: str = "C", **metadata: object
) -> Resource:
    return Resource(
        path,
        value=value,  # type: ignore[arg-type]
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="floatValue", unit=unit, writable=True, **metadata
        ),
    )


# ----------------------------------------------------------- zone readings
def test_a_zone_value_with_a_bound_that_is_no_number_is_no_reading():
    assert rrc.scalar_value(zone_leaf(MANUAL, 21.0, minimum=5.0, maximum=30.0)) == 21.0
    assert rrc.scalar_value(zone_leaf(MANUAL, 21.0, minimum=float("inf"))) is None
    assert rrc.scalar_value(zone_leaf(MANUAL, 21.0, maximum=float("nan"))) is None


@pytest.mark.parametrize("step", [0.0, -0.5, float("inf"), float("nan")])
def test_a_zone_setpoint_with_an_unusable_step_is_not_controllable(step):
    leaf = zone_leaf(MANUAL, 21.0, minimum=5.0, maximum=30.0, step_size=step)
    assert rrc.control_issue(leaf) == "invalid_step"
    assert (
        rrc.control_issue(zone_leaf(MANUAL, 21.0, minimum=5.0, step_size=0.5)) is None
    )


@pytest.mark.parametrize("value", [True, float("inf"), float("nan"), "21"])
def test_a_zone_detail_that_is_no_number_is_no_reading(value):
    valve = f"/zones/zn1/{rrc_contract_zone.VALVE_POSITION}"
    assert rrc.detail_value(zone_leaf(valve, 50.0, "%")) == 50.0
    assert rrc.detail_value(zone_leaf(valve, value, "%")) is None


def test_firmware_of_devices_is_only_listed_from_the_device_list():
    other = Resource("/devices/other", value=[{"id": 1}], has_value=True)
    assert rrc.device_firmware_paths(other, 5) == ()


# ------------------------------------------------------ program selection
@pytest.mark.parametrize("program", [None, 1, "C", "", ["A"]])
def test_only_a_known_program_letter_can_be_readable(program):
    data = {PROGRAM_A: resource(payload(PROGRAM_A))}
    circuit = "/heatingCircuits/hc1"
    assert program_readable(circuit, "A", data) is True
    assert program_readable(circuit, program, data) is False
    assert program_readable(circuit, "B", data) is False  # no reading of B


def test_a_reading_of_another_program_never_shows_the_week():
    shown = resource(payload(PROGRAM_A))
    points = assess_switch_program(shown).points
    assert program_shows(PROGRAM_A, points)(shown) is True
    assert program_shows(PROGRAM_A, points)(resource(payload(PROGRAM_B))) is False
    broken = copy.deepcopy(payload(PROGRAM_A))
    broken["switchPoints"] = "garbage"
    assert program_shows(PROGRAM_A, points)(resource(broken)) is False


# ----------------------------------------------------------------- sensor
def test_a_hot_water_program_without_its_range_shows_no_setpoint_limits():
    data = readings("wddw2", **{WDDW2_RANGE: None})
    attrs = attributes(data[WDDW2], data, True, 0, 400)
    assert attrs is not None
    assert "min_setpoint" not in attrs and "max_setpoint" not in attrs
    complete = readings("wddw2")
    full = attributes(complete[WDDW2], complete, True, 0, 400)
    assert full is not None and full["min_setpoint"] == 30.0
    assert items("wddw2") is not None


# ------------------------------------------------------------ diagnostics
def test_the_energy_report_without_a_started_import_says_so():
    class Coordinator:
        pass

    report = rrc_energy_statistics.diagnostics(Coordinator())  # type: ignore[arg-type]
    assert report == {"confirmation": "unconfirmed", "waiting": "not_started"}
