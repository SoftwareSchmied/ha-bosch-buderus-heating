"""P49b: the entities of a plant from its local readings alone."""

from __future__ import annotations

import random
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.gateway_identity import GatewayNamespace
from custom_components.bosch_buderus_heating.local_only_sensor import (
    local_only_unique_id,
)
from custom_components.bosch_buderus_heating.local_plant_view import (
    MAX_LISTED,
    EntityShape,
    build_local_plant_view,
    cloud_form,
    description_shapes,
    diagnostic_suffix,
    from_template,
    registry_shapes,
    shadow_comparison,
    template_compatible,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from tests.test_local_api import read

GATEWAY = "000123"


def local(path: str, kind: str, value=None, **extra):
    raw = {"id": path, "type": kind, "writeable": 0}
    if value is not None:
        raw["value"] = value
    raw.update(extra)
    return read(raw)


def cloud(path: str, kind: str, value=None, **extra):
    raw = {"id": path, "type": kind, "writeable": True}
    if value is not None:
        raw["value"] = value
    raw.update(extra)
    return parse_resource(raw)


ENERGY = [{"outputProduced": 30}, {"eheater": 2}, {"compressor": 14}]
STARTS = [{"ch": 139}, {"dhw": 30}, {"total": 30}]
HOURS = [{"ch": 139}, {"dhw": 30}, {"total": 30}]
LEVELS = ["off", "low", "high", "eco"]
DEMAND = ["", "ch", "dhw", "frost"]

# (path, type, value, local fields, cloud fields) for each value form.
FORMS = {
    "float_legacy": (
        "/heatSources/returnTemperature",
        "floatValue",
        31.5,
        {"unitOfMeasure": "C", "state": [{"open": -32768}, {"short": 32767}]},
        {"unitOfMeasure": "C", "state": [{"open": -32768}, {"short": 32767}]},
    ),
    "float": (
        "/heatingCircuits/hc1/currentRoomSetpoint",
        "floatValue",
        21.5,
        {"unitOfMeasure": "C"},
        {"unitOfMeasure": "C"},
    ),
    "integer": (
        "/heatSources/actualModulation",
        "integerValue",
        45,
        {"unitOfMeasure": "%"},
        {"unitOfMeasure": "%"},
    ),
    "enum": (
        "/dhwCircuits/dhw1/currentTemperatureLevel",
        "stringValue",
        "high",
        {"allowedValues": LEVELS},
        {"allowedValues": LEVELS},
    ),
    "on_off": (
        "/system/powerLimitation/active",
        "stringValue",
        "off",
        {"allowedValues": ["on", "off"]},
        {"allowedValues": ["on", "off"]},
    ),
    "energy": (
        "/heatSources/emon/totalConsumption",
        "emonValue",
        None,
        {"values": ENERGY, "unit": "kWh"},
        {"values": ENERGY},
    ),
    "start_counter": (
        "/heatSources/numberOfStarts",
        "floatValue",
        139,
        {},
        {},
    ),
    "start_counter_parts": (
        "/heatSources/hs1/numberOfStarts",
        "emonValue",
        None,
        {"values": STARTS},
        {"values": STARTS},
    ),
    "working_time_parts": (
        "/heatSources/hs1/workingTime",
        "emonValue",
        None,
        {"values": HOURS},
        {"values": HOURS},
    ),
    "array": (
        "/heatSources/actualHeatDemand",
        "arrayData",
        None,
        {"values": ["ch"], "allowedValues": DEMAND},
        {"values": ["ch"], "allowedValues": DEMAND},
    ),
}


def integer_reading(path: str, value: int, **extra):
    """An integer reading of a path the official examples document as float.

    Local documents vary by device; the conversion must follow the reported
    type, so the document is changed after schema validation.
    """
    observation = local(path, "floatValue", float(value), **extra)
    document = {**observation.document, "type": "integerValue", "value": value}
    return replace(
        observation,
        type="integerValue",
        data=replace(observation.data, value=value),
        document=MappingProxyType(document),
    )


def form(name):
    path, kind, value, local_fields, cloud_fields = FORMS[name]
    observation = (
        integer_reading(path, value, **local_fields)
        if kind == "integerValue"
        else local(path, kind, value, **local_fields)
    )
    return path, observation, cloud(path, kind, value, **cloud_fields)


# ------------------------------------------------------------ cloud form


@pytest.mark.parametrize("name", sorted(FORMS))
def test_paired_readings_give_the_cloud_entities(name) -> None:
    path, observation, resource = form(name)
    view = build_local_plant_view([observation])
    expected = description_shapes({path: resource})
    assert expected, "the cloud form yields entities"
    assert view.entity_shapes() == expected
    assert view.resources[path].metadata.writable is False
    assert not view.local_sensors


def test_unique_ids_keep_the_cloud_format() -> None:
    observations = [form(name)[1] for name in ("float_legacy", "float", "energy")]
    keys = set(build_local_plant_view(observations).entity_shapes())
    assert ("sensor", "gateway:return_temperature") in keys
    assert ("sensor", "heatingCircuits:hc1:currentRoomSetpoint") in keys
    assert ("sensor", "gateway:total_heat_produced") in keys
    assert ("sensor", "heatSources:emon:totalConsumption:compressor") in keys


def test_a_writable_local_reading_stays_read_only() -> None:
    path = "/heatingCircuits/hc1/maxFlowTemp"
    observation = read(
        {
            "id": path,
            "type": "floatValue",
            "writeable": 1,
            "value": 55.0,
            "unitOfMeasure": "C",
            "minValue": 25,
            "maxValue": 75,
        }
    )
    resource, reason = cloud_form(observation)
    assert reason is None and resource is not None
    assert resource.metadata.writable is False
    assert (resource.metadata.minimum, resource.metadata.maximum) == (25.0, 75.0)
    shapes = build_local_plant_view([observation]).entity_shapes()
    assert set(shapes) == set(
        description_shapes({path: cloud(path, "floatValue", 55.0, unitOfMeasure="C")})
    )


def test_energy_in_another_unit_keeps_its_unit() -> None:
    path = "/heatSources/emon/chConsumption"
    observation = local(path, "emonValue", values=ENERGY, unit="Wh")
    resource, _ = cloud_form(observation)
    assert resource is not None and resource.metadata.unit == "Wh"
    kilowatt_hours, _ = cloud_form(local(path, "emonValue", values=ENERGY, unit="kWh"))
    assert kilowatt_hours is not None and kilowatt_hours.metadata.unit is None
    # The entities are scaled to kWh, as from the cloud.
    shapes = build_local_plant_view([observation]).entity_shapes()
    assert (
        shapes
        == build_local_plant_view(
            [local(path, "emonValue", values=ENERGY, unit="kWh")]
        ).entity_shapes()
    )
    assert {shape.unit for shape in shapes.values()} == {"kWh"}


def test_a_single_state_marker_is_listed_like_the_cloud() -> None:
    _, observation, _ = form("float_legacy")
    document = {**observation.document, "state": MappingProxyType({"open": -32768})}
    resource, reason = cloud_form(
        replace(observation, document=MappingProxyType(document))
    )
    assert reason is None and resource is not None
    assert resource.metadata.numeric_error_values == (-32768,)


def test_a_path_the_cloud_offers_keeps_the_cloud_id() -> None:
    path = "/gateway/dataProcessing/status"
    states = ["in_progress", "completed"]
    observation = local(path, "stringValue", "completed", allowedValues=states)
    alone = build_local_plant_view([observation])
    assert [item.unique_suffix for item in alone.local_sensors] == [
        "local_only:gateway:dataProcessing:status"
    ]
    offered = build_local_plant_view([observation], cloud_paths={path})
    expected = description_shapes(
        {path: cloud(path, "stringValue", "completed", allowedValues=states)}
    )
    assert ("sensor", "gateway:dataProcessing:status") in expected
    assert offered.entity_shapes() == expected and not offered.local_sensors
    assert offered.resources[path].metadata.writable is False


def test_a_whole_float_takes_the_integer_form_of_its_template() -> None:
    path = "/heatSources/actualModulation"
    template = cloud(path, "integerValue", 40, unitOfMeasure="%")
    value = from_template(replace(template, value=45.0), template).value
    assert value == 45 and type(value) is int


def test_a_compatible_fresh_template_supplies_the_metadata() -> None:
    path, observation, _ = form("float")
    template = cloud(path, "floatValue", 20, unitOfMeasure="C", minValue=5, maxValue=30)
    view = build_local_plant_view([observation], templates={path: template})
    resource = view.resources[path]
    assert view.templates_used == 1
    assert resource.value == 21.5 and resource.metadata.maximum == 30.0
    assert resource.metadata.writable is False
    other = cloud(path, "floatValue", 20, unitOfMeasure="K")
    view = build_local_plant_view([observation], templates={path: other})
    assert view.templates_used == 0 and view.resources[path].metadata.unit == "C"


def test_template_rules() -> None:
    path, observation, _ = form("enum")
    parsed, _ = cloud_form(observation)
    assert parsed is not None
    assert template_compatible(parsed, cloud(path, "stringValue", "eco"))
    assert template_compatible(
        parsed, cloud(path, "stringValue", "eco", allowedValues=list(reversed(LEVELS)))
    )
    assert not template_compatible(
        parsed, cloud(path, "stringValue", "eco", allowedValues=["off", "on"])
    )
    assert not template_compatible(parsed, cloud(path, "floatValue", 1.0))
    energy, observation, _ = form("energy")
    parsed, _ = cloud_form(observation)
    assert parsed is not None
    assert template_compatible(parsed, cloud(energy, "emonValue", values=ENERGY))
    assert template_compatible(
        parsed, cloud(energy, "emonValue", values=ENERGY, unitOfMeasure="kWh")
    )
    assert not template_compatible(
        parsed, cloud(energy, "emonValue", values=ENERGY[:2])
    )


@pytest.mark.parametrize(
    ("path", "kind", "value", "unit"),
    [
        ("/heatSources/compressor/powerElecActual", "floatValue", 810.0, "W"),
        (
            "/heatSources/hs1/refrigerant/compressorActualSpeed",
            "integerValue",
            45,
            "%",
        ),
    ],
)
def test_a_paired_reading_without_a_cloud_entity_follows_p45(
    path, kind, value, unit
) -> None:
    # The cloud knows no entity for these (create-all: only a disabled
    # generic one); the reviewed local-only one remains in the plant view.
    shapes = description_shapes({path: cloud(path, kind, value, unitOfMeasure=unit)})
    assert all(not shape.enabled for shape in shapes.values())
    view = build_local_plant_view(
        [local(path, kind, value, unitOfMeasure=unit)],
        templates={path: cloud(path, kind, value, unitOfMeasure=unit)},
    )
    assert not view.resources and view.templates_used == 0
    (sensor,) = view.local_sensors
    assert sensor.reviewed and sensor.enabled == (kind == "floatValue")
    assert f"{GATEWAY}:{sensor.unique_suffix}" == local_only_unique_id(GATEWAY, path)


# ------------------------------------------------------ local-only sensors

SETPOINT = "/heatSources/currentSupplySetpoint"
ROOM = "/heatingCircuits/hc1/roomtemperature"
MAINTENANCE = "/system/powerConstraints/maintenanceMode/status"
SIGNAL = "/signals/SRC.CUHP.HP1.ReturnTemp"
INPUT = "/heatSources/externalInputs/input1/state"


def local_only_readings():
    return [
        local(SETPOINT, "floatValue", 34.5, unitOfMeasure="C"),
        local(ROOM, "floatValue", 21.25, unitOfMeasure="C"),
        local(
            MAINTENANCE, "stringValue", "inactive", allowedValues=["active", "inactive"]
        ),
        local(SIGNAL, "integerValue", 28, unitOfMeasure="C"),
        local(INPUT, "stringValue", "off", allowedValues=["on", "off"]),
        local("/signals/GWEEBUS.Status", "stringValue", "connected"),
        local(
            "/heatSources/emon/poolConsumption", "emonValue", values=ENERGY, unit="kWh"
        ),
        local("/heatSources/hs1/oduFanSpeed", "integerValue", 40, unitOfMeasure="%"),
    ]


def test_every_other_readable_reading_becomes_a_local_sensor() -> None:
    view = build_local_plant_view(local_only_readings())
    sensors = {item.path: item for item in view.local_sensors}
    assert set(sensors) == {
        SETPOINT,
        ROOM,
        MAINTENANCE,
        SIGNAL,
        INPUT,
        "/heatSources/hs1/oduFanSpeed",
    }
    for path, item in sensors.items():
        assert f"{GATEWAY}:{item.unique_suffix}" == local_only_unique_id(GATEWAY, path)
    # Everyday values enabled, technical values and signals disabled.
    assert {path for path, item in sensors.items() if item.enabled} == {SETPOINT, ROOM}
    assert sensors[SETPOINT].reviewed and not sensors[ROOM].reviewed
    shapes = view.entity_shapes()
    setpoint = shapes[("sensor", sensors[SETPOINT].unique_suffix)]
    assert (setpoint.unit, setpoint.device_class, setpoint.state_class) == (
        "°C",
        "temperature",
        "measurement",
    )
    room = shapes[("sensor", sensors[ROOM].unique_suffix)]
    # Generic: unit and class of the reading, no statistics.
    assert (room.unit, room.device_class, room.state_class) == (
        "°C",
        "temperature",
        None,
    )
    assert shapes[("sensor", sensors[MAINTENANCE].unique_suffix)].options == (
        "active",
        "inactive",
    )
    assert sensors[INPUT].options == ("on", "off") and not sensors[INPUT].enabled
    assert view.skipped == {"multipart": 1, "text": 1}


PRIVATE = [
    ("/gateway/eth/ip/ipv4", "stringValue", "192.0.2.10", {}),
    ("/gateway/wifi/ip/ipv4", "stringValue", "192.0.2.11", {}),
    (
        "/gateway/wifi/hotspotRemainingTime",
        "floatValue",
        3.0,
        {"unitOfMeasure": "mins"},
    ),
    ("/gateway/tzInfo/timeZone", "stringValue", "Europe/Berlin", {}),
    ("/devices/device1/sgtin", "stringValue", "30340000000000000000", {}),
    ("/devices/device1/name", "stringValue", "Living room", {}),
    ("/devices/device1/zoneId", "integerValue", 3, {}),
    ("/devices/device1/rfTimeofConnectionLost", "stringValue", "2026-11-01", {}),
    ("/system/variableTariff/tariffId", "stringValue", "tariff-4711", {}),
    ("/signals/GWEEBUS.CEM.ID", "stringValue", "secret-identifier", {}),
    ("/signals/GWEEBUS.CEM.SKI", "stringValue", "secret-identifier", {}),
]


def test_identifiers_and_private_data_produce_nothing() -> None:
    observations = [
        local(path, kind, value, **extra) for path, kind, value, extra in PRIVATE
    ]
    view = build_local_plant_view(observations)
    assert not view.resources and not view.local_sensors
    assert not view.entity_shapes()
    # The time of a lost radio connection is no identifier, only free text.
    assert view.skipped == {"private": len(PRIVATE) - 1, "text": 1}


def test_a_name_with_allowed_values_stays_private() -> None:
    path = "/devices/device1/name"
    view = build_local_plant_view(
        [local(path, "stringValue", "Bath", allowedValues=["Bath", "Kitchen"])]
    )
    assert not view.local_sensors and view.skipped == {"private": 1}


@pytest.mark.parametrize("count", [64, 65])
def test_a_state_offers_at_most_64_allowed_values(count) -> None:
    levels = [f"level{number}" for number in range(count)]
    view = build_local_plant_view(
        [local("/signals/HP.MODE.X", "stringValue", "level1", allowedValues=levels)]
    )
    # C7: always an entity; the options stay bounded to the first 64.
    (sensor,) = view.local_sensors
    assert sensor.options == tuple(levels[:64])


def test_the_view_is_deterministic() -> None:
    observations = [form(name)[1] for name in sorted(FORMS)] + local_only_readings()
    first = build_local_plant_view(observations)
    shuffled = list(observations)
    random.Random(4).shuffle(shuffled)
    second = build_local_plant_view(shuffled)
    assert list(first.resources) == list(second.resources) == sorted(first.resources)
    assert first.local_sensors == second.local_sensors
    assert first.entity_shapes() == second.entity_shapes()


# ------------------------------------------------------- shadow comparison


def test_shadow_comparison_counts_and_lists() -> None:
    path, observation, resource = form("float")
    view = build_local_plant_view([observation, local_only_readings()[0]])
    cloud_shapes = description_shapes({path: resource})
    registry = {
        ("number", "heatingCircuits:hc1:manualRoomSetpoint"): EntityShape(),
        ("sensor", "devices:A1B2C3D4E5:roomtemperature"): EntityShape(),
    }
    report = shadow_comparison(
        view, cloud=cloud_shapes, registry=registry, cloud_paths={path, SETPOINT}
    )
    assert report["counts"] == {
        "local": 2,
        "cloud": 3,
        "per_source_entities": 0,
        "matching": 1,
        "only_local": 1,
        "needs_cloud": 2,
        "local_only_with_cloud_path": 1,
        "mismatches": 0,
    }
    assert report["matching"] == ["sensor:heatingCircuits:hc1:currentRoomSetpoint"]
    assert report["only_local"] == [
        "sensor:local_only:heatSources:currentSupplySetpoint"
    ]
    assert report["needs_cloud"] == [
        "number:heatingCircuits:hc1:manualRoomSetpoint",
        "sensor:devices:{device}:roomtemperature",
    ]
    assert report["needs_cloud_domains"] == {"number": 1, "sensor": 1}
    assert "A1B2C3D4E5" not in repr(report)
    assert report["truncated"] is False


def test_shadow_comparison_reports_shape_differences() -> None:
    path, observation, resource = form("enum")
    view = build_local_plant_view([observation])
    key = next(iter(description_shapes({path: resource})))
    other = EntityShape(unit="K", device_class="enum", options=("off", "on"))
    report = shadow_comparison(view, cloud={key: other}, registry={})
    (mismatch,) = report["mismatches"]
    assert mismatch["id"] == "sensor:dhwCircuits:dhw1:currentTemperatureLevel"
    assert mismatch["unit"] == ["none", "K"]
    assert mismatch["options"] == {"local": 4, "cloud": 2, "shared": 1}
    assert "device_class" not in mismatch


def test_a_converted_unit_in_the_registry_is_no_mismatch() -> None:
    view = build_local_plant_view(
        [local(SETPOINT, "floatValue", 34.5, unitOfMeasure="C")]
    )
    (key,) = view.entity_shapes()
    fahrenheit = {key: EntityShape("°F", "temperature", "measurement")}
    assert shadow_comparison(view, cloud={}, registry=fahrenheit)["mismatches"] == []
    power = {key: EntityShape("W", "power", "measurement")}
    (mismatch,) = shadow_comparison(view, cloud={}, registry=power)["mismatches"]
    assert mismatch["unit"] == ["°C", "W"]
    assert mismatch["device_class"] == ["temperature", "power"]
    # The cloud's own description is compared with its unit.
    (mismatch,) = shadow_comparison(view, cloud=fahrenheit, registry={})["mismatches"]
    assert mismatch["unit"] == ["°C", "°F"]


def test_lists_are_bounded() -> None:
    view = build_local_plant_view([])
    registry = {("sensor", f"heatSources:value{i}"): EntityShape() for i in range(400)}
    report = shadow_comparison(view, cloud={}, registry=registry)
    assert report["counts"]["needs_cloud"] == 400
    assert len(report["needs_cloud"]) == MAX_LISTED and report["truncated"]
    # O94: how many each list shows and how many entries were left out.
    assert report["listed_at_most"] == MAX_LISTED
    assert report["omitted_entries"] == 400 - MAX_LISTED
    small = shadow_comparison(view, cloud={}, registry=dict(list(registry.items())[:3]))
    assert small["omitted_entries"] == 0 and not small["truncated"]


def test_registry_shapes_take_one_gateway_without_per_source_entities() -> None:
    entries = [
        SimpleNamespace(
            domain="sensor",
            unique_id=f"{GATEWAY}:heatSources:returnTemperature",
            unit_of_measurement="°C",
            original_device_class="temperature",
            capabilities={"state_class": "measurement"},
        ),
        SimpleNamespace(
            domain="sensor",
            unique_id=f"{GATEWAY}:heatSources:returnTemperature:source_local",
            capabilities=None,
        ),
        SimpleNamespace(domain="sensor", unique_id="other:heatSources:x"),
    ]
    shapes, per_source = registry_shapes(entries, GATEWAY)
    assert per_source == 1
    assert shapes == {
        ("sensor", "heatSources:returnTemperature"): EntityShape(
            "°C", "temperature", "measurement", None
        )
    }


def test_diagnostic_suffixes_hide_unrecognized_identifiers() -> None:
    assert diagnostic_suffix("gateway:outdoor_temperature") == (
        "gateway:outdoor_temperature"
    )
    assert diagnostic_suffix("local_only:signals:SRC.CUHP.HP1.ReturnTemp") == (
        "local_only:signals:SRC.CUHP.HP1.ReturnTemp"
    )
    assert diagnostic_suffix("heatingCircuits:circuit-77:overallStatus") == (
        "heatingCircuits:{hc}:overallStatus"
    )
    assert diagnostic_suffix("devices:0042AF:battery") == "devices:{device}:battery"
    # Unexpected characters or lengths are never listed.
    assert diagnostic_suffix("local_only:signals:" + "x" * 65) == (
        "local_only:signals:other"
    )
    assert diagnostic_suffix("local_only:signals:Living room") == (
        "local_only:signals:other"
    )


# ------------------------------------------------- diagnostics of an entry

CURRENT = "/heatingCircuits/hc2/currentRoomSetpoint"
RETURN = "/heatSources/returnTemperature"


def _publish(owner, observations) -> None:
    runtime = owner.connections
    context = runtime._contexts["local"]
    runtime._local_states["local"] = SimpleNamespace(
        context=context.identity_context(GatewayNamespace.K40_LOCAL),
        current_observations=tuple(observations),
    )
    runtime._notify()


@pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
async def test_diagnostics_compare_the_local_entities_with_the_cloud(hass) -> None:
    from tests.test_local_backed import ReadingBackend, local_values
    from tests.test_managed_platforms import native
    from tests.test_managed_pointt import eventually, running

    hass.config.components.add(DOMAIN)
    backend = ReadingBackend()
    sensor_id = f"{GATEWAY}:heatingCircuits:hc2:currentRoomSetpoint"
    async with running(hass, backend, configure=local_values()) as (
        entry,
        _,
        owner,
        _,
    ):
        await owner.start_platforms()
        await eventually(lambda: native(owner, "sensor", sensor_id))
        _publish(
            owner,
            [
                local(CURRENT, "floatValue", 47.75, unitOfMeasure="C"),
                local(RETURN, "floatValue", 61.25, unitOfMeasure="C"),
                local(SETPOINT, "floatValue", 38.125, unitOfMeasure="C"),
                local("/gateway/eth/ip/ipv4", "stringValue", "192.0.2.10"),
                local("/system/variableTariff/tariffId", "stringValue", "tariff-4711"),
            ],
        )
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        registered = {
            item.entity_id
            for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        }
        states = set(hass.states.async_entity_ids())
        calls = len(backend.calls)
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        # Diagnostics only: no request, no entity, no state change.
        assert len(backend.calls) == calls
        await hass.async_block_till_done()
        assert set(hass.states.async_entity_ids()) == states
        assert {
            item.entity_id
            for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        } == registered
        with patch(
            "custom_components.bosch_buderus_heating.local_plant_view."
            "shadow_comparison",
            side_effect=RuntimeError(GATEWAY),
        ):
            broken = await async_get_config_entry_diagnostics(hass, entry)

    # A failing comparison never takes the report down.
    assert broken["local_plant_view"] == [
        {"target": diagnostics["local_plant_view"][0]["target"], "error": "unavailable"}
    ]
    assert GATEWAY not in repr(broken["local_plant_view"])
    assert broken.keys() == diagnostics.keys()

    (report,) = diagnostics["local_plant_view"]
    assert report["local_readings"] == 5
    assert report["cloud_form_resources"] == 2
    assert report["local_sensors"] == 1
    assert report["skipped"] == {"private": 2}
    assert report["templates_compatible"] == 1
    # The paired reading and the running P45 entity keep their IDs.
    assert report["matching"] == [
        "sensor:heatingCircuits:hc2:currentRoomSetpoint",
        "sensor:local_only:heatSources:currentSupplySetpoint",
    ]
    # Not offered by this cloud: a cloud-form ID the local access adds.
    assert report["only_local"] == ["sensor:gateway:return_temperature"]
    # Controls, cloud-only readings, faults and notifications need the cloud.
    assert report["needs_cloud"] == [
        "binary_sensor:cloud_control",
        "binary_sensor:system_fault",
        "event:system_notifications",
        "number:heatingCircuits:hc2:manualRoomSetpoint:control",
        "sensor:active_faults",
        "sensor:active_notifications",
        "sensor:heatingCircuits:hc2:manualRoomSetpoint",
    ]
    assert report["counts"]["per_source_entities"] == 2
    assert report["mismatches"] == []
    text = repr(report)
    for private in (GATEWAY, "47.75", "61.25", "38.125", "192.0.2.10", "4711"):
        assert private not in text


FLOW = "/heatSources/hs1/pumpVolumeFlow"


@pytest.mark.parametrize(("value", "shown"), [(734, 734), (65535, None)])
def test_the_lowflow_marker_is_no_volume_flow(value, shown) -> None:
    """The declared marker 65535 is a device state, never a flow of 65535 l/h."""
    from custom_components.bosch_buderus_heating.local_plant_view import (
        local_reading,
    )

    observation = local(
        FLOW, "integerValue", value, unitOfMeasure="l/h", state=[{"lowflow": 65535}]
    )
    assert local_reading(observation).value == shown
    view = build_local_plant_view([observation])
    (sensor,) = [item for item in view.local_sensors if item.path == FLOW]
    assert sensor.definition.kind == "flow"
    # The cloud form lists the marker like the cloud, so it shows no value.
    resource, reason = cloud_form(observation)
    if resource is not None:
        assert reason is None
        assert 65535 in resource.metadata.numeric_error_values
