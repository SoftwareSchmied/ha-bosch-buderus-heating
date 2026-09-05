"""Tests for validated PointT write transactions."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.bosch_buderus_heating.pointt import (
    InvalidPayload,
    RequestTimeout,
    Resource,
    ResourceMetadata,
    WriteNotConfirmed,
    WriteRejected,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.writes import (
    AUXILIARY_HEATER_OPERATION_MODE_POLICY,
    HEATING_CIRCUIT_OPERATION_MODE_POLICY,
    SILENT_MODE_POLICY,
    WriteService,
    enum_policy_for_resource,
    number_policy_for_resource,
)

PATH = "/heatingCircuits/hc1/operationMode"


def _resource(
    value: str = "manual",
    *,
    path: str = PATH,
    writable: bool = True,
    resource_type: str = "stringValue",
    allowed_values: tuple[str, ...] = ("off", "manual", "auto"),
) -> Resource:
    return Resource(
        path=path,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type=resource_type,
            allowed_values=allowed_values,
            writable=writable,
        ),
    )


async def test_write_is_confirmed_only_after_read_back() -> None:
    client = AsyncMock()
    client.put_resource_value.return_value = None
    confirmed = _resource("auto")
    client.get_resource.return_value = confirmed
    sleep = AsyncMock()
    service = WriteService(client, sleep=sleep, read_back_delay=0.25)

    result = await service.async_write_enum(
        "gateway-one",
        _resource(),
        "auto",
        HEATING_CIRCUIT_OPERATION_MODE_POLICY,
    )

    client.put_resource_value.assert_awaited_once_with("gateway-one", PATH, "auto")
    sleep.assert_awaited_once_with(0.25)
    client.get_resource.assert_awaited_once_with("gateway-one", PATH)
    assert result.resource is confirmed
    assert result.request.value == "auto"
    assert not result.put_timed_out


async def test_matching_value_is_a_confirmed_noop() -> None:
    client = AsyncMock()
    current = _resource()
    client.get_resource.return_value = current

    result = await WriteService(client).async_write_enum(
        "gateway-one",
        current,
        "manual",
        HEATING_CIRCUIT_OPERATION_MODE_POLICY,
    )

    assert result.resource is current
    client.put_resource_value.assert_not_awaited()
    client.get_resource.assert_awaited_once_with("gateway-one", PATH)


async def test_matching_cache_does_not_skip_an_external_change() -> None:
    client = AsyncMock()
    client.put_resource_value.return_value = None
    client.get_resource.side_effect = [_resource("auto"), _resource("manual")]
    result = await WriteService(client, sleep=AsyncMock()).async_write_enum(
        "gateway-one",
        _resource("manual"),
        "manual",
        HEATING_CIRCUIT_OPERATION_MODE_POLICY,
    )
    client.put_resource_value.assert_awaited_once_with("gateway-one", PATH, "manual")
    assert result.resource.value == "manual"


async def test_matching_cache_revalidates_live_write_permissions() -> None:
    client = AsyncMock()
    client.get_resource.return_value = _resource("auto", writable=False)
    with pytest.raises(WriteValidationError):
        await WriteService(client).async_write_enum(
            "gateway-one",
            _resource("manual"),
            "manual",
            HEATING_CIRCUIT_OPERATION_MODE_POLICY,
        )
    client.put_resource_value.assert_not_awaited()


async def test_timed_out_put_is_not_retried_and_can_be_confirmed() -> None:
    client = AsyncMock()
    client.put_resource_value.side_effect = RequestTimeout()
    client.get_resource.return_value = _resource("auto")

    result = await WriteService(client, sleep=AsyncMock()).async_write_enum(
        "gateway-one",
        _resource(),
        "auto",
        HEATING_CIRCUIT_OPERATION_MODE_POLICY,
    )

    assert client.put_resource_value.await_count == 1
    assert result.put_timed_out


async def test_delayed_read_back_retries_only_get_requests() -> None:
    client = AsyncMock()
    client.put_resource_value.return_value = None
    client.get_resource.side_effect = (_resource("manual"), _resource("auto"))
    sleep = AsyncMock()

    result = await WriteService(
        client, sleep=sleep, read_back_delay=0.25
    ).async_write_enum(
        "gateway-one",
        _resource(),
        "auto",
        HEATING_CIRCUIT_OPERATION_MODE_POLICY,
    )

    assert result.resource.value == "auto"
    assert client.put_resource_value.await_count == 1
    assert client.get_resource.await_count == 2
    assert [call.args[0] for call in sleep.await_args_list] == [0.25, 0.5]


@pytest.mark.parametrize(
    "response",
    [
        Resource(path=PATH),
        _resource("manual"),
        _resource("auto", path="/heatingCircuits/hc2/operationMode"),
    ],
)
async def test_invalid_write_echo_is_rejected(response: Resource) -> None:
    client = AsyncMock()
    client.put_resource_value.return_value = response

    with pytest.raises(WriteRejected):
        await WriteService(client).async_write_enum(
            "gateway-one",
            _resource(),
            "auto",
            HEATING_CIRCUIT_OPERATION_MODE_POLICY,
        )
    client.get_resource.assert_not_awaited()


async def test_invalid_write_payload_is_rejected_without_read_back() -> None:
    client = AsyncMock()
    client.put_resource_value.side_effect = InvalidPayload("bad response")

    with pytest.raises(WriteRejected):
        await WriteService(client).async_write_enum(
            "gateway-one",
            _resource(),
            "auto",
            HEATING_CIRCUIT_OPERATION_MODE_POLICY,
        )


@pytest.mark.parametrize(
    ("resource", "value"),
    [
        (_resource(path="/dhwCircuits/dhw1/operationMode"), "auto"),
        (_resource(writable=False), "auto"),
        (_resource(resource_type="floatValue"), "auto"),
        (Resource(path=PATH, metadata=ResourceMetadata(writable=True)), "auto"),
        (_resource(), "holiday"),
        (_resource(allowed_values=("off", "manual")), "auto"),
    ],
)
async def test_write_validation_blocks_unreleased_shapes(
    resource: Resource, value: str
) -> None:
    client = AsyncMock()

    with pytest.raises(WriteValidationError):
        await WriteService(client).async_write_enum(
            "gateway-one",
            resource,
            value,
            HEATING_CIRCUIT_OPERATION_MODE_POLICY,
        )
    client.put_resource_value.assert_not_awaited()


async def test_unconfirmed_or_timed_out_read_back_fails() -> None:
    client = AsyncMock()
    client.put_resource_value.return_value = None
    client.get_resource.return_value = _resource("manual")
    service = WriteService(client, sleep=AsyncMock())

    with pytest.raises(WriteNotConfirmed):
        await service.async_write_enum(
            "gateway-one",
            _resource(),
            "auto",
            HEATING_CIRCUIT_OPERATION_MODE_POLICY,
        )

    client.get_resource.side_effect = RequestTimeout()
    with pytest.raises(WriteNotConfirmed):
        await service.async_write_enum(
            "gateway-one",
            _resource(),
            "auto",
            HEATING_CIRCUIT_OPERATION_MODE_POLICY,
        )


def test_negative_read_back_delay_is_rejected() -> None:
    with pytest.raises(ValueError):
        WriteService(AsyncMock(), read_back_delay=-0.1)
    with pytest.raises(ValueError):
        WriteService(AsyncMock(), read_back_attempts=0)


def _number_resource(
    value: float = 20.0,
    *,
    path: str = "/heatingCircuits/hc1/manualRoomSetpoint",
    unit: str = "C",
    minimum: float | None = 5.0,
    maximum: float | None = 30.0,
    writable: bool = True,
) -> Resource:
    return Resource(
        path=path,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="floatValue",
            unit=unit,
            minimum=minimum,
            maximum=maximum,
            writable=writable,
        ),
    )


@pytest.mark.parametrize("live", [20.0, 22.0])
async def test_matching_number_cache_is_checked_against_cloud(live: float) -> None:
    client = AsyncMock()
    cached = _number_resource(20.0)
    policy = number_policy_for_resource(cached)
    assert policy is not None
    client.put_resource_value.return_value = None
    client.get_resource.side_effect = [_number_resource(live), cached]
    result = await WriteService(client, sleep=AsyncMock()).async_write_number(
        "gateway-one", cached, 20.0, policy
    )
    assert result.resource.value == 20.0
    assert client.put_resource_value.await_count == int(live != 20.0)
    assert client.get_resource.await_count == 1 + int(live != 20.0)


async def test_number_write_is_bounded_and_confirmed() -> None:
    client = AsyncMock()
    current = _number_resource()
    confirmed = _number_resource(20.5)
    client.put_resource_value.return_value = None
    client.get_resource.return_value = confirmed
    policy = number_policy_for_resource(current)
    assert policy is not None

    result = await WriteService(client, sleep=AsyncMock()).async_write_number(
        "gateway-one", current, 20.5, policy
    )

    assert result.resource is confirmed
    client.put_resource_value.assert_awaited_once_with(
        "gateway-one", current.path, 20.5
    )


@pytest.mark.parametrize(
    ("resource", "value"),
    [
        (_number_resource(writable=False), 20.5),
        (_number_resource(unit="bar"), 20.5),
        (_number_resource(minimum=None), 20.5),
        (_number_resource(maximum=None), 20.5),
        (_number_resource(minimum=0), 20.5),
        (_number_resource(maximum=40), 20.5),
        (_number_resource(), 30.5),
        (_number_resource(), 20.25),
        (_number_resource(), float("inf")),
    ],
)
async def test_number_write_rejects_unsafe_metadata_or_value(
    resource: Resource, value: float
) -> None:
    client = AsyncMock()
    policy = next(
        policy
        for candidate in (_number_resource(),)
        if (policy := number_policy_for_resource(candidate)) is not None
    )

    with pytest.raises(WriteValidationError):
        await WriteService(client).async_write_number(
            "gateway-one", resource, value, policy
        )
    client.put_resource_value.assert_not_awaited()


def test_policy_discovery_excludes_administrative_writes() -> None:
    enum_resource = _resource()
    admin = Resource(
        path="/gateway/tosAccepted",
        value="true",
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="stringValue",
            allowed_values=("true", "false"),
            writable=True,
        ),
    )

    assert enum_policy_for_resource(enum_resource) is not None
    silent_mode = _resource(
        "auto",
        path="/system/silentMode/enabled",
        allowed_values=("off", "auto", "on"),
    )
    assert enum_policy_for_resource(silent_mode) is SILENT_MODE_POLICY
    auxiliary_heater_mode = _resource(
        "off",
        path="/heatSources/additionalHeater/operationMode",
        allowed_values=("off", "manual", "auto"),
    )
    assert (
        enum_policy_for_resource(auxiliary_heater_mode)
        is AUXILIARY_HEATER_OPERATION_MODE_POLICY
    )
    assert enum_policy_for_resource(admin) is None
    assert number_policy_for_resource(_number_resource()) is not None
    maximum_supply = _number_resource(
        40.0,
        path="/heatingCircuits/hc1/maxFlowTemp",
        minimum=30.0,
        maximum=60.0,
    )
    maximum_supply_policy = number_policy_for_resource(maximum_supply)
    assert maximum_supply_policy is not None
    assert maximum_supply_policy.step == 1.0
    assert maximum_supply_policy.safe_minimum == 0
    assert maximum_supply_policy.safe_maximum == 100

    other_system = _number_resource(
        55.0,
        path="/heatingCircuits/hc7/maxFlowTemp",
        minimum=20.0,
        maximum=80.0,
    )
    assert number_policy_for_resource(other_system) is maximum_supply_policy
