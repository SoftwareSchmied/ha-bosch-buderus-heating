"""W4: heating circuit settings of an RRC 2.0 room controller.

The contract ``rrc_contract_circuit`` turns the heating curve, the supply
limits, the room influence, the summer/winter and night switching, the
season mode, the heat-up speed and the setpoint optimization into controls,
and ``operatingSeason`` into a sensor (CT-G01, CT-G02). The fixture
``circuit.json`` is synthetic: ``hc1`` in the demo shapes, ``hc2`` in the
shapes of the TC100.2 diagnostics (#122) and with refusing metadata. Writes
run through real coordinator cycles against ``tests/helpers/rrc_cloud.py``.
Nothing of it reaches another gateway (S11).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import (
    rrc_circuit_entities,
    rrc_contract_circuit,
    rrc_contracts,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import Freshness
from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    Resource,
    ServiceUnavailable,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.rrc_contract_circuit import (
    check_order,
    check_weather_control,
    counterparts,
    guard_paths,
)
from custom_components.bosch_buderus_heating.writes import (
    GENERIC_CONTEXT,
    RRC_CONTEXT,
    ContractRejected,
    ControlLocked,
    ControlUnused,
    WriteService,
    assess_control,
    validate_control_value,
)
from tests.helpers.rrc_cloud import Clock, RrcCloud, cloud_patches, load_tree
from tests.test_entry import _entry

PACKAGE = "custom_components.bosch_buderus_heating"
STRINGS = Path(__file__).parents[1] / "custom_components" / DOMAIN / "strings.json"
HC1 = "/heatingCircuits/hc1"
HC2 = "/heatingCircuits/hc2"
CURVE_MIN = f"{HC1}/heatCurveMin"
CURVE_MAX = f"{HC1}/heatCurveMax"
SUPPLY_MIN = f"{HC1}/minSupply"
SUPPLY_MAX = f"{HC1}/maxSupply"
INFLUENCE = f"{HC1}/roomInfluence"
SUWI_MODE = f"{HC1}/suWiSwitchMode"
SUWI_THRESHOLD = f"{HC1}/suWiThreshold"
NIGHT_MODE = f"{HC1}/nightSwitchMode"
NIGHT_THRESHOLD = f"{HC1}/nightThreshold"
SEASON_MODE = f"{HC1}/seasonOptMode"
OPTIMIZATION = f"{HC1}/setpointOptimization"
HEATUP = f"{HC1}/buildingHeatup"
SEASON = f"{HC1}/operatingSeason"
RRC_GATEWAY = Gateway("gateway-one", device_type="rrc2")

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def uid(path: str, role: str = "control") -> str:
    return f"gateway-one:{path.strip('/').replace('/', ':')}:{role}"


def circuit_tree() -> dict[str, Resource]:
    return load_tree("rrc/circuit.json")


def plant() -> dict[str, Resource]:
    """A whole controller: the CT200-class tree with the circuit settings."""
    return {**load_tree("rrc/ct200-shapes.json"), **circuit_tree()}


class CircuitCloud(RrcCloud):
    """The shared fake with a server error after a PUT (5xx)."""

    def __init__(self, resources, **kwargs: Any) -> None:
        super().__init__(resources, **kwargs)
        # path -> whether the write was applied before the error.
        self.server_error_after_write: dict[str, bool] = {}

    async def put_resource_value(self, gateway_id, path, value, **kwargs):
        if path in self.server_error_after_write:
            self._write("PUT", path, value)
            if self.server_error_after_write[path]:
                self._settle(path, value)
            raise ServiceUnavailable(503)
        return await super().put_resource_value(gateway_id, path, value, **kwargs)


# ----------------------------------------------------------------- contract


def test_fixture_paths_have_no_contract_conflict() -> None:
    rrc_contracts.registry().check_paths(plant())


def test_fixture_is_synthetic() -> None:
    data = json.loads(
        (Path(__file__).parent / "fixtures/contracts/rrc/circuit.json").read_text(
            "utf-8"
        )
    )
    assert data["provenance"]["physical_device_verified"] is False
    assert data["provenance"]["basis"] == "compatibility_mapping"
    assert "demo shapes" in data["provenance"]["redaction"]


@pytest.mark.parametrize(
    ("path", "platform", "enabled", "extra"),
    [
        (CURVE_MIN, "number", False, {"ui_step": 1.0}),
        (CURVE_MAX, "number", False, {"ui_step": 1.0}),
        (SUPPLY_MIN, "number", False, {"ui_step": 1.0}),
        (SUPPLY_MAX, "number", False, {"ui_step": 1.0}),
        (INFLUENCE, "number", False, {"ui_step": 1.0}),
        # CIRCUIT-03: the summer/winter and night settings act only with
        # weather-compensated control; disabled by default like the curve.
        (SUWI_THRESHOLD, "number", False, {"ui_step": 0.5}),
        (NIGHT_THRESHOLD, "number", False, {"ui_step": 0.5}),
        (SUWI_MODE, "select", False, {"options": ("off", "automatic")}),
        (NIGHT_MODE, "select", False, {"options": ("off", "automatic")}),
        (
            SEASON_MODE,
            "select",
            True,
            {"options": ("always_heating", "always_cooling", "automatic")},
        ),
        (OPTIMIZATION, "switch", True, {"options": ("on", "off")}),
    ],
)
def test_settings_become_controls_only_on_an_rrc_plant(
    path: str, platform: str, enabled: bool, extra: dict[str, object]
) -> None:
    resource = circuit_tree()[path]
    assessment = assess_control(resource, RRC_CONTEXT)
    assert assessment.eligible, assessment.rejection_reason
    assert assessment.platform == platform
    assert rrc_contracts.owns(assessment.policy)
    assert assessment.enabled_by_default is enabled
    for name, value in extra.items():
        assert getattr(assessment, name) == value
    # S11: the same path on another gateway keeps today's answer.
    generic = assess_control(resource, GENERIC_CONTEXT)
    assert generic.policy is None
    assert generic.rejection_reason == "no_scalar_control_policy"
    assert rrc_contracts.contract_for(path) == "circuit"
    assert rrc_contracts.category(path) == "config"
    assert rrc_contracts.is_mirror(path)


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        # A single offered season mode (#122): read only.
        (f"{HC2}/seasonOptMode", "too_few_options"),
        # S3: limits only from metadata.
        (f"{HC2}/maxSupply", "missing_bounds"),
        # P41: unavailable without used, or malformed flags.
        (f"{HC2}/minSupply", "rrc_unavailable"),
        # CIRCUIT-01: switched off by the manufacturer, never a control.
        (HEATUP, "no_scalar_control_policy"),
        (f"{HC2}/buildingHeatup", "no_scalar_control_policy"),
        # An advertised empty list keeps the strict rules (no fallback).
        (f"{HC2}/nightSwitchMode", "missing_options"),
        # S1: only writeable settings.
        (f"{HC2}/suWiThreshold", "not_writable"),
        # S4: a step wider than the range.
        (f"{HC2}/nightThreshold", "invalid_step"),
        # Installer settings and readings: never a control.
        (f"{HC1}/control", "no_scalar_control_policy"),
        (f"{HC1}/type", "no_scalar_control_policy"),
        (f"{HC1}/typeRoomControl", "no_scalar_control_policy"),
        (SEASON, "no_scalar_control_policy"),
        (f"{HC1}/supplyTemperatureSetpoint", "no_scalar_control_policy"),
        (f"{HC1}/minOutdoorTemp", "no_scalar_control_policy"),
    ],
)
def test_settings_without_a_safe_contract_give_no_control(
    path: str, reason: str
) -> None:
    assert assess_control(circuit_tree()[path], RRC_CONTEXT).rejection_reason == (
        reason
    )


def test_room_influence_in_the_unit_other_is_a_whole_number_without_unit() -> None:
    """#122 reports ``other``; the demo shapes report ""."""
    for path in (INFLUENCE, f"{HC2}/roomInfluence"):
        assessment = assess_control(circuit_tree()[path], RRC_CONTEXT)
        assert assessment.eligible
        assert assessment.policy is rrc_contract_circuit.ROOM_INFLUENCE_POLICY
    policy = rrc_contract_circuit.ROOM_INFLUENCE_POLICY
    assert policy.wire_integer and policy.ui_unit_override == ""
    celsius = replace(
        circuit_tree()[INFLUENCE],
        metadata=replace(circuit_tree()[INFLUENCE].metadata, unit="C"),
    )
    assert assess_control(celsius, RRC_CONTEXT).rejection_reason == ("unsupported_unit")


