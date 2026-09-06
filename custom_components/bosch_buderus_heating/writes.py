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
    ServiceUnavailable,
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
    """Identify a supported enum function and its known presentation codes."""

    path_pattern: str
    resource_types: frozenset[str]
    known_values: frozenset[str]


@dataclass(frozen=True, slots=True)
class NumberWritePolicy:
    """Allowlist and UI constraints for one numeric control family."""

    path_pattern: str
    unit: str
    step: float
    enabled_by_default: bool = True


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
    NumberWritePolicy(r"^/heatingCircuits/[^/]+/manualRoomSetpoint$", "C", 0.5),
    NumberWritePolicy(
        r"^/heatingCircuits/[^/]+/maxFlowTemp$",
        "C",
        1.0,
        enabled_by_default=False,
    ),
    NumberWritePolicy(
        r"^/heatingCircuits/[^/]+/temperatureLevels/(?:comfort2|eco)$",
        "C",
        0.5,
    ),
    NumberWritePolicy(r"^/dhwCircuits/[^/]+/chargeDuration$", "mins", 15),
    NumberWritePolicy(
        r"^/dhwCircuits/[^/]+/(?:singleChargeSetpoint|temperatureLevels/(?:eco|high|low))$",
        "C",
        1.0,
    ),
)


@dataclass(frozen=True, slots=True)
class ControlAssessment:
    """One resource's current write contract, shared by UI and transactions."""

    policy: EnumWritePolicy | NumberWritePolicy | None
    rejection_reason: str | None
    writable: bool = False
    options: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    unit: str | None = None
    ui_step: float | None = None
    current_value_issue: str | None = None

    @property
    def eligible(self) -> bool:
        return self.policy is not None and self.rejection_reason is None

    @property
    def platform(self) -> str | None:
        if isinstance(self.policy, NumberWritePolicy):
            return "number"
        if self.policy is None:
            return None
        return "switch" if self.supports_switch else "select"

    @property
    def supports_switch(self) -> bool:
        """Keep legacy switches only when both exact actions are advertised."""
        return (
            isinstance(self.policy, EnumWritePolicy)
            and self.policy in STRING_SWITCH_POLICIES
            and self.policy.known_values.issubset(self.options)
        )

    @property
    def supports_select(self) -> bool:
        """Offer variants of switch functions without changing switch IDs."""
        return isinstance(self.policy, EnumWritePolicy) and (
            self.policy not in STRING_SWITCH_POLICIES
            or set(self.options) != self.policy.known_values
        )

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
        except (RequestTimeout, ServiceUnavailable) as err:
            # A lost connection or server error may follow an applied PUT.
            # Resolve the outcome by reading, never by sending another PUT.
            put_timed_out = isinstance(err, RequestTimeout)
        except InvalidPayload as err:
            raise WriteRejected("PointT returned an invalid write response") from err
        else:
            _validate_write_response(response, resource.path, value)

        last_temporary_error: RequestTimeout | ServiceUnavailable | None = None
        for attempt in range(self._read_back_attempts):
            await self._sleep(self._read_back_delay * (2**attempt))
            try:
                confirmed = await self._client.get_resource(
                    request.gateway_id, resource.path
                )
            except (RequestTimeout, ServiceUnavailable) as err:
                last_temporary_error = err
                continue
            if (
                confirmed.path == request.path
                and confirmed.has_value
                and _values_equal(confirmed.value, value)
            ):
                return WriteResult(request, confirmed, put_timed_out=put_timed_out)
        if last_temporary_error is not None:
            raise WriteNotConfirmed("PointT read-back could not be completed") from (
                last_temporary_error
            )
        raise WriteNotConfirmed("PointT read-back did not confirm the value")


