"""Ventilation: mode select, filter time, retired fan level select, diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import EntityCategory
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import _remove_retired_entities
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.number import build_number_descriptions
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    Resource,
    ResourceMetadata,
)
from custom_components.bosch_buderus_heating.resource_catalog import (
    entity_enabled_by_default,
    resource_name,
    supports_entity,
)
from custom_components.bosch_buderus_heating.select import build_select_descriptions
from custom_components.bosch_buderus_heating.sensor import build_sensor_descriptions
from custom_components.bosch_buderus_heating.ventilation_controls import (
    VENTILATION_OPERATION_MODES,
    is_retired_fan_level_select,
)
from tests.ha_test_helpers import MockConfigEntry

ZONE = "/ventilation/zone1"
# n42net's unit: 11 operating modes, the fan level not writable, a writable
# filter limit in months (values as the diagnostics report them).
N42NET_MODES = (
    "time",
    "off",
    "min",
    "red",
    "nom",
    "max",
    "dem",
    "party",
    "powerVent",
    "fireplace",
    "fallAsleep",
)


def _resource(
    path: str,
    value: object,
    kind: str,
    *,
    writable: bool = False,
    unit: str | None = None,
    allowed: tuple[str, ...] = (),
    minimum: float | None = None,
    maximum: float | None = None,
) -> Resource:
    return Resource(
        path=path,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type=kind,
            writable=writable,
            unit=unit,
            allowed_values=allowed,
            minimum=minimum,
            maximum=maximum,
        ),
    )


def _n42net() -> dict[str, Resource]:
    items = (
        _resource(
            f"{ZONE}/operationMode",
            "nom",
            "stringValue",
            writable=True,
            allowed=N42NET_MODES,
        ),
        _resource(
            f"{ZONE}/exhaustFanLevel",
            "nom",
            "stringValue",
            allowed=("dem", "max", "min", "nom", "off", "red"),
        ),
        _resource(
            f"{ZONE}/filter/maxRunTime",
            6.0,
            "floatValue",
            writable=True,
            unit="month",
            minimum=1.0,
            maximum=12.0,
        ),
        _resource(f"{ZONE}/filter/remainingTime", 100.0, "floatValue", unit="mins"),
        _resource(
            f"{ZONE}/demand/indoorAirQuality",
            "low",
            "stringValue",
            writable=True,
            allowed=("low", "medium", "high"),
        ),
        _resource(
            f"{ZONE}/demand/relativeHumidity",
            "low",
            "stringValue",
            writable=True,
            allowed=("low", "medium", "high"),
        ),
        _resource(f"{ZONE}/sensors/exhaustTemp", 12.0, "floatValue", unit="C"),
        _resource(f"{ZONE}/sensors/extractTemp", 21.0, "floatValue", unit="C"),
        _resource(f"{ZONE}/sensors/outdoorTemp", 5.0, "floatValue", unit="C"),
        _resource(
            f"{ZONE}/sensors/internalAirQuality", 600.0, "floatValue", unit="ppm"
        ),
        _resource(f"{ZONE}/sensors/internalHumidity", 45.0, "floatValue", unit="%"),
        _resource(f"{ZONE}/sensors/supplyTemp", 18.0, "floatValue", unit="C"),
    )
    return {item.path: item for item in items}


def test_the_n42net_unit_gets_the_mode_select_and_the_filter_number_only() -> None:
    resources = _n42net()
    (select,) = build_select_descriptions(resources)
    assert select.resource_path == f"{ZONE}/operationMode"
    assert select.entity_registry_enabled_default is True
    assert select.options == [
        "time", "off", "min", "red", "nom", "max", "dem", "party", "power_vent",
        "fireplace", "fall_asleep",
    ]  # fmt: skip
    (number,) = build_number_descriptions(resources)
    assert number.resource_path == f"{ZONE}/filter/maxRunTime"
    assert number.entity_registry_enabled_default is False


def test_the_app_mode_list_is_the_documented_one_without_the_hidden_mode() -> None:
    assert "exhaustOnly" not in VENTILATION_OPERATION_MODES
    assert len(set(VENTILATION_OPERATION_MODES)) == len(VENTILATION_OPERATION_MODES)


def test_no_control_without_the_paths() -> None:
    # A plant with other resources only: nothing is created.
    other = {
        "/pool/setpointTemp": _resource(
            "/pool/setpointTemp", 26.0, "floatValue", unit="C"
        )
    }
    assert not build_select_descriptions(other)
    assert not [
        item
        for item in build_number_descriptions(other)
        if "ventilation" in item.resource_path
    ]


def test_the_diagnostic_readings_are_disabled_diagnostic_sensors() -> None:
    resources = _n42net()
    extra = (
        "demand/indoorAirQuality",
        "demand/relativeHumidity",
        "sensors/exhaustTemp",
        "sensors/extractTemp",
        "sensors/outdoorTemp",
        "sensors/internalAirQuality",
        "sensors/internalHumidity",
    )
    descriptions = {
        item.resource_path: item for item in build_sensor_descriptions(resources)
    }
    for tail in extra:
        path = f"{ZONE}/{tail}"
        assert supports_entity(resources[path]), path
        assert not entity_enabled_by_default(path), path
        description = descriptions[path]
        assert description.entity_registry_enabled_default is False
        assert description.entity_category is EntityCategory.DIAGNOSTIC
    # What was enabled stays enabled.
    for tail in ("operationMode", "exhaustFanLevel", "sensors/supplyTemp"):
        assert entity_enabled_by_default(f"{ZONE}/{tail}"), tail
    assert resource_name(f"{ZONE}/sensors/exhaustTemp", language="en") == (
        "Exhaust air temperature"
    )
    assert resource_name(f"{ZONE}/demand/relativeHumidity", language="de") == (
        "Bedarf nach Luftfeuchtigkeit"
    )


@pytest.mark.parametrize(
    ("domain", "unique_id", "expected"),
    [
        ("select", "gw1:ventilation:zone1:exhaustFanLevel:control", True),
        ("select", "gw1:ventilation:zone2:exhaustFanLevel:control", True),
        ("sensor", "gw1:ventilation:zone1:exhaustFanLevel", False),
        ("sensor", "gw1:ventilation:zone1:exhaustFanLevel:control", False),
        ("select", "gw1:ventilation:zone1:operationMode:control", False),
        ("select", "gw2:ventilation:zone1:exhaustFanLevel:control", False),
        ("select", "gw1:heatingCircuits:hc1:operationMode:control", False),
    ],
)
def test_only_the_old_fan_level_select_of_a_known_gateway_is_retired(
    domain: str, unique_id: str, expected: bool
) -> None:
    assert is_retired_fan_level_select(domain, unique_id, ["gw1"]) is expected


def test_the_cleanup_removes_the_old_select_and_nothing_else(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)

    def add(domain: str, key: str, **kwargs: object) -> er.RegistryEntry:
        return registry.async_get_or_create(
            domain, DOMAIN, f"gw1:{key}", config_entry=entry, **kwargs
        )

    old = add(
        "select",
        "ventilation:zone1:exhaustFanLevel:control",
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    # A customised old select is removed too: nothing provides it any more.
    old_named = add("select", "ventilation:zone2:exhaustFanLevel:control")
    registry.async_update_entity(old_named.entity_id, name="My fan")
    kept = [
        add("sensor", "ventilation:zone1:exhaustFanLevel"),
        add("sensor", "ventilation:zone1:operationMode"),
        add("select", "ventilation:zone1:operationMode:control"),
        add("number", "ventilation:zone1:filter:maxRunTime:control"),
        add("select", "heatingCircuits:hc1:operationMode:control"),
    ]
    renamed = registry.async_update_entity(kept[0].entity_id, name="Fan level")
    kept[0] = renamed
    user_disabled = registry.async_update_entity(
        kept[1].entity_id, disabled_by=er.RegistryEntryDisabler.USER
    )
    kept[1] = user_disabled

    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gw1"), entry
    )
    resources = _n42net()
    coordinator.resources = resources
    coordinator.data = {
        key: ResourceSnapshot(item, True, datetime.now(UTC))
        for key, item in resources.items()
    }
    # Another gateway's entry with the same shape stays untouched.
    foreign = registry.async_get_or_create(
        "select",
        DOMAIN,
        "gw9:ventilation:zone1:exhaustFanLevel:control",
        config_entry=entry,
    )

    _remove_retired_entities(hass, entry, (coordinator,))

    assert registry.async_get(old.entity_id) is None
    assert registry.async_get(old_named.entity_id) is None
    for item in kept:
        after = registry.async_get(item.entity_id)
        assert after is not None
        assert (after.name, after.disabled_by, after.unique_id) == (
            item.name,
            item.disabled_by,
            item.unique_id,
        )
    assert registry.async_get(foreign.entity_id) is not None
