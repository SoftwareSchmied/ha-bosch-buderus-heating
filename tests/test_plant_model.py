"""Contracts for resource grouping without inventing PointT devices."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from custom_components.bosch_buderus_heating.plant_model import (
    GroupKey,
    PlantKey,
    PlantModel,
)
from custom_components.bosch_buderus_heating.pointt import Gateway


@pytest.mark.parametrize(
    ("path", "kind", "identifier"),
    [
        ("/heatingCircuits/hc1/operationMode", "heating_circuit", "hc1"),
        ("/heatingCircuits/hc3/roomtemperature", "heating_circuit", "hc3"),
        ("/heatingCircuits/hc12/name", "heating_circuit", "hc12"),
        ("/dhwCircuits/dhw2/actualTemp", "hot_water_circuit", "dhw2"),
        ("/heatSources/hs2/workingTime", "heat_source", "hs2"),
        ("/solarCircuits/sc1/solarYield", "solar_circuit", "sc1"),
        ("/ventilation/zone3/sensors/supplyTemp", "ventilation_zone", "zone3"),
        ("/zones/zn0/temperatureActual", "rrc_zone", "zn0"),
        ("/zones/zone2/averageCurrentTemperature", "room_zone", "zone2"),
        ("/swCircuits/soft2/state", "water_softener", "soft2"),
        ("/devices/device2/productName", "room_device", "device2"),
    ],
)
def test_groups_never_supply_device_evidence(
    path: str, kind: str, identifier: str
) -> None:
    model = PlantModel.from_pointt_gateway("owner-one", Gateway("gateway-one"))

    binding = model.bind_resource(path)

    assert binding.path == path
    assert binding.group_key == GroupKey(model.key, kind, identifier)
    assert binding.group is not None
    assert (binding.group.kind, binding.group.logical_id) == (kind, identifier)
    assert binding.device.identifier == "gateway-one"
    assert binding.device.evidence == "pointt_gateway"
    assert model.devices == (binding.device,)


@pytest.mark.parametrize(
    "path",
    [
        "/heatingCircuits",
        "/dhwCircuits/waterTotalConsumption",
        "/heatSources/numberOfStarts",
        "/heatSources/compressor/status",
        "/heatSources/emon/totalConsumption",
        "/gateway/serialId",
        "/system/info",
        "/devices/list",
        "/futureGroup/new1/value",
    ],
)
def test_general_and_unknown_resources_do_not_become_devices_or_groups(
    path: str,
) -> None:
    model = PlantModel.from_pointt_gateway("owner-one", Gateway("gateway-one"))

    binding = model.bind_resource(path)

    assert binding.group is None
    assert binding.group_key is None
    assert binding.device.identifier == "gateway-one"
    assert model.groups_for_paths([path]) == ()


def test_inventory_is_deterministic_and_does_not_fill_gaps() -> None:
    model = PlantModel.from_pointt_gateway("owner-one", Gateway("gateway-one"))
    paths = [
        "/heatingCircuits/hc3/operationMode",
        "/heatingCircuits/hc1/roomtemperature",
        "/solarCircuits/sc2/solarYield",
        "/heatingCircuits/hc1/operationMode",
        "/heatingCircuits/hc1/operationMode",
        "/heatSources/numberOfStarts",
    ]

    groups = model.groups_for_paths(iter(paths))

    assert groups == model.groups_for_paths(reversed(paths))
    assert [(group.key.kind, group.key.reported_id) for group in groups] == [
        ("heating_circuit", "hc1"),
        ("heating_circuit", "hc3"),
        ("solar_circuit", "sc2"),
    ]
    assert groups[0].paths == (
        "/heatingCircuits/hc1/operationMode",
        "/heatingCircuits/hc1/roomtemperature",
    )
    assert len(model.devices) == 1


def test_later_groups_and_temporary_absence_preserve_existing_identity() -> None:
    first = PlantModel.from_pointt_gateway(
        "owner-one", Gateway("gateway-one", model="old label")
    )
    reloaded = PlantModel.from_pointt_gateway(
        "owner-one", Gateway("gateway-one", model="new label", firmware_version="new")
    )
    initial_paths = ["/heatingCircuits/hc1/operationMode"]
    original = first.groups_for_paths(initial_paths)

    assert first.groups_for_paths([]) == ()
    assert first.groups_for_paths(initial_paths) == original
    expanded = reloaded.groups_for_paths(
        [
            *initial_paths,
            "/heatingCircuits/hc7/operationMode",
            "/solarCircuits/sc1/name",
        ]
    )

    assert expanded[0] == original[0]
    assert reloaded.devices == first.devices
    assert first.key == reloaded.key
    assert len(expanded) == 3


def test_same_group_numbers_are_scoped_to_owner_and_plant() -> None:
    models = [
        PlantModel.from_pointt_gateway("owner-one", Gateway("gateway-one")),
        PlantModel.from_pointt_gateway("owner-one", Gateway("gateway-two")),
        PlantModel.from_pointt_gateway("owner-two", Gateway("gateway-one")),
    ]
    keys = {
        model.bind_resource("/heatingCircuits/hc1/operationMode").group_key
        for model in models
    }

    assert len(keys) == 3
    assert models[0].key == PlantKey("owner-one", "gateway-one")


def test_reported_identifiers_are_not_renumbered_or_coerced() -> None:
    model = PlantModel.from_pointt_gateway("owner-one", Gateway("000123"))
    groups = model.groups_for_paths(
        [
            "/heatingCircuits/hc01/operationMode",
            "/heatingCircuits/hc1/operationMode",
            "/heatingCircuits/hc9007199254740993/operationMode",
            "/solarCircuits/sc01/solarYield",
        ]
    )

    assert {group.key.reported_id for group in groups} == {
        "hc01",
        "hc1",
        "hc9007199254740993",
        "sc01",
    }
    assert model.gateway_device.identifier == "000123"


def test_model_import_and_grouping_do_not_require_home_assistant() -> None:
    package = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"
    script = """