def _flagged(resource: Resource, **flags: bool) -> Resource:
    """The resource with valid ``used``/``available`` flags."""
    return replace(
        resource,
        metadata=replace(resource.metadata, availability_flags_valid=True, **flags),
    )


@pytest.mark.parametrize("path", [OPTIMIZATION, SEASON_MODE])
@pytest.mark.parametrize(
    ("used", "available", "reason", "refused"),
    [
        (True, True, None, None),
        # Shown and locked before; now no control, as for every setting that
        # is not available.
        (True, False, "rrc_unavailable", None),
        (False, False, "rrc_unavailable", None),
        # Shown with its value, every write refused unsent (it stayed
        # writable before).
        (False, True, None, ControlUnused),
    ],
)
def test_season_mode_and_setpoint_optimization_follow_the_flags(
    path: str,
    used: bool,
    available: bool,
    reason: str | None,
    refused: type[Exception] | None,
) -> None:
    """E3: both settings depend on ``used`` and ``available``: the general
    rule of the contract settings, without ``unused_writable``."""
    resource = _flagged(circuit_tree()[path], used=used, available=available)
    assessment = assess_control(resource, RRC_CONTEXT)
    assert assessment.rejection_reason == reason
    assert not assessment.policy.unused_writable
    assert assessment.enabled_by_default  # end-user settings (E3)
    if reason is not None:
        return
    value = "on" if path == OPTIMIZATION else "always_heating"
    if refused is None:
        validate_control_value(resource, value, RRC_CONTEXT)
    else:
        with pytest.raises(refused) as raised:
            validate_control_value(resource, value, RRC_CONTEXT)
        assert raised.type is refused


@pytest.mark.parametrize(
    ("value", "eligible"),
    [
        ("automatic", True),
        ("always_cooling", True),
        # Known, but not among the offered modes.
        ("always_heating", False),
        # The empty mode is never offered.
        ("", False),
        ("unknown_mode", False),
    ],
)
def test_season_mode_needs_its_current_mode_among_the_offered_ones(
    value: str, eligible: bool
) -> None:
    """E3: no selection while the current mode is not offered, instead of a
    selection without a current option."""
    base = circuit_tree()[SEASON_MODE]
    resource = replace(
        base,
        value=value,
        metadata=replace(
            base.metadata, allowed_values=("always_cooling", "automatic", "")
        ),
    )
    assessment = assess_control(resource, RRC_CONTEXT)
    assert assessment.eligible is eligible
    if not eligible:
        assert assessment.rejection_reason == "current_option_not_advertised"
    # Only the season mode needs it.
    assert [
        policy
        for policy in rrc_contract_circuit.ENUM_POLICIES
        if policy.current_required
    ] == [rrc_contract_circuit.SEASON_MODE_POLICY]


@pytest.mark.parametrize(
    ("used", "available", "refused"),
    [
        (False, True, None),  # not used: still writable
        (True, False, ControlLocked),  # used, not available: shown and locked
        (True, True, None),
    ],
)
def test_the_other_circuit_settings_keep_their_rule(
    used: bool, available: bool, refused: type[Exception] | None
) -> None:
    """``unused_writable`` stays on the curve, the limits and the switching."""
    resource = _flagged(
        circuit_tree()[f"{HC2}/roomInfluence"], used=used, available=available
    )
    assessment = assess_control(resource, RRC_CONTEXT)
    assert assessment.eligible, assessment.rejection_reason
    assert assessment.policy is rrc_contract_circuit.ROOM_INFLUENCE_POLICY
    assert assessment.policy.unused_writable
    if refused is not None:
        with pytest.raises(refused) as raised:
            validate_control_value(resource, 1, RRC_CONTEXT)
        assert raised.type is refused  # locked, not "not used"


def test_counterparts() -> None:
    """CIRCUIT-04: each lower limit against both upper limits and back."""
    assert counterparts(CURVE_MIN) == (CURVE_MAX, SUPPLY_MAX)
    assert counterparts(SUPPLY_MIN) == (CURVE_MAX, SUPPLY_MAX)
    assert counterparts(CURVE_MAX) == (CURVE_MIN, SUPPLY_MIN)
    assert counterparts(f"{HC2}/maxSupply") == (
        f"{HC2}/heatCurveMin",
        f"{HC2}/minSupply",
    )
    for path in (INFLUENCE, SUWI_THRESHOLD, "/heatingCircuits/hc0/heatCurveMin"):
        assert counterparts(path) == ()
    # The fresh reads before a write: the limits and the control strategy.
    assert guard_paths(CURVE_MIN) == (CURVE_MAX, SUPPLY_MAX, f"{HC1}/control")
    assert guard_paths(INFLUENCE) == (f"{HC1}/control",)
    assert guard_paths(NIGHT_MODE) == (f"{HC1}/control",)
    for path in (SEASON_MODE, OPTIMIZATION, HEATUP, SEASON):
        assert guard_paths(path) == ()


def _reading(path: str, value: object) -> Resource:
    data: dict[str, object] = {"id": path, "type": "floatValue"}
    if value is not None:
        data["value"] = value
    return parse_resource(data)


