"""W0: generic number, select and button controls from RRC contracts.

A contract policy becomes a control of the generic platforms on an RRC
controller, with the contract's name, key, unit, options and rules; the
generic read-only sensor of its path becomes a disabled diagnostic one.
Nothing of it reaches another gateway (S11).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import controls_flow, rrc_contracts
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.control import async_set_control
from custom_components.bosch_buderus_heating.enum_translation import (
    writable_enum_options,
)
from custom_components.bosch_buderus_heating.pointt import Gateway, Resource
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.resource_catalog import (
    entity_enabled_by_default,
)
from custom_components.bosch_buderus_heating.rrc_contract_base import RrcContract
from custom_components.bosch_buderus_heating.rrc_contracts import Registry
from custom_components.bosch_buderus_heating.rrc_write import RrcWritePending
from custom_components.bosch_buderus_heating.writes import (
    ContractRejected,
    EnumWritePolicy,
    NumberWritePolicy,
    RrcWriteNotConfirmed,
)
from tests.helpers.rrc_cloud import Clock, RrcCloud, cloud_patches, load_tree
from tests.test_entry import _entry

PACKAGE = "custom_components.bosch_buderus_heating"
AWAY_TEMPERATURE = "/system/awayMode/temperature"
DHW_MODE = "/dhwCircuits/dhw1/operationMode"
COMMAND = "/system/w0probe/start"

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def _too_warm(resource: Resource, value: object, related) -> str | None:
    return "rrc_w0_probe_refused" if isinstance(value, float) and value > 25 else None


TEMPERATURE = NumberWritePolicy(
    rf"^{AWAY_TEMPERATURE}$", "C", 0.5, strict_grid=True, value_check=_too_warm
)
MODE = EnumWritePolicy(
    rf"^{DHW_MODE}$",
    frozenset({"stringValue"}),
    frozenset({"Off", "high", "ownprogram"}),
    fallback_options=("Off", "high", "ownprogram"),
    equivalent_reads=MappingProxyType({"Off": frozenset({"off"})}),
)
START = EnumWritePolicy(
    rf"^{COMMAND}$",
    frozenset({"stringValue"}),
    frozenset({"on"}),
    known_only=True,
    action=True,
)
CONTRACT = RrcContract(
    name="w_zero_controls",
    enum_policies=(MODE, START),
    number_policies=(TEMPERATURE,),
    names=MappingProxyType(
        {
            rf"^{AWAY_TEMPERATURE}$": ("Abwesenheitstemperatur", "Away temperature"),
            rf"^{DHW_MODE}$": ("Warmwasser-Betriebsart", "Hot water mode"),
            rf"^{COMMAND}$": ("Probe starten", "Start probe"),
        }
    ),
    entity_keys=MappingProxyType(
        {
            rf"^{DHW_MODE}$": "hot_water_operation_mode",
            rf"^{AWAY_TEMPERATURE}$": "rrc_away_temperature",
        }
    ),
    option_keys=MappingProxyType(
        {rf"^{DHW_MODE}$": {"Off": "off", "ownprogram": "own_program"}}
    ),
    read_only_mirrors=(rf"^{AWAY_TEMPERATURE}$",),
    icons=MappingProxyType({rf"^{COMMAND}$": "mdi:play"}),
    categories=MappingProxyType({rf"^{AWAY_TEMPERATURE}$": "config"}),
    dialog_paths=(rf"^{AWAY_TEMPERATURE}$",),
)


@pytest.fixture(autouse=True)
def legacy_setup_path():
    with patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)):
        yield


@contextmanager
def installed(*contracts: RrcContract) -> Iterator[None]:
    original = rrc_contracts._registry
    rrc_contracts._registry = Registry((*rrc_contracts.CONTRACTS, *contracts))
    try:
        yield
    finally:
        rrc_contracts._registry = original


def wire(path: str, **fields: object) -> Resource:
    return parse_resource({"id": path, "writeable": 1, "recordable": 0, **fields})


def _tree() -> dict[str, Resource]:
    tree = load_tree("rrc/ct200-shapes.json")
    tree["/dhwCircuits"] = parse_resource(
        {
            "id": "/dhwCircuits",
            "type": "refEnum",
            "references": [{"id": "/dhwCircuits/dhw1"}],
        }
    )
    tree["/dhwCircuits/dhw1"] = parse_resource(
        {
            "id": "/dhwCircuits/dhw1",
            "type": "refEnum",
            "references": [{"id": DHW_MODE}],
        }
    )
    tree[DHW_MODE] = wire(DHW_MODE, type="stringValue", value="off")
    system = tree["/system"]
    tree["/system"] = parse_resource(
        {
            "id": "/system",
            "type": "refEnum",
            "references": [
                *({"id": item.path} for item in system.references),
                {"id": "/system/w0probe"},
            ],
        }
    )
    tree["/system/w0probe"] = parse_resource(
        {"id": "/system/w0probe", "type": "refEnum", "references": [{"id": COMMAND}]}
    )
    tree[COMMAND] = wire(
        COMMAND, type="stringValue", value="off", allowedValues=["on", "off"]
    )
    return tree


def _entity(hass: HomeAssistant, domain: str, unique_id: str) -> er.RegistryEntry:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
    assert entity_id is not None, unique_id
    entry = registry.async_get(entity_id)
    assert entry is not None
    return entry


@pytest.mark.parametrize("rrc_plant", [True, False])
async def test_contract_controls_on_the_generic_platforms(
    hass: HomeAssistant, rrc_plant: bool
) -> None:
    cloud = RrcCloud(_tree())
    gateway = Gateway("gateway-one", device_type="rrc2")
    if not rrc_plant:
        # The same paths without any RRC evidence.
        for path in [path for path in cloud.tree if path.startswith("/zones")]:
            del cloud.tree[path]
        del cloud.tree["/gateway/productType"]
        gateway = Gateway("gateway-one")
        cloud.missing_as_500 = False  # another gateway answers 404
    entry = _entry(hass)
    with installed(CONTRACT), cloud_patches(cloud, Clock(), gateway):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data.coordinators[0]

        async def no_wait(_delay: float) -> None:
            return None

        coordinator._write_service._sleep = no_wait
        registry = er.async_get(hass)
        number_id = registry.async_get_entity_id(
            "number", DOMAIN, "gateway-one:system:awayMode:temperature:control"
        )
        select_id = registry.async_get_entity_id(
            "select", DOMAIN, "gateway-one:dhwCircuits:dhw1:operationMode:control"
        )
        button_id = registry.async_get_entity_id(
            "button", DOMAIN, "gateway-one:system:w0probe:start:start"
        )
        mirror = _entity(hass, "sensor", "gateway-one:system:awayMode:temperature")
        if not rrc_plant:
            assert number_id is None and button_id is None
            # The generic hot water selection has no options without a list.
            assert select_id is None
            assert (mirror.disabled_by is None) == entity_enabled_by_default(
                AWAY_TEMPERATURE
            )
            assert AWAY_TEMPERATURE not in controls_flow.controls(coordinator)
            await hass.config_entries.async_unload(entry.entry_id)
            return

        assert number_id and select_id and button_id
        number = hass.states.get(number_id)
        assert number.attributes["friendly_name"].endswith("Away temperature")
        assert number.attributes["step"] == 0.5
        assert number.attributes["unit_of_measurement"] == "°C"
        assert (
            _entity(
                hass, "number", "gateway-one:system:awayMode:temperature:control"
            ).entity_category
            is EntityCategory.CONFIG
        )
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": number_id, "value": 16.0},
            blocking=True,
        )
        assert cloud.written("PUT")[-1] == ("PUT", AWAY_TEMPERATURE, 16.0)
        with pytest.raises(ServiceValidationError) as refused:
            await hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": number_id, "value": 26.0},
                blocking=True,
            )
        assert refused.value.translation_key == "rrc_w0_probe_refused"
        with pytest.raises(ServiceValidationError) as off_grid:
            await hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": number_id, "value": 16.2},
                blocking=True,
            )
        assert off_grid.value.translation_key == "write_validation_failed"
        assert len(cloud.written("PUT")) == 1

        # The generic sensor of the setting is now a disabled diagnostic.
        assert mirror.entity_category is EntityCategory.DIAGNOSTIC
        assert mirror.disabled_by is er.RegistryEntryDisabler.INTEGRATION

        select = hass.states.get(select_id)
        assert select.attributes["options"] == ["off", "high", "own_program"]
        assert select.state == "off"  # read as "off", offered as "Off"
        assert select.attributes["friendly_name"].endswith("Hot water mode")
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": select_id, "option": "own_program"},
            blocking=True,
        )
        assert cloud.written("PUT")[-1] == ("PUT", DHW_MODE, "ownprogram")
        assert hass.states.get(select_id).state == "own_program"

        button = hass.states.get(button_id)
        assert button.attributes["friendly_name"].endswith("Start probe")
        assert button.attributes["icon"] == "mdi:play"
        await hass.services.async_call(
            "button", "press", {"entity_id": button_id}, blocking=True
        )
        assert cloud.written("PUT")[-1] == ("PUT", COMMAND, "on")

        # The controls dialog lists the contract's dialog path.
        assert AWAY_TEMPERATURE in controls_flow.controls(coordinator)
        stats = coordinator.rrc_write_stats.snapshot("w_zero_controls")
        assert stats["writes_sent"] == 3 and stats["writes_confirmed"] == 3
        assert stats["rejection_reasons"] == {
            "rrc_w0_probe_refused": 1,
            "validation": 1,
        }
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("error", "kind", "key"),
    [
        (
            ContractRejected("rrc_curve_order"),
            ServiceValidationError,
            "rrc_curve_order",
        ),
        (
            RrcWritePending("rrc_event_pending"),
            ServiceValidationError,
            "rrc_event_pending",
        ),
        (
            RrcWriteNotConfirmed("late", reason="rrc_override_ended"),
            HomeAssistantError,
            "rrc_override_ended",
        ),
        (
            RrcWriteNotConfirmed("late"),
            HomeAssistantError,
            "rrc_write_pending_confirmation",
        ),
    ],
)
async def test_contract_errors_reach_home_assistant_with_their_key(
    error: Exception, kind: type[Exception], key: str
) -> None:
    coordinator = SimpleNamespace(async_write_control=AsyncMock(side_effect=error))
    with pytest.raises(kind) as raised:
        await async_set_control(coordinator, AWAY_TEMPERATURE, 16.0, TEMPERATURE)  # type: ignore[arg-type]
    assert raised.value.translation_key == key


def test_option_keys_name_codes_without_taking_other_keys() -> None:
    assert writable_enum_options(
        "hot_water_operation_mode",
        ("Off", "off", "high"),
        {"Off": "off"},
    ) == {"off": "Off", "pointt:off": "off", "high": "high"}
    # Without overrides nothing changes.
    assert writable_enum_options("hot_water_operation_mode", ("Off", "high")) == {
        "off": "Off",
        "high": "high",
    }
