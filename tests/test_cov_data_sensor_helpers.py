"""Helpers of the sensor platform: module summaries, subkeys of partial
payloads, heat demand lists, units of readings and the entity category."""

from __future__ import annotations

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory

from custom_components.bosch_buderus_heating import sensor
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata


def _resource(
    path: str, value: object = None, values: tuple[object, ...] = (), **meta: object
) -> Resource:
    return Resource(
        path=path,
        value=value,  # type: ignore[arg-type]
        has_value=value is not None,
        values=values,  # type: ignore[arg-type]
        metadata=ResourceMetadata(resource_type="floatValue", **meta),  # type: ignore[arg-type]
    )


# ------------------------------------------------------------- module list
def test_a_module_without_a_name_is_numbered_and_a_repeated_version_is_not_added() -> (
    None
):
    resource = _resource(
        "/system/info",
        values=(
            {"Ver": "1.2"},
            {"ProductName": "Controller", "Ver": "Controller"},
            {"ProductName": "Pump", "Ver": "3.4"},
            "not a module",
        ),
    )
    assert sensor._system_info_summary(resource) == (
        "Modul 1 \N{MIDDLE DOT} Version 1.2; Controller; "
        "Pump \N{MIDDLE DOT} Version 3.4"
    )


def test_without_any_module_the_summary_says_so() -> None:
    assert sensor._system_info_summary(_resource("/system/info", values=("x",))) == (
        "Keine Module erkannt"
    )


def test_a_long_module_list_is_shortened_and_a_single_long_name_is_cut() -> None:
    many = tuple({"ProductName": f"Module number {index:03d}"} for index in range(40))
    summary = sensor._system_info_summary(_resource("/system/info", values=many))
    assert len(summary) <= sensor._MAX_SENSOR_STATE_LENGTH
    assert summary.endswith("weitere)") and "Module number 000" in summary
    single = _resource("/system/info", values=({"ProductName": "x" * 400},))
    cut = sensor._system_info_summary(single)
    assert len(cut) == sensor._MAX_SENSOR_STATE_LENGTH
    assert cut.endswith("\N{HORIZONTAL ELLIPSIS}")


def test_the_first_usable_text_of_a_module_field_list_is_taken() -> None:
    item = {"a": "  ", "b": 5, "c": " value ", "d": "later"}
    assert sensor._first_nonempty_string(item, ("a", "b", "c", "d")) == "value"
    assert sensor._first_nonempty_string(item, ("a", "b", "missing")) is None


# ---------------------------------------------------------- partial payloads
def test_known_subkeys_of_start_and_runtime_counters_with_a_partial_payload() -> None:
    assert sensor._known_value_keys("/heatSources/hs1/numberOfStarts") == (
        "total",
        "ch",
        "cooling",
        "dhw",
        "pool",
    )
    assert sensor._known_value_keys("/heatSources/hs1/workingTime") == (
        "total",
        "ch",
        "cooling",
        "dhw",
    )
    assert sensor._known_value_keys("/heatSources/hs1/other") == ()
    assert sensor._known_value_keys("/heatSources/a/b/numberOfStarts") == ()


def test_the_heat_demand_list_becomes_one_stable_state() -> None:
    def state(*items: object) -> str:
        return sensor._heat_demand_state(_resource("/heatSources/demand", values=items))

    assert state() == "none"
    assert state("central_heating", "domestic_hot_water") == "ch_dhw"
    assert state("Frost-Protection", "Heating") == "ch_frost"
    assert state(5, None, ["x"], "hot_water") == "dhw"
    assert state("unknown demand") == "none"


# ------------------------------------------------------- units and category
def test_a_percent_value_key_gives_a_percentage_measurement() -> None:
    unit, device_class, state_class, scale = sensor._measurement_attributes(
        _resource("/heatSources/something/level", 40.0), "cur_percent"
    )
    assert (unit, device_class, state_class, scale) == (
        PERCENTAGE,
        None,
        SensorStateClass.MEASUREMENT,
        1.0,
    )
    plain = sensor._measurement_attributes(
        _resource("/heatSources/something/level", 40.0), None
    )
    assert plain == (None, None, SensorStateClass.MEASUREMENT, 1.0)
    text = sensor._measurement_attributes(
        Resource(
            path="/heatSources/something/level",
            value="x",
            has_value=True,
            metadata=ResourceMetadata(resource_type="stringValue"),
        ),
        None,
    )
    assert text == (None, None, None, 1.0)
    assert SensorDeviceClass.ENERGY


@pytest.mark.parametrize(
    ("path", "category"),
    [
        ("/heatSources/hs1/numberOfStarts", EntityCategory.DIAGNOSTIC),
        ("/heatSources/hs1/workingTime", EntityCategory.DIAGNOSTIC),
        ("/heatSources/emon/totalConsumption", None),
        ("/system/sensors/temperatures/outdoor_t1", None),
        ("/gateway/versionFirmware", EntityCategory.DIAGNOSTIC),
        ("/system/something/else", EntityCategory.DIAGNOSTIC),
        ("/heatSources/systemPressureRange", EntityCategory.DIAGNOSTIC),
    ],
)
def test_the_entity_category_of_service_values_is_diagnostic(
    path: str, category: EntityCategory | None
) -> None:
    assert sensor._entity_category(_resource(path, 1.0)) == category
