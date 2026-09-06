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
    require_all_options: bool = True


@dataclass(frozen=True, slots=True)
class NumberWritePolicy:
    """Allowlist and UI constraints for one numeric control family."""

    path_pattern: str
    unit: str
    safe_minimum: float
    safe_maximum: float
    step: float
    enabled_by_default: bool = True


HEATING_CIRCUIT_OPERATION_MODE_POLICY = EnumWritePolicy(
    r"^/heatingCircuits/[^/]+/operationMode$",
    frozenset({"stringValue"}),
    frozenset({"off", "manual", "auto"}),
    require_all_options=False,
)
DHW_OPERATION_MODE_POLICY = EnumWritePolicy(
    r"^/dhwCircuits/[^/]+/operationMode$",
    frozenset({"stringValue"}),
    frozenset({"Off", "low", "high", "ownprogram", "eco"}),
)
SILENT_MODE_POLICY = EnumWritePolicy(
    r"^/system/silentMode/enabled$",
    frozenset({"stringValue"}),
    frozenset({"off", "auto", "on"}),
)
AUXILIARY_HEATER_OPERATION_MODE_POLICY = EnumWritePolicy(
    r"^/heatSources/additionalHeater/operationMode$",
    frozenset({"stringValue"}),
    frozenset({"off", "manual", "auto"}),
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
    # The user-visible range comes from the individual gateway. The broad
    # envelope rejects corrupt metadata without imposing the K40 test system's
    # 30-60 °C limits on other heating systems.
    NumberWritePolicy(
        r"^/heatingCircuits/[^/]+/maxFlowTemp$",
        "C",
        0,
        100,
        1.0,
        enabled_by_default=False,
    ),
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


@dataclass(frozen=True, slots=True)
class ControlAssessment:
    """A scalar control's policy and first failed capability check."""

    policy: EnumWritePolicy | NumberWritePolicy | None
    rejection_reason: str | None

    @property
    def eligible(self) -> bool:
        return self.policy is not None and self.rejection_reason is None

    @property
    def platform(self) -> str | None:
        if isinstance(self.policy, NumberWritePolicy):
            return "number"
        if self.policy is None:
            return None
        return "switch" if self.policy in STRING_SWITCH_POLICIES else "select"

    @property
    def enabled_by_default(self) -> bool | None:
        if isinstance(self.policy, NumberWritePolicy):
            return self.policy.enabled_by_default
        return True if self.policy is not None else None


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
        if _values_equal(resource.value, value):
            resource = await self._client.get_resource(gateway_id, resource.path)
            _validate_enum(resource, request, policy)
            if _values_equal(resource.value, value):
                return WriteResult(request, resource)
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
        if _values_equal(resource.value, value):
            resource = await self._client.get_resource(gateway_id, resource.path)
            _validate_number(resource, request, policy)
            if _values_equal(resource.value, value):
                return WriteResult(request, resource)
        return await self._async_write(request, resource)

    async def _async_write(
        self, request: WriteRequest, resource: Resource
    ) -> WriteResult:
        value = request.value
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
    reason = _number_rejection_reason(resource, request.value, policy)
    if reason is not None:
        raise WriteValidationError(f"Numeric control validation failed: {reason}")


def _number_rejection_reason(
    resource: Resource, value: object, policy: NumberWritePolicy
) -> str | None:
    """Share numeric validation with control creation and diagnostics."""
    metadata = resource.metadata
    if not metadata.writable:
        return "not_writable"
    if metadata.resource_type != "floatValue":
        return "unsupported_resource_type"
    if metadata.unit != policy.unit:
        return "unsupported_unit"
    minimum, maximum = metadata.minimum, metadata.maximum
    if minimum is None or maximum is None:
        return "missing_bounds"
    if not math.isfinite(minimum) or not math.isfinite(maximum):
        return "non_finite_bounds"
    if minimum > maximum:
        return "inverted_bounds"
    if minimum < policy.safe_minimum or maximum > policy.safe_maximum:
        return "unsafe_bounds"
    if value is None:
        return "missing_value"
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "invalid_value_type"
    if not math.isfinite(value):
        return "non_finite_value"
    if not minimum <= value <= maximum:
        return "value_out_of_bounds"
    steps = (float(value) - minimum) / policy.step
    if not math.isclose(steps, round(steps), abs_tol=1e-7):
        return "value_off_step"
    return None


def _enum_rejection_reason(resource: Resource, policy: EnumWritePolicy) -> str | None:
    """Validate only released options actually advertised by this resource."""
    metadata = resource.metadata
    if not metadata.writable:
        return "not_writable"
    if metadata.resource_type not in policy.resource_types:
        return "unsupported_resource_type"
    if not policy.allowed_values.intersection(metadata.allowed_values):
        return "no_supported_options"
    if policy.require_all_options and not policy.allowed_values.issubset(
        metadata.allowed_values
    ):
        return "incomplete_options"
    if not resource.has_value or resource.value is None:
        return "missing_value"
    if not isinstance(resource.value, str):
        return "invalid_value_type"
    if resource.value not in policy.allowed_values:
        return "unsupported_current_option"
    if resource.value not in metadata.allowed_values:
        return "current_option_not_advertised"
    return None


def assess_control(resource: Resource) -> ControlAssessment:
    """Assess scalar controls without cloud requests or exposing raw values."""
    policies = (
        HEATING_CIRCUIT_OPERATION_MODE_POLICY,
        DHW_OPERATION_MODE_POLICY,
        SILENT_MODE_POLICY,
        AUXILIARY_HEATER_OPERATION_MODE_POLICY,
        *STRING_SWITCH_POLICIES,
    )
    for policy in policies:
        if re.fullmatch(policy.path_pattern, resource.path):
            return ControlAssessment(policy, _enum_rejection_reason(resource, policy))
    for number_policy in NUMBER_WRITE_POLICIES:
        if re.fullmatch(number_policy.path_pattern, resource.path):
            return ControlAssessment(
                number_policy,
                _number_rejection_reason(
                    resource,
                    resource.value if resource.has_value else None,
                    number_policy,
                ),
            )
    return ControlAssessment(None, "no_scalar_control_policy")


def enum_policy_for_resource(resource: Resource) -> EnumWritePolicy | None:
    """Return a released enum policy only when live metadata matches."""
    assessment = assess_control(resource)
    if assessment.eligible and isinstance(assessment.policy, EnumWritePolicy):
        return assessment.policy
    return None


def number_policy_for_resource(resource: Resource) -> NumberWritePolicy | None:
    """Return a released numeric policy only when live metadata is safe."""
    assessment = assess_control(resource)
    if assessment.eligible and isinstance(assessment.policy, NumberWritePolicy):
        return assessment.policy
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
