"""Validated PointT write transactions with mandatory read-back."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from functools import lru_cache

from . import rrc, rrc_contract_zone, rrc_contracts
from .legacy_holidays import WDDW2_HOLIDAY_PATH
from .pointt import (
    InvalidPayload,
    PointTClient,
    PointTError,
    RateLimited,
    RequestTimeout,
    Resource,
    ServiceUnavailable,
    TransportError,
    WriteError,
    WriteNotConfirmed,
    WriteRejected,
    WriteRequest,
    WriteResult,
    WriteValidationError,
)
from .pointt.models import JsonScalar
from .rrc_contracts import ContractConflict
from .unit_contracts import DURATION_UNITS, TEMPERATURE_UNITS
from .water_softener import softener_capability, softener_schema_issue, softener_unit
from .write_policies import DEFAULT_READ_BACK_ATTEMPTS as DEFAULT_READ_BACK_ATTEMPTS
from .write_policies import DEFAULT_READ_BACK_DELAY as DEFAULT_READ_BACK_DELAY
from .write_policies import GENERIC_CONTEXT as GENERIC_CONTEXT
from .write_policies import RRC_CONTEXT as RRC_CONTEXT
from .write_policies import RRC_READ_BACK_DELAYS as RRC_READ_BACK_DELAYS
from .write_policies import ContractRejected as ContractRejected
from .write_policies import ControlContext as ControlContext
from .write_policies import EnumWritePolicy as EnumWritePolicy
from .write_policies import NumberWritePolicy as NumberWritePolicy
from .write_policies import RrcWriteNotConfirmed as RrcWriteNotConfirmed
from .write_policies import WritePolicy as WritePolicy
from .write_policies import context_of as context_of
from .zone_controls import (
    ZONE_MANUAL_PATTERN,
    ZONE_MODE_PATTERN,
    ZONE_MODES,
    is_zone_control,
    is_zone_temporary,
    zone_control_issue,
)

# C34: broad plausibility limits that only reject corrupt metadata, such as a
# placeholder maximum of 6553.5 °C. They prescribe no reference-device range.
PLAUSIBLE_TEMPERATURE_RANGE_C = (-30.0, 100.0)
PLAUSIBLE_DURATION_RANGE_MIN = (0.0, 2880.0)
_CELSIUS_UNITS = frozenset({"C", "degC", "°C", "ºC"})


class WriteBusy(WriteError):
    """C58: a poll or another write held the gateway too long; nothing was sent."""


class ControlLocked(WriteValidationError):
    """#113: the controller shows the setting but locks it for now."""


class ControlUnused(ControlLocked):
    """The controller offers the setting but does not use it now (``used``
    false); it stays shown and is not written."""


