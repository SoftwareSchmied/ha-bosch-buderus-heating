"""ConnectKey zone commands use current, independent and confirmed contracts."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.translation import async_get_translations

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import Freshness
from custom_components.bosch_buderus_heating.diagnostics import _control_diagnostics
from custom_components.bosch_buderus_heating.number import (
    BoschBuderusNumber,
    build_number_descriptions,
)
from custom_components.bosch_buderus_heating.number import (
    async_setup_entry as setup_numbers,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    RateLimited,
    RequestTimeout,
    ResourceReference,
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.select import (
    BoschBuderusSelect,
    build_select_descriptions,
)
from custom_components.bosch_buderus_heating.select import (
    async_setup_entry as setup_selects,
)
from custom_components.bosch_buderus_heating.sensor import (
    BoschBuderusSensor,
    build_sensor_descriptions,
)
from custom_components.bosch_buderus_heating.writes import WriteService, assess_control
from tests import test_dynamic_controls
from tests.test_dynamic_controls import snapshots

PAYLOADS = json.loads(
    (
        Path(__file__).parent / "fixtures/contracts/connectkey/zone-controls.json"
    ).read_text()
)["resources"]
RESOURCES = tuple(parse_resource(payload) for payload in PAYLOADS)
MODE = RESOURCES[0]
NUMBER = RESOURCES[4]


@pytest.fixture(name="environment")
def zone_environment(hass):
    yield from test_dynamic_controls.environment.__wrapped__(hass)


def entity_for(coordinator, resource):
    builder, cls = (
        (build_select_descriptions, BoschBuderusSelect)
        if resource.path.endswith("/operationMode")
        else (build_number_descriptions, BoschBuderusNumber)
    )
    return cls(coordinator, builder({resource.path: resource})[0])


@pytest.mark.parametrize("resource", RESOURCES, ids=lambda r: r.path)
async def test_each_zone_command_writes_only_its_path_and_uses_its_metadata(
    environment, resource
):
    coordinator, client, _ = environment
    coordinator.data = snapshots(*RESOURCES)
    entity = entity_for(coordinator, resource)
    if resource.path.endswith("/operationMode"):
        assert entity.options == [
            mode
            for mode in ("off", "manual", "auto")
            if mode in resource.metadata.allowed_values
        ]
        target = next(mode for mode in entity.options if mode != resource.value)
        client.get_resource.return_value = replace(resource, value=target)
        await entity.async_select_option(target)
        assert entity.current_option == target
    else:
        assert entity.native_min_value == resource.metadata.minimum
        assert entity.native_max_value == resource.metadata.maximum
        assert entity.native_step == (resource.metadata.step_size or 0.5)
        assert entity.native_unit_of_measurement == "°C"
        # The UI increment is not an unproven API quantization rule.
        target = resource.value + 0.1
        client.get_resource.return_value = replace(resource, value=target)
        await entity.async_set_native_value(target)
        assert entity.native_value == target
    client.put_resource_value.assert_awaited_once_with(
        "synthetic-gateway", resource.path, target
    )
    client.get_resource.assert_awaited_once_with("synthetic-gateway", resource.path)
    assert all(
        coordinator.data[other.path].resource == other
        for other in RESOURCES
        if other.path != resource.path
    )


@pytest.mark.parametrize("base,setup", [(MODE, setup_selects), (NUMBER, setup_numbers)])
async def test_late_metadata_permission_loss_and_recovery_keep_identity(
    hass, environment, base, setup
):
    coordinator, client, entry = environment
    readonly = replace(base, metadata=replace(base.metadata, writable=False))
    coordinator.data = snapshots(readonly)
    added = []
    await setup(hass, entry, added.extend)
    assert not added
    sensor_description = build_sensor_descriptions({base.path: readonly})[0]
    sensor = BoschBuderusSensor(coordinator, sensor_description)
    identity = sensor.unique_id
    coordinator.async_set_updated_data(snapshots(base))
    entity = added[0]
    control_identity = entity.unique_id
    assert control_identity != identity
    assert (
        sensor.unique_id == identity
        and sensor_description.entity_registry_enabled_default
    )
    for invalid in (
        readonly,
        replace(base, value={"value": 20}),
        replace(base, has_values=True),
        replace(base, references=(ResourceReference("/zones/zone2"),)),
    ):
        coordinator.async_set_updated_data(snapshots(invalid))
        assert not entity.available
        with pytest.raises(WriteValidationError):
            await coordinator.async_write_control(
                base.path,
                "auto" if base is MODE else 21,
                entity.entity_description.write_policy,
            )
        coordinator.async_set_updated_data(snapshots(base))
        assert entity.available and entity.unique_id == control_identity
    coordinator.async_set_updated_data({})
    assert not entity.available
    stale = replace(snapshots(base)[base.path], freshness=Freshness.STALE)
    coordinator.async_set_updated_data({base.path: stale})
    assert not entity.available
    coordinator.async_set_updated_data(snapshots(base))
    assert entity.available and len(added) == 1
    client.put_resource_value.assert_not_awaited()


@pytest.mark.parametrize("unit", ["F", "K", None, "unknown"])
def test_zone_temperature_does_not_borrow_other_families_units(unit):
    invalid = replace(NUMBER, metadata=replace(NUMBER.metadata, unit=unit))
    assert not build_number_descriptions({invalid.path: invalid})


@pytest.mark.parametrize("unit", ["°C", "ºC", "degC"])
def test_zone_temperature_accepts_other_spellings_of_celsius(unit):
    """SEL-2: the same unit spelled differently keeps the zone setpoint."""
    spelled = replace(NUMBER, metadata=replace(NUMBER.metadata, unit=unit))
    (description,) = build_number_descriptions({spelled.path: spelled})
    (reference,) = build_number_descriptions({NUMBER.path: NUMBER})
    assert description.native_unit_of_measurement == "°C"
    assert description.key == reference.key


@pytest.mark.parametrize(
    "value",
    [True, None, "21", {}, [], float("nan"), float("inf"), 32767, -32768, 4, 26],
)
async def test_invalid_reading_or_target_cannot_reach_put(environment, value):
    coordinator, client, _ = environment
    invalid = replace(NUMBER, value=value)
    assert not assess_control(invalid).eligible
    policy = assess_control(NUMBER).policy
    with pytest.raises(WriteValidationError):
        await coordinator._write_service.async_write_number(
            "synthetic-gateway", NUMBER, value, policy
        )
    client.put_resource_value.assert_not_awaited()


@pytest.mark.parametrize(
    "metadata",
    [
        {"minimum": None},
        {"maximum": None},
        {"minimum": 30},
        {"minimum": float("-inf")},
        {"resource_type": "stringValue"},
        {"numeric_error_values": (20,)},
        {"numeric_error_states_valid": False},
    ],
)
def test_incomplete_or_invalid_numeric_contract_has_no_control(metadata):
    invalid = replace(NUMBER, metadata=replace(NUMBER.metadata, **metadata))
    assert not assess_control(invalid).eligible


@pytest.mark.parametrize(
    "step", [None, True, "0.5", {}, [], 0, -1, float("nan"), float("inf")]
)
def test_invalid_step_does_not_hide_reading_but_blocks_control(step):
    resource = parse_resource({**PAYLOADS[4], "stepSize": step})
    assert len(build_sensor_descriptions({resource.path: resource})) == 1
    assert not assess_control(resource).eligible
    assert _control_diagnostics(resource)["rejection_reason"] == "invalid_step"


async def test_changed_options_bounds_and_step_are_checked_at_write(environment):
    coordinator, client, _ = environment
    coordinator.data = snapshots(MODE, NUMBER)
    select = entity_for(coordinator, MODE)
    number = entity_for(coordinator, NUMBER)
    changed_mode = replace(
        MODE, metadata=replace(MODE.metadata, allowed_values=("manual",))
    )
    changed_number = replace(
        NUMBER, metadata=replace(NUMBER.metadata, maximum=20, step_size=0.25)
    )
    coordinator.async_set_updated_data(snapshots(changed_mode, changed_number))
    assert select.options == ["manual"]
    assert number.native_max_value == 20 and number.native_step == 0.25
    with pytest.raises(ServiceValidationError):
        await select.async_select_option("auto")
    with pytest.raises(ServiceValidationError):
        await number.async_set_native_value(21)
    client.put_resource_value.assert_not_awaited()


@pytest.mark.parametrize("mode", ["heating", "cooling"])
def test_temporary_control_stays_read_only_even_with_writable_metadata(mode):
    temporary = replace(NUMBER, path=f"/zones/zone2/{mode}/temporaryRoomSetpoint")
    assert len(build_sensor_descriptions({temporary.path: temporary})) == 1
    assert not build_number_descriptions({temporary.path: temporary})
    assert (
        _control_diagnostics(temporary)["rejection_reason"]
        == "zone_override_lifecycle_unverified"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/zones/zn1/heating/manualRoomSetpoint",
        "/zones/zone1/heat/manualRoomSetpoint",
        "/zones/zone1/heating/childLock",
    ],
)
def test_other_zone_families_and_commands_are_not_enabled(path):
    assert not assess_control(replace(NUMBER, path=path)).eligible


@pytest.mark.parametrize("language,needle", [("de", "einstellen"), ("en", "Set ")])
async def test_controls_are_distinct_from_unchanged_readings_and_translated(
    hass, environment, language, needle, enable_custom_integrations
):
    coordinator, _, _ = environment
    hass.config.language = language
    coordinator.data = snapshots(*RESOURCES)
    for resource in RESOURCES:
        control = entity_for(coordinator, resource)
        sensor = BoschBuderusSensor(
            coordinator, build_sensor_descriptions({resource.path: resource})[0]
        )
        assert needle in control.name
        assert control.name != sensor.name and control.unique_id != sensor.unique_id
    translations = await async_get_translations(hass, language, "entity", {DOMAIN})
    prefix = f"component.{DOMAIN}.entity.select.zone_operation_mode.state."
    assert [translations[prefix + value] for value in ("off", "manual", "auto")] == (
        ["Aus", "Manuell", "Automatik"]
        if language == "de"
        else ["Off", "Manual", "Auto"]
    )


@pytest.mark.parametrize("resource,target", [(MODE, "auto"), (NUMBER, 21)])
async def test_uncertain_put_is_not_retried_and_delayed_readback_confirms(
    environment, resource, target
):
    coordinator, client, _ = environment
    coordinator.data = snapshots(resource)
    client.put_resource_value.side_effect = RequestTimeout("lost response")
    client.get_resource.side_effect = [resource, replace(resource, value=target)]
    result = await coordinator.async_write_control(
        resource.path, target, assess_control(resource).policy
    )
    assert result.value == target
    assert client.put_resource_value.await_count == 1
    assert client.get_resource.await_count == 2


@pytest.mark.parametrize(
    "change",
    [
        {"unit": "F"},
        {"numeric_error_values": (21,)},
        {"numeric_error_states_valid": False},
        {"maximum": 20},
        {"step_size": -1},
    ],
)
async def test_same_numeric_value_in_invalid_readback_does_not_confirm(
    environment, change
):
    coordinator, client, _ = environment
    client.get_resource.return_value = replace(
        NUMBER, value=21, metadata=replace(NUMBER.metadata, **change)
    )
    with pytest.raises(WriteNotConfirmed):
        await coordinator._write_service.async_write_number(
            "synthetic-gateway", NUMBER, 21, assess_control(NUMBER).policy
        )
    assert client.put_resource_value.await_count == 1
    assert client.get_resource.await_count == 3


async def test_device_error_marker_is_never_sent_even_with_wide_bounds(environment):
    coordinator, client, _ = environment
    resource = replace(
        NUMBER,
        metadata=replace(
            NUMBER.metadata, minimum=-40000, maximum=40000, numeric_error_values=(21,)
        ),
    )
    for target in (21, 32767, -32768):
        with pytest.raises(WriteValidationError):
            await coordinator._write_service.async_write_number(
                "synthetic-gateway", resource, target, assess_control(resource).policy
            )
    client.put_resource_value.assert_not_awaited()


async def test_unknown_mode_is_neither_offered_written_nor_leaked(environment):
    coordinator, client, _ = environment
    current = replace(
        MODE,
        metadata=replace(MODE.metadata, allowed_values=("manual", "private-new-mode")),
    )
    coordinator.data = snapshots(current)
    entity = entity_for(coordinator, current)
    assert entity.options == ["manual"]
    report = _control_diagnostics(current)
    assert report["unrecognized_option_count"] == 1
    assert "private-new-mode" not in json.dumps(report)
    with pytest.raises(ServiceValidationError):
        await entity.async_select_option("private-new-mode")
    coordinator.async_set_updated_data(
        snapshots(replace(current, value="private-new-mode"))
    )
    assert not entity.available
    assert assess_control(coordinator.data[MODE.path].resource).rejection_reason == (
        "unknown_zone_mode"
    )
    client.put_resource_value.assert_not_awaited()


@pytest.mark.parametrize("resource,target", [(MODE, "auto"), (NUMBER, 21)])
async def test_matching_value_in_collection_or_wrong_zone_is_not_confirmation(
    environment, resource, target
):
    coordinator, client, _ = environment
    client.get_resource.side_effect = [
        replace(resource, value=target, has_values=True),
        replace(resource, value=target, path=resource.path.replace("zone1", "zone2")),
        replace(resource, value=target, references=(ResourceReference("/zones"),)),
    ]
    write = (
        coordinator._write_service.async_write_enum
        if resource is MODE
        else coordinator._write_service.async_write_number
    )
    with pytest.raises(WriteNotConfirmed):
        await write(
            "synthetic-gateway", resource, target, assess_control(resource).policy
        )
    client.put_resource_value.assert_awaited_once()


@pytest.mark.parametrize("resource", [MODE, NUMBER])
async def test_noop_rechecks_current_contract_before_success(environment, resource):
    _coordinator, client, _ = environment
    client.get_resource.return_value = replace(
        resource, metadata=replace(resource.metadata, writable=False)
    )
    service = WriteService(client, sleep=AsyncMock())
    write = service.async_write_enum if resource is MODE else service.async_write_number
    with pytest.raises(WriteValidationError):
        await write(
            "synthetic-gateway",
            resource,
            resource.value,
            assess_control(resource).policy,
        )
    client.put_resource_value.assert_not_awaited()


@pytest.mark.parametrize("error", [RateLimited(30), AuthenticationError("expired")])
async def test_service_errors_stop_without_repeating_commands(environment, error):
    coordinator, client, _ = environment
    client.put_resource_value.side_effect = error
    with pytest.raises(type(error)):
        await coordinator._write_service.async_write_number(
            "synthetic-gateway", NUMBER, 21, assess_control(NUMBER).policy
        )
    client.put_resource_value.assert_awaited_once()
    client.get_resource.assert_not_awaited()


async def test_queued_write_revalidates_permission_after_first_confirmation(
    environment,
):
    coordinator, client, _ = environment
    coordinator.data = snapshots(NUMBER)
    started, finish = asyncio.Event(), asyncio.Event()

    async def put(*_args):
        started.set()
        await finish.wait()

    client.put_resource_value.side_effect = put
    client.get_resource.return_value = replace(
        NUMBER, value=21, metadata=replace(NUMBER.metadata, writable=False)
    )
    policy = assess_control(NUMBER).policy
    first = asyncio.create_task(
        coordinator.async_write_control(NUMBER.path, 21, policy)
    )
    await started.wait()
    second = asyncio.create_task(
        coordinator.async_write_control(NUMBER.path, 22, policy)
    )
    await asyncio.sleep(0)
    assert client.put_resource_value.await_count == 1
    finish.set()
    assert (await first).value == 21
    with pytest.raises(WriteValidationError):
        await second
    client.put_resource_value.assert_awaited_once()
