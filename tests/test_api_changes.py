"""P22: a changed field never breaks the integration or is silently misread.

Each case builds the entity from the shape known at discovery and then lets
the cloud report a changed shape for the same path, as a firmware or server
update could. The entity must stay limited to that resource: show the value
correctly, or show nothing ("unknown"/"unavailable"), and never write with a
contract that no longer matches.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.coordinator import ResourceSnapshot
from custom_components.bosch_buderus_heating.pointt import (
    Resource,
    ResourceMetadata,
)
from custom_components.bosch_buderus_heating.writes import number_policy_for_resource
from tests.test_sensor import _sensor

OUTDOOR = "/system/sensors/temperatures/outdoor_t1"
PRESSURE = "/heatSources/systemPressure"
SETPOINT = "/heatingCircuits/hc1/manualRoomSetpoint"


def scalar(path, value, *, kind="floatValue", unit="C", **metadata) -> Resource:
    return Resource(
        path,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(resource_type=kind, unit=unit, **metadata),
    )


def report(sensor, resource: Resource) -> None:
    """The next poll returns a changed shape for the same path."""
    coordinator = sensor.coordinator
    coordinator.data = {
        **coordinator.data,
        resource.path: ResourceSnapshot(resource, True, datetime.now(UTC)),
    }


# ------------------------------------------------------------------- sensors


@pytest.mark.parametrize(
    ("unit", "value", "native"),
    [("F", 44.6, "°F"), ("K", 280.65, "K")],
)
async def test_changed_temperature_unit_is_shown_in_the_reported_unit(
    hass: HomeAssistant, unit, value, native
) -> None:
    """Value and unit come from the same reading; HA converts for display."""
    sensor = _sensor(hass, scalar(OUTDOOR, 7.0))
    assert sensor.native_unit_of_measurement == "°C"
    report(sensor, scalar(OUTDOOR, value, unit=unit))
    assert sensor.native_value == value
    assert sensor.native_unit_of_measurement == native


@pytest.mark.parametrize("unit", ["bar", None, "unknown"])
async def test_temperature_reported_as_another_quantity_shows_nothing(
    hass: HomeAssistant, unit
) -> None:
    sensor = _sensor(hass, scalar(OUTDOOR, 7.0))
    report(sensor, scalar(OUTDOOR, 7.0, unit=unit))
    assert sensor.native_value is None


async def test_changed_pressure_unit_is_shown_in_the_reported_unit(
    hass: HomeAssistant,
) -> None:
    sensor = _sensor(hass, scalar(PRESSURE, 1.5, unit="bar"))
    report(sensor, scalar(PRESSURE, 150.0, unit="kPa"))
    assert sensor.native_value == 150.0
    assert sensor.native_unit_of_measurement == "kPa"
    report(sensor, scalar(PRESSURE, 1.5, unit="C"))
    assert sensor.native_value is None


@pytest.mark.parametrize(
    "changed",
    [
        scalar(OUTDOOR, "7.5", kind="stringValue", unit=None),
        scalar(OUTDOOR, "on", kind="stringValue", unit=None),
        scalar(OUTDOOR, True, kind="booleanValue", unit=None),
        scalar(OUTDOOR, [7.5], kind="arrayData"),
        scalar(OUTDOOR, {"value": 7.5}, kind="objectValue"),
        Resource(OUTDOOR, metadata=ResourceMetadata(resource_type="floatValue")),
    ],
    ids=["number-as-text", "text", "boolean", "list", "object", "no-value"],
)
async def test_changed_value_type_shows_nothing_instead_of_a_wrong_value(
    hass: HomeAssistant, changed: Resource
) -> None:
    sensor = _sensor(hass, scalar(OUTDOOR, 7.0))
    report(sensor, changed)
    assert sensor.native_value is None


async def test_additional_unknown_fields_keep_the_value(hass: HomeAssistant) -> None:
    sensor = _sensor(hass, scalar(OUTDOOR, 7.0))
    report(
        sensor,
        replace(scalar(OUTDOOR, 8.0), unknown_fields=frozenset({"newVendorField"})),
    )
    assert sensor.native_value == 8.0


async def test_error_marker_after_a_change_is_not_a_temperature(
    hass: HomeAssistant,
) -> None:
    sensor = _sensor(hass, scalar(OUTDOOR, 7.0))
    report(sensor, scalar(OUTDOOR, -3276.8, numeric_error_values=(-3276.8,)))
    assert sensor.native_value is None


async def test_new_enum_value_is_unknown_and_counted(hass: HomeAssistant) -> None:
    path = "/heatingCircuits/hc1/operationMode"
    sensor = _sensor(
        hass,
        scalar(
            path,
            "auto",
            kind="stringValue",
            unit=None,
            allowed_values=("auto", "manual"),
        ),
    )
    report(
        sensor,
        scalar(
            path,
            "boost",
            kind="stringValue",
            unit=None,
            allowed_values=("auto", "manual", "boost"),
        ),
    )
    assert sensor.native_value is None
    assert sensor.coordinator.unknown_enum_value_count(path) == 1


# -------------------------------------------------------------------- writes


def test_setpoint_in_another_temperature_unit_stays_consistent() -> None:
    """The number entity takes its unit from the live reading, bounds and value
    use the same unit, so °F or K is shown and written consistently."""
    from custom_components.bosch_buderus_heating.writes import assess_control

    for unit, value, low, high in (("F", 68.0, 41, 86), ("K", 293.0, 278, 303)):
        changed = scalar(
            SETPOINT, value, unit=unit, minimum=low, maximum=high, writable=True
        )
        assert number_policy_for_resource(changed) is not None
        assert assess_control(changed).unit == unit


@pytest.mark.parametrize(
    "changed",
    [
        scalar(SETPOINT, "20", kind="stringValue", unit=None, writable=True),
        scalar(SETPOINT, 20.0, unit="C", writable=True),  # bounds disappeared
        scalar(SETPOINT, 20.0, unit="bar", minimum=5, maximum=30, writable=True),
        scalar(SETPOINT, 20.0, unit="C", minimum=30, maximum=5, writable=True),
    ],
    ids=["text", "no-bounds", "other-quantity", "inverted-bounds"],
)
def test_changed_setpoint_contract_offers_no_write(changed: Resource) -> None:
    known = scalar(SETPOINT, 20.0, minimum=5, maximum=30, writable=True)
    assert number_policy_for_resource(known) is not None
    assert number_policy_for_resource(changed) is None


async def test_text_in_a_unitless_measurement_is_not_shown(
    hass: HomeAssistant,
) -> None:
    """A floatValue without a unit still expects a number (state class)."""
    path = "/heatSources/actualSupplyTemperature"
    sensor = _sensor(hass, scalar(path, 3.0, unit=None))
    assert sensor.state_class is not None
    for value in ("7.5", "on"):
        report(sensor, scalar(path, value, kind="stringValue", unit=None))
        assert sensor.native_value is None
    assert sensor.coordinator.diagnostics_summary()["changed_contracts_detected"] == 1
    report(sensor, scalar(path, 4.0, unit=None))
    assert sensor.native_value == 4.0


async def test_type_change_is_logged_once_and_counted(
    hass: HomeAssistant, caplog
) -> None:
    sensor = _sensor(hass, scalar(OUTDOOR, 7.0))
    for value in ("7.5", "8.0"):
        report(sensor, scalar(OUTDOOR, value, kind="stringValue", unit="C"))
        assert sensor.native_value is None
    assert caplog.text.count("changed its value type") == 1
    assert "7.5" not in caplog.text
    summary = sensor.coordinator.diagnostics_summary()
    assert summary["changed_contracts_detected"] == 1
    report(sensor, scalar(OUTDOOR, 8.0))  # back to the known type
    assert sensor.native_value == 8.0


# --------------------------------------------------------------------- local


@pytest.mark.parametrize(
    "changes",
    [
        {"unitOfMeasure": "F"},
        {"unitOfMeasure": "K"},
    ],
    ids=["fahrenheit", "kelvin"],
)
def test_changed_local_reading_falls_back_to_the_cloud(changes) -> None:
    """A local reading in another unit or type never replaces the cloud value."""
    from custom_components.bosch_buderus_heating.local_selection import (
        LocalSelection,
    )
    from tests.test_local_selection import RETURN, TARGET, FakeRuntime, cloud, local

    snapshot = cloud(RETURN, 30.0)
    value = changes.pop("value", 31.5)
    selection = LocalSelection(FakeRuntime([local(RETURN, value, **changes)]), TARGET)
    shown = selection.select(RETURN, snapshot)
    assert shown is not None and shown.resource.value == 30.0


def test_local_reading_of_another_type_is_rejected_when_read() -> None:
    from custom_components.bosch_buderus_heating.local_api.errors import (
        LocalResourceError,
    )
    from tests.test_local_selection import RETURN, local

    with pytest.raises(LocalResourceError, match="invalid_schema"):
        local(RETURN, "31.5", type="stringValue")
