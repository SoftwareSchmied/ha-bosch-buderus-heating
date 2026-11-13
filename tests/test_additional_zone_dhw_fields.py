"""Current zone/DHW readings supported by the reviewed resource mappings."""

from dataclasses import replace
from unittest.mock import Mock

import pytest
from homeassistant.components.sensor import SensorDeviceClass

from custom_components.bosch_buderus_heating.coordinator import Freshness
from custom_components.bosch_buderus_heating.discovery import _optional_children
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Resource,
    ResourceMetadata,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.resource_catalog import (
    PollGroup,
    additional_app_value,
    poll_group,
    resource_name,
    supports_entity,
)
from custom_components.bosch_buderus_heating.sensor import (
    BoschBuderusSensor,
    build_sensor_descriptions,
)
from tests.test_water_softener import coordinator_for


def resource(path="/zones/zone2/heating/manualRoomSetpoint", value=21):
    numeric = path.endswith("Setpoint")
    return Resource(
        path,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="floatValue" if numeric else "stringValue",
            unit="C" if numeric else None,
        ),
    )


@pytest.mark.parametrize("mode", ["heating", "cooling"])
@pytest.mark.parametrize(
    "field", ["manualRoomSetpoint", "temporaryRoomSetpoint", "operationMode"]
)
def test_separate_zone_modes_have_localized_names_and_metadata(mode, field):
    item = resource(
        f"/zones/zone2/{mode}/{field}", "auto" if field == "operationMode" else 21
    )
    descriptions = build_sensor_descriptions({item.path: item})
    assert len(descriptions) == 1
    description = descriptions[0]
    assert description.entity_registry_enabled_default
    assert description.entity_category is None
    assert resource_name(item.path).startswith(
        "Heizen:" if mode == "heating" else "Kühlen:"
    )
    assert resource_name(item.path, language="en").startswith(mode.title() + ":")
    assert poll_group(item) is not PollGroup.STATIC
    assert description.device_class is (
        SensorDeviceClass.ENUM
        if field == "operationMode"
        else SensorDeviceClass.TEMPERATURE
    )


@pytest.mark.parametrize(
    "unit,kind", [("F", "floatValue"), (None, "floatValue"), ("C", "stringValue")]
)
def test_wrong_live_unit_or_type_is_not_exposed(unit, kind):
    item = resource()
    item = replace(item, metadata=replace(item.metadata, unit=unit, resource_type=kind))
    assert not supports_entity(item)
    assert not build_sensor_descriptions({item.path: item})


@pytest.mark.parametrize(
    "value",
    [True, None, float("inf"), float("nan"), 32767, -32768, "21", {"value": 21}, [21]],
)
def test_invalid_temperature_never_becomes_a_measurement(value):
    assert additional_app_value(resource(value=value)) is None


def test_dhw_type_is_optional_diagnostic_and_has_no_numeric_metadata():
    item = resource("/dhwCircuits/dhw2/dhwType", "tank")
    (description,) = build_sensor_descriptions({item.path: item})
    assert not description.entity_registry_enabled_default
    assert description.device_class is None
    assert poll_group(item) is PollGroup.STATIC
    assert additional_app_value(item) == "tank"
    assert additional_app_value(replace(item, value="")) is None
    assert additional_app_value(replace(item, value="line\nfeed")) is None
    assert additional_app_value(replace(item, value="x" * 65)) is None


def test_optional_probes_follow_only_reported_component_ids():
    assert "/zones/zone2/heating/operationMode" in _optional_children("/zones/zone2")
    assert "/zones/zone2/cooling/operationMode" in _optional_children("/zones/zone2")
    assert "/dhwCircuits/dhw2/dhwType" in _optional_children("/dhwCircuits/dhw2")
    assert "/zones/zone2" not in _optional_children("/zones")


