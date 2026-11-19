"""Synthetic softener write tests, including ambiguous one-shot outcomes."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.bosch_buderus_heating.button import (
    BoschBuderusRegenerationButton,
    async_setup_entry,
    build_button_descriptions,
)
from custom_components.bosch_buderus_heating.coordinator import (
    Freshness,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.diagnostics import _control_diagnostics
from custom_components.bosch_buderus_heating.number import (
    BoschBuderusNumber,
    build_number_descriptions,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    RateLimited,
    RequestTimeout,
    ResourceMetadata,
    ServiceUnavailable,
    TransportError,
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.pointt.models import BatchItemResult
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.select import build_select_descriptions
from custom_components.bosch_buderus_heating.switch import (
    BoschBuderusSwitch,
    build_switch_descriptions,
)
from custom_components.bosch_buderus_heating.writes import (
    SOFTENER_HOLIDAY_POLICY,
    SOFTENER_REGENERATION_POLICY,
    WriteService,
    assess_control,
    number_policy_for_resource,
)
from tests.test_water_softener import coordinator_for, reading


def writable(
    suffix="regeneration/enabled",
    value="off",
    *,
    unit=None,
    options=("on", "off"),
    minimum=5,
    maximum=90,
):
    resource = reading(suffix, value, unit)
    return replace(
        resource,
        metadata=replace(
            resource.metadata,
            writable=True,
            allowed_values=options,
            minimum=minimum,
            maximum=maximum,
        ),
    )


def serve_live(coordinator, resource):
    """L16: what the gateway answers to the live read before a write."""
    coordinator.client.get_resource.return_value = resource
    coordinator.client.get_resources_bulk.side_effect = lambda gateway, paths: tuple(
        BatchItemResult(gateway, path, 200, resource=resource) for path in paths
    )


def writer(client):
    return WriteService(
        client, sleep=AsyncMock(), read_back_delay=0, read_back_attempts=2
    )


@pytest.mark.parametrize(
    "response_error", [None, RequestTimeout(), ServiceUnavailable(), TransportError()]
)
async def test_start_once_and_confirm_after_lost_response(response_error):
    current = writable()
    client = AsyncMock()
    client.put_resource_value.return_value = None
    client.put_resource_value.side_effect = response_error
    client.get_resource.return_value = replace(current, value="on")
    result = await writer(client).async_write_enum(
        "softener-a", current, "on", SOFTENER_REGENERATION_POLICY
    )
    assert result.resource.value == "on"
    client.put_resource_value.assert_awaited_once_with("softener-a", current.path, "on")
    assert all(
        call.args == ("softener-a", current.path)
        for call in client.get_resource.await_args_list
    )


async def test_unconfirmed_start_never_retries_command():
    current = writable()
    client = AsyncMock()
    client.put_resource_value.side_effect = RequestTimeout()
    client.get_resource.return_value = current
    with pytest.raises(WriteNotConfirmed):
        await writer(client).async_write_enum(
            "softener", current, "on", SOFTENER_REGENERATION_POLICY
        )
    client.put_resource_value.assert_awaited_once()
    assert client.get_resource.await_count == 2


@pytest.mark.parametrize("error", [AuthenticationError(), RateLimited(90)])
async def test_start_respects_auth_and_rate_limit(error):
    current = writable()
    client = AsyncMock()
    client.put_resource_value.side_effect = error
    with pytest.raises(type(error)):
        await writer(client).async_write_enum(
            "softener", current, "on", SOFTENER_REGENERATION_POLICY
        )
    client.put_resource_value.assert_awaited_once()
    client.get_resource.assert_not_awaited()


@pytest.mark.parametrize("value", ["off", "reset", "unexpected"])
async def test_start_policy_does_not_offer_unproven_cancel_or_unknown_command(value):
    current = writable(options=("on", "off", "reset", "unexpected"))
    client = AsyncMock()
    with pytest.raises(WriteValidationError):
        await writer(client).async_write_enum(
            "softener", current, value, SOFTENER_REGENERATION_POLICY
        )
    client.put_resource_value.assert_not_awaited()


@pytest.mark.parametrize("value", [None, "unknown", True])
def test_no_command_when_current_state_is_unknown(value):
    resource = writable(value=value)
    assert build_button_descriptions({resource.path: resource}) == ()


def test_action_does_not_become_select_or_switch():
    resource = writable()
    resources = {resource.path: resource}
    assert len(build_button_descriptions(resources)) == 1
    assert build_select_descriptions(resources) == ()
    assert build_switch_descriptions(resources) == ()
    assert assess_control(resource).platform == "button"


@pytest.mark.parametrize("value", [5, 5.1, 42.5, 90])
async def test_percent_threshold_uses_live_bounds_without_invented_step(value):
    resource = writable("sensor/saltLevelAlarm", 25, unit="%")
    client = AsyncMock()
    client.put_resource_value.return_value = None
    client.get_resource.return_value = replace(resource, value=value)
    result = await writer(client).async_write_number(
        "s", resource, value, number_policy_for_resource(resource)
    )
    assert result.resource.value == value
    client.put_resource_value.assert_awaited_once_with("s", resource.path, value)


@pytest.mark.parametrize(
    "value", [-1, 4.99, 90.01, float("nan"), float("inf"), True, "20"]
)
async def test_invalid_threshold_never_writes(value):
    resource = writable("sensor/saltLevelAlarm", 25, unit="%")
    client = AsyncMock()
    with pytest.raises(WriteValidationError):
        await writer(client).async_write_number(
            "s", resource, value, number_policy_for_resource(resource)
        )
    client.put_resource_value.assert_not_awaited()


@pytest.mark.parametrize(
    "minimum,maximum,unit",
    [(None, 100, "%"), (-1, 90, "%"), (5, 101, "%"), (5, 90, "kWh"), (90, 5, "%")],
)
def test_rejects_invalid_softener_number_contract(minimum, maximum, unit):
    resource = writable(
        "sensor/saltLevelAlarm", 25, unit=unit, minimum=minimum, maximum=maximum
    )
    assert number_policy_for_resource(resource) is None


async def test_holiday_mode_rejects_unoffered_codes_and_confirms_write():
    resource = writable(
        "holidayMode/enabled", "off", options=("off", "on", "vendor_unknown")
    )
    client = AsyncMock()
    client.put_resource_value.return_value = None
    client.get_resource.return_value = replace(resource, value="on")
    service = writer(client)
    with pytest.raises(WriteValidationError):
        await service.async_write_enum(
            "s", resource, "vendor_unknown", SOFTENER_HOLIDAY_POLICY
        )
    await service.async_write_enum("s", resource, "on", SOFTENER_HOLIDAY_POLICY)
    assert len(build_switch_descriptions({resource.path: resource})) == 1
    client.put_resource_value.assert_awaited_once()


def test_number_units_defaults_and_runtime_metadata_change(hass):
    resource = writable("sensor/saltLevelAlarm", 25)
    resources = {resource.path: resource}
    coordinator = coordinator_for(hass, resources)
    entity = BoschBuderusNumber(coordinator, build_number_descriptions(resources)[0])
    assert entity.native_unit_of_measurement == "%"
    assert not entity.entity_description.entity_registry_enabled_default
    changed = replace(
        resource, metadata=replace(resource.metadata, minimum=10, maximum=80)
    )
    coordinator.data[resource.path] = ResourceSnapshot(changed, True, datetime.now(UTC))
    assert entity.native_min_value == 10 and entity.native_max_value == 80
    scheduled = writable("regeneration/singleRegAppoint", 3, minimum=1, maximum=15)
    descriptions = build_number_descriptions({scheduled.path: scheduled})
    assert descriptions[0].native_unit_of_measurement == "d"
    assert not descriptions[0].entity_registry_enabled_default


async def test_button_uses_coordinator_and_shared_device_and_unregisters(hass):
    resource = writable()
    coordinator = coordinator_for(hass, {resource.path: resource})
    coordinator.async_write_control = AsyncMock()
    entry = SimpleNamespace(
        async_on_unload=Mock(),
        runtime_data=SimpleNamespace(coordinators=(coordinator,)),
    )
    entities = []
    await async_setup_entry(hass, entry, entities.extend)
    button = entities[0]
    assert isinstance(button, BoschBuderusRegenerationButton)
    assert button.available
    assert not button.entity_description.entity_registry_enabled_default
    assert button.device_info["identifiers"] == {
        (coordinator._config_entry.domain, "softener")
    }
    await button.async_press()
    coordinator.async_write_control.assert_awaited_once_with(
        resource.path, "on", SOFTENER_REGENERATION_POLICY
    )
    coordinator.data[resource.path] = replace(
        coordinator.data[resource.path], freshness=Freshness.STALE
    )
    assert not button.available
    entry.async_on_unload.call_args.args[0]()


async def test_read_only_and_withdrawn_metadata_never_write(hass):
    current = writable()
    coordinator = coordinator_for(hass, {current.path: current})
    withdrawn = replace(
        current, metadata=ResourceMetadata(resource_type="stringValue", writable=False)
    )
    coordinator.data[current.path] = ResourceSnapshot(
        withdrawn, True, datetime.now(UTC)
    )
    # L16: the gateway's live reading says the same.
    serve_live(coordinator, withdrawn)
    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(
            current.path, "on", SOFTENER_REGENERATION_POLICY
        )
    coordinator.client.put_resource_value.assert_not_awaited()


@pytest.mark.parametrize(
    "suffix,current,target,entity_class,build",
    [
        (
            "sensor/saltLevelAlarm",
            25,
            30,
            BoschBuderusNumber,
            build_number_descriptions,
        ),
        (
            "regeneration/singleRegAppoint",
            3,
            4,
            BoschBuderusNumber,
            build_number_descriptions,
        ),
        (
            "regeneration/enabled",
            "off",
            "on",
            BoschBuderusRegenerationButton,
            build_button_descriptions,
        ),
        (
            "holidayMode/enabled",
            "off",
            "on",
            BoschBuderusSwitch,
            build_switch_descriptions,
        ),
    ],
)
@pytest.mark.parametrize("shape", ["references", "values", "compound_value"])
async def test_changed_wire_schema_blocks_existing_controls_and_recovers(
    hass, suffix, current, target, entity_class, build, shape
):
    path = f"/swCircuits/sw1/{suffix}"
    payload = {
        "id": path,
        "type": "stringValue" if isinstance(current, str) else "floatValue",
        "value": current,
        "writeable": 1,
        "minValue": 0,
        "maxValue": 90,
        "allowedValues": ["off", "on"],
    }
    good = parse_resource(payload)
    coordinator = coordinator_for(hass, {path: good})
    entity = entity_class(coordinator, build({path: good})[0])
    identity = entity.unique_id
    policy = assess_control(good).policy
    assert entity.available
    if shape == "references":
        payload["references"] = [{"id": path + "/details"}]
    elif shape == "values":
        payload["values"] = [current]
    else:
        payload["value"] = {"value": current}
    bad = parse_resource(payload)
    coordinator.data[path] = ResourceSnapshot(bad, True, datetime.now(UTC))
    serve_live(coordinator, bad)
    assert not entity.available
    assert build({path: bad}) == ()
    diagnosis = _control_diagnostics(bad)
    assert not diagnosis["eligible"]
    assert diagnosis["rejection_reason"] == (
        "unsupported_resource_type"
        if shape == "references"
        else "unsupported_value_shape"
    )
    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(path, target, policy)
    coordinator.client.put_resource_value.assert_not_awaited()
    coordinator.client.get_resource.assert_not_awaited()
    coordinator.data[path] = ResourceSnapshot(good, True, datetime.now(UTC))
    assert entity.available
    assert entity.unique_id == identity


@pytest.mark.parametrize(
    "suffix,current",
    [
        ("sensor/saltLevelAlarm", 25),
        ("regeneration/singleRegAppoint", 3),
        ("regeneration/enabled", "on"),
        ("holidayMode/enabled", "on"),
    ],
)
async def test_same_value_recheck_rejects_changed_softener_schema(
    hass, suffix, current
):
    good = writable(suffix, current, minimum=0)
    coordinator = coordinator_for(hass, {good.path: good})
    serve_live(coordinator, replace(good, values=(current,)))
    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(
            good.path, current, assess_control(good).policy
        )
    coordinator.client.get_resources_bulk.assert_awaited_once_with(
        "softener", (good.path,)
    )
    coordinator.client.put_resource_value.assert_not_awaited()
