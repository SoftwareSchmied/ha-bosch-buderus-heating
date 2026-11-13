"""P18: optional per-source entities and the shown source of paired readings."""

from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import Mock

from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.local_selection import LocalSelection
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata
from custom_components.bosch_buderus_heating.sensor import (
    BoschBuderusSensor,
    BoschBuderusSourceSensor,
    _track_source_sensors,
    build_sensor_descriptions,
)
from tests.test_local_selection import RETURN, TARGET, FakeRuntime, local
from tests.test_validity import _coordinator

COUNTER = "/heatSources/numberOfStarts"


def return_temperature(value: float = 30.0) -> Resource:
    return Resource(
        RETURN,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="C"),
    )


def counter() -> Resource:
    return Resource(
        COUNTER,
        value=100,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit=""),
    )


def coordinator_with(hass, *, local_values: bool, local_access: bool = True):
    coordinator = _coordinator(hass, return_temperature(), counter())
    runtime = FakeRuntime(
        [local(RETURN, 31.5, monotonic=20.0)],
        enabled=local_values,
        binding_enabled=local_access,
    )
    coordinator.local_selection = LocalSelection(runtime, TARGET)
    return coordinator


def regular(coordinator) -> BoschBuderusSensor:
    description = next(
        item
        for item in build_sensor_descriptions(coordinator.resources)
        if item.resource_path == RETURN
    )
    return BoschBuderusSensor(coordinator, description)


def sources(coordinator) -> dict[str, BoschBuderusSourceSensor]:
    description = next(
        item
        for item in build_sensor_descriptions(coordinator.resources)
        if item.resource_path == RETURN
    )
    return {
        source: BoschBuderusSourceSensor(coordinator, description, source)
        for source in ("local", "cloud")
    }


async def setup_platform(coordinator, track) -> list:
    """Run the platform's per-source tracker on its real discovery path."""
    entry = SimpleNamespace(async_on_unload=Mock())
    added: list = []
    track(entry, coordinator, added.extend)
    return added


# ------------------------------------------------------------ 1B: entities


async def test_two_disabled_source_entities_per_paired_reading(
    hass: HomeAssistant,
) -> None:
    coordinator = coordinator_with(hass, local_values=False)
    added = await setup_platform(coordinator, _track_source_sensors)
    found = [item for item in added if isinstance(item, BoschBuderusSourceSensor)]
    # Only the paired, non-counter reading; never the counter.
    assert sorted(item.source for item in found) == ["cloud", "local"]
    assert {item.entity_description.resource_path for item in found} == {RETURN}
    for item in found:
        assert item.entity_registry_enabled_default is False
        assert item.entity_category is EntityCategory.DIAGNOSTIC
        assert item.state_class is None  # no second statistic or energy series
        assert item.unique_id.endswith(":source_" + item.source)
    assert regular(coordinator).state_class is not None
    await coordinator.async_shutdown()


async def test_no_source_entities_without_a_local_access(hass: HomeAssistant) -> None:
    coordinator = coordinator_with(hass, local_values=False, local_access=False)
    added = await setup_platform(coordinator, _track_source_sensors)
    assert not [item for item in added if isinstance(item, BoschBuderusSourceSensor)]
    await coordinator.async_shutdown()


async def test_source_entities_show_one_source_regardless_of_the_option(
    hass: HomeAssistant,
) -> None:
    for local_values in (False, True):
        coordinator = coordinator_with(hass, local_values=local_values)
        entities = sources(coordinator)
        assert entities["local"].available and entities["local"].native_value == 31.5
        assert entities["cloud"].available and entities["cloud"].native_value == 30.0
        assert "source" not in (entities["local"].extra_state_attributes or {})
        await coordinator.async_shutdown()


async def test_local_source_entity_is_unavailable_without_a_local_reading(
    hass: HomeAssistant,
) -> None:
    coordinator = coordinator_with(hass, local_values=True)
    coordinator.local_selection._runtime.observations = []
    entities = sources(coordinator)
    assert not entities["local"].available
    assert entities["cloud"].available
    await coordinator.async_shutdown()


async def test_source_entity_names_are_localized(hass: HomeAssistant) -> None:
    hass.config.language = "de"
    coordinator = coordinator_with(hass, local_values=False)
    entities = sources(coordinator)
    assert entities["local"].name.endswith("(lokal)")
    assert entities["cloud"].name.endswith("(Cloud)")
    hass.config.language = "en"
    entities = sources(coordinator)
    assert entities["local"].name.endswith("(local)")
    await coordinator.async_shutdown()


# ---------------------------------------------------- 2B: shown source