async def test_same_zone_on_two_gateways_has_distinct_identity_and_recovers(hass):
    item = resource()
    first = coordinator_for(hass, {item.path: item}, "one")
    second = coordinator_for(hass, {item.path: item}, "two")
    (description,) = build_sensor_descriptions({item.path: item})
    sensor = BoschBuderusSensor(first, description)
    other = BoschBuderusSensor(second, description)
    assert sensor.unique_id != other.unique_id
    identity = sensor.unique_id
    assert sensor.native_value == 21
    snapshot = first.data[item.path]
    first.data[item.path] = replace(snapshot, freshness=Freshness.STALE)
    assert not sensor.available
    assert sensor.native_value is None
    first.data[item.path] = replace(
        snapshot, resource=replace(item, metadata=replace(item.metadata, unit="F"))
    )
    assert sensor.native_value is None
    first.data[item.path] = snapshot
    assert sensor.available
    assert sensor.native_value == 21
    assert sensor.unique_id == identity


async def test_mode_sensor_rejects_unknown_later_state_and_recovers(hass):
    item = resource("/zones/zone2/cooling/operationMode", "auto")
    coordinator = coordinator_for(hass, {item.path: item})
    (description,) = build_sensor_descriptions({item.path: item})
    sensor = BoschBuderusSensor(coordinator, description)
    assert sensor.native_value == "auto"
    snapshot = coordinator.data[item.path]
    coordinator.data[item.path] = replace(
        snapshot, resource=replace(item, value="unreviewed")
    )
    assert sensor.native_value is None
    coordinator.data[item.path] = replace(
        snapshot, resource=replace(item, value="manual")
    )
    assert sensor.native_value == "manual"


@pytest.mark.parametrize("mode", ["heating", "cooling"])
@pytest.mark.parametrize(
    "field", ["manualRoomSetpoint", "temporaryRoomSetpoint", "operationMode"]
)
@pytest.mark.parametrize("writable", [False, True])
async def test_zone_polling_recovers_bad_schema_after_real_startup_read(
    hass, mode, field, writable
):
    value = "auto" if field == "operationMode" else 21
    good = resource(f"/zones/zone2/{mode}/{field}", value)
    good = replace(good, metadata=replace(good.metadata, writable=writable))
    device_type = resource("/dhwCircuits/dhw2/dhwType", "tank")
    resources = {r.path: r for r in (good, device_type)}
    coordinator = coordinator_for(hass, resources)
    coordinator._paths_by_group = {
        group: tuple(
            path for path, item in resources.items() if poll_group(item) is group
        )
        for group in coordinator._poll_intervals
    }
    sensor = BoschBuderusSensor(
        coordinator, build_sensor_descriptions({good.path: good})[0]
    )
    identity = sensor.unique_id
    group = PollGroup.CONTROL if writable else PollGroup.FAST
    assert coordinator._paths_by_group[group] == (good.path,)
    assert sensor.native_value == value
    bad = replace(
        good,
        metadata=replace(good.metadata, resource_type=None)
        if field == "operationMode"
        else replace(good.metadata, unit=None),
    )
    startup = parse_resource(
        {"id": "/gateway/tosAccepted", "type": "booleanValue", "value": True}
    )
    responses = {bad.path: bad, startup.path: startup}
    coordinator.client.backoff.raise_if_active = Mock()
    coordinator.client.get_resources_bulk.side_effect = (
        lambda gateway, paths, **kwargs: tuple(
            BatchItemResult(gateway, path, 200, resource=responses[path])
            for path in paths
        )
    )
    coordinator._next_update = {}
    coordinator.data = await coordinator._async_update_data_locked()
    assert sensor.native_value is None
    # A real supplementary read rebuilds the whole polling plan, not just its
    # own static paths. The zone must remain eligible for regular group reads.
    coordinator.startup_data.schedule(startup.path, 1, advertised=True)
    assert await coordinator.async_read_startup_batch(coordinator.data)
    assert coordinator.startup_data.reads[startup.path].result == "success"
    assert coordinator._paths_by_group[group] == (good.path,)
    responses[good.path] = good
    coordinator.client.get_resources_bulk.reset_mock()
    coordinator._next_update = {}
    coordinator.data = await coordinator._async_update_data_locked()
    coordinator.client.get_resources_bulk.assert_awaited_once_with(
        "softener", (good.path,)
    )
    assert sensor.native_value == value
    assert sensor.available
    assert sensor.unique_id == identity
    assert all(
        device_type.path not in paths for paths in coordinator._paths_by_group.values()
    )
    coordinator.client.get_resource.assert_not_awaited()
