"""A4: the cloud recognises a flow below its measuring range like the local
access (#73): unknown instead of the declared 65535, with an attribute."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.discovery_inventory import (
    resource_from_data,
    resource_to_data,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from tests.test_read_contracts import sensor_for

FLOW = "/heatSources/hs1/pumpVolumeFlow"
BELOW = {"measuring_range": "below"}


def flow(value, state=None, *, path=FLOW, unit="l/min"):
    payload = {
        "id": path,
        "type": "floatValue",
        "writeable": 0,
        "recordable": 0,
        "value": value,
        "unitOfMeasure": unit,
    }
    if state is not None:
        payload["state"] = state
    return parse_resource(payload)


@pytest.mark.parametrize("key", ["lowflow", "LOW_FLOW", "lowFlow", "low-flow"])
def test_the_low_flow_state_is_recognised(key) -> None:
    resource = flow(65535, [{key: 65535}])
    metadata = resource.metadata
    assert metadata.below_range_values == (65535,)
    assert 65535 in metadata.numeric_error_values
    assert metadata.numeric_error_states_valid


def test_other_states_are_no_low_flow() -> None:
    resource = flow(5, [{"open": -3276.8}, {"flowing": 1}, {"lowflow": "x"}])
    assert resource.metadata.below_range_values == ()
    assert resource.metadata.numeric_error_values == (-3276.8,)
    assert resource.metadata.numeric_error_states_valid
    assert (
        flow(5, [{"overflow": 9}, {"slowflower": 3}]).metadata.below_range_values == ()
    )
    assert flow(5, "lowflow").metadata.below_range_values == ()
    assert flow(5, ["lowflow"]).metadata.below_range_values == ()


def test_the_stored_inventory_keeps_the_low_flow_value() -> None:
    resource = flow(65535, [{"lowflow": 65535}])
    data = resource_to_data(resource)
    assert data["metadata"]["below_range_values"] == [65535]
    restored = resource_from_data(data)
    assert restored.metadata.below_range_values == (65535,)
    # Stored before alpha.3: without the field.
    del data["metadata"]["below_range_values"]
    assert resource_from_data(data).metadata.below_range_values == ()
    data["metadata"]["below_range_values"] = [True]
    with pytest.raises(ValueError):
        resource_from_data(data)


async def test_a_cloud_flow_below_the_range_is_unknown_and_marked(
    hass: HomeAssistant,
) -> None:
    sensor = sensor_for(hass, flow(65535, [{"lowflow": 65535}]))
    assert sensor.available
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == BELOW
    assert sensor.translation_key == "volume_flow"
    # A measured flow: its value, no attribute.
    measured = sensor_for(hass, flow(12.5, [{"lowflow": 65535}]))
    assert measured.native_value == 12.5
    assert not measured.extra_state_attributes
    # Without the declared state the value is shown as before.
    plain = sensor_for(hass, flow(65535))
    assert plain.native_value == 65535
    assert not plain.extra_state_attributes


async def test_other_readings_keep_their_translation(hass: HomeAssistant) -> None:
    sensor = sensor_for(
        hass,
        flow(
            30.0,
            [{"lowflow": 65535}],
            path="/heatSources/actualSupplyTemperature",
            unit="C",
        ),
    )
    assert sensor.translation_key != "volume_flow"
    assert sensor.native_value == 30.0