def _limits(**values: object) -> dict[str, Resource]:
    names = {
        "curve_min": CURVE_MIN,
        "curve_max": CURVE_MAX,
        "supply_min": SUPPLY_MIN,
        "supply_max": SUPPLY_MAX,
    }
    return {names[key]: _reading(names[key], value) for key, value in values.items()}


@pytest.mark.parametrize(
    ("path", "value", "related", "result"),
    [
        # Equal is allowed for every pair.
        (CURVE_MIN, 60.0, _limits(curve_max=60.0, supply_max=75.0), None),
        (CURVE_MIN, 61.0, _limits(curve_max=60.0, supply_max=75.0), "rrc_curve_order"),
        (CURVE_MAX, 25.0, _limits(curve_min=25.0, supply_min=20.0), None),
        (CURVE_MAX, 24.0, _limits(curve_min=25.0, supply_min=20.0), "rrc_curve_order"),
        (SUPPLY_MIN, 60.0, _limits(curve_max=60.0, supply_max=75.0), None),
        (SUPPLY_MAX, 25.0, _limits(curve_min=25.0, supply_min=20.0), None),
        (SUPPLY_MAX, 19.0, _limits(curve_min=25.0, supply_min=20.0), "rrc_curve_order"),
        # Cross limits: the base point not above the maximum supply
        # temperature, the minimum supply temperature not above the end point.
        (CURVE_MIN, 60.0, _limits(curve_max=75.0, supply_max=50.0), "rrc_curve_order"),
        (SUPPLY_MIN, 40.0, _limits(curve_max=30.0, supply_max=75.0), "rrc_curve_order"),
        (CURVE_MAX, 30.0, _limits(curve_min=25.0, supply_min=40.0), "rrc_curve_order"),
        (SUPPLY_MAX, 30.0, _limits(curve_min=35.0, supply_min=20.0), "rrc_curve_order"),
        # The two lower (and the two upper) limits do not depend on each other.
        (
            CURVE_MIN,
            60.0,
            _limits(curve_max=70.0, supply_max=70.0, supply_min=65.0),
            None,
        ),
        # An unknown or missing reading of a present limit refuses.
        (CURVE_MIN, 30.0, _limits(curve_max="high"), "rrc_curve_order"),
        (CURVE_MIN, 30.0, _limits(curve_max=None), "rrc_curve_order"),
        (CURVE_MIN, 30.0, _limits(supply_max=True), "rrc_curve_order"),
    ],
)
def test_order_check(
    path: str, value: float, related: dict[str, Resource], result
) -> None:
    assert check_order(circuit_tree()[path], value, related) == result


def _control(value: object, **extra: object) -> Resource:
    data: dict[str, object] = {"id": f"{HC1}/control", "type": "stringValue"}
    if value is not None:
        data["value"] = value
    return parse_resource({**data, **extra})


@pytest.mark.parametrize(
    ("control", "result"),
    [
        (_control("weather"), None),
        (_control("room"), "rrc_weather_control_off"),
        (_control("none"), "rrc_weather_control_off"),
        (_control(None), "rrc_weather_control_off"),
        (_control(1), "rrc_weather_control_off"),
        # A circuit without a current reading keeps the setting.
        (None, None),
    ],
)
@pytest.mark.parametrize("path", [CURVE_MIN, INFLUENCE, SUWI_MODE, NIGHT_THRESHOLD])
def test_weather_settings_need_weather_compensated_control(
    path: str, control: Resource | None, result: str | None
) -> None:
    """CIRCUIT-03: only with ``control`` "weather"."""
    related = {} if control is None else {control.path: control}
    assert check_weather_control(circuit_tree()[path], 1.0, related) == result
    policy = assess_control(circuit_tree()[path], RRC_CONTEXT).policy
    assert policy is not None and policy.value_check is not None
    reason = policy.value_check(circuit_tree()[path], 30.0, related)
    assert reason == result


def test_other_settings_do_not_depend_on_the_control_strategy() -> None:
    for path in (SEASON_MODE, OPTIMIZATION):
        policy = assess_control(circuit_tree()[path], RRC_CONTEXT).policy
        assert policy is not None and policy.value_check is None
    assert set(rrc_contract_circuit.WEATHER_SETTINGS) == {
        "heatCurveMin",
        "heatCurveMax",
        "minSupply",
        "maxSupply",
        "roomInfluence",
        "suWiSwitchMode",
        "suWiThreshold",
        "nightSwitchMode",
        "nightThreshold",
    }


def test_order_check_without_the_other_limit_keeps_the_metadata_limits() -> None:
    resource = circuit_tree()[f"{HC2}/heatCurveMin"]
    assert check_order(resource, 89.0, {}) is None
    assert check_order(circuit_tree()[INFLUENCE], 3.0, {}) is None


class _Client:
    def __init__(self, resource: Resource) -> None:
        self.resource = resource
        self.puts: list[object] = []

    async def get_resource(self, gateway_id, path):
        return self.resource

    async def put_resource_value(self, gateway_id, path, value):
        self.puts.append(value)
        self.resource = replace(self.resource, value=value)


async def _no_sleep(_delay: float) -> None:
    return None


async def test_room_influence_goes_out_as_an_integer() -> None:
    """S2: ``int`` on the wire; a fraction is refused, never cut."""
    resource = circuit_tree()[INFLUENCE]
    client = _Client(resource)
    service = WriteService(client, sleep=_no_sleep)  # type: ignore[arg-type]
    policy = rrc_contract_circuit.ROOM_INFLUENCE_POLICY
    result = await service.async_write_number(
        "gw", resource, 2.0, policy, context=RRC_CONTEXT
    )
    assert client.puts == [2] and type(client.puts[0]) is int
    assert result.resource.value == 2
    with pytest.raises(WriteValidationError):
        await service.async_write_number(
            "gw", client.resource, 1.5, policy, context=RRC_CONTEXT
        )
    assert client.puts == [2]


@pytest.mark.parametrize(
    ("path", "value"),
    [(CURVE_MIN, 25.5), (SUWI_THRESHOLD, 16.25), (NIGHT_THRESHOLD, 5.2)],
)
async def test_values_off_the_grid_are_refused(path: str, value: float) -> None:
    """S4: the advertised step from minValue; never rounded."""
    resource = circuit_tree()[path]
    client = _Client(resource)
    service = WriteService(client, sleep=_no_sleep)  # type: ignore[arg-type]
    policy = assess_control(resource, RRC_CONTEXT).policy
    with pytest.raises(WriteValidationError):
        await service.async_write_number(
            "gw", resource, value, policy, context=RRC_CONTEXT
        )
    assert client.puts == []


