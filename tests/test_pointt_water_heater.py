"""PointT hot-water circuits, native readings and confirmed operation writes."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.water_heater import WaterHeaterEntityFeature
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Gateway,
    RateLimited,
    RequestTimeout,
    Resource,
    ResourceMetadata,
    ResourceReference,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.pointt_water_heater import (
    PointTWaterHeater,
    track_pointt_water_heaters,
)
from custom_components.bosch_buderus_heating.resource_catalog import PollGroup
from custom_components.bosch_buderus_heating.runtime import BoschBuderusRuntimeData
from custom_components.bosch_buderus_heating.writes import WriteService

ROOT = "/dhwCircuits/dhw2"
MODE = ROOT + "/operationMode"
ACTUAL = ROOT + "/actualTemp"
TARGET = ROOT + "/currentSetpoint"
MODES = ("Off", "low", "high", "ownprogram", "eco")


def resource(path, value, *, unit="C", writable=False, options=()):
    return Resource(
        path,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="stringValue" if isinstance(value, str) else "floatValue",
            unit=None if isinstance(value, str) else unit,
            writable=writable,
            allowed_values=options,
        ),
    )


def setup(hass, *, mode="ownprogram", options=MODES, writable=True):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"brand": "buderus", "gateway_ids": ["gateway"]}
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, client, Gateway("gateway"), entry
    )
    resources = (
        resource(MODE, mode, writable=writable, options=options),
        resource(ACTUAL, 48.5),
        resource(TARGET, 52),
        resource(ROOT + "/temperatureLevels/low", 41, writable=True),
        resource(ROOT + "/singleChargeSetpoint", 65, writable=True),
    )
    plant = {item.path: item for item in resources}
    coordinator.resources = dict(plant)
    coordinator.data = {
        path: ResourceSnapshot(item, True, datetime.now(UTC))
        for path, item in plant.items()
    }
    coordinator.last_update_success = True
    client.get_resource.side_effect = lambda _gateway, path: plant[path]

    async def write(_gateway, path, value):
        plant[path] = replace(plant[path], value=value)

    async def poll(gateway, paths):
        return tuple(BatchItemResult(gateway, path, 200, plant[path]) for path in paths)

    client.put_resource_value.side_effect = write
    client.get_resources_bulk.side_effect = poll
    coordinator._write_service = WriteService(client, sleep=AsyncMock())
    coordinator._paths_by_group = {PollGroup.FAST: (MODE, ACTUAL, TARGET)}
    coordinator._next_update = {PollGroup.FAST: 0}
    entry.runtime_data = BoschBuderusRuntimeData(
        client, AsyncMock(), (coordinator.gateway,), (coordinator,)
    )
    heater = PointTWaterHeater(coordinator, ROOT)
    heater.hass = hass
    return entry, coordinator, heater, plant


@pytest.mark.parametrize("german", [False, True])
def test_identity_and_readings_use_the_exact_circuit(hass, german):
    hass.config.language = "de" if german else "en"
    _, coordinator, heater, _ = setup(hass)
    coordinator.data["/dhwCircuits/dhw1/actualTemp"] = ResourceSnapshot(
        resource("/dhwCircuits/dhw1/actualTemp", 12), True, datetime.now(UTC)
    )
    assert heater.name == ("Warmwasser 2" if german else "Hot water 2")
    assert heater.unique_id == "gateway:dhwCircuits:dhw2:water_heater"
    assert heater.device_info["identifiers"] == {(DOMAIN, "gateway")}
    assert heater.current_temperature == 48.5
    assert heater.target_temperature == 52
    assert heater.current_operation == "ownprogram"
    assert heater.operation_list == ["off", "low", "high", "ownprogram", "eco"]
    assert heater.supported_features == WaterHeaterEntityFeature.OPERATION_MODE
    assert set(heater.capability_attributes) == {"operation_list"}
    coordinator.client.get_resource.assert_not_awaited()
    coordinator.client.get_resources_bulk.assert_not_awaited()


@pytest.mark.parametrize("mode", MODES)
async def test_exact_mode_write_is_guarded_and_confirmed(hass, mode):
    _, coordinator, heater, _ = setup(hass, mode="high" if mode != "high" else "low")
    display = "off" if mode == "Off" else mode
    await heater.async_set_operation_mode(display)
    coordinator.client.put_resource_value.assert_awaited_once_with(
        "gateway", MODE, mode
    )
    assert coordinator.client.get_resource.await_count == 2
    assert all(
        call.args == ("gateway", MODE)
        for call in coordinator.client.get_resource.await_args_list
    )
    assert heater.current_operation == display
    assert heater.target_temperature is None
    assert heater.current_temperature == 48.5
    await coordinator.async_refresh()
    assert heater.target_temperature == 52
    coordinator.client.get_resources_bulk.assert_awaited_once()
    await coordinator.async_shutdown()


async def test_current_setpoint_cannot_be_written_or_replaced_with_a_start_level(hass):
    _, coordinator, heater, _ = setup(hass)
    for values in (
        {"temperature": 45},
        {"temperature": 45, "operation_mode": "high"},
        {"temperature": float("nan")},
    ):
        with pytest.raises(ServiceValidationError) as failure:
            await heater.async_set_temperature(**values)
        assert failure.value.translation_key == "pointt_dhw_temperature_read_only"
    coordinator.client.put_resource_value.assert_not_awaited()
    coordinator.client.get_resource.assert_not_awaited()
    del coordinator.data[TARGET]
    assert heater.target_temperature is None
    del coordinator.data[ACTUAL]
    assert heater.current_temperature is None
    assert heater.available


@pytest.mark.parametrize("writable", [False, True])
async def test_subset_unknown_codes_and_readonly_mode(hass, writable):
    _, coordinator, heater, _ = setup(
        hass,
        mode="high",
        options=("high", "ownprogram", "newMode", "off"),
        writable=writable,
    )
    assert heater.available
    assert heater.operation_list == (["high", "ownprogram"] if writable else [])
    for mode in ("eco", "newMode", "off", "pointt:off", "Off"):
        with pytest.raises(ServiceValidationError):
            await heater.async_set_operation_mode(mode)
    coordinator.client.put_resource_value.assert_not_awaited()


async def test_heating_program_mode_keeps_its_exact_wire_code(hass):
    _, coordinator, heater, _ = setup(hass, options=(*MODES, "HCprogram"))
    assert "follow_heating_program" in heater.operation_list
    await heater.async_set_operation_mode("follow_heating_program")
    coordinator.client.put_resource_value.assert_awaited_once_with(
        "gateway", MODE, "HCprogram"
    )


@pytest.mark.parametrize("path", [ACTUAL, TARGET])
@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "stale",
        "failed",
        "wrong_path",
        "bool",
        "nan",
        "unit",
        "error",
        "unused",
        "flags",
        "collection",
        "references",
    ],
)
def test_unusable_temperature_is_not_a_reading(hass, path, defect):
    _, coordinator, heater, _ = setup(hass)
    snapshot = coordinator.data[path]
    item = snapshot.resource
    if defect == "missing":
        del coordinator.data[path]
    elif defect == "stale":
        coordinator.data[path] = replace(snapshot, freshness=Freshness.STALE)
    elif defect == "failed":
        coordinator.data[path] = replace(snapshot, available=False)
    else:
        if defect == "wrong_path":
            item = replace(item, path="/dhwCircuits/dhw1/actualTemp")
        elif defect == "bool":
            item = replace(item, value=True)
        elif defect == "nan":
            item = replace(item, value=float("nan"))
        elif defect == "unit":
            item = replace(item, metadata=replace(item.metadata, unit="l/min"))
        elif defect == "error":
            item = replace(
                item,
                metadata=replace(item.metadata, numeric_error_values=(item.value,)),
            )
        elif defect == "unused":
            item = replace(item, metadata=replace(item.metadata, used=False))
        elif defect == "flags":
            item = replace(
                item, metadata=replace(item.metadata, availability_flags_valid=False)
            )
        elif defect == "collection":
            item = replace(item, has_values=True)
        else:
            item = replace(item, references=(ResourceReference("/other"),))
        coordinator.data[path] = replace(snapshot, resource=item)
    assert (
        heater.current_temperature if path == ACTUAL else heater.target_temperature
    ) is None


@pytest.mark.parametrize("unit,value", [("C", 48.5), ("F", 119.3), ("K", 321.65)])
def test_temperature_units_are_converted_without_changing_source(hass, unit, value):
    _, coordinator, heater, _ = setup(hass)
    for path in (ACTUAL, TARGET):
        coordinator.data[path] = replace(
            coordinator.data[path], resource=resource(path, value, unit=unit)
        )
    assert heater.current_temperature == pytest.approx(48.5)
    assert heater.target_temperature == pytest.approx(48.5)


async def test_gateway_changes_are_read_before_mode_write(hass):
    _, coordinator, heater, plant = setup(hass)
    plant[MODE] = replace(plant[MODE], value="eco")
    with pytest.raises(ServiceValidationError):
        await heater.async_set_operation_mode("high")
    coordinator.client.put_resource_value.assert_not_awaited()
    assert heater.current_operation == "eco"


@pytest.mark.parametrize(
    "error", [RateLimited(120), RequestTimeout(), ServiceUnavailable()]
)
async def test_failed_preflight_never_writes(hass, error):
    _, coordinator, heater, _ = setup(hass)
    coordinator.client.get_resource.side_effect = error
    with pytest.raises(HomeAssistantError):
        await heater.async_set_operation_mode("high")
    coordinator.client.put_resource_value.assert_not_awaited()
    assert not heater.available


async def test_unconfirmed_mode_never_retries_put_and_recovers_by_polling(hass):
    _, coordinator, heater, plant = setup(hass)
    calls = 0

    async def read(_gateway, path):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RequestTimeout()
        return plant[path]

    coordinator.client.get_resource.side_effect = read
    with pytest.raises(HomeAssistantError) as failure:
        await heater.async_set_operation_mode("eco")
    assert failure.value.translation_key == "write_not_confirmed"
    coordinator.client.put_resource_value.assert_awaited_once()
    assert not heater.available
    await coordinator.async_refresh()
    assert heater.available and heater.current_operation == "eco"
    await coordinator.async_shutdown()


async def test_discovery_adds_late_circuits_once_and_recovers_existing_identity(hass):
    entry, coordinator, _, _ = setup(hass, mode="newMode")
    add = Mock()
    track_pointt_water_heaters(entry, coordinator, add)
    add.assert_not_called()
    mode = coordinator.data[MODE]
    updated = {
        **coordinator.data,
        MODE: replace(mode, resource=replace(mode.resource, value="high")),
    }
    coordinator.async_set_updated_data(updated)
    heater = add.call_args.args[0][0]
    assert heater.available
    identity = heater.unique_id
    coordinator.async_set_updated_data(
        {**updated, MODE: replace(updated[MODE], available=False)}
    )
    assert not heater.available
    coordinator.async_set_updated_data(updated)
    assert heater.available and heater.unique_id == identity
    add.assert_called_once()
    root3 = "/dhwCircuits/dhw3"
    coordinator.async_set_updated_data(
        {
            **updated,
            root3 + "/operationMode": ResourceSnapshot(
                resource(
                    root3 + "/operationMode", "low", writable=True, options=("low",)
                ),
                True,
                datetime.now(UTC),
            ),
        }
    )
    assert add.call_count == 2
    assert add.call_args.args[0][0].unique_id != identity
    await coordinator.async_shutdown()


def test_target_requires_a_reading_after_a_confirmed_mode_write(hass):
    from custom_components.bosch_buderus_heating.coordinator import SnapshotSource

    _, coordinator, heater, _ = setup(hass)
    timestamp = datetime.now(UTC)
    coordinator.data[MODE] = replace(
        coordinator.data[MODE], source=SnapshotSource.WRITE, last_success=timestamp
    )
    assert heater.target_temperature is None
    heater.async_write_ha_state = Mock()
    heater._handle_coordinator_update()
    coordinator.data[MODE] = replace(
        coordinator.data[MODE],
        source=SnapshotSource.BATCH,
        last_success=timestamp + timedelta(seconds=1),
    )
    heater._handle_coordinator_update()
    assert heater.target_temperature is None
    coordinator.data[TARGET] = replace(
        coordinator.data[TARGET], last_success=timestamp + timedelta(seconds=2)
    )
    assert heater.target_temperature == 52