ZONE_OPERATION_MODE_POLICY = EnumWritePolicy(
    ZONE_MODE_PATTERN,
    frozenset({"stringValue"}),
    ZONE_MODES,
    known_only=True,
)
# #113: the TC100.2 advertises no allowedValues for userMode; its app offers
# exactly the two fallback modes.
RRC_OPERATION_MODE_POLICY = EnumWritePolicy(
    rrc.MODE_PATTERN,
    frozenset({"stringValue"}),
    rrc.MODES,
    known_only=True,
    fallback_options=tuple(str(mode) for mode in rrc.FALLBACK_MODES),
    # ZONE-05: no change while the zone is unreachable or in standby.
    value_check=rrc_contract_zone.mode_value_check,
)
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
SEASON_SWITCHING_POLICY = EnumWritePolicy(
    r"^/system/seasonOptimizer/mode$",
    frozenset({"stringValue"}),
    frozenset({"off", "automatic", "forcedHeat", "forcedCool"}),
)
AUXILIARY_HEATER_OPERATION_MODE_POLICY = EnumWritePolicy(
    r"^/heatSources/additionalHeater/operationMode$",
    frozenset({"stringValue"}),
    frozenset({"off", "manual", "auto"}),
)
SOFTENER_REGENERATION_POLICY = EnumWritePolicy(
    r"^/swCircuits/[^/]+/regeneration/enabled$",
    frozenset({"stringValue"}),
    frozenset({"on"}),
    known_only=True,
    action=True,
    enabled_by_default=False,
)
SOFTENER_HOLIDAY_POLICY = EnumWritePolicy(
    r"^/swCircuits/[^/]+/holidayMode/enabled$",
    frozenset({"stringValue"}),
    frozenset({"on", "off"}),
    known_only=True,
    enabled_by_default=False,
)
WDDW2_HOLIDAY_POLICY = EnumWritePolicy(
    r"^/system/holidayMode$",
    frozenset({"stringValue"}),
    frozenset({"on", "off"}),
    known_only=True,
)
# The exhaust fan levels documented for the local interface; any other
# advertised level makes the whole control unavailable.
VENTILATION_FAN_LEVELS = ("off", "min", "red", "nom", "max", "dem")
VENTILATION_FAN_LEVEL_POLICY = EnumWritePolicy(
    r"^/ventilation/zone[^/]+/exhaustFanLevel$",
    frozenset({"stringValue"}),
    frozenset(VENTILATION_FAN_LEVELS),
    known_only=True,
    options_rule="subset",
)
POOL_ENABLED_POLICY = EnumWritePolicy(
    r"^/pool/enabled$",
    frozenset({"stringValue"}),
    frozenset({"on", "off"}),
    known_only=True,
    options_rule="exact",
)
STRING_SWITCH_POLICIES = (
    WDDW2_HOLIDAY_POLICY,
    SOFTENER_HOLIDAY_POLICY,
    POOL_ENABLED_POLICY,
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
# The pool target follows the advertised limits and step of each gateway.
POOL_SETPOINT_POLICY = NumberWritePolicy(
    r"^/pool/setpointTemp$", "C", 1.0, live_step=True
)
NUMBER_WRITE_POLICIES = (
    # ZONE-02, ZONE-05: no change while away or while the zone is locked.
    NumberWritePolicy(
        rrc.MANUAL_PATTERN,
        "C",
        0.5,
        value_check=rrc_contract_zone.manual_value_check,
    ),
    NumberWritePolicy(ZONE_MANUAL_PATTERN, "C", 0.5),
    NumberWritePolicy(
        r"^/swCircuits/[^/]+/sensor/saltLevelAlarm$", "%", 1.0, enabled_by_default=False
    ),
    NumberWritePolicy(
        r"^/swCircuits/[^/]+/regeneration/singleRegAppoint$",
        "d",
        1.0,
        enabled_by_default=False,
    ),
    NumberWritePolicy(
        r"^/heatingCircuits/[^/]+/manualRoomSetpoint$", "C", 0.5, write_grid=True
    ),
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
        write_grid=True,
    ),
    NumberWritePolicy(r"^/dhwCircuits/[^/]+/chargeDuration$", "mins", 15),
    NumberWritePolicy(
        r"^/dhwCircuits/[^/]+/(?:singleChargeSetpoint|temperatureLevels/(?:eco|high|low))$",
        "C",
        1.0,
        write_grid=True,
    ),
    POOL_SETPOINT_POLICY,
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
        if self.policy.action:
            return "button"
        return "switch" if self.supports_switch else "select"

    @property
    def supports_switch(self) -> bool:
        """Keep legacy switches only when both exact actions are advertised."""
        values = _switch_values(self.policy)
        return values is not None and values.issubset(self.options)

    @property
    def supports_select(self) -> bool:
        """Offer variants of switch functions without changing switch IDs."""
        if not isinstance(self.policy, EnumWritePolicy) or self.policy.action:
            return False
        values = _switch_values(self.policy)
        return values is None or set(self.options) != values

    @property
    def enabled_by_default(self) -> bool | None:
        if isinstance(self.policy, NumberWritePolicy):
            return self.policy.enabled_by_default
        return self.policy.enabled_by_default if self.policy is not None else None


def _switch_values(
    policy: EnumWritePolicy | NumberWritePolicy | None,
) -> frozenset[str] | None:
    """The two values of a switch policy: a legacy string switch or a
    contract switch with declared on and off values."""
    if not isinstance(policy, EnumWritePolicy):
        return None
    if policy in STRING_SWITCH_POLICIES:
        return policy.known_values
    declared = policy.switch_values
    return frozenset(declared) if declared is not None else None


def is_switch_policy(policy: object) -> bool:
    """Whether a policy writes a two-value switch."""
    return isinstance(policy, EnumWritePolicy) and _switch_values(policy) is not None


def _context(context: ControlContext | None) -> ControlContext:
    return context if context is not None else GENERIC_CONTEXT


class WriteService:
    """Validate, write once, and confirm the resulting resource value."""

    def __init__(
        self,
        client: PointTClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        read_back_delay: float = DEFAULT_READ_BACK_DELAY,
        read_back_attempts: int = DEFAULT_READ_BACK_ATTEMPTS,
        rrc_read_back_delays: tuple[float, ...] = RRC_READ_BACK_DELAYS,
    ) -> None:
        if read_back_delay < 0 or any(delay < 0 for delay in rrc_read_back_delays):
            raise ValueError("Read-back delay must not be negative")
        if read_back_attempts < 1 or not rrc_read_back_delays:
            raise ValueError("Read-back attempts must be positive")
        self._client = client
        self._sleep = sleep
        self._read_back_delay = read_back_delay
        self._read_back_attempts = read_back_attempts
        self._rrc_read_back_delays = rrc_read_back_delays

    def read_back_delays(self, context: ControlContext | None) -> tuple[float, ...]:
        """S7: the read-back schedule of a write in this context."""
        if _context(context).rrc_plant:
            return self._rrc_read_back_delays
        return tuple(
            self._read_back_delay * (2**attempt)
            for attempt in range(self._read_back_attempts)
        )

    async def async_write_enum(
        self,
        gateway_id: str,
        resource: Resource,
        value: str,
        policy: EnumWritePolicy,
        *,
        current: Resource | None = None,
        context: ControlContext | None = None,
        related: Mapping[str, Resource] | None = None,
        on_sent: Callable[[], None] | None = None,
    ) -> WriteResult:
        """Write an allowlisted enum and return only a confirmed result.

        ``current`` is a reading of this resource that the caller has just
        received from the gateway (C35); it replaces a reading of our own.
        ``related`` holds the current readings a contract rule may compare
        with; ``on_sent`` is called once the write request goes out.
        """
        request = WriteRequest(gateway_id, resource.path, value)
        live = current is not None and current.path == resource.path
        if live and current is not None:
            resource = current
        elif _reads_fresh(resource.path, policy, context):
            resource = await self._client.get_resource(gateway_id, resource.path)
        _validate_enum(resource, request, policy, context, related)
        if _confirms(policy, resource.value, value):
            if not live:
                resource = await self._client.get_resource(gateway_id, resource.path)
                _validate_enum(resource, request, policy, context, related)
            if _confirms(policy, resource.value, value):
                return WriteResult(request, resource)
        return await self._async_write(request, resource, policy, context, on_sent)

    async def async_write_number(
        self,
        gateway_id: str,
        resource: Resource,
        value: float,
        policy: NumberWritePolicy,
        *,
        current: Resource | None = None,
        context: ControlContext | None = None,
        related: Mapping[str, Resource] | None = None,
        on_sent: Callable[[], None] | None = None,
    ) -> WriteResult:
        """Write a bounded finite number and return only a confirmed result.

        ``current`` is a reading of this resource that the caller has just
        received from the gateway (C35); it replaces a reading of our own.
        ``related`` holds the current readings a contract rule may compare
        with; ``on_sent`` is called once the write request goes out.
        """
        wire: float | int = value
        if policy.wire_integer:
            # S2: the contract says int; a fraction would be refused or cut.
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or not float(value).is_integer()
            ):
                raise WriteValidationError("Requested value must be a whole number")
            wire = int(value)
        request = WriteRequest(gateway_id, resource.path, wire)
        live = current is not None and current.path == resource.path
        if live and current is not None:
            resource = current
        elif _reads_fresh(resource.path, policy, context):
            resource = await self._client.get_resource(gateway_id, resource.path)
        _validate_number(resource, request, policy, context, related)
        if _values_equal(resource.value, wire):
            if not live:
                resource = await self._client.get_resource(gateway_id, resource.path)
                _validate_number(resource, request, policy, context, related)
            if _values_equal(resource.value, wire):
                return WriteResult(request, resource)
        return await self._async_write(request, resource, policy, context, on_sent)

    async def _async_write(
        self,
        request: WriteRequest,
        resource: Resource,
        policy: EnumWritePolicy | NumberWritePolicy,
        context: ControlContext | None,
        on_sent: Callable[[], None] | None,
    ) -> WriteResult:
        value = request.value
        rrc_plant = _context(context).rrc_plant
        not_confirmed: type[WriteNotConfirmed] = (
            RrcWriteNotConfirmed if rrc_plant else WriteNotConfirmed
        )
        put_timed_out = False
        if on_sent is not None:
            on_sent()
        try:
            response = await self._client.put_resource_value(
                request.gateway_id, resource.path, value
            )
        except RateLimited:
            raise
        except TransportError as err:
            # A lost connection or server error may follow an applied PUT.
            # Resolve the outcome by reading, never by sending another PUT.
            put_timed_out = isinstance(err, RequestTimeout)
        except InvalidPayload as err:
            raise WriteRejected("PointT returned an invalid write response") from err
        else:
            _validate_write_response(response, resource.path, value, policy)

        last_temporary_error: RequestTimeout | ServiceUnavailable | None = None
        for delay in self.read_back_delays(context):
            await self._sleep(delay)
            try:
                confirmed = await self._client.get_resource(
                    request.gateway_id, resource.path
                )
            except (RequestTimeout, ServiceUnavailable) as err:
                last_temporary_error = err
                continue
            except PointTError as err:
                # C32: the PUT was sent and may have taken effect. A failed
                # read-back (429, sign-in, payload, path) leaves it unconfirmed;
                # it is never reported as a refusal. The cause stays attached.
                raise not_confirmed(
                    "PointT read-back failed after the write was sent"
                ) from err
            valid = (
                confirmed.path == request.path
                and confirmed.has_value
                and (
                    not is_zone_control(request.path) or _zone_readback_valid(confirmed)
                )
                and (
                    not rrc.is_control(request.path)
                    or rrc.control_issue(confirmed) is None
                )
            )
            if valid and _confirms(policy, confirmed.value, value):
                return WriteResult(request, confirmed, put_timed_out=put_timed_out)
            if valid and policy.read_back_check is not None:
                reason = policy.read_back_check(confirmed, value)
                if reason is not None:
                    # A contract rule: this reading ends the wait (for example
                    # a temporary setpoint the controller already ended). The
                    # reading itself is valid and current.
                    raise RrcWriteNotConfirmed(
                        "PointT read-back ended the write unconfirmed",
                        reason=reason,
                        reading=confirmed,
                    )
        if last_temporary_error is not None:
            raise not_confirmed("PointT read-back could not be completed") from (
                last_temporary_error
            )
        raise not_confirmed("PointT read-back did not confirm the value")