async def test_order_is_checked_before_sending() -> None:
    resource = circuit_tree()[CURVE_MIN]
    client = _Client(resource)
    service = WriteService(client, sleep=_no_sleep)  # type: ignore[arg-type]
    for value, related, reason in (
        (61.0, {CURVE_MAX: circuit_tree()[CURVE_MAX]}, "rrc_curve_order"),
        (
            30.0,
            {f"{HC1}/control": _control("room")},
            "rrc_weather_control_off",
        ),
    ):
        with pytest.raises(ContractRejected) as refused:
            await service.async_write_number(
                "gw",
                resource,
                value,
                rrc_contract_circuit.HEAT_CURVE_MIN_POLICY,
                context=RRC_CONTEXT,
                related=related,
            )
        assert refused.value.translation_key == reason
    assert client.puts == []


def test_translation_keys_exist_for_every_entity_and_option() -> None:
    strings = json.loads(STRINGS.read_text("utf-8"))
    tree = circuit_tree()
    for path in tree:
        for platform in ("number", "select", "switch", "sensor"):
            key = rrc_contracts.entity_key(path, platform)
            if key is None:
                continue
            entry = strings["entity"][platform][key]
            assert entry["name"]
            for option in rrc_contracts.option_keys(path).values():
                assert entry["state"][option]
    for season in rrc_contract_circuit.OPERATING_SEASONS:
        assert strings["entity"]["sensor"]["rrc_operating_season"]["state"][season]
    for key in ("rrc_curve_order", "rrc_weather_control_off"):
        assert strings["exceptions"][key]["message"]
    for key in (
        "controls_curve_order",
        "controls_weather_control_off",
        "controls_locked",
        "controls_unused",
        "controls_rejected",
    ):
        assert strings["options"]["error"][key]
    # CIRCUIT-01: the heat-up speed has no selection any more.
    assert "rrc_building_heatup" not in strings["entity"]["select"]


def test_diagnostics_are_value_free() -> None:
    report = rrc_contracts.diagnostics(plant())["circuit"]
    assert report["confirmation"] == "unconfirmed"
    assert report["circuits"] == 2
    settings = report["settings"]
    assert settings["heatCurveMin"] == {
        "present": 2,
        "writable": 2,
        "unavailable": 0,
        "with_limits": 2,
    }
    assert settings["buildingHeatup"]["unavailable"] == 1
    assert settings["seasonOptMode"]["offered_modes"] == 1  # the last one read
    text = json.dumps(report)
    for marker in ("automatic", "heating", "radiator", "weather", "60.0"):
        assert marker not in text


# --------------------------------------------------------------- real setup


@pytest.fixture(autouse=True)
def legacy_setup_path():
    with patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)):
        yield


async def _setup(
    hass: HomeAssistant,
    cloud: RrcCloud,
    *,
    enabled: tuple[str | tuple[str, str], ...] = (),
    gateway: Gateway = RRC_GATEWAY,
):
    entry = _entry(hass)
    registry = er.async_get(hass)
    for item in enabled:
        # The user enabled these entities (they are disabled by default); a
        # bare unique ID is a number.
        domain, unique_id = item if isinstance(item, tuple) else ("number", item)
        registry.async_get_or_create(
            domain, DOMAIN, unique_id, config_entry=entry, disabled_by=None
        )
    clock = Clock()
    stack = cloud_patches(cloud, clock, gateway)
    stack.__enter__()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinators[0]
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    coordinator._write_service._sleep = sleep
    return entry, coordinator, sleeps, stack, clock


async def _teardown(hass: HomeAssistant, entry, stack) -> None:
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stack.close()


def _entity_id(hass: HomeAssistant, domain: str, unique_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)
    assert entity_id is not None, unique_id
    return entity_id


async def _set_number(hass: HomeAssistant, entity_id: str, value: float) -> None:
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
    )


async def _select(hass: HomeAssistant, entity_id: str, option: str) -> None:
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": entity_id, "option": option},
        blocking=True,
    )


ALL_LIMITS = (uid(CURVE_MIN), uid(CURVE_MAX), uid(SUPPLY_MIN), uid(SUPPLY_MAX))


async def test_entities_on_a_controller(hass: HomeAssistant) -> None:
    """Numbers disabled by default (W4-E1), as are the summer/winter and
    night settings (CIRCUIT-03); the season mode and the switch enabled, all
    configuration; mirrors disabled; nothing for refusals or the heat-up
    speed (CIRCUIT-01)."""
    cloud = CircuitCloud(plant())
    entry, _coordinator, _sleeps, stack, _clock = await _setup(hass, cloud)
    registry = er.async_get(hass)
    try:
        expected = {
            ("number", uid(CURVE_MIN)): True,
            ("number", uid(CURVE_MAX)): True,
            ("number", uid(SUPPLY_MIN)): True,
            ("number", uid(SUPPLY_MAX)): True,
            ("number", uid(INFLUENCE)): True,
            ("number", uid(f"{HC2}/roomInfluence")): True,
            ("number", uid(f"{HC2}/heatCurveMin")): True,
            ("number", uid(SUWI_THRESHOLD)): True,
            ("number", uid(NIGHT_THRESHOLD)): True,
            ("select", uid(SUWI_MODE)): True,
            ("select", uid(NIGHT_MODE)): True,
            ("select", uid(SEASON_MODE)): False,
            ("switch", uid(OPTIMIZATION)): False,
        }
        for (domain, unique_id), disabled in expected.items():
            entity = registry.async_get(_entity_id(hass, domain, unique_id))
            assert entity is not None
            assert (entity.disabled_by is not None) is disabled, unique_id
            assert entity.entity_category is EntityCategory.CONFIG, unique_id
            assert entity.translation_key is not None, unique_id
        for domain, path in (
            # E3: used but not available, as every unavailable setting.
            ("switch", f"{HC2}/setpointOptimization"),
            ("select", f"{HC2}/seasonOptMode"),
            ("number", f"{HC2}/maxSupply"),
            ("number", f"{HC2}/minSupply"),
            ("select", HEATUP),
            ("select", f"{HC2}/buildingHeatup"),
            ("select", f"{HC2}/nightSwitchMode"),
            ("number", f"{HC2}/suWiThreshold"),
            ("number", f"{HC2}/nightThreshold"),
            ("select", f"{HC1}/control"),
            ("select", f"{HC1}/type"),
            ("select", f"{HC1}/typeRoomControl"),
        ):
            assert registry.async_get_entity_id(domain, DOMAIN, uid(path)) is None
        # The switch has both values: no selection beside it.
        assert (
            registry.async_get_entity_id(
                "select", DOMAIN, uid(OPTIMIZATION, "options:control")
            )
            is None
        )
        influence = registry.async_get(
            _entity_id(hass, "number", uid(f"{HC2}/roomInfluence"))
        )
        assert influence.unit_of_measurement is None
        assert influence.capabilities["step"] == 1.0
        season_mode = hass.states.get(_entity_id(hass, "select", uid(SEASON_MODE)))
        assert season_mode.attributes["options"] == [
            "always_heating",
            "always_cooling",
            "automatic",
        ]
        assert season_mode.state == "automatic"
        switch = hass.states.get(_entity_id(hass, "switch", uid(OPTIMIZATION)))
        assert switch.state == "off"

        # The operating season: an enum sensor, unknown for other readings.
        sensor_id = _entity_id(hass, "sensor", uid(SEASON, "season"))
        season = hass.states.get(sensor_id)
        assert season.state == "heating"
        assert season.attributes["options"] == ["heating", "cooling", "idle"]
        assert season.attributes["device_class"] == "enum"
        assert (
            hass.states.get(
                _entity_id(hass, "sensor", uid(f"{HC2}/operatingSeason", "season"))
            ).state
            == "idle"
        )
        # Read-only mirrors of the controls, and the heat-up speed that is
        # only read: disabled diagnostic sensors.
        for path in (SUWI_MODE, SUWI_THRESHOLD, CURVE_MIN, HEATUP):
            mirror_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"gateway-one:{path.strip('/').replace('/', ':')}"
            )
            assert mirror_id is not None, path
            mirror = registry.async_get(mirror_id)
            assert mirror.disabled_by is not None, path
            assert mirror.entity_category is EntityCategory.DIAGNOSTIC, path
    finally:
        await _teardown(hass, entry, stack)


