"""Validated PointT write transactions with mandatory read-back."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .pointt import (
    InvalidPayload,
    PointTClient,
    RequestTimeout,
    Resource,
    WriteNotConfirmed,
    WriteRejected,
    WriteRequest,
    WriteResult,
    WriteValidationError,
)
from .pointt.models import JsonScalar

DEFAULT_READ_BACK_DELAY = 0.5
DEFAULT_READ_BACK_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class EnumWritePolicy:
    """Narrow allowlist for one released string-enum control."""

    path_pattern: str
    resource_types: frozenset[str]
    allowed_values: frozenset[str]


@dataclass(frozen=True, slots=True)
class NumberWritePolicy:
    """Allowlist and UI constraints for one numeric control family."""

    path_pattern: str
    unit: str
    safe_minimum: float
    safe_maximum: float
    step: float


HEATING_CIRCUIT_OPERATION_MODE_POLICY = EnumWritePolicy(
    r"^/heatingCircuits/[^/]+/operationMode$",
    frozenset({"stringValue"}),
    frozenset({"off", "manual", "auto"}),
)
DHW_OPERATION_MODE_POLICY = EnumWritePolicy(
    r"^/dhwCircuits/[^/]+/operationMode$",
    frozenset({"stringValue"}),
    frozenset({"Off", "low", "high", "ownprogram", "eco"}),
)
STRING_SWITCH_POLICIES = (
    EnumWritePolicy(
        r"^/dhwCircuits/[^/]+/charge$",
        frozenset({"stringValue"}),
        frozenset({"start", "stop"}),
    ),
    EnumWritePolicy(
        r"^/dhwCircuits/[^/]+/reduceTempOnAlarm$",
        frozenset({"stringValue"}),
        frozenset({"on", "off"}),
    ),
    EnumWritePolicy(
        r"^/system/awayMode/enabled$",
        frozenset({"stringValue"}),
        frozenset({"on", "off"}),
    ),
)
NUMBER_WRITE_POLICIES = (
    NumberWritePolicy(r"^/heatingCircuits/[^/]+/manualRoomSetpoint$", "C", 5, 30, 0.5),
    NumberWritePolicy(
        r"^/heatingCircuits/[^/]+/temperatureLevels/(?:comfort2|eco)$",
        "C",
        5,
        30,
        0.5,
    ),
    NumberWritePolicy(r"^/dhwCircuits/[^/]+/chargeDuration$", "mins", 1, 2880, 15),
    NumberWritePolicy(
        r"^/dhwCircuits/[^/]+/(?:singleChargeSetpoint|temperatureLevels/(?:eco|high|low))$",
        "C",
        20,
        70,
        1.0,
    ),
)


class WriteService:
    """Validate, write once, and confirm the resulting resource value."""

    def __init__(
        self,
        client: PointTClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        read_back_delay: float = DEFAULT_READ_BACK_DELAY,
        read_back_attempts: int = DEFAULT_READ_BACK_ATTEMPTS,
    ) -> None:
        if read_back_delay < 0:
            raise ValueError("Read-back delay must not be negative")
        if read_back_attempts < 1:
            raise ValueError("Read-back attempts must be positive")
        self._client = client
        self._sleep = sleep
        self._read_back_delay = read_back_delay
        self._read_back_attempts = read_back_attempts

    async def async_write_enum(
        self,
        gateway_id: str,
        resource: Resource,
        value: str,
        policy: EnumWritePolicy,
    ) -> WriteResult:
        """Write an allowlisted enum and return only a confirmed result."""
        request = WriteRequest(gateway_id, resource.path, value)
        _validate_enum(resource, request, policy)
        return await self._async_write(request, resource)

    async def async_write_number(
        self,
        gateway_id: str,
        resource: Resource,
        value: float,
        policy: NumberWritePolicy,
    ) -> WriteResult:
        """Write a bounded finite number and return only a confirmed result."""
        request = WriteRequest(gateway_id, resource.path, value)
        _validate_number(resource, request, policy)
        return await self._async_write(request, resource)

    async def _async_write(
        self, request: WriteRequest, resource: Resource
    ) -> WriteResult:
        value = request.value
        if _values_equal(resource.value, value):
            return WriteResult(request, resource)
        put_timed_out = False
        try:
            response = await self._client.put_resource_value(
                request.gateway_id, resource.path, value
            )
        except RequestTimeout:
            put_timed_out = True
        except InvalidPayload as err:
            raise WriteRejected("PointT returned an invalid write response") from err
        else:
            _validate_write_response(response, resource.path, value)

        last_timeout: RequestTimeout | None = None
        for attempt in range(self._read_back_attempts):
            await self._sleep(self._read_back_delay * (2**attempt))
            try:
                confirmed = await self._client.get_resource(
                    request.gateway_id, resource.path
                )
            except RequestTimeout as err:
                last_timeout = err
                continue
            if confirmed.has_value and _values_equal(confirmed.value, value):
                return WriteResult(request, confirmed, put_timed_out=put_timed_out)
        if last_timeout is not None:
            raise WriteNotConfirmed("PointT read-back timed out") from last_timeout
        raise WriteNotConfirmed("PointT read-back did not confirm the value")


def _validate_enum(
    resource: Resource, request: WriteRequest, policy: EnumWritePolicy
) -> None:
    if request.path != resource.path or not re.fullmatch(
        policy.path_pattern, resource.path
    ):
        raise WriteValidationError("Resource path is not approved for this write")
    if not resource.metadata.writable:
        raise WriteValidationError("Resource is not currently writable")
    if resource.metadata.resource_type not in policy.resource_types:
        raise WriteValidationError("Resource type is not approved for this write")
    if not resource.has_value or not isinstance(resource.value, str):
        raise WriteValidationError("Resource does not contain a string enum")
    if not isinstance(request.value, str) or request.value not in policy.allowed_values:
        raise WriteValidationError("Requested enum value is not released")
    if request.value not in resource.metadata.allowed_values:
        raise WriteValidationError("Requested enum value is not currently advertised")


def _validate_number(
    resource: Resource, request: WriteRequest, policy: NumberWritePolicy
) -> None:
    if request.path != resource.path or not re.fullmatch(
        policy.path_pattern, resource.path
    ):
        raise WriteValidationError("Resource path is not approved for this write")
    metadata = resource.metadata
    if not metadata.writable or metadata.resource_type != "floatValue":
        raise WriteValidationError("Resource is not an approved writable number")
    if metadata.unit != policy.unit:
        raise WriteValidationError("Resource unit is not approved for this write")
    minimum, maximum = metadata.minimum, metadata.maximum
    if (
        minimum is None
        or maximum is None
        or minimum < policy.safe_minimum
        or maximum > policy.safe_maximum
        or minimum > maximum
    ):
        raise WriteValidationError("Resource bounds are missing or unsafe")
    value = request.value
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise WriteValidationError("Requested number is outside current limits")
    steps = (float(value) - minimum) / policy.step
    if not math.isclose(steps, round(steps), abs_tol=1e-7):
        raise WriteValidationError("Requested number does not match the allowed step")


def enum_policy_for_resource(resource: Resource) -> EnumWritePolicy | None:
    """Return a released enum policy only when live metadata matches."""
    policies = (
        HEATING_CIRCUIT_OPERATION_MODE_POLICY,
        DHW_OPERATION_MODE_POLICY,
        *STRING_SWITCH_POLICIES,
    )
    for policy in policies:
        if (
            re.fullmatch(policy.path_pattern, resource.path)
            and resource.metadata.writable
            and resource.metadata.resource_type in policy.resource_types
            and policy.allowed_values.issubset(resource.metadata.allowed_values)
            and resource.has_value
            and isinstance(resource.value, str)
            and resource.value in policy.allowed_values
        ):
            return policy
    return None


def number_policy_for_resource(resource: Resource) -> NumberWritePolicy | None:
    """Return a released numeric policy only when live metadata is safe."""
    if isinstance(resource.value, bool) or not isinstance(resource.value, int | float):
        return None
    for policy in NUMBER_WRITE_POLICIES:
        try:
            _validate_number(
                resource,
                WriteRequest("validation", resource.path, resource.value),
                policy,
            )
        except WriteValidationError:
            continue
        return policy
    return None


def _validate_write_response(
    response: Resource | None, path: str, value: JsonScalar
) -> None:
    if response is None:
        return
    if (
        response.path != path
        or not response.has_value
        or not _values_equal(response.value, value)
    ):
        raise WriteRejected("PointT write response did not match the request")


def _values_equal(left: object, right: object) -> bool:
    if (
        not isinstance(left, bool)
        and not isinstance(right, bool)
        and isinstance(left, int | float)
        and isinstance(right, int | float)
    ):
        return math.isclose(float(left), float(right), abs_tol=1e-7)
    return left == right