def _reads_fresh(
    path: str,
    policy: EnumWritePolicy | NumberWritePolicy,
    context: ControlContext | None,
) -> bool:
    """S5: whether the transaction reads the resource itself before a PUT.

    A zone control always; on an RRC controller every contract setting, so
    that its flags, limits and value are checked against the controller's
    current state, not against the reading of the last poll. A caller may
    pass that current reading as ``current`` instead (the coordinator does).
    """
    return rrc.is_control(path) or (
        _context(context).rrc_plant and rrc_contracts.owns(policy)
    )


def validate_control_value(
    resource: Resource, value: object, context: ControlContext | None = None
) -> None:
    """Validate a draft with exactly the same rules as the write transaction."""
    policy = assess_control(resource, context).policy
    if isinstance(policy, EnumWritePolicy) and isinstance(value, str):
        _validate_enum(
            resource, WriteRequest("", resource.path, value), policy, context
        )
    elif (
        isinstance(policy, NumberWritePolicy)
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    ):
        _validate_number(
            resource, WriteRequest("", resource.path, value), policy, context
        )
    else:
        raise WriteValidationError("Unsupported control value")


def _run_value_check(
    policy: EnumWritePolicy | NumberWritePolicy,
    resource: Resource,
    value: JsonScalar,
    related: Mapping[str, Resource] | None,
) -> None:
    if policy.value_check is None:
        return
    reason = policy.value_check(resource, value, related or {})
    if reason is not None:
        raise ContractRejected(reason)