async def test_curve_writes_read_both_limits_and_keep_the_order(
    hass: HomeAssistant,
) -> None:
    """CIRCUIT-04: each lower limit at or below each upper limit, equal
    allowed; the limits and the control strategy are read first (S5)."""
    cloud = CircuitCloud(plant())
    entry, coordinator, sleeps, stack, _clock = await _setup(
        hass, cloud, enabled=ALL_LIMITS
    )
    curve_min = _entity_id(hass, "number", uid(CURVE_MIN))
    curve_max = _entity_id(hass, "number", uid(CURVE_MAX))
    supply_min = _entity_id(hass, "number", uid(SUPPLY_MIN))
    supply_max = _entity_id(hass, "number", uid(SUPPLY_MAX))
    control = f"{HC1}/control"
    try:
        assert hass.states.get(curve_max).state == "60.0"
        before = cloud.calls.copy()
        await _set_number(hass, curve_max, 65)
        # S5: the lower limits and the control strategy read freshly in the
        # same request, then exactly one PUT as a float.
        for path in (CURVE_MAX, CURVE_MIN, SUPPLY_MIN, control):
            assert cloud.calls["BULK", path] == before["BULK", path] + 1, path
        assert cloud.calls["BULK", SUPPLY_MAX] == before["BULK", SUPPLY_MAX]
        assert cloud.written("PUT") == [("PUT", CURVE_MAX, 65.0)]
        assert type(cloud.written("PUT")[0][2]) is float
        assert sleeps == [1.0]
        assert hass.states.get(curve_max).state == "65.0"

        # The order, with the cross limits: refused before sending.
        for entity_id, value in (
            (curve_min, 66),  # above the end point
            (curve_min, 76),  # above both upper limits
            (supply_min, 66),  # above the end point (cross)
        ):
            with pytest.raises(ServiceValidationError) as refused:
                await _set_number(hass, entity_id, value)
            assert refused.value.translation_key == "rrc_curve_order", entity_id
        assert len(cloud.written("PUT")) == 1
        # Equal limits are allowed, also base point and end point.
        await _set_number(hass, curve_min, 65)
        assert cloud.written("PUT")[-1] == ("PUT", CURVE_MIN, 65.0)
        for entity_id in (curve_max, supply_max):
            # Below the base point, also for the maximum supply temperature.
            with pytest.raises(ServiceValidationError) as below:
                await _set_number(hass, entity_id, 64)
            assert below.value.translation_key == "rrc_curve_order", entity_id
        await _set_number(hass, supply_min, 65)
        assert cloud.written("PUT")[-1] == ("PUT", SUPPLY_MIN, 65.0)
        assert len(cloud.written("PUT")) == 3

        # The other limit changed on the gateway: nothing is sent, and the
        # new reading is taken over.
        cloud.tree[CURVE_MAX] = replace(cloud.tree[CURVE_MAX], value=50.0)
        with pytest.raises(ServiceValidationError) as changed:
            await _set_number(hass, curve_min, 45)
        assert changed.value.translation_key == "write_validation_failed"
        assert len(cloud.written("PUT")) == 3
        await hass.async_block_till_done()
        assert coordinator.data[CURVE_MAX].resource.value == 50.0
        with pytest.raises(ServiceValidationError) as order:
            await _set_number(hass, curve_min, 55)
        assert order.value.translation_key == "rrc_curve_order"
        await _set_number(hass, curve_min, 30)
        assert cloud.written("PUT")[-1] == ("PUT", CURVE_MIN, 30.0)

        # A plant without one limit keeps the others and the metadata limits.
        del cloud.tree[SUPPLY_MAX]
        snapshots = dict(coordinator.data)
        del snapshots[SUPPLY_MAX]
        coordinator.async_set_updated_data(snapshots)
        with pytest.raises(ServiceValidationError):
            await _set_number(hass, supply_min, 51)
        await _set_number(hass, supply_min, 50)
        assert cloud.written("PUT")[-1] == ("PUT", SUPPLY_MIN, 50.0)

        stats = coordinator.rrc_write_stats.snapshot("circuit")
        assert stats["writes_sent"] == 5
        assert stats["writes_confirmed"] == 5
        assert stats["rejection_reasons"]["rrc_curve_order"] == 7
    finally:
        await _teardown(hass, entry, stack)


WEATHER_ENTITIES = (
    *ALL_LIMITS,
    uid(INFLUENCE),
    uid(SUWI_THRESHOLD),
    uid(NIGHT_THRESHOLD),
    ("select", uid(SUWI_MODE)),
    ("select", uid(NIGHT_MODE)),
)


