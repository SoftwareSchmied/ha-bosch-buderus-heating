"""Create-all for cloud resources the integration does not know.

Every unknown resource with a single readable value becomes a read-only,
disabled diagnostic sensor under the unique ID a known resource gets.
Identifiers, network and privacy data, free text, names, lists, structures
and configuration never do. Synthetic resources; no request is added.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import resource_catalog
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import ResourceSnapshot
from custom_components.bosch_buderus_heating.pointt import (
    Resource,
    ResourceMetadata,
    ResourceReference,
)
from custom_components.bosch_buderus_heating.resource_catalog import (
    CapabilityMaturity,
    PollGroup,
    poll_group,
    unknown_entity_kind,
)
from custom_components.bosch_buderus_heating.sensor import (
    BoschBuderusSensor,
    build_sensor_descriptions,
)
from custom_components.bosch_buderus_heating.writes import assess_control
from tests.test_sensor import _sensor

PATH = "/heatSources/hs1/futureReading"


def resource(path=PATH, value=None, *, kind="floatValue", unit=None, **meta):
    return Resource(
        path,
        value=value,
        has_value=value is not None,
        metadata=ResourceMetadata(resource_type=kind, unit=unit, **meta),
    )


def only(item):
    (description,) = build_sensor_descriptions({item.path: item})
    return description


def test_an_unknown_number_becomes_a_disabled_diagnostic_sensor(hass) -> None:
    item = resource(value=41.5, unit="C")
    assert resource_catalog.capability_maturity(PATH) is CapabilityMaturity.OBSERVED
    description = only(item)
    assert description.unique_key == "heatSources:hs1:futureReading"
    assert description.entity_registry_enabled_default is False
    assert description.entity_category is EntityCategory.DIAGNOSTIC
    assert description.state_class is None
    assert description.device_class is SensorDeviceClass.TEMPERATURE
    sensor = _sensor(hass, item)
    assert sensor.native_value == 41.5
    assert sensor.unique_id == "gateway-one:heatSources:hs1:futureReading"


def test_an_unknown_unit_shows_the_number_without_it(hass) -> None:
    item = resource(value=7, kind="integerValue", unit="furlongs")
    description = only(item)
    assert description.native_unit_of_measurement is None
    assert _sensor(hass, item).native_value == 7
    percent = resource(value=12, kind="integerValue", unit="%")
    assert only(percent).native_unit_of_measurement == "%"


def test_text_with_declared_values_becomes_an_enum(hass) -> None:
    item = resource(
        value="Mode A",
        kind="stringValue",
        allowed_values=("Mode A", "Mode B"),
    )
    description = only(item)
    assert description.device_class is SensorDeviceClass.ENUM
    assert description.options == ["mode_a", "mode_b"]
    assert _sensor(hass, item).native_value == "mode_a"


@pytest.mark.parametrize("value", ["true", "FALSE", True])
def test_true_false_becomes_on_off(hass, value) -> None:
    item = resource(value=value, kind="stringValue")
    description = only(item)
    assert description.options == ["on", "off"]
    assert description.translation_key == "on_off_state"
    expected = "on" if str(value).casefold() == "true" else "off"
    assert _sensor(hass, item).native_value == expected


@pytest.mark.parametrize(
    "item",
    [
        resource("/system/network/macAddress", 5, kind="integerValue"),
        resource("/heatSources/hs1/linkSki", 7, kind="integerValue"),
        resource("/heatSources/hs1/radio/IpV4", 3, kind="integerValue"),
        resource(
            "/heatSources/hs1/moduleSsid",
            "home",
            kind="stringValue",
            allowed_values=("home", "work"),
        ),
        resource("/gateway/wifi/ip/ipv4", "192.0.2.10", kind="stringValue"),
        resource(value="free text", kind="stringValue"),  # free text
        resource("/heatingCircuits/hc1/futureName/name", "x", kind="stringValue"),
        Resource(PATH, references=(ResourceReference(PATH + "/a"),)),
        resource(value=None, kind="configData"),
        Resource(
            PATH,
            values=({"a": 1},),
            has_values=True,
            metadata=ResourceMetadata(resource_type="floatValue"),
        ),
        resource(value=3, kind="switchProgram"),
    ],
)
def test_identifiers_text_names_and_structures_stay_out(item) -> None:
    assert unknown_entity_kind(item) is None
    assert not build_sensor_descriptions({item.path: item})


def test_a_writable_unknown_resource_is_a_read_only_sensor_only() -> None:
    item = resource(value=21.0, unit="C", writable=True, minimum=5, maximum=30)
    description = only(item)
    assert description.entity_registry_enabled_default is False
    assert not assess_control(item).eligible  # never a control


def test_a_resource_known_later_keeps_its_unique_id() -> None:
    path = "/heatSources/returnTemperature"
    item = resource(path, 30.5, unit="C")
    known = only(item)
    assert not known.value_kind.startswith("unknown_")

    def observed(candidate):
        return CapabilityMaturity.OBSERVED

    with patch.object(resource_catalog, "capability_maturity", observed):
        unknown = only(item)
    assert unknown.value_kind == "unknown_number"
    # Including an established legacy key of the path.
    assert unknown.unique_key == known.unique_key == "return_temperature"


def test_no_request_is_added() -> None:
    # The polling plan of a resource does not depend on its entity.
    number = resource(value=41.5, unit="C")
    assert poll_group(number) is PollGroup.SLOW  # polled like before
    # A known resource in a shape its rules reject keeps its schema guard:
    # no entity under the known ID, and the cadence of its path.
    path = "/heatSources/hs1/numberOfStarts"
    unexpected = resource(path, "text", kind="stringValue", allowed_values=("a",))
    assert unknown_entity_kind(unexpected) is None
    assert not build_sensor_descriptions({path: unexpected})


async def test_retirement_keeps_unknown_entities_while_they_exist(hass) -> None:
    from custom_components.bosch_buderus_heating import _remove_retired_entities

    item = resource(value=41.5, unit="C")
    structure = Resource(
        "/heatSources/hs1/futureList",
        references=(ResourceReference("/heatSources/hs1/futureList/a"),),
    )
    sensor = _sensor(hass, item)
    coordinator = sensor.coordinator
    coordinator.resources = {item.path: item, structure.path: structure}
    coordinator.data = {item.path: ResourceSnapshot(item, True, datetime.now(UTC))}
    entry = coordinator.config_entry
    registry = er.async_get(hass)
    kept = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "gateway-one:heatSources:hs1:futureReading",
        config_entry=entry,
    )
    retired = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "gateway-one:heatSources:hs1:futureList",
        config_entry=entry,
    )
    _remove_retired_entities(hass, entry, (coordinator,))
    assert registry.async_get(kept.entity_id) is not None
    assert registry.async_get(retired.entity_id) is None  # no entity of its own
    assert isinstance(BoschBuderusSensor(coordinator, only(item)), BoschBuderusSensor)


# ------------------------------------------------------------- review fixes


def test_a_kelvin_value_is_a_temperature_difference(hass) -> None:
    """HAQ-4: 5 K is a spread, never -268.15 °C."""
    from homeassistant.components.sensor import SensorDeviceClass

    item = resource(value=5.0, unit="K")
    description = only(item)
    assert description.native_unit_of_measurement == "K"
    assert description.device_class is SensorDeviceClass.TEMPERATURE_DELTA
    assert _sensor(hass, item).native_value == 5.0
    celsius = only(resource(value=41.5, unit="C"))
    assert celsius.device_class is SensorDeviceClass.TEMPERATURE


def test_a_converted_unit_keeps_its_scale(hass) -> None:
    item = resource(value=10.0, unit="kBtu")
    description = only(item)
    assert description.native_unit_of_measurement == "kWh"
    assert description.value_scale == pytest.approx(0.29307107)
    assert _sensor(hass, item).native_value == pytest.approx(2.9307107)


@pytest.mark.parametrize(
    "current",
    [
        resource(value=None),  # no value at this start
        resource(value=float("nan")),
        resource(value="now a text", kind="stringValue"),  # another type
    ],
)
async def test_an_unknown_entity_survives_any_current_shape(hass, current) -> None:
    from custom_components.bosch_buderus_heating import _remove_retired_entities

    sensor = _sensor(hass, resource(value=41.5, unit="C"))
    coordinator = sensor.coordinator
    child = resource(PATH + "/part", value=None)  # missing from this batch
    coordinator.resources = {PATH: current}
    registry = er.async_get(hass)
    entry = coordinator.config_entry
    ids = (
        "gateway-one:heatSources:hs1:futureReading",
        "gateway-one:heatSources:hs1:futureReading:part",
    )
    entries = [
        registry.async_get_or_create("sensor", DOMAIN, unique_id, config_entry=entry)
        for unique_id in ids
    ]
    _remove_retired_entities(hass, entry, (coordinator,))
    assert all(registry.async_get(item.entity_id) for item in entries)
    assert unknown_entity_kind(child) is None  # no value now, but its ID stays


async def test_private_unknown_resources_lose_their_entity(hass) -> None:
    from custom_components.bosch_buderus_heating import _remove_retired_entities

    sensor = _sensor(hass, resource(value=41.5, unit="C"))
    coordinator = sensor.coordinator
    private = resource("/heatSources/hs1/installerPinCode", 1234, kind="integerValue")
    coordinator.resources = {private.path: private}
    registry = er.async_get(hass)
    created = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "gateway-one:heatSources:hs1:installerPinCode",
        config_entry=coordinator.config_entry,
    )
    _remove_retired_entities(hass, coordinator.config_entry, (coordinator,))
    assert registry.async_get(created.entity_id) is None


@pytest.mark.parametrize(
    ("name", "private"),
    [
        ("latitude", True),
        ("siteLongitude", True),
        ("postalCode", True),
        ("zipCode", True),
        ("pinCode", True),
        ("installerCode", True),
        ("pairingCode", True),
        ("deviceGtin", True),
        ("sgtin", True),
        ("ean", True),
        ("modemImei", True),
        ("IPv4", True),
        ("ipv6", True),
        ("lanIp4", True),
        ("ownerEmail", True),
        ("pump", False),
        ("dripPanTemp", False),
        ("errorCode", False),
        ("pumpSpeed", False),
        ("supplyTemperature", False),
        # 0.9.0-alpha.2: more identifiers, addresses and locations.
        ("geolocation", True),
        ("siteGeoLocation", True),
        ("latLong", True),
        ("latlong", True),
        ("gpsPosition", True),
        ("nodeIdentifier", True),
        ("IDs", True),
        ("gatewayIds", True),
        ("gatewayIDs", True),
        ("moduleNo", True),
        ("SerNo", True),
        ("customerNumber", True),
        ("houseNumber", True),
        ("bssid", True),
        ("wifiBssid", True),
        ("hostname", True),
        ("hostName", True),
        ("netmask", True),
        ("subnetMask", True),
        ("netMask", True),
        ("ipaddr", True),
        ("ipAddr", True),
        ("gatewayAddr", True),
        # Real readings stay: "No" only as its own last word, never inside
        # a word or for a count.
        ("numberOfStarts", False),
        ("NoOfStarts", False),
        ("No", False),
        ("nominalPower", False),
        ("noiseLevel", False),
        ("lowNoiseMode", False),
        ("pumpNumber", False),
        ("compressorStarts", False),
        ("longTermAverage", False),
        ("houseHeatingDemand", False),
        ("maskedAlarms", False),
        ("idsPresent", False),
    ],
)
def test_identifier_words(name, private) -> None:
    from custom_components.bosch_buderus_heating.resource_catalog import (
        identifier_name,
    )

    assert identifier_name(name) is private


def test_an_unknown_part_never_takes_a_known_id() -> None:
    child = resource("/heatSources/hs1/numberOfStarts/ch", 3, kind="integerValue")
    assert not build_sensor_descriptions({child.path: child})
    parent = Resource(
        "/heatSources/hs1/numberOfStarts",
        values=({"ch": 3, "total": 5},),
        has_values=True,
        metadata=ResourceMetadata(resource_type="emonValue"),
    )
    descriptions = build_sensor_descriptions({parent.path: parent, child.path: child})
    keys = [item.unique_key for item in descriptions]
    assert keys.count("heatSources:hs1:numberOfStarts:ch") == 1
    assert all(not item.value_kind.startswith("unknown_") for item in descriptions)


def test_the_plant_view_shows_an_offered_unknown_path_under_its_cloud_id() -> None:
    from custom_components.bosch_buderus_heating.local_plant_view import (
        build_local_plant_view,
    )
    from tests.test_local_api import read

    path = "/heatSources/compressor/powerElecActual"
    reading = read(
        {
            "id": path,
            "type": "floatValue",
            "writeable": 0,
            "value": 810.0,
            "unitOfMeasure": "W",
        }
    )
    offered = build_local_plant_view([reading], cloud_paths={path})
    assert list(offered.resources) == [path] and not offered.local_sensors
    alone = build_local_plant_view([reading])
    assert not alone.resources and [s.path for s in alone.local_sensors] == [path]


async def test_a_child_of_an_unknown_node_is_never_retired(hass) -> None:
    """The node ``P`` (a structure) loses its own ID; its child ``P/x``, missing
    from this cycle, keeps ``P:x`` as its own entity."""
    from custom_components.bosch_buderus_heating import _remove_retired_entities

    sensor = _sensor(hass, resource(value=41.5, unit="C"))
    coordinator = sensor.coordinator
    node = Resource(
        "/heatSources/hs1/futureNode",
        references=(ResourceReference("/heatSources/hs1/futureNode/reading"),),
    )
    coordinator.resources = {node.path: node}
    registry = er.async_get(hass)
    entry = coordinator.config_entry
    own, child = (
        registry.async_get_or_create("sensor", DOMAIN, unique_id, config_entry=entry)
        for unique_id in (
            "gateway-one:heatSources:hs1:futureNode",
            "gateway-one:heatSources:hs1:futureNode:reading",
        )
    )
    _remove_retired_entities(hass, entry, (coordinator,))
    assert registry.async_get(own.entity_id) is None
    assert registry.async_get(child.entity_id) is not None


@pytest.mark.parametrize(
    "parent",
    ["customerData", "host", "moduleNo", "gpsUnit", "deviceIdentifiers", "subnetA"],
)
def test_newer_identifier_words_only_retire_by_the_last_segment(parent) -> None:
    from custom_components.bosch_buderus_heating.resource_catalog import (
        unknown_never_entity,
    )

    # A reading below a parent with a newer word is not retired for it.
    below = resource(f"/system/{parent}/supplyTemperature", 21.0)
    assert not unknown_never_entity(below)
    # As the last segment the word still counts.
    assert unknown_never_entity(resource(f"/system/{parent}", 21.0))


@pytest.mark.parametrize("parent", ["macInfo", "serialData", "ipSettings", "pinCode"])
def test_earlier_identifier_words_still_retire_a_whole_subtree(parent) -> None:
    from custom_components.bosch_buderus_heating.resource_catalog import (
        unknown_never_entity,
    )

    assert unknown_never_entity(resource(f"/system/{parent}/supplyTemperature", 1.0))