def _validate_enum(
    resource: Resource,
    request: WriteRequest,
    policy: EnumWritePolicy,
    context: ControlContext | None = None,
    related: Mapping[str, Resource] | None = None,
) -> None:
    if request.path != resource.path or not re.fullmatch(
        policy.path_pattern, resource.path
    ):
        raise WriteValidationError("Resource path is not approved for this write")
    assessment = assess_control(resource, context)
    if not assessment.eligible or assessment.policy != policy:
        raise WriteValidationError("Resource does not advertise an enum write contract")
    if rrc.mode_locked(resource) or _locked(resource, policy):
        raise ControlLocked("The controller currently locks this mode")
    if _unused(resource, policy):
        raise ControlUnused("The controller does not use this setting now")
    if not isinstance(request.value, str) or request.value not in assessment.options:
        raise WriteValidationError("Requested enum value is not currently advertised")
    _run_value_check(policy, resource, request.value, related)


def _contract_availability_issue(
    resource: Resource, policy: EnumWritePolicy | NumberWritePolicy
) -> str | None:
    """The one availability check of every contract policy (P41, S8).

    A setting the controller marks as not available gets no control at all,
    whether it is used or not (for example extra hot water on an
    instantaneous water heater, or the fireplace function while away mode
    runs); malformed flags get none either (``rrc_unavailable``). An
    available setting that is not used keeps its control, which refuses to
    write (``_unused``). A policy with ``unused_writable`` keeps the earlier
    rule: no control only when unavailable and not used, a used but
    unavailable setting is shown and locked (``_locked``). The flags come
    from ``rrc.flag_state``; readings are not affected.
    """
    if not rrc_contracts.owns(policy):
        return None
    state = rrc.flag_state(resource)
    if state == "invalid":
        return "rrc_unavailable"
    if policy.unused_writable:
        if state == "unavailable" and resource.metadata.used is not True:
            return "rrc_unavailable"
        return None
    return "rrc_unavailable" if state == "unavailable" else None