async def test_weather_settings_only_with_weather_compensated_control(
    hass: HomeAssistant,
) -> None:
    """CIRCUIT-03 in real cycles: in room control every setting of
    weather-compensated control refuses unsent; the others stay."""
    tree = plant()
    control = f"{HC1}/control"
    tree[control] = replace(tree[control], value="room")
    cloud = CircuitCloud(tree)
    entry, coordinator, _sleeps, stack, _clock = await _setup(
        hass, cloud, enabled=WEATHER_ENTITIES
    )
    try:
        for domain, path, value in (
            ("number", CURVE_MIN, 26),
            ("number", CURVE_MAX, 61),
            ("number", SUPPLY_MIN, 21),
            ("number", SUPPLY_MAX, 74),
            ("number", INFLUENCE, 2),
            ("number", SUWI_THRESHOLD, 17.5),
            ("number", NIGHT_THRESHOLD, 10.5),
            ("select", SUWI_MODE, "off"),
            ("select", NIGHT_MODE, "automatic"),
        ):
            entity_id = _entity_id(hass, domain, uid(path))
            with pytest.raises(ServiceValidationError) as refused:
                if domain == "number":
                    await _set_number(hass, entity_id, value)
                else:
                    await _select(hass, entity_id, value)
            assert refused.value.translation_key == "rrc_weather_control_off", path
        assert cloud.written() == []
        # Settings outside weather-compensated control are not affected.
        await _select(
            hass, _entity_id(hass, "select", uid(SEASON_MODE)), "always_heating"
        )
        assert cloud.written("PUT") == [("PUT", SEASON_MODE, "always_heating")]
        stats = coordinator.rrc_write_stats.snapshot("circuit")
        assert stats["rejection_reasons"]["rrc_weather_control_off"] == 9

        # Weather-compensated control again: the change is read before the
        # write (S5), so the first write after it sends nothing and the
        # next one goes out.
        cloud.tree[control] = replace(cloud.tree[control], value="weather")
        curve_max = _entity_id(hass, "number", uid(CURVE_MAX))
        with pytest.raises(ServiceValidationError) as changed:
            await _set_number(hass, curve_max, 61)
        assert changed.value.translation_key == "write_validation_failed"
        await hass.async_block_till_done()
        await _set_number(hass, curve_max, 61)
        await _select(hass, _entity_id(hass, "select", uid(SUWI_MODE)), "off")
        assert cloud.written("PUT")[1:] == [
            ("PUT", CURVE_MAX, 61.0),
            ("PUT", SUWI_MODE, "off"),
        ]

        # Room control again, set after the last poll: the number reads it
        # freshly and sends nothing; a later write is refused with the
        # message.
        cloud.tree[control] = replace(cloud.tree[control], value="room")
        with pytest.raises(ServiceValidationError) as changed_again:
            await _set_number(hass, curve_max, 62)
        assert changed_again.value.translation_key == "write_validation_failed"
        await hass.async_block_till_done()
        with pytest.raises(ServiceValidationError) as off:
            await _set_number(hass, curve_max, 62)
        assert off.value.translation_key == "rrc_weather_control_off"
        assert len(cloud.written("PUT")) == 3
    finally:
        await _teardown(hass, entry, stack)


async def test_other_limit_without_a_current_reading_blocks_the_write(
    hass: HomeAssistant,
) -> None:
    """The order cannot be checked: nothing is sent."""
    cloud = CircuitCloud(plant())
    entry, coordinator, _sleeps, stack, _clock = await _setup(
        hass, cloud, enabled=ALL_LIMITS
    )
    try:
        snapshots = dict(coordinator.data)
        snapshots[CURVE_MAX] = replace(
            snapshots[CURVE_MAX], freshness=Freshness.STALE, available=False
        )
        coordinator.async_set_updated_data(snapshots)
        with pytest.raises(ServiceValidationError) as refused:
            await _set_number(hass, _entity_id(hass, "number", uid(CURVE_MIN)), 30)
        assert refused.value.translation_key == "write_validation_failed"
        assert cloud.written() == []
    finally:
        await _teardown(hass, entry, stack)


async def test_a_stale_control_strategy_does_not_refuse_the_write(
    hass: HomeAssistant,
) -> None:
    """VER-05: after a failed read of the control strategy the number sends
    its write as the dialog does; it is not refused as "changed" until the
    next poll. The limits are still read freshly with it."""
    cloud = CircuitCloud(plant())
    entry, coordinator, _sleeps, stack, _clock = await _setup(
        hass, cloud, enabled=ALL_LIMITS
    )
    control = f"{HC1}/control"
    try:
        snapshots = dict(coordinator.data)
        snapshots[control] = replace(snapshots[control], freshness=Freshness.STALE)
        coordinator.async_set_updated_data(snapshots)
        before = cloud.calls.copy()
        await _set_number(hass, _entity_id(hass, "number", uid(CURVE_MAX)), 65)
        assert cloud.written("PUT") == [("PUT", CURVE_MAX, 65.0)]
        assert cloud.calls["BULK", CURVE_MIN] == before["BULK", CURVE_MIN] + 1
    finally:
        await _teardown(hass, entry, stack)


async def test_room_influence_and_thresholds_through_the_setup(
    hass: HomeAssistant,
) -> None:
    cloud = CircuitCloud(plant())
    entry, _coordinator, _sleeps, stack, _clock = await _setup(
        hass, cloud, enabled=(uid(INFLUENCE), uid(SUWI_THRESHOLD))
    )
    try:
        influence = _entity_id(hass, "number", uid(INFLUENCE))
        before = cloud.calls["BULK", INFLUENCE]
        await _set_number(hass, influence, 3)
        assert cloud.calls["BULK", INFLUENCE] == before + 1  # S5
        assert cloud.written("PUT") == [("PUT", INFLUENCE, 3)]
        assert type(cloud.written("PUT")[0][2]) is int
        with pytest.raises(ServiceValidationError):
            await _set_number(hass, influence, 1.5)
        threshold = _entity_id(hass, "number", uid(SUWI_THRESHOLD))
        with pytest.raises(ServiceValidationError):
            await _set_number(hass, threshold, 16.25)
        await _set_number(hass, threshold, 17.5)
        assert cloud.written("PUT")[-1] == ("PUT", SUWI_THRESHOLD, 17.5)
        assert len(cloud.written("PUT")) == 2
    finally:
        await _teardown(hass, entry, stack)


async def test_selections_and_switch_write_once_with_read_back(
    hass: HomeAssistant,
) -> None:
    tree = plant()
    # Used but not available: a setting with ``unused_writable`` is locked.
    tree[INFLUENCE] = _flagged(tree[INFLUENCE], used=True, available=False)
    cloud = CircuitCloud(tree)
    entry, _coordinator, sleeps, stack, _clock = await _setup(
        hass,
        cloud,
        enabled=(
            ("select", uid(SUWI_MODE)),
            ("select", uid(NIGHT_MODE)),
            uid(INFLUENCE),
        ),
    )
    try:
        suwi = hass.states.get(_entity_id(hass, "select", uid(SUWI_MODE)))
        assert suwi.state == "automatic"
        assert suwi.attributes["options"] == ["off", "automatic"]
        await _select(hass, _entity_id(hass, "select", uid(SUWI_MODE)), "off")
        await _select(hass, _entity_id(hass, "select", uid(NIGHT_MODE)), "automatic")
        await _select(
            hass, _entity_id(hass, "select", uid(SEASON_MODE)), "always_heating"
        )
        await hass.services.async_call(
            "switch",
            "turn_on",
            {"entity_id": _entity_id(hass, "switch", uid(OPTIMIZATION))},
            blocking=True,
        )
        assert cloud.written("PUT") == [
            ("PUT", SUWI_MODE, "off"),
            ("PUT", NIGHT_MODE, "automatic"),
            ("PUT", SEASON_MODE, "always_heating"),
            ("PUT", OPTIMIZATION, "on"),
        ]
        assert sleeps == [1.0] * 4
        assert (
            hass.states.get(_entity_id(hass, "select", uid(NIGHT_MODE))).state
            == "automatic"
        )
        # The empty season mode is never offered.
        with pytest.raises(ServiceValidationError):
            await _select(hass, _entity_id(hass, "select", uid(SEASON_MODE)), "")
        # E3: the controller stops using the setpoint optimization; the
        # fresh read before the write finds it: shown, refused, not sent.
        switch_id = _entity_id(hass, "switch", uid(OPTIMIZATION))
        cloud.tree[OPTIMIZATION] = _flagged(
            cloud.tree[OPTIMIZATION], used=False, available=True
        )
        with pytest.raises(ServiceValidationError) as unused:
            await hass.services.async_call(
                "switch", "turn_off", {"entity_id": switch_id}, blocking=True
            )
        assert unused.value.translation_key == "rrc_setting_unused"
        assert hass.states.get(switch_id).state == "on"
        # S8 for a setting that keeps ``unused_writable``: used but
        # unavailable is shown, locked, nothing sent.
        influence = _entity_id(hass, "number", uid(INFLUENCE))
        assert hass.states.get(influence).state == "1.0"  # shown
        with pytest.raises(ServiceValidationError) as locked:
            await _set_number(hass, influence, 2)
        assert locked.value.translation_key == "rrc_circuit_setting_locked"
        assert len(cloud.written("PUT")) == 4
    finally:
        await _teardown(hass, entry, stack)


