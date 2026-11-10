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
    counterpart,
)
from custom_components.bosch_buderus_heating.writes import (
    GENERIC_CONTEXT,
    RRC_CONTEXT,
    ContractRejected,
    WriteService,
    assess_control,
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
    assert data["provenance"]["basis"] == "synthetic_from_app_demo_shapes"


@pytest.mark.parametrize(
    ("path", "platform", "enabled", "extra"),
    [
        (CURVE_MIN, "number", False, {"ui_step": 1.0}),
        (CURVE_MAX, "number", False, {"ui_step": 1.0}),
        (SUPPLY_MIN, "number", False, {"ui_step": 1.0}),
        (SUPPLY_MAX, "number", False, {"ui_step": 1.0}),
        (INFLUENCE, "number", False, {"ui_step": 1.0}),
        (SUWI_THRESHOLD, "number", True, {"ui_step": 0.5}),
        (NIGHT_THRESHOLD, "number", True, {"ui_step": 0.5}),
        (SUWI_MODE, "select", True, {"options": ("off", "automatic")}),
        (NIGHT_MODE, "select", True, {"options": ("off", "automatic")}),
        (
            SEASON_MODE,
            "select",
            True,
            {"options": ("always_heating", "always_cooling", "automatic")},
        ),
        (HEATUP, "select", True, {"options": ("slow", "normal", "fast")}),
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
        (f"{HC2}/buildingHeatup", "rrc_unavailable"),
        (f"{HC2}/minSupply", "rrc_unavailable"),
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


def test_used_but_unavailable_switch_is_shown_and_locked() -> None:
    """S8: ``hc2/setpointOptimization`` stays a control and refuses writes."""
    resource = circuit_tree()[f"{HC2}/setpointOptimization"]
    assert assess_control(resource, RRC_CONTEXT).eligible


def test_counterparts() -> None:
    assert counterpart(CURVE_MIN) == CURVE_MAX
    assert counterpart(CURVE_MAX) == CURVE_MIN
    assert counterpart(SUPPLY_MIN) == SUPPLY_MAX
    assert counterpart(f"{HC2}/maxSupply") == f"{HC2}/minSupply"
    for path in (INFLUENCE, SUWI_THRESHOLD, "/heatingCircuits/hc0/heatCurveMin"):
        assert counterpart(path) is None


def _reading(path: str, value: object) -> Resource:
    data: dict[str, object] = {"id": path, "type": "floatValue"}
    if value is not None:
        data["value"] = value
    return parse_resource(data)


@pytest.mark.parametrize(
    ("path", "value", "other", "result"),
    [
        (CURVE_MIN, 59.0, 60.0, None),
        (CURVE_MIN, 60.0, 60.0, "rrc_curve_order"),  # strictly below
        (CURVE_MIN, 61.0, 60.0, "rrc_curve_order"),
        (CURVE_MAX, 26.0, 25.0, None),
        (CURVE_MAX, 25.0, 25.0, "rrc_curve_order"),
        (SUPPLY_MIN, 75.0, 75.0, None),  # not above
        (SUPPLY_MIN, 76.0, 75.0, "rrc_curve_order"),
        (SUPPLY_MAX, 20.0, 20.0, None),
        (SUPPLY_MAX, 19.0, 20.0, "rrc_curve_order"),
        (CURVE_MIN, 30.0, "high", "rrc_curve_order"),  # unknown other limit
        (CURVE_MIN, 30.0, None, "rrc_curve_order"),  # no reading
        (CURVE_MIN, 30.0, True, "rrc_curve_order"),
    ],
)
def test_order_check(path: str, value: float, other: object, result) -> None:
    resource = circuit_tree()[path]
    related = {counterpart(path): _reading(counterpart(path), other)}
    assert check_order(resource, value, related) == result


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
    with pytest.raises(ContractRejected) as refused:
        await service.async_write_number(
            "gw",
            resource,
            60.0,
            rrc_contract_circuit.HEAT_CURVE_MIN_POLICY,
            context=RRC_CONTEXT,
            related={CURVE_MAX: circuit_tree()[CURVE_MAX]},
        )
    assert refused.value.translation_key == "rrc_curve_order"
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
    assert strings["exceptions"]["rrc_curve_order"]["message"]


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
    enabled: tuple[str, ...] = (),
    gateway: Gateway = RRC_GATEWAY,
):
    entry = _entry(hass)
    registry = er.async_get(hass)
    for unique_id in enabled:
        # The user enabled these entities (they are disabled by default).
        registry.async_get_or_create(
            "number", DOMAIN, unique_id, config_entry=entry, disabled_by=None
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
    """Numbers disabled by default (W4-E1), selections and the switch
    enabled, all configuration; mirrors disabled; nothing for refusals."""
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
            ("number", uid(SUWI_THRESHOLD)): False,
            ("number", uid(NIGHT_THRESHOLD)): False,
            ("select", uid(SUWI_MODE)): False,
            ("select", uid(NIGHT_MODE)): False,
            ("select", uid(SEASON_MODE)): False,
            ("select", uid(HEATUP)): False,
            ("switch", uid(OPTIMIZATION)): False,
            ("switch", uid(f"{HC2}/setpointOptimization")): False,
        }
        for (domain, unique_id), disabled in expected.items():
            entity = registry.async_get(_entity_id(hass, domain, unique_id))
            assert entity is not None
            assert (entity.disabled_by is not None) is disabled, unique_id
            assert entity.entity_category is EntityCategory.CONFIG, unique_id
            assert entity.translation_key is not None, unique_id
        for domain, path in (
            ("select", f"{HC2}/seasonOptMode"),
            ("number", f"{HC2}/maxSupply"),
            ("number", f"{HC2}/minSupply"),
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
        suwi = hass.states.get(_entity_id(hass, "select", uid(SUWI_MODE)))
        assert suwi.state == "automatic"
        assert suwi.attributes["options"] == ["off", "automatic"]
        switch = hass.states.get(_entity_id(hass, "switch", uid(OPTIMIZATION)))
        assert switch.state == "off"
        locked = hass.states.get(
            _entity_id(hass, "switch", uid(f"{HC2}/setpointOptimization"))
        )
        assert locked.state == "on"

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
        # Read-only mirrors of the controls: disabled diagnostic sensors.
        for path in (SUWI_MODE, SUWI_THRESHOLD, CURVE_MIN):
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
    cloud = CircuitCloud(plant())
    entry, coordinator, sleeps, stack, _clock = await _setup(
        hass, cloud, enabled=ALL_LIMITS
    )
    curve_min = _entity_id(hass, "number", uid(CURVE_MIN))
    curve_max = _entity_id(hass, "number", uid(CURVE_MAX))
    supply_min = _entity_id(hass, "number", uid(SUPPLY_MIN))
    supply_max = _entity_id(hass, "number", uid(SUPPLY_MAX))
    try:
        assert hass.states.get(curve_max).state == "60.0"
        before = cloud.calls.copy()
        await _set_number(hass, curve_max, 65)
        # S5: both limits read freshly, then exactly one PUT as a float.
        assert cloud.calls["BULK", CURVE_MAX] == before["BULK", CURVE_MAX] + 1
        assert cloud.calls["BULK", CURVE_MIN] == before["BULK", CURVE_MIN] + 1
        assert cloud.written("PUT") == [("PUT", CURVE_MAX, 65.0)]
        assert type(cloud.written("PUT")[0][2]) is float
        assert sleeps == [1.0]
        assert hass.states.get(curve_max).state == "65.0"

        # The order: refused before sending.
        for entity_id, value in (
            (curve_min, 65),
            (curve_min, 70),
            (curve_max, 25),
            (supply_min, 76),
        ):
            with pytest.raises(ServiceValidationError) as refused:
                await _set_number(hass, entity_id, value)
            assert refused.value.translation_key == "rrc_curve_order"
        assert len(cloud.written("PUT")) == 1
        # Equal supply limits are allowed.
        await _set_number(hass, supply_min, 75)
        assert cloud.written("PUT")[-1] == ("PUT", SUPPLY_MIN, 75.0)
        with pytest.raises(ServiceValidationError) as below:
            await _set_number(hass, supply_max, 74)
        assert below.value.translation_key == "rrc_curve_order"
        assert len(cloud.written("PUT")) == 2

        # The other limit changed on the gateway: nothing is sent, and the
        # new reading is taken over.
        cloud.tree[CURVE_MAX] = replace(cloud.tree[CURVE_MAX], value=50.0)
        with pytest.raises(ServiceValidationError) as changed:
            await _set_number(hass, curve_min, 55)
        assert changed.value.translation_key == "write_validation_failed"
        assert len(cloud.written("PUT")) == 2
        await hass.async_block_till_done()
        assert coordinator.data[CURVE_MAX].resource.value == 50.0
        with pytest.raises(ServiceValidationError) as order:
            await _set_number(hass, curve_min, 55)
        assert order.value.translation_key == "rrc_curve_order"
        await _set_number(hass, curve_min, 30)
        assert cloud.written("PUT")[-1] == ("PUT", CURVE_MIN, 30.0)

        # A plant without the other limit keeps the metadata limits.
        del cloud.tree[SUPPLY_MAX]
        snapshots = dict(coordinator.data)
        del snapshots[SUPPLY_MAX]
        coordinator.async_set_updated_data(snapshots)
        await _set_number(hass, supply_min, 90)
        assert cloud.written("PUT")[-1] == ("PUT", SUPPLY_MIN, 90.0)

        stats = coordinator.rrc_write_stats.snapshot("circuit")
        assert stats["writes_sent"] == 4
        assert stats["writes_confirmed"] == 4
        assert stats["rejection_reasons"]["rrc_curve_order"] == 6
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


async def test_room_influence_and_thresholds_through_the_setup(
    hass: HomeAssistant,
) -> None:
    cloud = CircuitCloud(plant())
    entry, _coordinator, _sleeps, stack, _clock = await _setup(
        hass, cloud, enabled=(uid(INFLUENCE),)
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
    cloud = CircuitCloud(plant())
    entry, _coordinator, sleeps, stack, _clock = await _setup(hass, cloud)
    try:
        await _select(hass, _entity_id(hass, "select", uid(SUWI_MODE)), "off")
        await _select(hass, _entity_id(hass, "select", uid(NIGHT_MODE)), "automatic")
        await _select(
            hass, _entity_id(hass, "select", uid(SEASON_MODE)), "always_heating"
        )
        await _select(hass, _entity_id(hass, "select", uid(HEATUP)), "fast")
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
            ("PUT", HEATUP, "fast"),
            ("PUT", OPTIMIZATION, "on"),
        ]
        assert sleeps == [1.0] * 5
        assert hass.states.get(_entity_id(hass, "select", uid(HEATUP))).state == "fast"
        # The empty season mode is never offered.
        with pytest.raises(ServiceValidationError):
            await _select(hass, _entity_id(hass, "select", uid(SEASON_MODE)), "")
        # S8: used but unavailable: shown, locked, nothing sent.
        with pytest.raises(ServiceValidationError) as locked:
            await hass.services.async_call(
                "switch",
                "turn_off",
                {
                    "entity_id": _entity_id(
                        hass, "switch", uid(f"{HC2}/setpointOptimization")
                    )
                },
                blocking=True,
            )
        assert locked.value.translation_key == "rrc_mode_locked"
        assert len(cloud.written("PUT")) == 5
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
    heatup = _entity_id(hass, "select", uid(HEATUP))

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
        cloud.timeout_after_write[HEATUP] = True
        await _select(hass, heatup, "slow")
        del cloud.timeout_after_write[HEATUP]

        # A server error after the PUT, not applied: unconfirmed.
        cloud.server_error_after_write[HEATUP] = False
        with pytest.raises(HomeAssistantError) as server:
            await _select(hass, heatup, "fast")
        assert server.value.translation_key == "rrc_write_pending_confirmation"
        del cloud.server_error_after_write[HEATUP]
        await poll()
        assert hass.states.get(heatup).state == "slow"

        # 204 without effect: unconfirmed and never sent again.
        cloud.ignore_writes.add(CURVE_MAX)
        with pytest.raises(HomeAssistantError) as ignored:
            await _set_number(hass, curve_max, 74)
        assert ignored.value.translation_key == "rrc_write_pending_confirmation"
        cloud.ignore_writes.clear()
        await poll()
        assert hass.states.get(curve_max).state == "72.0"

        # 403 on the PUT: refused, not repeated.
        cloud.forbidden.add(HEATUP)
        with pytest.raises(HomeAssistantError) as forbidden:
            await _select(hass, heatup, "normal")
        assert forbidden.value.translation_key == "write_failed"
        cloud.forbidden.discard(HEATUP)
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
        assert puts.count(HEATUP) == 3
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


@pytest.mark.parametrize("name", rrc_contract_circuit.INSTALLER_SETTINGS)
def test_installer_settings_have_no_policy(name: str) -> None:
    resource = circuit_tree()[f"{HC1}/{name}"]
    assert resource.metadata.writable  # writeable on the wire, still refused
    assert assess_control(resource, RRC_CONTEXT).policy is None