def _locked(resource: Resource, policy: EnumWritePolicy | NumberWritePolicy) -> bool:
    """S8: a used but unavailable contract setting with ``unused_writable``
    is shown and locked."""
    metadata = resource.metadata
    return (
        rrc_contracts.owns(policy)
        and policy.unused_writable
        and metadata.availability_flags_valid
        and metadata.used is True
        and metadata.available is False
    )


def _unused(resource: Resource, policy: EnumWritePolicy | NumberWritePolicy) -> bool:
    """An available contract setting the controller does not use now: shown,
    never written (``ControlUnused``)."""
    return (
        rrc_contracts.owns(policy)
        and not policy.unused_writable
        and rrc.flag_state(resource) == "unused"
    )


def _validate_number(
    resource: Resource,
    request: WriteRequest,
    policy: NumberWritePolicy,
    context: ControlContext | None = None,
    related: Mapping[str, Resource] | None = None,
) -> None:
    if request.path != resource.path or not re.fullmatch(
        policy.path_pattern, resource.path
    ):
        raise WriteValidationError("Resource path is not approved for this write")
    assessment = assess_control(resource, context)
    if not assessment.eligible or assessment.policy != policy:
        raise WriteValidationError(
            "Resource does not advertise a numeric write contract"
        )
    if _locked(resource, policy):
        raise ControlLocked("The controller currently locks this setting")
    if _unused(resource, policy):
        raise ControlUnused("The controller does not use this setting now")
    reason = _number_value_rejection_reason(resource, request.value)
    if reason is not None:
        raise WriteValidationError(f"Numeric control validation failed: {reason}")
    if is_zone_control(resource.path) and zone_control_issue(
        replace(resource, value=request.value, has_value=True)
    ):
        raise WriteValidationError("Requested value is not a valid zone temperature")
    if (
        policy.write_grid
        and resource.metadata.unit in _CELSIUS_UNITS
        and isinstance(request.value, int | float)
    ):
        grid = request.value / policy.step
        if not math.isclose(grid, round(grid), abs_tol=1e-7):
            raise WriteValidationError("Requested value is outside the step size")
    step = _live_step(resource) if policy.live_step else None
    if policy.strict_grid:
        # S4: the advertised step, else the contract's, counted from minValue.
        step = _live_step(resource) or policy.step
    minimum = resource.metadata.minimum
    if (
        step is not None
        and isinstance(request.value, int | float)
        and (minimum is not None or policy.strict_grid)
    ):
        ticks = (request.value - (minimum or 0.0)) / step
        if not math.isclose(ticks, round(ticks), abs_tol=1e-7):
            raise WriteValidationError("Requested value is outside the step size")
    if rrc.is_control(resource.path):
        if rrc.control_issue(replace(resource, value=request.value, has_value=True)):
            raise WriteValidationError("Requested value is not a valid RRC temperature")
        step = resource.metadata.step_size
        minimum = resource.metadata.minimum
        if (
            step is not None
            and minimum is not None
            and isinstance(request.value, int | float)
        ):
            ticks = (request.value - minimum) / step
            if not math.isclose(ticks, round(ticks), abs_tol=1e-7):
                raise WriteValidationError(
                    "Requested RRC temperature is outside the step size"
                )
    _run_value_check(policy, resource, request.value, related)