async def test_write_failure_paths_in_real_cycles(hass: HomeAssistant) -> None:
    """Late adoption, timeout and 5xx after the PUT, 204 without effect, 403
    and 429: never a second PUT; a regular poll confirms a late change."""
    cloud = CircuitCloud(plant())
    entry, coordinator, sleeps, stack, clock = await _setup(
        hass, cloud, enabled=ALL_LIMITS
    )
    curve_max = _entity_id(hass, "number", uid(CURVE_MAX))
    season_mode = _entity_id(hass, "select", uid(SEASON_MODE))

    async def poll() -> None:
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    try:
        # Applied at the third read: confirmed after 1, 2 and 4 seconds.
        cloud.adopt_after[CURVE_MAX] = 3
        await _set_number(hass, curve_max, 70)
        assert sleeps == [1.0, 2.0, 4.0]
        del cloud.adopt_after[CURVE_MAX]

        # Applied only at the fifth read: unconfirmed, the next regular poll
        # confirms it late.
        sleeps.clear()
        cloud.adopt_after[CURVE_MAX] = 5
        with pytest.raises(HomeAssistantError) as pending:
            await _set_number(hass, curve_max, 72)
        assert pending.value.translation_key == "rrc_write_pending_confirmation"
        assert sleeps == [1.0, 2.0, 4.0, 8.0]
        assert coordinator.data[CURVE_MAX].freshness is Freshness.STALE
        assert hass.states.get(curve_max).state == "unavailable"
        await poll()
        assert hass.states.get(curve_max).state == "72.0"
        assert coordinator.rrc_write_stats.snapshot("circuit")["late_confirmed"] == 1
        del cloud.adopt_after[CURVE_MAX]

        # A timeout after an applied PUT: confirmed by reading alone.
        cloud.timeout_after_write[SEASON_MODE] = True
        await _select(hass, season_mode, "always_cooling")
        del cloud.timeout_after_write[SEASON_MODE]

        # A server error after the PUT, not applied: unconfirmed.
        cloud.server_error_after_write[SEASON_MODE] = False
        with pytest.raises(HomeAssistantError) as server:
            await _select(hass, season_mode, "always_heating")
        assert server.value.translation_key == "rrc_write_pending_confirmation"
        del cloud.server_error_after_write[SEASON_MODE]
        await poll()
        assert hass.states.get(season_mode).state == "always_cooling"

        # 204 without effect: unconfirmed and never sent again.
        cloud.ignore_writes.add(CURVE_MAX)
        with pytest.raises(HomeAssistantError) as ignored:
            await _set_number(hass, curve_max, 74)
        assert ignored.value.translation_key == "rrc_write_pending_confirmation"
        cloud.ignore_writes.clear()
        await poll()
        assert hass.states.get(curve_max).state == "72.0"

        # 403 on the PUT: refused, not repeated.
        cloud.forbidden.add(SEASON_MODE)
        with pytest.raises(HomeAssistantError) as forbidden:
            await _select(hass, season_mode, "automatic")
        assert forbidden.value.translation_key == "write_failed"
        cloud.forbidden.discard(SEASON_MODE)
        await poll()

        # 429 on the PUT: the brake stops the next write before sending.
        cloud.rate_limit_writes[CURVE_MAX] = 120
        with pytest.raises(HomeAssistantError) as limited:
            await _set_number(hass, curve_max, 73)
        assert limited.value.translation_key == "write_rate_limited"
        del cloud.rate_limit_writes[CURVE_MAX]
        with pytest.raises(HomeAssistantError) as braked:
            await _set_number(hass, curve_max, 73)
        assert braked.value.translation_key == "write_rate_limited"

        puts = [item[1] for item in cloud.written("PUT")]
        assert puts.count(CURVE_MAX) == 4
        assert puts.count(SEASON_MODE) == 3
        stats = coordinator.rrc_write_stats.snapshot("circuit")
        assert stats["writes_sent"] == 7
        assert stats["writes_confirmed"] == 2
        assert stats["writes_unconfirmed"] == 3

        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        report = diagnostics["gateways"][0]["rrc_features"]["contracts"]["circuit"]
        assert report["confirmation"] == "unconfirmed"
        assert report["writes"]["writes_sent"] == 7
        assert report["settings"]["heatCurveMax"]["present"] == 1
    finally:
        await _teardown(hass, entry, stack)


async def test_other_gateways_get_nothing_of_the_contract(hass: HomeAssistant) -> None:
    """S11: the same leaves below a heat pump gateway stay as they were."""
    k40 = load_tree("captures/k40-cloud-2026-11-05.json")
    tree = {**k40, **circuit_tree()}
    hc1 = k40[HC1]
    combined = parse_resource(
        {
            "id": HC1,
            "type": "refEnum",
            "references": [
                {"id": path}
                for path in sorted(
                    {
                        *(ref.path for ref in hc1.references),
                        *(ref.path for ref in circuit_tree()[HC1].references),
                    }
                )
            ],
        }
    )
    tree[HC1] = combined
    tree["/heatingCircuits"] = k40["/heatingCircuits"]
    for path in list(tree):
        if path.startswith(f"{HC2}"):
            del tree[path]
    cloud = CircuitCloud(tree, forbidden=set(), missing_as_500=False)
    entry, coordinator, _sleeps, stack, _clock = await _setup(
        hass, cloud, gateway=Gateway("gateway-one", device_type="k40")
    )
    registry = er.async_get(hass)
    try:
        assert not coordinator.control_context().rrc_plant
        assert CURVE_MIN in coordinator.data
        for path in rrc_contract_circuit.SETTING_NAMES:
            for domain in ("number", "select", "switch"):
                assert (
                    registry.async_get_entity_id(domain, DOMAIN, uid(f"{HC1}/{path}"))
                    is None
                ), path
        assert registry.async_get_entity_id(
            "sensor", DOMAIN, uid(SEASON, "season")
        ) is (None)
        assert cloud.written() == []
    finally:
        await _teardown(hass, entry, stack)