async def test_regular_sensor_names_its_shown_source(hass: HomeAssistant) -> None:
    coordinator = coordinator_with(hass, local_values=True)
    sensor = regular(coordinator)
    assert sensor.native_value == 31.5
    assert sensor.extra_state_attributes == {"source": "local"}
    coordinator.local_selection._runtime.snapshot.enabled = False
    assert sensor.native_value == 30.0
    assert sensor.extra_state_attributes == {"source": "cloud"}
    await coordinator.async_shutdown()


async def test_no_source_attribute_without_a_local_access(hass: HomeAssistant) -> None:
    coordinator = coordinator_with(hass, local_values=True, local_access=False)
    assert regular(coordinator).extra_state_attributes is None
    coordinator.local_selection = None
    assert regular(coordinator).extra_state_attributes is None
    await coordinator.async_shutdown()


# ------------------------------------------------- 4A: user decisions


async def test_sensor_platform_adds_source_entities(hass: HomeAssistant) -> None:
    from custom_components.bosch_buderus_heating.platform_scope import PlatformScope
    from custom_components.bosch_buderus_heating.sensor import _setup_entities

    coordinator = coordinator_with(hass, local_values=False)
    added: list = []
    _setup_entities(
        PlatformScope(
            entry_id="entry", async_on_unload=Mock(), coordinators=(coordinator,)
        ),
        added.extend,
    )
    assert sorted(
        item.source for item in added if isinstance(item, BoschBuderusSourceSensor)
    ) == ["cloud", "local"]
    await coordinator.async_shutdown()


# ------------------------------------------------------ review follow-ups


async def test_local_updates_reach_source_entities_with_the_option_off(
    hass: HomeAssistant,
) -> None:
    coordinator = coordinator_with(hass, local_values=False)
    selection = coordinator.local_selection
    selection.changed()
    selection._runtime.observations = [local(RETURN, 32.0, monotonic=30.0)]
    assert selection.changed() is True  # entities are refreshed
    assert sources(coordinator)["local"].native_value == 32.0
    await coordinator.async_shutdown()


async def test_writable_paired_setting_is_shown_as_read_locally(
    hass: HomeAssistant,
) -> None:
    path = "/heatingCircuits/hc1/maxFlowTemp"
    coordinator = _coordinator(
        hass,
        Resource(
            path,
            value=50.0,
            has_value=True,
            metadata=ResourceMetadata(
                resource_type="floatValue", unit="C", writable=True
            ),
        ),
    )
    coordinator.local_selection = LocalSelection(
        FakeRuntime([local(path, 55.0, monotonic=20.0)]), TARGET
    )
    shown = coordinator.source_snapshot(path, "local")
    assert shown is not None and shown.resource.value == 55.0
    # The regular entity stays on the cloud value (writable, P11/P16).
    assert coordinator.display_snapshot(path).resource.value == 50.0
    await coordinator.async_shutdown()


async def test_a_switched_off_local_access_counts_as_none(hass: HomeAssistant) -> None:
    coordinator = coordinator_with(hass, local_values=True)
    selection = coordinator.local_selection
    assert selection.has_local_access
    snapshot = selection._runtime.snapshot
    binding = snapshot.bindings["binding-local"]
    snapshot.bindings["binding-local"] = SimpleNamespace(
        target_id=binding.target_id, adapter=binding.adapter, connection_id="lan"
    )
    original = snapshot.to_options

    def options():
        value = original()
        value["connections"] = {"lan": {"enabled": False}}
        return value

    snapshot.to_options = options
    # A configuration change replaces the (immutable) snapshot.
    selection._runtime.snapshot = copy.copy(snapshot)
    assert not selection.has_local_access
    added = await setup_platform(coordinator, _track_source_sensors)
    assert not [item for item in added if isinstance(item, BoschBuderusSourceSensor)]
    assert regular(coordinator).extra_state_attributes is None
    await coordinator.async_shutdown()


async def test_unknown_local_enum_state_is_not_counted_as_pointt_value(
    hass: HomeAssistant,
) -> None:
    coordinator = coordinator_with(hass, local_values=False)
    entity = sources(coordinator)["local"]
    coordinator.record_unknown_enum_value = Mock()
    entity._attr_options = ["a", "b"]
    assert entity._validated_enum_state("z") is None
    coordinator.record_unknown_enum_value.assert_not_called()
    await coordinator.async_shutdown()


async def test_discovery_skips_unchanged_resources(hass: HomeAssistant) -> None:
    from unittest.mock import patch

    coordinator = coordinator_with(hass, local_values=False)
    entry = SimpleNamespace(async_on_unload=Mock())
    with patch(
        "custom_components.bosch_buderus_heating.sensor.build_sensor_descriptions",
        wraps=build_sensor_descriptions,
    ) as built:
        _track_source_sensors(entry, coordinator, Mock())
        coordinator.async_update_listeners()
        coordinator.async_update_listeners()
    assert built.call_count == 1
    await coordinator.async_shutdown()