def _zone_readback_valid(resource: Resource) -> bool:
    """Confirm a valid zone sample, even if its write permission was removed."""
    return zone_control_issue(resource) is None and (
        resource.path.endswith("/operationMode")
        or _number_value_rejection_reason(resource, resource.value) is None
    )


def _number_rejection_reason(
    resource: Resource, policy: NumberWritePolicy
) -> str | None:
    """Validate the advertised numeric contract independently of the state."""
    metadata = resource.metadata
    if not metadata.writable:
        return "not_writable"
    if metadata.resource_type != "floatValue":
        return "unsupported_resource_type"
    if policy.accepted_units is not None:
        if (metadata.unit or "") not in policy.accepted_units:
            return "unsupported_unit"
    elif policy.unit in {"%", "d"}:
        supported_units = {"%"} if policy.unit == "%" else {"d", "days"}
        if softener_unit(resource) not in supported_units:
            return "unsupported_unit"
    else:
        supported_units = (
            set(TEMPERATURE_UNITS)
            if policy.unit == "C"
            else {unit for unit, native in DURATION_UNITS.items() if native != "d"}
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
    if not _plausible_bounds(minimum, maximum, metadata.unit, policy.unit):
        return "implausible_bounds"
    if (policy.live_step or policy.strict_grid) and (
        not metadata.step_size_valid
        or (metadata.step_size is not None and _live_step(resource) is None)
    ):
        return "invalid_step"
    if policy.strict_grid:
        step = _live_step(resource) or policy.step
        if maximum > minimum and step > maximum - minimum:
            # S4: a step wider than the range (``nextSetpoint`` 100) leaves
            # nothing to set; the reading itself stays shown.
            return "invalid_step"
    if softener_capability(resource.path) is not None and (
        minimum < 0 or (policy.unit == "%" and maximum > 100)
    ):
        return "invalid_softener_bounds"
    return None


def _plausible_bounds(
    minimum: float, maximum: float, unit: str | None, policy_unit: str
) -> bool:
    """C34: whether both limits lie in a broad physical range for the unit."""
    if policy_unit == "C":
        native = TEMPERATURE_UNITS.get(unit or "")
        offset, scale = {"°F": (32.0, 5 / 9), "K": (273.15, 1.0)}.get(
            native or "", (0.0, 1.0)
        )
        low, high = PLAUSIBLE_TEMPERATURE_RANGE_C
    elif policy_unit == "mins":
        offset = 0.0
        scale = {"s": 1 / 60, "h": 60.0}.get(DURATION_UNITS.get(unit or "", ""), 1.0)
        low, high = PLAUSIBLE_DURATION_RANGE_MIN
    else:
        return True
    tolerance = 1e-9
    return (minimum - offset) * scale >= low - tolerance and (
        maximum - offset
    ) * scale <= high + tolerance


def _live_step(resource: Resource) -> float | None:
    """The advertised step if it is a usable positive finite number."""
    step = resource.metadata.step_size
    if (
        isinstance(step, bool)
        or not isinstance(step, int | float)
        or not math.isfinite(step)
        or step <= 0
    ):
        return None
    return float(step)


def _number_value_rejection_reason(resource: Resource, value: object) -> str | None:
    """Validate a target against gateway bounds.

    UI steps are not API rules; only ``write_grid`` policies enforce one, in
    ``_validate_number``.
    """
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


def advertised_options(
    resource: Resource, policy: EnumWritePolicy
) -> tuple[JsonScalar, ...]:
    """The advertised values; without any ``allowedValues`` key, the policy's
    fallback options when the current value is one of them (or an equivalent
    read of one, such as "off" for "Off").

    An empty or different list stays as advertised, so it keeps the strict
    rules (#113, generalised from ``userMode``).
    """
    metadata = resource.metadata
    if (
        policy.fallback_options
        and metadata.allowed_values_missing
        and not metadata.allowed_values
        and resource.has_value
        and isinstance(resource.value, str)
        and any(
            policy.confirms(resource.value, option)
            for option in policy.fallback_options
        )
    ):
        return policy.fallback_options
    if policy.ignored_options:
        return tuple(
            value
            for value in metadata.allowed_values
            if value not in policy.ignored_options
        )
    return metadata.allowed_values


def _enum_rejection_reason(resource: Resource, policy: EnumWritePolicy) -> str | None:
    """Accept each advertised string code without a reference-device allowlist."""
    metadata = resource.metadata
    if not metadata.writable:
        return "not_writable"
    if policy.action and (
        not resource.has_value or resource.value not in ("on", "off")
    ):
        return "unknown_action_state"
    if metadata.resource_type not in policy.resource_types:
        return "unsupported_resource_type"
    allowed = advertised_options(resource, policy)
    if not allowed:
        return "missing_options"
    if any(
        not isinstance(value, str) or not value or not value.isprintable()
        for value in allowed
    ):
        return "invalid_options"
    if policy.known_only and not policy.known_values.intersection(allowed):
        return "unsupported_options"
    advertised = set(allowed)
    if (policy.options_rule == "subset" and not advertised <= policy.known_values) or (
        policy.options_rule == "exact" and advertised != policy.known_values
    ):
        return "unsupported_options"
    usable = {
        value
        for value in advertised
        if not policy.known_only or value in policy.known_values
    }
    if len(usable) < policy.min_options:
        return "too_few_options"
    return None


def _enum_current_value_issue(
    resource: Resource, policy: EnumWritePolicy
) -> str | None:
    if not resource.has_value or resource.value is None:
        return "missing_value"
    if not isinstance(resource.value, str):
        return "invalid_value_type"
    if not any(
        isinstance(option, str) and policy.confirms(resource.value, option)
        for option in advertised_options(resource, policy)
    ):
        return "current_option_not_advertised"
    return None


def assess_control(
    resource: Resource, context: ControlContext | None = None
) -> ControlAssessment:
    """Assess scalar controls without cloud requests or exposing raw values.

    S11: an RRC contract policy applies only with an RRC ``context``, and
    before the generic policies.
    """
    # A known path and writable metadata do not make a compound response a
    # supported scalar. Share the read-side contract with every control caller.
    schema_issue = (
        softener_schema_issue(resource)
        if softener_capability(resource.path) is not None
        else None
    )
    if is_zone_temporary(resource.path):
        return ControlAssessment(None, "zone_override_lifecycle_unverified")
    if (blocked := rrc_contracts.blocked_reason(resource.path)) is not None:
        return ControlAssessment(None, blocked)
    if rrc.is_control(resource.path):
        schema_issue = rrc.control_issue(resource)
    if is_zone_control(resource.path):
        schema_issue = zone_control_issue(resource)
    if resource.path == WDDW2_HOLIDAY_PATH and (
        resource.has_values
        or not resource.has_value
        or resource.value not in ("on", "off")
        or resource.metadata.unit not in (None, "")
    ):
        schema_issue = "unsupported_holiday_mode"
    try:
        policy = rrc_contracts.policy_for(resource, context) or _policy_for_path(
            resource.path
        )
    except ContractConflict:
        return ControlAssessment(None, "rrc_contract_conflict")
    if policy is not None:
        schema_issue = schema_issue or _contract_availability_issue(resource, policy)
    if isinstance(policy, EnumWritePolicy):
        return ControlAssessment(
            policy,
            schema_issue or _enum_rejection_reason(resource, policy),
            writable=resource.metadata.writable,
            options=tuple(
                dict.fromkeys(
                    value
                    for value in advertised_options(resource, policy)
                    if isinstance(value, str)
                    and value
                    and value.isprintable()
                    and (not policy.known_only or value in policy.known_values)
                )
            ),
            current_value_issue=_enum_current_value_issue(resource, policy),
        )
    if isinstance(policy, NumberWritePolicy):
        number_policy = policy
        return ControlAssessment(
            number_policy,
            schema_issue
            or _number_rejection_reason(resource, number_policy)
            or (
                _number_value_rejection_reason(resource, resource.value)
                if is_zone_control(resource.path) or rrc.is_control(resource.path)
                else None
            ),
            writable=resource.metadata.writable,
            minimum=resource.metadata.minimum,
            maximum=resource.metadata.maximum,
            unit=softener_unit(resource)
            if softener_capability(resource.path) is not None
            else resource.metadata.unit,
            ui_step=(
                resource.metadata.step_size
                if (is_zone_control(resource.path) or rrc.is_control(resource.path))
                and schema_issue is None
                and resource.metadata.step_size is not None
                else _live_step(resource)
                if (number_policy.live_step or number_policy.strict_grid)
                and _live_step(resource) is not None
                else number_policy.step
            ),
            current_value_issue=_number_value_rejection_reason(
                resource, resource.value if resource.has_value else None
            ),
        )
    return ControlAssessment(None, "no_scalar_control_policy")


# The enum policies in the order ``assess_control`` tries them.
_ENUM_POLICIES = (
    RRC_OPERATION_MODE_POLICY,
    ZONE_OPERATION_MODE_POLICY,
    HEATING_CIRCUIT_OPERATION_MODE_POLICY,
    DHW_OPERATION_MODE_POLICY,
    SILENT_MODE_POLICY,
    SEASON_SWITCHING_POLICY,
    AUXILIARY_HEATER_OPERATION_MODE_POLICY,
    VENTILATION_FAN_LEVEL_POLICY,
    SOFTENER_REGENERATION_POLICY,
    *STRING_SWITCH_POLICIES,
)


@lru_cache(maxsize=4096)
def _policy_for_path(path: str) -> EnumWritePolicy | NumberWritePolicy | None:
    """O4: the first policy whose pattern matches, enum before number.

    The generic policies are constants, so the answer depends on the path
    alone. The RRC contract policies depend on the context and are chosen
    in ``assess_control`` before this.
    """
    for policy in _ENUM_POLICIES:
        if re.fullmatch(policy.path_pattern, path):
            return policy
    for number_policy in NUMBER_WRITE_POLICIES:
        if re.fullmatch(number_policy.path_pattern, path):
            return number_policy
    return None


def enum_policy_for_resource(
    resource: Resource, context: ControlContext | None = None
) -> EnumWritePolicy | None:
    """Return a released enum policy only when live metadata matches."""
    assessment = assess_control(resource, context)
    if assessment.eligible and isinstance(assessment.policy, EnumWritePolicy):
        return assessment.policy
    return None


def number_policy_for_resource(
    resource: Resource, context: ControlContext | None = None
) -> NumberWritePolicy | None:
    """Return a released numeric policy only when live metadata is safe."""
    assessment = assess_control(resource, context)
    if assessment.eligible and isinstance(assessment.policy, NumberWritePolicy):
        return assessment.policy
    return None


def _validate_write_response(
    response: Resource | None,
    path: str,
    value: JsonScalar,
    policy: EnumWritePolicy | NumberWritePolicy | None = None,
) -> None:
    if response is None:
        return
    if (
        response.path != path
        or not response.has_value
        or not _confirms(policy, response.value, value)
    ):
        raise WriteRejected("PointT write response did not match the request")


def confirms_value(
    policy: EnumWritePolicy | NumberWritePolicy | None, read: object, requested: object
) -> bool:
    """Whether a reading shows the requested value (or an equivalent read)."""
    return _values_equal(read, requested) or (
        isinstance(policy, EnumWritePolicy) and policy.confirms(read, requested)
    )


_confirms = confirms_value


def _values_equal(left: object, right: object) -> bool:
    if (
        not isinstance(left, bool)
        and not isinstance(right, bool)
        and isinstance(left, int | float)
        and isinstance(right, int | float)
    ):
        return math.isclose(float(left), float(right), abs_tol=1e-7)
    return left == right