def test_entity_module_tracks_numbers_and_sensors() -> None:
    assert set(rrc_circuit_entities.TRACKERS) == {"number", "sensor"}


def test_generic_number_builder_leaves_the_circuit_numbers_out() -> None:
    """Only the circuit's own numbers (with the fresh reads) are created."""
    from custom_components.bosch_buderus_heating.number import (
        build_number_descriptions,
    )

    tree = plant()
    generic = {
        item.resource_path for item in build_number_descriptions(tree, RRC_CONTEXT)
    }
    own = {
        item.resource_path
        for item in rrc_circuit_entities.build_number_descriptions(tree, RRC_CONTEXT)
    }
    assert not generic & own
    assert own == {
        CURVE_MIN,
        CURVE_MAX,
        SUPPLY_MIN,
        SUPPLY_MAX,
        INFLUENCE,
        SUWI_THRESHOLD,
        NIGHT_THRESHOLD,
        f"{HC2}/roomInfluence",
        f"{HC2}/heatCurveMin",
    }
    assert rrc_circuit_entities.build_number_descriptions(tree, GENERIC_CONTEXT) == ()


@pytest.mark.parametrize(
    "name",
    [
        *rrc_contract_circuit.INSTALLER_SETTINGS,
        *rrc_contract_circuit.READ_ONLY_SETTINGS,
    ],
)
def test_installer_settings_have_no_policy(name: str) -> None:
    """Also the heat-up speed (CIRCUIT-01), switched off by the maker."""
    resource = circuit_tree()[f"{HC1}/{name}"]
    assert resource.metadata.writable  # writeable on the wire, still refused
    assert assess_control(resource, RRC_CONTEXT).policy is None


@pytest.mark.parametrize(
    ("flags", "created"),
    [
        ({"available": "false"}, False),  # P41
        ({"available": "maybe"}, False),  # malformed flags
        ({"used": "true", "available": "false"}, True),
        ({}, True),
    ],
)
def test_operating_season_sensor_follows_p41(
    flags: dict[str, object], created: bool
) -> None:
    season = parse_resource(
        {"id": SEASON, "type": "stringValue", "writeable": 0, "value": "idle", **flags}
    )
    built = rrc_circuit_entities.build_sensor_descriptions(
        {SEASON: season}, RRC_CONTEXT
    )
    assert bool(built) is created


# ------------------------------------------------------- controls dialog


async def _dialog(hass: HomeAssistant, entry):
    from custom_components.bosch_buderus_heating.config_flow import (
        BoschBuderusOptionsFlow,
    )

    flow = BoschBuderusOptionsFlow(entry)
    flow.hass = hass
    opened = await flow.async_step_controls()
    assert opened["step_id"] == "operate"
    return flow, opened


def _dialog_config(result) -> dict[str, Any]:
    selector = next(iter(result["data_schema"].schema.values()))
    config: dict[str, Any] = selector.serialize()["selector"]["bosch-buderus-controls"]
    return config


async def test_dialog_changes_several_limits_in_a_safe_order(
    hass: HomeAssistant,
) -> None:
    """CIRCUIT-08: lowering base and end point in one step writes the base
    point first; raising them writes the end point first."""
    tree = plant()
    tree[CURVE_MIN] = replace(tree[CURVE_MIN], value=40.0)
    cloud = CircuitCloud(tree)
    entry, _coordinator, _sleeps, stack, _clock = await _setup(hass, cloud)
    try:
        flow, _ = await _dialog(hass, entry)
        # The end point below the current base point: in name order the end
        # point would go first and be refused.
        lowered = await flow.async_step_operate(
            {"controls": {"changes": {CURVE_MAX: 35, CURVE_MIN: 30}}}
        )
        assert not lowered["errors"]
        assert cloud.written("PUT") == [
            ("PUT", CURVE_MIN, 30.0),
            ("PUT", CURVE_MAX, 35.0),
        ]
        flow, _ = await _dialog(hass, entry)
        raised = await flow.async_step_operate(
            {"controls": {"changes": {CURVE_MIN: 70, SUPPLY_MIN: 70, CURVE_MAX: 80}}}
        )
        assert not raised["errors"]
        assert cloud.written("PUT")[2:] == [
            ("PUT", CURVE_MAX, 80.0),
            ("PUT", CURVE_MIN, 70.0),
            ("PUT", SUPPLY_MIN, 70.0),
        ]
    finally:
        await _teardown(hass, entry, stack)


async def test_dialog_refuses_a_draft_out_of_order_unsent(
    hass: HomeAssistant,
) -> None:
    """CIRCUIT-08: the order of the whole draft is checked before anything
    is written, with its own message and the field marked."""
    cloud = CircuitCloud(plant())
    entry, _coordinator, _sleeps, stack, _clock = await _setup(hass, cloud)
    try:
        flow, _ = await _dialog(hass, entry)
        result = await flow.async_step_operate(
            {"controls": {"changes": {SEASON_MODE: "always_heating", CURVE_MIN: 70}}}
        )
        assert result["errors"] == {"base": "controls_curve_order"}
        assert _dialog_config(result)["invalid"] == [CURVE_MIN]
        assert cloud.written() == []
        # Not available (E3: also when used): not offered in the dialog; a
        # lock found only while writing has its own message
        # (test_heating_controls::test_dialog_partial_write_failure_is_explicit).
        _flow, opened = await _dialog(hass, entry)
        keys = {row["key"] for row in _dialog_config(opened)["rows"]}
        assert f"{HC2}/setpointOptimization" not in keys
        assert cloud.written() == []
    finally:
        await _teardown(hass, entry, stack)


async def test_dialog_offers_weather_settings_only_with_weather_control(
    hass: HomeAssistant,
) -> None:
    """CIRCUIT-03: in room control the dialog leaves the settings of
    weather-compensated control out; the others stay."""
    tree = plant()
    control = f"{HC1}/control"
    tree[control] = replace(tree[control], value="room")
    cloud = CircuitCloud(tree)
    entry, _coordinator, _sleeps, stack, _clock = await _setup(hass, cloud)
    try:
        _flow, opened = await _dialog(hass, entry)
        keys = {row["key"] for row in _dialog_config(opened)["rows"]}
        for name in rrc_contract_circuit.WEATHER_SETTINGS:
            assert f"{HC1}/{name}" not in keys, name
        assert {SEASON_MODE, OPTIMIZATION} <= keys
        # hc2 reports no control strategy: its settings stay.
        assert f"{HC2}/heatCurveMin" in keys
    finally:
        await _teardown(hass, entry, stack)
