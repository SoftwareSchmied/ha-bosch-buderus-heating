"""#73 flow below the measuring range, #76 energy manager placeholders."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.local_only import (
    below_measuring_range,
    energy_manager_not_connected,
    energy_manager_setpoint,
)
from custom_components.bosch_buderus_heating.local_only_sensor import (
    track_local_only_sensors,
)
from custom_components.bosch_buderus_heating.local_plant_view import local_reading
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata
from tests.test_c7_signals import signal
from tests.test_local_only import (
    FLOW,
    HOT_GAS,
    by_path,
    coordinator_with,
    number,
    track,
)
from tests.test_managed_local_plant import plant, reading

INTEGRATION = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"
CIRCUIT_FLOW = "/signals/SRC.HC1.FlowCtrl.PumpVolumeFlow"
EM_LOWER = "/signals/SRC.CUHP.EM.UsedLowerFlowTempSetp"
EM_UPPER = "/signals/SRC.CUHP.EM.UsedUpperFlowTempSetp"
FLOW_SETPOINT = "/signals/SC.HC1.FlowTempSetp"
EM_STATUS = "/heatSources/emStatus"
BELOW = {"measuring_range": "below"}


# ------------------------------------------------------------------- #73


def flow(value, marker=65535):
    return number(
        FLOW, value, unit="l/h", kind="integerValue", state=[{"lowflow": marker}]
    )


async def test_a_flow_below_the_measuring_range_is_unknown_and_marked(
    hass: HomeAssistant,
) -> None:
    coordinator = coordinator_with(hass, [flow(65535)])
    entity = by_path(track(coordinator))[FLOW]
    assert entity.available
    assert entity.native_value is None
    assert entity.extra_state_attributes == BELOW
    assert entity.native_unit_of_measurement == "L/h"
    assert entity.translation_key == "volume_flow"
    await coordinator.async_shutdown()


async def test_a_measured_flow_has_no_marker(hass: HomeAssistant) -> None:
    coordinator = coordinator_with(hass, [flow(1180)])
    entity = by_path(track(coordinator))[FLOW]
    assert entity.native_value == 1180
    assert entity.extra_state_attributes is None
    await coordinator.async_shutdown()


async def test_a_zero_low_flow_marker_is_below_the_range(hass: HomeAssistant) -> None:
    """A device declaring 0 as its low-flow state: the pump stands still."""
    coordinator = coordinator_with(hass, [flow(0, marker=0)])
    entity = by_path(track(coordinator))[FLOW]
    assert entity.native_value is None
    assert entity.extra_state_attributes == BELOW
    await coordinator.async_shutdown()


async def test_the_circuit_flow_signal_declares_low_flow_too(
    hass: HomeAssistant,
) -> None:
    coordinator = coordinator_with(
        hass,
        [
            signal(
                "SRC.HC1.FlowCtrl.PumpVolumeFlow",
                65535,
                unit="l/h",
                state={"LOW_FLOW": 65535},
            )
        ],
    )
    entity = by_path(track(coordinator))[CIRCUIT_FLOW]
    assert entity.native_value is None
    assert entity.extra_state_attributes == BELOW
    assert entity.translation_key == "volume_flow"
    await coordinator.async_shutdown()


async def test_other_markers_and_missing_readings_are_not_below_the_range(
    hass: HomeAssistant,
) -> None:
    open_sensor = number(HOT_GAS, -32768, state=[{"open": -32768}])
    coordinator = coordinator_with(hass, [open_sensor, flow(65535)])
    entities = by_path(track(coordinator))
    assert entities[HOT_GAS].native_value is None
    assert entities[HOT_GAS].extra_state_attributes is None
    # Without a current reading the entity is unavailable, without the marker.
    coordinator.local_selection._runtime.observations.clear()
    coordinator.local_selection._local_only_cache = None
    assert not entities[FLOW].available
    assert entities[FLOW].extra_state_attributes is None
    await coordinator.async_shutdown()


async def test_a_state_signal_named_low_flow_gets_no_attribute(
    hass: HomeAssistant,
) -> None:
    observation = signal("SRC.Test.State", 1, state={"LOW_FLOW": 1, "OK": 2})
    assert below_measuring_range(local_reading(observation))
    coordinator = coordinator_with(hass, [observation])
    entity = by_path(track(coordinator))["/signals/SRC.Test.State"]
    assert entity.available
    assert entity.extra_state_attributes is None
    await coordinator.async_shutdown()


@pytest.mark.parametrize(
    ("states", "below"),
    [
        ([{"lowflow": 65535}], True),
        ({"LOW_FLOW": 65535}, True),
        ({"PUMP_LOW_FLOW_X": 65535}, True),
        ([{"open": 65535}], False),
        ({"FLOWLOW": 65535}, False),
    ],
)
def test_low_flow_state_names(states, below) -> None:
    observation = number(FLOW, 65535, unit="l/h", kind="integerValue", state=states)
    assert below_measuring_range(local_reading(observation)) is below


def test_the_attribute_is_translated() -> None:
    expected = {
        "strings.json": ("Measuring range", "Below measuring range"),
        "translations/en.json": ("Measuring range", "Below measuring range"),
        "translations/de.json": ("Messbereich", "Unter Messbereich"),
    }
    for name, (label, below) in expected.items():
        texts = json.loads((INTEGRATION / name).read_text(encoding="utf-8"))
        attribute = texts["entity"]["sensor"]["volume_flow"]["state_attributes"][
            "measuring_range"
        ]
        assert attribute == {"name": label, "state": {"below": below}}, name


# ------------------------------------------------------------------- #76


def em_status(value: str) -> Resource:
    return Resource(
        EM_STATUS,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="stringValue",
            allowed_values=("notConnected", "inactive", "activeCh", "activeDhw"),
        ),
    )


def em_readings():
    return [
        signal("SRC.CUHP.EM.UsedLowerFlowTempSetp", 0, unit="C"),
        signal("SRC.CUHP.EM.UsedUpperFlowTempSetp", 0, unit="C"),
        signal("SC.HC1.FlowTempSetp", 23, unit="C"),
    ]


async def test_energy_manager_setpoints_are_unknown_while_not_connected(
    hass: HomeAssistant,
) -> None:
    coordinator = coordinator_with(hass, em_readings(), em_status("notConnected"))
    entities = by_path(track(coordinator))
    for path in (EM_LOWER, EM_UPPER):
        assert entities[path].available
        assert entities[path].native_value is None
        assert entities[path].native_unit_of_measurement == "°C"
    # Other setpoints are unaffected.
    assert entities[FLOW_SETPOINT].native_value == 23
    await coordinator.async_shutdown()


@pytest.mark.parametrize("status", ["activeCh", "inactive", None])
async def test_energy_manager_setpoints_show_otherwise(
    hass: HomeAssistant, status
) -> None:
    resources = () if status is None else (em_status(status),)
    coordinator = coordinator_with(hass, em_readings(), *resources)
    entities = by_path(track(coordinator))
    assert entities[EM_LOWER].native_value == 0
    assert entities[EM_UPPER].native_value == 0
    await coordinator.async_shutdown()


async def test_the_local_plant_view_uses_its_local_status(hass: HomeAssistant) -> None:
    coordinator, runtime = await plant(hass)
    runtime.publish(
        [
            reading(
                EM_STATUS,
                "stringValue",
                "notConnected",
                allowedValues=["notConnected", "inactive", "activeCh", "activeDhw"],
            ),
            reading(EM_LOWER, "integerValue", 0, unitOfMeasure="C"),
        ]
    )
    assert EM_STATUS in coordinator.resources
    entities: list = []
    track_local_only_sensors(
        SimpleNamespace(async_on_unload=Mock()), coordinator, entities.extend
    )
    (entity,) = (item for item in entities if item.resource_path == EM_LOWER)
    entity.hass = hass
    assert entity.available and entity.native_value is None
    await coordinator.async_release_retention()


@pytest.mark.parametrize(
    ("path", "setpoint"),
    [
        (EM_LOWER, True),
        (EM_UPPER, True),
        ("/signals/EM.FlowTempSetp", True),
        (FLOW_SETPOINT, False),
        ("/signals/SRC.CUHP.EM.Status", False),
        ("/signals/SRC.CUHP.EMX.UsedLowerFlowTempSetp", False),
        ("/heatSources/hs1/EM.Setp", False),
    ],
)
def test_energy_manager_setpoint_paths(path, setpoint) -> None:
    assert energy_manager_setpoint(path) is setpoint


@pytest.mark.parametrize(
    ("status", "absent"),
    [
        ("notConnected", True),
        ("not_connected", True),
        ("NOT_CONNECTED", True),
        ("connected", False),
        ("activeCh", False),
        (None, False),
        (0, False),
    ],
)
def test_energy_manager_status(status, absent) -> None:
    assert energy_manager_not_connected(status) is absent
