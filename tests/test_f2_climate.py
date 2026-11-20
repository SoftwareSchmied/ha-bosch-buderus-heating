"""Hybrid burner share, switch categories and COP plausibility (synthetic)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.burner_share import (
    BURNER_SHARE,
    burner_reported,
    burner_share,
    hybrid_detected,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.cop_plausibility import (
    LOW_COP_SECONDS,
    LowCopWatch,
)
from custom_components.bosch_buderus_heating.performance import live_cop
from custom_components.bosch_buderus_heating.performance_history import Period
from custom_components.bosch_buderus_heating.performance_sensors import (
    CurrentCopSensor,
    PerformanceFactorSensor,
)
from custom_components.bosch_buderus_heating.pointt import Gateway
from custom_components.bosch_buderus_heating.pointt.models import (
    Resource,
    ResourceMetadata,
)
from custom_components.bosch_buderus_heating.sensor import (
    BoschBuderusSensor,
    build_sensor_descriptions,
)
from custom_components.bosch_buderus_heating.switch import (
    BoschBuderusSwitch,
    build_switch_descriptions,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.test_derived_values import COOLING, SUWI, TOTAL, air
from tests.test_performance import _engine, running

HYBRID_PATH = "/heatSources/hybrid/activeHeatSource"


def _collection(burner: float | None) -> Resource:
    parts: list[dict[str, float]] = [
        {"compressor": 10.0},
        {"eheater": 2.0},
        {"outputProduced": 20.0},
    ]
    if burner is not None:
        parts.append({"burner": burner})
    return Resource(
        path=TOTAL,
        values=tuple(parts),
        metadata=ResourceMetadata(resource_type="emonValue"),
    )


def _environmental(hass: HomeAssistant, resource: Resource, *, hybrid: bool = False):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass,
        SimpleNamespace(),  # type: ignore[arg-type]
        Gateway("gateway-one", device_type="MX300", firmware_version="1.2.3"),
        entry,
    )
    coordinator.resources = {resource.path: resource}
    coordinator.update_interval = None
    coordinator.data = {
        resource.path: ResourceSnapshot(resource, True, datetime.now(UTC))
    }
    if hybrid:
        manager = Resource(
            path=HYBRID_PATH,
            value="gas_boiler",
            has_value=True,
            metadata=ResourceMetadata(resource_type="stringValue"),
        )
        coordinator.resources[HYBRID_PATH] = manager
        coordinator.data[HYBRID_PATH] = ResourceSnapshot(
            manager, True, datetime.now(UTC)
        )
    coordinator.last_update_success = True
    description = next(
        item
        for item in build_sensor_descriptions({resource.path: resource})
        if item.value_kind == "environmental_energy"
    )
    return BoschBuderusSensor(coordinator, description)  # type: ignore[arg-type]


# --------------------------------------------------- environmental energy
def test_a_heat_pump_only_plant_keeps_its_environmental_energy(
    hass: HomeAssistant,
) -> None:
    sensor = _environmental(hass, _collection(None))
    assert sensor.native_value == pytest.approx(8.0)
    assert not (sensor.extra_state_attributes or {}).get("reason")
    # A burner part at zero is no share.
    sensor = _environmental(hass, _collection(0.0))
    assert sensor.native_value == pytest.approx(8.0)
    assert not (sensor.extra_state_attributes or {}).get("reason")


def test_reported_burner_heat_leaves_the_environmental_energy_unknown(
    hass: HomeAssistant,
) -> None:
    sensor = _environmental(hass, _collection(3.5))
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {"reason": BURNER_SHARE}


def test_a_detected_hybrid_manager_leaves_the_environmental_energy_unknown(
    hass: HomeAssistant,
) -> None:
    sensor = _environmental(hass, _collection(None), hybrid=True)
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {"reason": BURNER_SHARE}


def test_the_burner_part_rule() -> None:
    assert burner_reported({"burner": 0.1})
    assert not burner_reported({"burner": 0.0})
    assert not burner_reported({"compressor": 1.0})


# ------------------------------------------------- performance factors
async def _factor(plant, period=Period.SINCE_COMMISSIONING):
    engine = await _engine(plant)
    sensor = PerformanceFactorSensor(plant.coordinator, engine, period, "total")
    sensor._refresh()
    return sensor


def _with_burner(plant, burner: float | None) -> None:
    parts = [{"outputProduced": 1614.45}, {"compressor": 400.0}, {"eheater": 0.0}]
    if burner is not None:
        parts.append({"burner": burner})
    plant.cloud(TOTAL, values=tuple(parts))


async def test_the_factor_of_a_heat_pump_only_plant_is_unchanged(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    _with_burner(plant, None)
    sensor = await _factor(plant)
    assert sensor.native_value == pytest.approx(1614.45 / 400.0, abs=0.01)
    assert "reason" not in (sensor.extra_state_attributes or {})
    assert not hybrid_detected(plant.coordinator)
    _with_burner(plant, 0.0)
    sensor._refresh()
    assert sensor.native_value == pytest.approx(1614.45 / 400.0, abs=0.01)


async def test_the_factor_is_unknown_with_a_burner_share(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    _with_burner(plant, 120.0)
    sensor = await _factor(plant)
    assert burner_share(plant.coordinator, TOTAL)
    assert sensor.native_value is None
    attributes = sensor.extra_state_attributes
    assert attributes is not None and attributes["reason"] == BURNER_SHARE
    # The counters of the factor stay in the attributes for inspection.
    assert attributes["heat_kwh"] == pytest.approx(1614.45)


async def test_the_factor_is_unknown_with_a_hybrid_manager(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = air(hass, freezer)
    _with_burner(plant, None)
    plant.cloud(HYBRID_PATH, "heat_pump")
    sensor = await _factor(plant)
    assert hybrid_detected(plant.coordinator)
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["reason"] == BURNER_SHARE


# ----------------------------------------------------- switch categories
SWITCHES = {
    "/system/awayMode/enabled": ("away_mode", "off", ("off", "on")),
    "/system/holidayMode": ("wddw2_holiday_mode", "off", ("off", "on")),
    "/swCircuits/sw1/holidayMode/enabled": (
        "softener_holiday_mode",
        "off",
        ("off", "on"),
    ),
    "/dhwCircuits/dhw1/reduceTempOnAlarm": (
        "reduce_temperature_on_alarm",
        "off",
        ("off", "on"),
    ),
    "/dhwCircuits/dhw1/charge": ("extra_hot_water", "stop", ("start", "stop")),
    "/pool/enabled": ("pool_enabled", "off", ("off", "on")),
}


def _switch_resource(path: str) -> Resource:
    _, value, options = SWITCHES[path]
    return Resource(
        path=path,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="stringValue", allowed_values=options, writable=True
        ),
    )


@pytest.mark.parametrize(
    ("path", "category"),
    [
        ("/system/awayMode/enabled", EntityCategory.CONFIG),
        ("/system/holidayMode", EntityCategory.CONFIG),
        ("/swCircuits/sw1/holidayMode/enabled", EntityCategory.CONFIG),
        ("/dhwCircuits/dhw1/reduceTempOnAlarm", EntityCategory.CONFIG),
        # Extra hot water and the pool stay as they are.
        ("/dhwCircuits/dhw1/charge", None),
        ("/pool/enabled", None),
    ],
)
def test_away_and_holiday_switches_are_configuration_entities(
    path: str, category: EntityCategory | None
) -> None:
    resource = _switch_resource(path)
    (description,) = build_switch_descriptions({path: resource})
    assert description.translation_key == SWITCHES[path][0]
    assert description.entity_category is category


async def test_an_existing_registry_entry_takes_the_category_and_nothing_else(
    hass: HomeAssistant,
) -> None:
    try:
        from pytest_homeassistant.common import MockEntityPlatform
    except ImportError:  # Home Assistant before 2026.11
        from pytest_homeassistant_custom_component.common import (  # type: ignore[no-redef]
            MockEntityPlatform,
        )

    path = "/system/awayMode/enabled"
    resource = _switch_resource(path)
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass,
        SimpleNamespace(),
        Gateway("gateway-one", device_type="K40"),
        entry,  # type: ignore[arg-type]
    )
    coordinator.update_interval = None
    coordinator.resources = {path: resource}
    coordinator.data = {path: ResourceSnapshot(resource, True, datetime.now(UTC))}
    coordinator.last_update_success = True
    (description,) = build_switch_descriptions(coordinator.resources)
    entity = BoschBuderusSwitch(coordinator, description)
    unique_id = "gateway-one:system:awayMode:enabled:control"
    assert entity.unique_id == unique_id
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "switch",
        DOMAIN,
        unique_id,
        config_entry=entry,
        suggested_object_id="my_away",
        entity_category=None,
        original_name="System: Away mode",
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    assert old.entity_category is None
    platform = MockEntityPlatform(hass, domain="switch", platform_name=DOMAIN)
    platform.config_entry = entry
    await platform.async_add_entities([entity])
    new = registry.async_get(old.entity_id)
    assert new is not None
    assert new.entity_category is EntityCategory.CONFIG
    assert new.entity_id == old.entity_id
    assert new.unique_id == old.unique_id
    assert new.disabled_by is er.RegistryEntryDisabler.USER
    assert new.name == old.name


# ------------------------------------------------------- COP plausibility
def test_a_cooling_run_is_no_heating_cop(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = running(air(hass, freezer))
    assert live_cop(plant.coordinator).value is not None
    plant.flag(COOLING, True)
    result = live_cop(plant.coordinator)
    assert result.value is None and result.attributes["reason"] == "cooling"
    # The cooling capacity as heat output would be an EER: still no value.
    running(plant, output=2.1, power=600.0)
    assert live_cop(plant.coordinator).attributes["reason"] == "cooling"
    # No heat output at all in the cooling run: cooling, not "no_heat_output".
    running(plant, output=0.0, power=600.0)
    assert live_cop(plant.coordinator).attributes["reason"] == "cooling"
    plant.flag(COOLING, False)
    assert live_cop(plant.coordinator).attributes["reason"] == "no_heat_output"


def test_the_cooling_season_while_the_circuit_is_served_is_no_heating_cop(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = running(air(hass, freezer))
    plant.cloud(SUWI, "cooling")
    assert live_cop(plant.coordinator).attributes["reason"] == "cooling"
    # Hot water in the cooling season is a heating COP.
    plant.flag("/signals/SRC.CUHP.HP1.DHWValve", True)
    result = live_cop(plant.coordinator)
    assert result.value is not None and result.attributes["side"] == "dhw"


def test_an_implausibly_high_cop_is_not_shown(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = running(air(hass, freezer), output=3.4, power=300.0)  # 11.3
    result = live_cop(plant.coordinator)
    assert result.value is None and result.attributes["reason"] == "implausible"
    running(plant, output=3.4, power=350.0)  # 9.7
    assert live_cop(plant.coordinator).value == pytest.approx(9.71, abs=0.01)


def test_a_cop_below_one_stays_shown_at_first_then_is_implausible(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = running(air(hass, freezer), output=0.5, power=800.0)  # 0.62
    watch = LowCopWatch()
    first = live_cop(plant.coordinator, None, watch)
    assert first.value == pytest.approx(0.62, abs=0.01)
    plant.later(LOW_COP_SECONDS / 60 - 1)
    running(plant, output=0.5, power=800.0)
    assert live_cop(plant.coordinator, None, watch).value is not None
    plant.later(1.1)
    running(plant, output=0.5, power=800.0)
    result = live_cop(plant.coordinator, None, watch)
    assert result.value is None
    assert result.attributes["reason"] == "implausible_low"


def test_a_short_dip_below_one_never_counts(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = running(air(hass, freezer), output=0.5, power=800.0)
    watch = LowCopWatch()
    assert live_cop(plant.coordinator, None, watch).value is not None
    plant.later(8)
    running(plant)  # a normal COP again: the wait starts over
    assert live_cop(plant.coordinator, None, watch).value is not None
    plant.later(8)
    running(plant, output=0.5, power=800.0)
    assert live_cop(plant.coordinator, None, watch).value is not None
    plant.later(8)
    running(plant, output=0.5, power=800.0)
    assert live_cop(plant.coordinator, None, watch).value is not None  # 8 min only


def test_the_low_cop_wait_ends_with_a_stopped_compressor(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    from tests.test_derived_values import COMPRESSOR_ACTIVE

    plant = running(air(hass, freezer), output=0.5, power=800.0)
    watch = LowCopWatch()
    live_cop(plant.coordinator, None, watch)
    plant.later(9)
    plant.flag(COMPRESSOR_ACTIVE, False)
    assert live_cop(plant.coordinator, None, watch).attributes["reason"] == (
        "compressor_off"
    )
    plant.flag(COMPRESSOR_ACTIVE, True)
    plant.later(2)
    running(plant, output=0.5, power=800.0)
    # 11 minutes since the first low reading, but not without a break.
    assert live_cop(plant.coordinator, None, watch).value is not None


def test_the_current_cop_sensor_follows_a_long_low_cop(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    plant = running(air(hass, freezer), output=0.5, power=800.0)
    sensor = CurrentCopSensor(plant.coordinator)
    sensor._result = sensor._derive()
    assert sensor.native_value is not None
    plant.later(11)
    running(plant, output=0.5, power=800.0)
    sensor._result = sensor._derive()
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["reason"] == "implausible_low"


def test_the_room_controller_away_switch_is_a_configuration_entity() -> None:
    from custom_components.bosch_buderus_heating import rrc_contracts

    assert rrc_contracts.category("/system/awayMode/enabled") == "config"
