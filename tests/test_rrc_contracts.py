"""W0: the RRC contract registry and the write context (S1 to S12).

Every RRC variant applies only with an RRC write context (S11); the policy
extensions keep today's behaviour by default. The contracts shipped with W0
are empty apart from the temporary-setpoint block moved from the write
service.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from types import MappingProxyType

import pytest

from custom_components.bosch_buderus_heating import (
    rrc,
    rrc_contract_zone,
    rrc_contracts,
    writes,
)
from custom_components.bosch_buderus_heating.pointt import (
    RateLimited,
    RequestTimeout,
    Resource,
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.poll_group import PollGroup
from custom_components.bosch_buderus_heating.rrc_contract_base import (
    RRC_ID,
    RrcContract,
)
from custom_components.bosch_buderus_heating.rrc_contracts import (
    ContractConflict,
    Registry,
)
from custom_components.bosch_buderus_heating.writes import (
    GENERIC_CONTEXT,
    RRC_CONTEXT,
    RRC_OPERATION_MODE_POLICY,
    ContractRejected,
    ControlLocked,
    EnumWritePolicy,
    NumberWritePolicy,
    RrcWriteNotConfirmed,
    WriteService,
    assess_control,
)

AWAY = "/system/awayMode/enabled"
OVERRIDE = "/zones/zn1/clockOverride/temperatureHeating"
AWAY_POLICY = EnumWritePolicy(
    rf"^{AWAY}$",
    frozenset({"stringValue"}),
    frozenset({"true", "false"}),
    known_only=True,
    fallback_options=("true", "false"),
    on_value="true",
    off_value="false",
)
DHW_MODE_POLICY = EnumWritePolicy(
    r"^/dhwCircuits/dhw1/operationMode$",
    frozenset({"stringValue"}),
    frozenset({"Off", "high", "ownprogram"}),
    fallback_options=("Off", "high", "ownprogram"),
    equivalent_reads=MappingProxyType({"Off": frozenset({"off"})}),
)
DURATION_POLICY = NumberWritePolicy(
    r"^/dhwCircuits/dhw1/extraDhwDuration$",
    "mins",
    15,
    wire_integer=True,
    strict_grid=True,
)
INFLUENCE_POLICY = NumberWritePolicy(
    r"^/heatingCircuits/hc1/roomInfluence$",
    "other",
    1,
    wire_integer=True,
    accepted_units=frozenset({"", "other"}),
    ui_unit_override="",
    create_entity=False,
)


def _order(resource: Resource, value: object, related) -> str | None:
    other = related.get("/heatingCircuits/hc1/heatCurveMin")
    if other is not None and isinstance(value, float) and value <= other.value:
        return "rrc_curve_order"
    return None


CURVE_POLICY = NumberWritePolicy(
    r"^/heatingCircuits/hc1/heatCurveMax$", "C", 1.0, value_check=_order
)
TEST_CONTRACT = RrcContract(
    name="test",
    enum_policies=(AWAY_POLICY, DHW_MODE_POLICY),
    number_policies=(DURATION_POLICY, INFLUENCE_POLICY, CURVE_POLICY),
)


def wire(path: str, **fields: object) -> Resource:
    return parse_resource({"id": path, "writeable": 1, "recordable": 0, **fields})


def away(value: str = "false", **fields: object) -> Resource:
    return wire(AWAY, type="stringValue", value=value, **fields)


def _without_package_policies() -> tuple[RrcContract, ...]:
    return tuple(
        contract
        for contract in rrc_contracts.CONTRACTS
        if not (contract.enum_policies or contract.number_policies)
    )


@contextmanager
def installed(*contracts: RrcContract) -> Iterator[Registry]:
    """Swap the installed contracts for the duration of a test.

    The probes stand in for the package contracts with write policies (W3
    ships the hot water policies on the paths the probes use); the others
    stay.
    """
    registry = Registry((*_without_package_policies(), *contracts))
    original = rrc_contracts._registry
    rrc_contracts._registry = registry
    try:
        yield registry
    finally:
        rrc_contracts._registry = original


class FakeClient:
    """Reads a resource; a PUT shows after ``adopt_after`` reads."""

    def __init__(self, resource: Resource, adopt_after: int = 1) -> None:
        self.resource = resource
        self.adopt_after = adopt_after
        self.puts: list[object] = []
        self.reads = 0
        self.pending: object | None = None
        self.put_error: Exception | None = None
        self.read_errors: list[Exception] = []

    async def put_resource_value(self, gateway_id, path, value):
        self.puts.append(value)
        if self.put_error is not None and not isinstance(
            self.put_error, RequestTimeout
        ):
            raise self.put_error
        self.pending = value
        self.reads = 0
        if self.put_error is not None:
            raise self.put_error
        return None

    async def get_resource(self, gateway_id, path):
        if self.read_errors and self.puts:
            raise self.read_errors.pop(0)
        if self.pending is not None:
            self.reads += 1
            if self.reads >= self.adopt_after:
                self.resource = replace(self.resource, value=self.pending)
                self.pending = None
        return self.resource


def _service(client: FakeClient, sleeps: list[float]) -> WriteService:
    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    return WriteService(client, sleep=sleep)  # type: ignore[arg-type]


# ------------------------------------------------------------- registry


def test_shipped_contracts_are_empty_except_the_moved_override_block() -> None:
    registry = rrc_contracts.registry()
    assert [contract.name for contract in registry.contracts] == [
        "zone",
        "absence",
        "dhw",
        "circuit",
        "system",
        "devices",
    ]
    # The packages fill their own contracts (W3: hot water policies); the
    # other lookups below stay empty until their package ships them.
    assert rrc_contracts.zone_fields() == ()
    assert rrc_contracts.extra_roots() == ()
    assert rrc_contracts.device_leaves("thermostat_valve") == ()
    assert rrc_contracts.optional_children("/system/appliance") == ()
    assert rrc_contract_zone.CONTRACT.blocked
    # The block moved unchanged: every RRC zone, nothing else.
    for path in (OVERRIDE, "/zones/zn0/clockOverride/temperatureHeating"):
        assert rrc_contracts.blocked_reason(path) == "rrc_override_lifecycle_unverified"
        assert rrc.scalar_field(path) == "clockOverride/temperatureHeating"
    for path in (
        "/zones/zn01/clockOverride/temperatureHeating",
        "/zones/zone1/clockOverride/temperatureHeating",
        "/zones/zn1/clockOverride",
    ):
        assert rrc_contracts.blocked_reason(path) is None
    resource = wire(
        OVERRIDE, type="floatValue", unitOfMeasure="C", minValue=0, maxValue=30
    )
    for context in (None, GENERIC_CONTEXT, RRC_CONTEXT):
        assert assess_control(resource, context).rejection_reason == (
            "rrc_override_lifecycle_unverified"
        )


def test_equal_patterns_in_two_contracts_fail_at_import() -> None:
    first = RrcContract(name="first", enum_policies=(AWAY_POLICY,))
    second = RrcContract(
        name="second",
        enum_policies=(EnumWritePolicy(rf"^{AWAY}$", frozenset(), frozenset()),),
    )
    with pytest.raises(ContractConflict, match="first and second"):
        Registry((first, second))
    with pytest.raises(ContractConflict):
        Registry((first, RrcContract(name="first")))
    with pytest.raises(ContractConflict):
        Registry((RrcContract(name="Bad Name"),))
    for table in (
        {"poll_groups": MappingProxyType({"^/a$": PollGroup.SLOW})},
        {"names": MappingProxyType({"^/a$": ("A", "A")})},
        {"read_only_mirrors": ("^/a$",)},
        {"zone_fields": ("status",)},
        {"extra_roots": ("/events",)},
        {"device_leaves": MappingProxyType({"thermostat_valve": ("etrv/status",)})},
    ):
        with pytest.raises(ContractConflict):
            Registry(
                (RrcContract(name="one", **table), RrcContract(name="two", **table))
            )
    with pytest.raises(ContractConflict, match="invalid pattern"):
        Registry((RrcContract(name="one", read_only_mirrors=("^/a($",)),))
    for invalid in (
        {"zone_fields": ("../x",)},
        {"extra_roots": ("events",)},
        {"optional_children": MappingProxyType({"^/a$": ("b/../c",)})},
    ):
        with pytest.raises(ContractConflict):
            Registry((RrcContract(name="one", **invalid),))


def test_overlapping_patterns_refuse_the_control_and_are_found() -> None:
    broad = RrcContract(
        name="broad",
        enum_policies=(
            EnumWritePolicy(
                r"^/system/[^/]+/enabled$",
                frozenset({"stringValue"}),
                frozenset({"true", "false"}),
                fallback_options=("true", "false"),
            ),
        ),
    )
    with installed(TEST_CONTRACT, broad) as registry:
        assessment = assess_control(away(), RRC_CONTEXT)
        assert assessment.policy is None
        assert assessment.rejection_reason == "rrc_contract_conflict"
        with pytest.raises(ContractConflict):
            registry.check_paths([AWAY])
        registry.check_paths(["/system/fireplace/enabled"])
        # Without an RRC context nothing of the contracts applies.
        assert assess_control(away(), GENERIC_CONTEXT).rejection_reason == (
            "missing_options"
        )


def test_lookups_answer_per_path() -> None:
    contract = RrcContract(
        name="lookups",
        poll_groups=MappingProxyType({r"^/events/list[123]$": PollGroup.CONTROL}),
        names=MappingProxyType({rf"^{AWAY}$": ("Abwesend", "Away")}),
        entity_keys=MappingProxyType(
            {
                rf"^{AWAY}$": "away_mode",
                r"^/system/fireplace/enabled$": MappingProxyType({"switch": "fire"}),
            }
        ),
        option_keys=MappingProxyType(
            {r"^/dhwCircuits/dhw1/operationMode$": {"Off": "off"}}
        ),
        read_only_mirrors=(rf"^{AWAY}$",),
        understood=(r"^/events/list[123]$",),
        default_enabled=MappingProxyType({r"^/events/list1$": False}),
        categories=MappingProxyType({r"^/system/fireplace/enabled$": "config"}),
        icons=MappingProxyType({rf"^{AWAY}$": "mdi:home-export-outline"}),
        dialog_paths=(rf"^{AWAY}$",),
        zone_fields=("status",),
        extra_roots=("/events",),
        device_leaves=MappingProxyType({"thermostat_valve": ("etrv/status",)}),
        optional_children=MappingProxyType({r"^/system/appliance$": ("displayCode",)}),
        texts=(("Kamin", "Fireplace"),),
        diagnostics=lambda resources: {"lists": len(resources)},
    )
    with installed(contract) as registry:
        assert rrc_contracts.poll_group("/events/list2") is PollGroup.CONTROL
        assert rrc_contracts.poll_group("/events/list4") is None
        assert rrc_contracts.name(AWAY, "de") == "Abwesend"
        assert rrc_contracts.name(AWAY, "en-GB") == "Away"
        assert rrc_contracts.entity_key(AWAY, "switch") == "away_mode"
        assert rrc_contracts.entity_key("/system/fireplace/enabled", "switch") == "fire"
        assert rrc_contracts.entity_key("/system/fireplace/enabled", "select") is None
        assert (
            rrc_contracts.option_key("/dhwCircuits/dhw1/operationMode", "Off") == "off"
        )
        assert rrc_contracts.is_mirror(AWAY)
        assert rrc_contracts.is_understood("/events/list3")
        assert rrc_contracts.default_enabled("/events/list1") is False
        assert rrc_contracts.category("/system/fireplace/enabled") == "config"
        assert rrc_contracts.icon(AWAY) == "mdi:home-export-outline"
        assert rrc_contracts.in_dialog(AWAY)
        assert rrc_contracts.zone_fields() == ("status",)
        assert rrc_contracts.extra_roots() == ("/events",)
        assert rrc_contracts.device_leaves("thermostat_valve") == ("etrv/status",)
        assert rrc_contracts.optional_children("/system/appliance") == (
            "/system/appliance/displayCode",
        )
        assert ("Kamin", "Fireplace") in rrc_contracts.texts()
        assert ("Abwesend", "Away") in rrc_contracts.texts()
        report = rrc_contracts.diagnostics({AWAY: away()})
        assert report["lookups"] == {"confirmation": "unconfirmed", "lists": 1}
        assert report["zone"] == {"confirmation": "unconfirmed"}
        assert registry.contract_for(OVERRIDE) == "zone"


# ------------------------------------------------------- write context


def test_contract_policy_applies_only_on_an_rrc_plant() -> None:
    """S11: the same path at an EMS gateway keeps the generic policy."""
    with installed(TEST_CONTRACT):
        generic = assess_control(away("off", allowedValues=["on", "off"]))
        assert generic.policy is not AWAY_POLICY
        assert generic.supports_switch
        # No allowedValues key: the generic policy offers nothing.
        assert assess_control(away(), GENERIC_CONTEXT).rejection_reason == (
            "missing_options"
        )
        variant = assess_control(away(), RRC_CONTEXT)
        assert variant.policy is AWAY_POLICY and variant.eligible
        assert variant.options == ("true", "false")
        assert variant.supports_switch and not variant.supports_select
        assert variant.platform == "switch"
        assert writes.is_switch_policy(AWAY_POLICY)
        assert rrc_contracts.owns(AWAY_POLICY)
        assert not rrc_contracts.owns(RRC_OPERATION_MODE_POLICY)


def test_fallback_options_need_a_missing_key_and_a_matching_value() -> None:
    with installed(TEST_CONTRACT):
        assert assess_control(away("true"), RRC_CONTEXT).options == ("true", "false")
        # A value outside the fallback: no options, no control.
        odd = assess_control(away("on"), RRC_CONTEXT)
        assert odd.options == () and odd.rejection_reason == "missing_options"
        # An advertised list, even an empty one, stays as advertised.
        listed = assess_control(away("true", allowedValues=["true"]), RRC_CONTEXT)
        assert listed.options == ("true",) and not listed.supports_switch
        empty = assess_control(away("true", allowedValues=[]), RRC_CONTEXT)
        assert empty.rejection_reason == "missing_options"


def test_user_mode_fallback_still_works_as_before() -> None:
    mode = wire("/zones/zn1/userMode", type="stringValue", value="clock")
    assessment = assess_control(mode)
    assert assessment.policy is RRC_OPERATION_MODE_POLICY
    assert assessment.options == ("manual", "clock") and assessment.eligible
    assert assess_control(mode, RRC_CONTEXT) == assessment


def test_equivalent_reads_count_as_the_written_option() -> None:
    mode = wire("/dhwCircuits/dhw1/operationMode", type="stringValue", value="off")
    with installed(TEST_CONTRACT):
        assessment = assess_control(mode, RRC_CONTEXT)
        assert assessment.policy is DHW_MODE_POLICY
        # "off" reads as "Off": the fallback options apply, the value is valid.
        assert assessment.options == ("Off", "high", "ownprogram")
        assert assessment.eligible and assessment.current_value_issue is None
        odd = wire("/dhwCircuits/dhw1/operationMode", type="stringValue", value="eco")
        assert assess_control(odd, RRC_CONTEXT).rejection_reason == "missing_options"
        listed = wire(
            "/dhwCircuits/dhw1/operationMode",
            type="stringValue",
            value="off",
            allowedValues=["Off", "high"],
        )
        assert assess_control(listed, RRC_CONTEXT).current_value_issue is None
        assert DHW_MODE_POLICY.confirms("off", "Off")
        assert not DHW_MODE_POLICY.confirms("Off", "off")
        assert writes.confirms_value(DHW_MODE_POLICY, "off", "Off")


async def test_equivalent_read_confirms_a_write() -> None:
    listed = wire(
        "/dhwCircuits/dhw1/operationMode",
        type="stringValue",
        value="high",
        allowedValues=["Off", "high"],
    )
    client = FakeClient(listed)
    sleeps: list[float] = []
    with installed(TEST_CONTRACT):
        service = _service(client, sleeps)

        async def put(gateway_id, path, value):
            client.puts.append(value)
            client.pending = "off"  # the controller reads back lower case
            client.reads = 0

        client.put_resource_value = put  # type: ignore[method-assign]
        result = await service.async_write_enum(
            "gw", listed, "Off", DHW_MODE_POLICY, context=RRC_CONTEXT
        )
    assert result.resource.value == "off"
    assert client.puts == ["Off"]


async def test_wire_integer_and_strict_grid() -> None:
    duration = wire(
        "/dhwCircuits/dhw1/extraDhwDuration",
        type="floatValue",
        unitOfMeasure="mins",
        minValue=0,
        maxValue=2865,
        value=30,
    )
    client = FakeClient(duration)
    sleeps: list[float] = []
    with installed(TEST_CONTRACT):
        service = _service(client, sleeps)
        assert assess_control(duration, RRC_CONTEXT).ui_step == 15
        for refused in (30.5, 31.0):
            with pytest.raises(WriteValidationError):
                await service.async_write_number(
                    "gw", duration, refused, DURATION_POLICY, context=RRC_CONTEXT
                )
        result = await service.async_write_number(
            "gw", duration, 45.0, DURATION_POLICY, context=RRC_CONTEXT
        )
    assert client.puts == [45] and type(client.puts[0]) is int
    assert result.resource.value == 45
    # S4: a step wider than the range leaves nothing to set.
    wide = wire(
        "/dhwCircuits/dhw1/extraDhwDuration",
        type="floatValue",
        unitOfMeasure="mins",
        minValue=0,
        maxValue=60,
        stepSize=100,
        value=30,
    )
    with installed(TEST_CONTRACT):
        assert assess_control(wide, RRC_CONTEXT).rejection_reason == "invalid_step"


def test_accepted_units_and_entityless_policies() -> None:
    influence = wire(
        "/heatingCircuits/hc1/roomInfluence",
        type="floatValue",
        unitOfMeasure="other",
        minValue=0,
        maxValue=3,
        value=1,
    )
    with installed(TEST_CONTRACT):
        assessment = assess_control(influence, RRC_CONTEXT)
        assert assessment.eligible and assessment.policy is INFLUENCE_POLICY
        celsius = wire(
            "/heatingCircuits/hc1/roomInfluence",
            type="floatValue",
            unitOfMeasure="C",
            minValue=0,
            maxValue=3,
            value=1,
        )
        assert assess_control(celsius, RRC_CONTEXT).rejection_reason == (
            "unsupported_unit"
        )
        # S3: no limits, no number.
        assert (
            assess_control(
                wire(
                    "/heatingCircuits/hc1/roomInfluence",
                    type="floatValue",
                    unitOfMeasure="other",
                    value=1,
                ),
                RRC_CONTEXT,
            ).rejection_reason
            == "missing_bounds"
        )


async def test_value_check_refuses_before_sending() -> None:
    maximum = wire(
        "/heatingCircuits/hc1/heatCurveMax",
        type="floatValue",
        unitOfMeasure="C",
        minValue=25,
        maxValue=90,
        value=60,
    )
    minimum = wire(
        "/heatingCircuits/hc1/heatCurveMin",
        type="floatValue",
        unitOfMeasure="C",
        minValue=20,
        maxValue=90,
        value=40,
    )
    client = FakeClient(maximum)
    with installed(TEST_CONTRACT):
        service = _service(client, [])
        with pytest.raises(ContractRejected) as refused:
            await service.async_write_number(
                "gw",
                maximum,
                30.0,
                CURVE_POLICY,
                context=RRC_CONTEXT,
                related={minimum.path: minimum},
            )
        assert refused.value.translation_key == "rrc_curve_order"
        assert client.puts == []
        await service.async_write_number(
            "gw",
            maximum,
            70.0,
            CURVE_POLICY,
            context=RRC_CONTEXT,
            related={minimum.path: minimum},
        )
    assert client.puts == [70.0]


async def test_a_used_but_unavailable_contract_setting_is_locked() -> None:
    """S8: shown, but writing is refused with its own error."""
    locked = away("false", used="true", available="false")
    with installed(TEST_CONTRACT):
        assert assess_control(locked, RRC_CONTEXT).policy is AWAY_POLICY
        client = FakeClient(locked)
        with pytest.raises(WriteValidationError):
            await _service(client, []).async_write_enum(
                "gw", locked, "true", AWAY_POLICY, context=RRC_CONTEXT
            )
        assert client.puts == []
        # Flags valid, used and available: writable.
        assert assess_control(away("false", used="true"), RRC_CONTEXT).eligible


async def test_locked_contract_setting_raises_control_locked() -> None:
    policy = EnumWritePolicy(
        rf"^{AWAY}$",
        frozenset({"stringValue"}),
        frozenset({"true", "false"}),
        on_value="true",
        off_value="false",
    )
    contract = RrcContract(name="locked", enum_policies=(policy,))
    locked = away(
        "false", used="true", available="false", allowedValues=["true", "false"]
    )
    with installed(contract):
        assessment = assess_control(locked, RRC_CONTEXT)
        assert assessment.eligible
        client = FakeClient(locked)
        with pytest.raises(ControlLocked):
            await _service(client, []).async_write_enum(
                "gw", locked, "true", policy, context=RRC_CONTEXT
            )
        assert client.puts == []


# ------------------------------------------------------- read-back (S7)


def _manual(value: float = 20.0) -> Resource:
    return wire(
        "/zones/zn1/manualTemperatureHeating",
        type="floatValue",
        unitOfMeasure="C",
        minValue=5,
        maxValue=30,
        stepSize=0.5,
        value=value,
    )


@pytest.mark.parametrize(("reads", "confirmed"), [(1, True), (3, True), (4, True)])
async def test_rrc_read_back_waits_1_2_4_8_seconds(reads: int, confirmed: bool) -> None:
    client = FakeClient(_manual(), adopt_after=reads)
    sleeps: list[float] = []
    service = _service(client, sleeps)
    policy = writes.number_policy_for_resource(_manual(), RRC_CONTEXT)
    assert policy is not None
    result = await service.async_write_number(
        "gw", _manual(), 21.5, policy, context=RRC_CONTEXT
    )
    assert result.resource.value == 21.5
    assert client.puts == [21.5]
    assert sleeps == [1.0, 2.0, 4.0, 8.0][:reads]


async def test_rrc_write_unconfirmed_after_the_fourth_read_sends_once() -> None:
    client = FakeClient(_manual(), adopt_after=5)
    sleeps: list[float] = []
    service = _service(client, sleeps)
    policy = writes.number_policy_for_resource(_manual(), RRC_CONTEXT)
    assert policy is not None
    with pytest.raises(RrcWriteNotConfirmed) as unconfirmed:
        await service.async_write_number(
            "gw", _manual(), 21.5, policy, context=RRC_CONTEXT
        )
    assert unconfirmed.value.translation_key == "rrc_write_pending_confirmation"
    assert client.puts == [21.5]
    assert sleeps == [1.0, 2.0, 4.0, 8.0]
    # The same write elsewhere keeps the short schedule and the plain error.
    client = FakeClient(_manual(), adopt_after=5)
    sleeps.clear()
    with pytest.raises(WriteNotConfirmed) as plain:
        await _service(client, sleeps).async_write_number("gw", _manual(), 21.5, policy)
    assert not isinstance(plain.value, RrcWriteNotConfirmed)
    assert sleeps == [0.5, 1.0, 2.0]


async def test_rrc_read_back_after_a_put_timeout_never_writes_again() -> None:
    client = FakeClient(_manual(), adopt_after=2)
    client.put_error = RequestTimeout("lost")
    policy = writes.number_policy_for_resource(_manual(), RRC_CONTEXT)
    assert policy is not None
    result = await _service(client, []).async_write_number(
        "gw", _manual(), 22.0, policy, context=RRC_CONTEXT
    )
    assert result.put_timed_out and client.puts == [22.0]


async def test_rrc_read_back_rate_limit_leaves_the_write_unconfirmed() -> None:
    client = FakeClient(_manual(), adopt_after=3)
    client.read_errors = [RateLimited(retry_after=30)]
    policy = writes.number_policy_for_resource(_manual(), RRC_CONTEXT)
    assert policy is not None
    with pytest.raises(RrcWriteNotConfirmed) as unconfirmed:
        await _service(client, []).async_write_number(
            "gw", _manual(), 22.0, policy, context=RRC_CONTEXT
        )
    assert isinstance(unconfirmed.value.__cause__, RateLimited)
    assert client.puts == [22.0]


async def test_read_back_check_ends_the_wait_with_its_own_token() -> None:
    """W1's case: the controller ended the temporary setpoint at once."""

    def ended(resource: Resource, requested: object) -> str | None:
        return "rrc_override_ended" if resource.value == 0 else None

    policy = NumberWritePolicy(
        rf"^/zones/zn{RRC_ID}/manualTemperatureHeating$",
        "C",
        0.5,
        read_back_check=ended,
    )
    contract = RrcContract(name="ending", number_policies=(policy,))
    resource = wire(
        "/zones/zn1/manualTemperatureHeating",
        type="floatValue",
        unitOfMeasure="C",
        minValue=0,
        maxValue=30,
        stepSize=0.5,
        value=0,
    )
    client = FakeClient(resource, adopt_after=10)
    sleeps: list[float] = []
    with installed(contract):
        assert assess_control(resource, RRC_CONTEXT).policy is policy
        with pytest.raises(RrcWriteNotConfirmed) as ended_early:
            await _service(client, sleeps).async_write_number(
                "gw", resource, 21.0, policy, context=RRC_CONTEXT
            )
    assert ended_early.value.translation_key == "rrc_override_ended"
    assert sleeps == [1.0] and client.puts == [21.0]
