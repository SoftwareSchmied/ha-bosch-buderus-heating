"""W0: the platform hooks of the RRC packages."""

from __future__ import annotations

import sys
from types import MappingProxyType, ModuleType
from unittest.mock import patch

import pytest
from homeassistant.components.sensor import SensorEntityDescription
from homeassistant.const import EntityCategory, Platform

from custom_components.bosch_buderus_heating import rrc_contracts, rrc_platforms
from custom_components.bosch_buderus_heating.rrc_contract_base import RrcContract
from custom_components.bosch_buderus_heating.rrc_contracts import Registry
from custom_components.bosch_buderus_heating.sensor import (
    BoschBuderusSensorEntityDescription,
)

PATH = "/system/appliance/displayCode"


def test_every_entity_module_declares_empty_trackers() -> None:
    rrc_platforms.trackers.cache_clear()
    loaded = rrc_platforms.trackers()
    assert [name for name, _ in loaded] == list(rrc_platforms.ENTITY_MODULES)
    assert all(not table for _, table in loaded)
    assert Platform.TIME in rrc_platforms.PLATFORMS


def test_a_tracker_for_a_platform_without_a_hook_is_refused() -> None:
    module = ModuleType("probe")
    module.TRACKERS = {Platform.FAN: lambda *_args: None}  # type: ignore[attr-defined]
    package = rrc_platforms.__package__
    rrc_platforms.trackers.cache_clear()
    try:
        with (
            patch.dict(sys.modules, {f"{package}.rrc_probe_entities": module}),
            patch.object(rrc_platforms, "ENTITY_MODULES", ("rrc_probe_entities",)),
            pytest.raises(ValueError, match="without a hook"),
        ):
            rrc_platforms.trackers()
    finally:
        rrc_platforms.trackers.cache_clear()


def test_generic_presentation_applies_the_contract() -> None:
    contract = RrcContract(
        name="presentation",
        names=MappingProxyType({rf"^{PATH}$": ("Anzeigecode", "Display code")}),
        entity_keys=MappingProxyType({rf"^{PATH}$": {"sensor": "rrc_display_code"}}),
        categories=MappingProxyType({rf"^{PATH}$": "diagnostic"}),
        icons=MappingProxyType({rf"^{PATH}$": "mdi:numeric"}),
        default_enabled=MappingProxyType({rf"^{PATH}$": False}),
    )
    description = BoschBuderusSensorEntityDescription(
        key="system:appliance:displayCode",
        name="Display code",
        resource_path=PATH,
        value_kind="value",
        value_key=None,
        unique_key="system:appliance:displayCode",
    )
    original = rrc_contracts._registry
    rrc_contracts._registry = Registry((*rrc_contracts.CONTRACTS, contract))
    try:
        shown = rrc_platforms.generic_presentation(description, Platform.SENSOR)
        assert shown.name == "Anzeigecode"
        assert shown.translation_key == "rrc_display_code"
        assert shown.entity_category is EntityCategory.DIAGNOSTIC
        assert shown.icon == "mdi:numeric"
        assert shown.entity_registry_enabled_default is False
        # A nested reading keeps its own name; another platform its key.
        nested = rrc_platforms.generic_presentation(
            BoschBuderusSensorEntityDescription(
                key="x",
                name="Nested",
                resource_path=PATH,
                value_kind="nested",
                value_key="part",
                unique_key="x",
            ),
            Platform.BINARY_SENSOR,
        )
        assert nested.name == "Nested" and nested.translation_key is None
        # Without a resource path or a contract: unchanged.
        plain = SensorEntityDescription(key="plain")
        assert rrc_platforms.generic_presentation(plain, Platform.SENSOR) is plain
    finally:
        rrc_contracts._registry = original
    assert rrc_platforms.generic_presentation(description, Platform.SENSOR) is (
        description
    )