import importlib.abc
import pathlib
import sys
import types

class NoHomeAssistant(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "homeassistant" or fullname.startswith("homeassistant."):
            raise AssertionError("Plant model imported Home Assistant")

sys.meta_path.insert(0, NoHomeAssistant())
root = pathlib.Path(sys.argv[1])
for name, directory in [
    ("custom_components", root.parent),
    ("custom_components.bosch_buderus_heating", root),
]:
    package = types.ModuleType(name)
    package.__path__ = [str(directory)]
    sys.modules[name] = package

from custom_components.bosch_buderus_heating.plant_model import PlantModel
from custom_components.bosch_buderus_heating.pointt import Gateway
model = PlantModel.from_pointt_gateway("owner", Gateway("gateway"))
assert len(model.groups_for_paths([
    "/heatingCircuits/hc1/operationMode", "/solarCircuits/sc1/solarYield"
])) == 2
assert len(model.devices) == 1
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(package)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_the_system_key_follows_the_target_not_the_gateway() -> None:
    """P50 E-A: an exchanged gateway keeps the system, only the device changes."""
    old = PlantModel.from_pointt_gateway(
        "owner", Gateway("gateway-old"), target_id="t1"
    )
    new = PlantModel.from_pointt_gateway(
        "owner", Gateway("gateway-new"), target_id="t1"
    )
    assert old.key == new.key == PlantKey("owner", "t1")
    assert old.gateway_device != new.gateway_device
    assert (
        old.bind_resource("/heatingCircuits/hc1").group_key
        == new.bind_resource("/heatingCircuits/hc1").group_key
    )
    # Without a stored target (older entry format) the gateway stands in.
    assert PlantModel.from_pointt_gateway("owner", Gateway("g")).key.target_id == "g"
