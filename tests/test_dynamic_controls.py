"""Installation variants use their own write contracts and live lifecycle."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.enum_translation import (
    writable_enum_options,
)
from custom_components.bosch_buderus_heating.number import (
    async_setup_entry as setup_numbers,
)
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    Resource,
    ResourceMetadata,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.select import (
    BoschBuderusSelect,
    build_select_descriptions,
)
from custom_components.bosch_buderus_heating.select import (
    async_setup_entry as setup_selects,
)
from custom_components.bosch_buderus_heating.switch import (
    async_setup_entry as setup_switches,
)
from custom_components.bosch_buderus_heating.writes import WriteService, assess_control

MODE = "/heatingCircuits/hc2/operationMode"
NUMBER = "/heatingCircuits/hc2/manualRoomSetpoint"


def enum(path=MODE, options=("manual", "auto"), value="manual"):
    return Resource(
        path=path,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="stringValue", writable=True, allowed_values=options
        ),
    )


def number(minimum=5, maximum=30, value=20, unit="C"):
    return Resource(
        path=NUMBER,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="floatValue",
            writable=True,
            minimum=minimum,
            maximum=maximum,
            unit=unit,
        ),
    )


def snapshots(*resources):
    return {r.path: ResourceSnapshot(r, True, datetime.now(UTC)) for r in resources}


@pytest.fixture
def environment(hass):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.put_resource_value.return_value = None
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, client, Gateway("synthetic-gateway"), entry
    )
    coordinator._write_service = WriteService(client, sleep=AsyncMock())
    coordinator.data = {}
    coordinator.last_update_success = True
    unload = []
    platform_entry = SimpleNamespace(
        runtime_data=SimpleNamespace(coordinators=(coordinator,)),
        async_on_unload=unload.append,
    )
    yield coordinator, client, platform_entry
    for callback in unload:
        callback()


@pytest.mark.parametrize(
    "path",
    [
        MODE,
        "/dhwCircuits/dhw7/operationMode",
        "/system/silentMode/enabled",
        "/heatSources/additionalHeater/operationMode",
    ],
)
async def test_each_enum_family_accepts_and_writes_a_new_offered_code(
    hass, environment, path
):
    coordinator, client, entry = environment
    current = enum(path, ("OnlyOld", "NewVendorCode"), "OnlyOld")
    coordinator.data = snapshots(current)
    added = []
    await setup_selects(hass, entry, added.extend)
    entity = added[0]
    assert entity.options == ["OnlyOld", "NewVendorCode"]
    client.get_resource.return_value = replace(current, value="NewVendorCode")
    await entity.async_select_option("NewVendorCode")
    client.put_resource_value.assert_awaited_once_with(
        "synthetic-gateway", path, "NewVendorCode"
    )
    assert entity.current_option == "NewVendorCode"
    assert coordinator.data[path].resource.value == "NewVendorCode"


async def test_two_gateways_with_the_same_circuit_path_remain_independent(
    hass, environment
):
    first, _, entry = environment
    second = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("second-gateway"), first._config_entry
    )
    first.data = snapshots(enum(options=("manual",)))
    second.data = snapshots(enum(options=("auto", "VendorNew"), value="VendorNew"))
    second.last_update_success = True
    entry.runtime_data.coordinators = (first, second)
    added = []
    await setup_selects(hass, entry, added.extend)
    assert [e.options for e in added] == [["manual"], ["auto", "VendorNew"]]
    assert added[0].unique_id != added[1].unique_id


async def test_aliases_remain_reversible_across_metadata_changes(hass, environment):
    coordinator, client, entry = environment
    path = "/dhwCircuits/dhw1/operationMode"
    current = enum(path, ("Off",), "Off")
    coordinator.data = snapshots(current)
    added = []
    await setup_selects(hass, entry, added.extend)
    entity = added[0]
    assert entity.options == ["off"]
    current = enum(path, ("Off", "off", "pointt:off"), "Off")
    coordinator.async_set_updated_data(snapshots(current))
    assert entity.options == ["off", "pointt:off", "pointt:pointt:off"]
    for key, raw in [
        ("pointt:off", "off"),
        ("off", "Off"),
        ("pointt:pointt:off", "pointt:off"),
    ]:
        client.get_resource.return_value = replace(current, value=raw)
        await entity.async_select_option(key)
        assert coordinator.data[path].resource.value == raw
    assert [call.args[2] for call in client.put_resource_value.await_args_list] == [
        "off",
        "Off",
        "pointt:off",
    ]
    assert writable_enum_options("hot_water_operation_mode", ("off",)) == {
        "pointt:off": "off"
    }


async def test_controls_appear_once_when_metadata_arrives_and_stop_on_unload(
    hass, environment
):
    coordinator, _, entry = environment
    current = enum(options=())
    coordinator.data = snapshots(current)
    added = []
    await setup_selects(hass, entry, added.extend)
    assert added == []
    coordinator.async_set_updated_data(snapshots(enum()))
    assert len(added) == 1
    identity = added[0].unique_id
    coordinator.async_set_updated_data(snapshots(enum(options=("manual", "New"))))
    assert added[0].options == ["manual", "New"]
    assert len(added) == 1 and added[0].unique_id == identity
    coordinator.async_set_updated_data(
        snapshots(replace(enum(), metadata=replace(enum().metadata, writable=False)))
    )
    assert not added[0].available
    coordinator.async_set_updated_data({})
    assert not added[0].available
    coordinator.async_set_updated_data(snapshots(enum()))
    assert len(added) == 1 and added[0].available
    entry.async_on_unload.__self__.pop(0)()
    coordinator.async_set_updated_data(
        snapshots(enum(), enum("/heatingCircuits/hc3/operationMode"))
    )
    assert len(added) == 1


async def test_switch_variants_add_select_without_replacing_existing_switch(
    hass, environment
):
    coordinator, _, entry = environment
    path = "/dhwCircuits/dhw1/charge"
    current = enum(path, ("start", "stop"), "stop")
    coordinator.data = snapshots(current)
    switches = []
    selects = []
    await setup_switches(hass, entry, switches.extend)
    await setup_selects(hass, entry, selects.extend)
    identity = switches[0].unique_id
    assert selects == []
    coordinator.async_set_updated_data(
        snapshots(enum(path, ("start", "stop", "boost"), "boost"))
    )
    assert len(selects) == 1 and switches[0].unique_id == identity
    assert selects[0].options == ["start", "stop", "boost"]
    assert switches[0].is_on is None
    assert selects[0].unique_id != identity
    coordinator.async_set_updated_data(snapshots(enum(path, ("start",), "start")))
    assert selects[0].options == ["start"] and selects[0].available
    assert not switches[0].available
    assert len(selects) == len(switches) == 1


@pytest.mark.parametrize(
    ("minimum", "maximum", "target", "unit"),
    [
        (0, 40, 35.25, "C"),
        (-10, 45, -2.25, "C"),
        (41, 104, 68.25, "F"),
        (270, 320, 291.25, "K"),
    ],
)
async def test_numeric_writes_use_device_limits_not_reference_limits_or_ui_step(
    environment, minimum, maximum, target, unit
):
    _coordinator, client, _ = environment
    current = number(minimum, maximum, 20, unit)
    client.get_resource.return_value = replace(current, value=target)
    policy = assess_control(current).policy
    await WriteService(client, sleep=AsyncMock()).async_write_number(
        "synthetic-gateway", current, target, policy
    )
    client.put_resource_value.assert_awaited_once_with(
        "synthetic-gateway", NUMBER, target
    )
    assert assess_control(current).minimum == minimum
    assert assess_control(current).maximum == maximum


async def test_number_appears_after_bounds_arrive_and_changes_native_unit(
    hass, environment
):
    coordinator, _, entry = environment
    current = number()
    coordinator.data = snapshots(
        replace(current, metadata=replace(current.metadata, minimum=None))
    )
    added = []
    await setup_numbers(hass, entry, added.extend)
    assert added == []
    coordinator.async_set_updated_data(snapshots(current))
    entity = added[0]
    assert entity.native_unit_of_measurement == "°C"
    coordinator.async_set_updated_data(snapshots(number(40, 100, 68, "F")))
    assert len(added) == 1
    assert entity.native_min_value == 40 and entity.native_max_value == 100
    assert entity.native_unit_of_measurement == "°F"
    coordinator.async_set_updated_data(snapshots(number(value=float("nan"))))
    assert entity.native_value is None


async def test_removed_option_is_rejected_at_entity_and_locked_write(environment):
    coordinator, client, _ = environment
    current = enum()
    coordinator.data = snapshots(current)
    entity = BoschBuderusSelect(
        coordinator, build_select_descriptions({MODE: current})[0]
    )
    coordinator.async_set_updated_data(snapshots(enum(options=("manual",))))
    with pytest.raises(ServiceValidationError):
        await entity.async_select_option("auto")
    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(
            MODE, "auto", entity.entity_description.write_policy
        )
    client.put_resource_value.assert_not_awaited()


@pytest.mark.parametrize(
    "options", [("manual", 42), ("manual", None), ("bad\ncode",), ("",)]
)
def test_malformed_enum_contract_is_rejected_without_affecting_other_resource(options):
    invalid = enum(options=options)
    valid = enum("/heatingCircuits/hc3/operationMode")
    descriptions = build_select_descriptions({MODE: invalid, valid.path: valid})
    assert len(descriptions) == 1 and descriptions[0].resource_path == valid.path
    assert assess_control(invalid).rejection_reason == "invalid_options"


async def test_missing_values_do_not_invent_states_or_hide_valid_contracts(
    hass, environment
):
    coordinator, client, entry = environment
    mode = replace(enum(), has_value=False)
    target = replace(number(), has_value=False)
    charge = replace(
        enum("/dhwCircuits/dhw1/charge", ("start", "stop"), "stop"), has_value=False
    )
    coordinator.data = snapshots(mode, target, charge)
    selects = []
    numbers = []
    switches = []
    await setup_selects(hass, entry, selects.extend)
    await setup_numbers(hass, entry, numbers.extend)
    await setup_switches(hass, entry, switches.extend)
    assert selects[0].available and selects[0].current_option is None
    assert numbers[0].available and numbers[0].native_value is None
    assert switches[0].available and switches[0].is_on is None
    client.get_resource.return_value = replace(mode, value="auto", has_value=True)
    await selects[0].async_select_option("auto")
    assert selects[0].current_option == "auto"