def _validate_enum(
    resource: Resource, request: WriteRequest, policy: EnumWritePolicy
) -> None:
    if request.path != resource.path or not re.fullmatch(
        policy.path_pattern, resource.path
    ):
        raise WriteValidationError("Resource path is not approved for this write")
    assessment = assess_control(resource)
    if not assessment.eligible or assessment.policy != policy:
        raise WriteValidationError("Resource does not advertise an enum write contract")
    if not isinstance(request.value, str) or request.value not in assessment.options:
        raise WriteValidationError("Requested enum value is not currently advertised")


def _validate_number(
    resource: Resource, request: WriteRequest, policy: NumberWritePolicy
) -> None:
    if request.path != resource.path or not re.fullmatch(
        policy.path_pattern, resource.path
    ):
        raise WriteValidationError("Resource path is not approved for this write")
    assessment = assess_control(resource)
    if not assessment.eligible or assessment.policy != policy:
        raise WriteValidationError(
            "Resource does not advertise a numeric write contract"
        )
    reason = _number_value_rejection_reason(resource, request.value)
    if reason is not None:
        raise WriteValidationError(f"Numeric control validation failed: {reason}")


def _number_rejection_reason(
    resource: Resource, policy: NumberWritePolicy
) -> str | None:
    """Validate the advertised numeric contract independently of the state."""
    metadata = resource.metadata
    if not metadata.writable:
        return "not_writable"
    if metadata.resource_type != "floatValue":
        return "unsupported_resource_type"
    supported_units = (
        {"C", "F", "K", "°C", "°F"} if policy.unit == "C" else {"mins", "min", "s", "h"}
    )
    if metadata.unit not in supported_units:
        return "unsupported_unit"
    minimum, maximum = metadata.minimum, metadata.maximum
    if minimum is None or maximum is None:
        return "missing_bounds"
    if not math.isfinite(minimum) or not math.isfinite(maximum):
        return "non_finite_bounds"
    if minimum > maximum:
        return "inverted_bounds"
    return None


def _number_value_rejection_reason(resource: Resource, value: object) -> str | None:
    """Validate a target against gateway bounds; UI steps are not API rules."""
    if value is None:
        return "missing_value"
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "invalid_value_type"
    if not math.isfinite(value):
        return "non_finite_value"
    minimum, maximum = resource.metadata.minimum, resource.metadata.maximum
    if minimum is not None and maximum is not None and not minimum <= value <= maximum:
        return "value_out_of_bounds"
    return None


def _enum_rejection_reason(resource: Resource, policy: EnumWritePolicy) -> str | None:
    """Accept each advertised string code without a reference-device allowlist."""
    metadata = resource.metadata
    if not metadata.writable:
        return "not_writable"
    if metadata.resource_type not in policy.resource_types:
        return "unsupported_resource_type"
    if not metadata.allowed_values:
        return "missing_options"
    if any(
        not isinstance(value, str) or not value or not value.isprintable()
        for value in metadata.allowed_values
    ):
        return "invalid_options"
    return None


def _enum_current_value_issue(resource: Resource) -> str | None:
    if not resource.has_value or resource.value is None:
        return "missing_value"
    if not isinstance(resource.value, str):
        return "invalid_value_type"
    if resource.value not in resource.metadata.allowed_values:
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
            return ControlAssessment(
                policy,
                _enum_rejection_reason(resource, policy),
                writable=resource.metadata.writable,
                options=tuple(
                    dict.fromkeys(
                        value
                        for value in resource.metadata.allowed_values
                        if isinstance(value, str) and value and value.isprintable()
                    )
                ),
                current_value_issue=_enum_current_value_issue(resource),
            )
    for number_policy in NUMBER_WRITE_POLICIES:
        if re.fullmatch(number_policy.path_pattern, resource.path):
            return ControlAssessment(
                number_policy,
                _number_rejection_reason(resource, number_policy),
                writable=resource.metadata.writable,
                minimum=resource.metadata.minimum,
                maximum=resource.metadata.maximum,
                unit=resource.metadata.unit,
                ui_step=number_policy.step,
                current_value_issue=_number_value_rejection_reason(
                    resource, resource.value if resource.has_value else None
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
