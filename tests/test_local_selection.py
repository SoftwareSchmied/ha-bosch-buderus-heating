"""Existing entities may display paired local readings; cloud stays the fallback."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.bosch_buderus_heating.configuration import (
    LOCAL_KIND,
    ConfigurationError,
    load_configuration,
)
from custom_components.bosch_buderus_heating.configuration_changes import (
    configuration_changes,
)
from custom_components.bosch_buderus_heating.configuration_management import (
    ConfigurationManagement,
)
from custom_components.bosch_buderus_heating.coordinator import (
    Freshness,
    ResourceSnapshot,
    SnapshotSource,
)
from custom_components.bosch_buderus_heating.local_selection import (
    LocalSelection,
    _native,
    eligible,
    local_snapshot,
)
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata
from tests.test_local_api import read, scalar
from tests.test_local_setup import registered

NOW = datetime(2026, 11, 3, 8, 0, tzinfo=UTC)
TARGET = "target-a"
LOCAL_BINDING = "binding-local"


@dataclass
class FakeSnapshot:
    enabled: bool = True
    binding_enabled: bool = True
    target: str = TARGET
    bindings: dict = field(default_factory=dict)

    def __post_init__(self):
        self.bindings = {
            LOCAL_BINDING: SimpleNamespace(target_id=self.target, adapter=LOCAL_KIND),
            "binding-cloud": SimpleNamespace(target_id=TARGET, adapter="pointt"),
        }

    def to_options(self):
        return {
            "targets": {TARGET: {"read_order": [], "local_values": self.enabled}},
            "bindings": {
                LOCAL_BINDING: {"enabled": self.binding_enabled},
                "binding-cloud": {"enabled": True},
            },
        }


class FakeRuntime:
    def __init__(self, observations=(), **kwargs):
        self.snapshot = FakeSnapshot(**kwargs)
        self.observations = list(observations)

    def local_state(self, binding_id):
        if binding_id != LOCAL_BINDING:
            return None
        return SimpleNamespace(current_observations=tuple(self.observations))


def cloud(
    path,
    value,
    *,
    resource_type="floatValue",
    unit="C",
    writable=False,
    available=True,
    freshness=Freshness.FRESH,
):
    return ResourceSnapshot(
        Resource(
            path,
            value=value,
            has_value=True,
            metadata=ResourceMetadata(
                resource_type=resource_type, unit=unit, writable=writable
            ),
        ),
        available,
        NOW - timedelta(minutes=2),
        freshness=freshness,
    )


def local(path, value, *, monotonic=10.0, **changes):
    return read(
        scalar(path, value=value, **changes),
        observed_at=NOW,
        observed_monotonic=monotonic,
    )


RETURN = "/heatSources/returnTemperature"


def test_disabled_selection_returns_the_exact_cloud_snapshot() -> None:
    snapshot = cloud(RETURN, 30.0)
    selection = LocalSelection(
        FakeRuntime([local(RETURN, 31.5)], enabled=False), TARGET
    )
    assert selection.select(RETURN, snapshot) is snapshot
    assert selection.select(RETURN, None) is None


def test_current_local_value_is_displayed_with_cloud_metadata() -> None:
    snapshot = cloud(RETURN, 30.0, freshness=Freshness.STALE, available=False)
    selection = LocalSelection(FakeRuntime([local(RETURN, 31.5)]), TARGET)
    shown = selection.select(RETURN, snapshot)
    assert shown is not snapshot
    assert shown.resource.value == 31.5
    assert isinstance(shown.resource.value, float)
    assert shown.resource.metadata == snapshot.resource.metadata
    assert shown.resource.path == RETURN
    assert (shown.available, shown.freshness, shown.source) == (
        True,
        Freshness.FRESH,
        SnapshotSource.LOCAL,
    )
    assert shown.last_success == NOW
    # The coordinator's own snapshot is untouched.
    assert snapshot.resource.value == 30.0 and snapshot.available is False


def test_cloud_is_used_when_no_compatible_local_value_exists() -> None:
    snapshot = cloud(RETURN, 30.0)
    assert LocalSelection(FakeRuntime([]), TARGET).select(RETURN, snapshot) is snapshot
    mismatched_unit = local(RETURN, 31.5, unitOfMeasure="K")
    assert (
        LocalSelection(FakeRuntime([mismatched_unit]), TARGET).select(RETURN, snapshot)
        is snapshot
    )
    string_cloud = cloud(RETURN, "warm", resource_type="stringValue", unit=None)
    assert (
        LocalSelection(FakeRuntime([local(RETURN, 31.5)]), TARGET).select(
            RETURN, string_cloud
        )
        is string_cloud
    )
    marker = local(
        "/system/sensors/temperatures/outdoor_t1",
        -32768,
        state=[{"open": -32768, "short": 32767}],
    )
    outdoor = cloud("/system/sensors/temperatures/outdoor_t1", 4.5)
    assert (
        LocalSelection(FakeRuntime([marker]), TARGET).select(
            outdoor.resource.path, outdoor
        )
        is outdoor
    )


@pytest.mark.parametrize(
    ("path", "observation"),
    [
        ("/heatingCircuits/hc1/maxFlowTemp", {"value": 48.0, "writeable": 1}),
        (
            "/heatSources/hs1/pumpVolumeFlow",
            {"value": 900, "type": "integerValue", "unitOfMeasure": "l/h"},
        ),
        ("/heatingCircuits/hc1/manualRoomSetpoint", None),
    ],
)
def test_adjustable_unpaired_and_single_source_readings_stay_cloud(
    path, observation
) -> None:
    writable = path.endswith("maxFlowTemp")
    snapshot = cloud(path, 30.0, writable=writable)
    observations = (
        [local(path, **observation)] if observation else [local(RETURN, 31.0)]
    )
    selection = LocalSelection(FakeRuntime(observations), TARGET)
    assert selection.select(path, snapshot) is snapshot
    assert eligible(path, snapshot) is False


def test_counters_stay_cloud() -> None:
    path = "/heatSources/emon/totalConsumption"
    snapshot = ResourceSnapshot(
        Resource(
            path,
            values=({"total": 10},),
            has_values=True,
            metadata=ResourceMetadata(resource_type="emonValue", unit="kWh"),
        ),
        True,
        NOW,
    )
    observation = read(
        {
            "id": path,
            "type": "emonValue",
            "writeable": 0,
            "unit": "kWh",
            "values": [{"total": 11}],
        }
    )
    assert (
        LocalSelection(FakeRuntime([observation]), TARGET).select(path, snapshot)
        is snapshot
    )
    assert local_snapshot(snapshot, observation, TARGET) is None


def test_disabled_local_binding_and_other_targets_are_ignored() -> None:
    snapshot = cloud(RETURN, 30.0)
    off = LocalSelection(
        FakeRuntime([local(RETURN, 31.5)], binding_enabled=False), TARGET
    )
    assert off.select(RETURN, snapshot) is snapshot
    foreign = LocalSelection(
        FakeRuntime([local(RETURN, 31.5)], target="target-b"), TARGET
    )
    assert foreign.select(RETURN, snapshot) is snapshot


def test_numeric_and_string_readings_keep_their_native_types() -> None:
    modulation = cloud("/heatSources/actualModulation", 40.0, unit="%")
    shown = LocalSelection(
        FakeRuntime([local("/heatSources/actualModulation", 42, unitOfMeasure="%")]),
        TARGET,
    ).select(modulation.resource.path, modulation)
    assert shown.resource.value == 42.0 and type(shown.resource.value) is float
    text_path = "/heatSources/flameStatus"
    text = cloud(text_path, "off", resource_type="stringValue", unit=None)
    observation = read(
        {
            "id": text_path,
            "type": "stringValue",
            "writeable": 0,
            "value": "on",
            "allowedValues": ["off", "on"],
        }
    )
    shown = LocalSelection(FakeRuntime([observation]), TARGET).select(text_path, text)
    assert shown.resource.value == "on"
    assert _native(5, "integerValue") == 5
    assert _native(Decimal("5.5"), "integerValue") is None
    assert _native(True, "floatValue") is None
    assert _native("x", "floatValue") is None


def test_change_detection_reports_only_new_local_readings() -> None:
    runtime = FakeRuntime([local(RETURN, 31.5, monotonic=10.0)])
    selection = LocalSelection(runtime, TARGET)
    assert selection.changed() is True
    assert selection.changed() is False
    runtime.observations = [local(RETURN, 31.5, monotonic=70.0)]
    assert selection.changed() is True
    runtime.snapshot.enabled = False
    assert selection.changed() is True
    assert selection.changed() is False


def test_summary_counts_without_values() -> None:
    data = {
        RETURN: cloud(RETURN, 30.0),
        "/heatSources/actualSupplyTemperature": cloud(
            "/heatSources/actualSupplyTemperature", 40.0
        ),
        "/heatingCircuits/hc1/maxFlowTemp": cloud(
            "/heatingCircuits/hc1/maxFlowTemp", 55.0, writable=True
        ),
    }
    selection = LocalSelection(FakeRuntime([local(RETURN, 31.5)]), TARGET)
    assert selection.summary(data) == {
        "enabled": True,
        "eligible_entities_sources": 2,
        "using_local": 1,
        "using_cloud": 1,
        "cloud_reasons": {"local_not_current": 1},
        "cloud_reason_measurements": {"local_not_current": ["supply_temperature"]},
        "write_checks": {},
    }
    assert "31.5" not in repr(selection.summary(data))


async def test_coordinator_display_snapshot_is_unchanged_without_selection(
    hass,
) -> None:
    from tests.test_sensor import _sensor

    snapshot = replace(cloud(RETURN, 30.0), last_success=datetime.now(UTC))
    coordinator = _sensor(hass, snapshot.resource).coordinator
    coordinator.data = {RETURN: snapshot}
    assert coordinator.local_selection is None
    assert coordinator.display_snapshot(RETURN) is snapshot
    coordinator.local_selection = LocalSelection(
        FakeRuntime([local(RETURN, 31.5)]), TARGET
    )
    assert coordinator.display_snapshot(RETURN).resource.value == 31.5


# ------------------------------------------------- stored option and changes


async def test_local_values_option_is_a_view_preference(hass) -> None:
    _entry, management, cloud_id, target = registered(hass, local=True)
    before = management.current()
    local_id = next(
        key for key, item in before.connections.items() if item.kind == LOCAL_KIND
    )
    after = management.set_local_values(management.edit(local_id), True)
    assert after.to_options()["targets"][target]["local_values"] is True
    changes = configuration_changes(before, after)
    assert not changes.connections and not changes.bindings and not changes.tokens
    off = management.set_local_values(management.edit(local_id), False)
    assert off.to_options()["targets"][target]["local_values"] is False
    with pytest.raises(ConfigurationError, match="unsupported_connection"):
        management.set_local_values(management.edit(cloud_id), True)
    with pytest.raises(ConfigurationError, match="invalid_local_values"):
        management.set_local_values(management.edit(local_id), "yes")
    options = off.to_options()
    options["targets"][target]["local_values"] = "yes"
    with pytest.raises(ConfigurationError, match="invalid_local_values"):
        load_configuration(
            off.owner_id, off.to_data(), options, minor_version=off.minor_version
        )
    assert isinstance(management, ConfigurationManagement)


async def test_existing_sensor_entity_displays_local_then_cloud_values(hass) -> None:
    from tests.test_sensor import _sensor

    resource = Resource(
        RETURN,
        value=30.0,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="C"),
    )
    sensor = _sensor(hass, resource)
    coordinator = sensor.coordinator
    unique_id = sensor.unique_id
    assert sensor.native_value == 30.0 and sensor.available

    runtime = FakeRuntime([local(RETURN, 31.5)])
    coordinator.local_selection = LocalSelection(runtime, TARGET)
    assert sensor.native_value == 31.5
    assert sensor.unique_id == unique_id

    # Cloud outage: the local value keeps the entity available.
    coordinator.data = {
        RETURN: ResourceSnapshot(resource, False, NOW, freshness=Freshness.STALE)
    }
    assert sensor.available and sensor.native_value == 31.5

    # Local value no longer current: the cloud rules apply again.
    runtime.observations = []
    assert not sensor.available

    # Switched off: exactly the cloud behavior.
    runtime.observations = [local(RETURN, 31.5)]
    runtime.snapshot.enabled = False
    coordinator.data = {RETURN: ResourceSnapshot(resource, True, NOW)}
    assert sensor.native_value == 30.0


def test_new_local_readings_refresh_entities_once_and_only_for_current_sources() -> (
    None
):
    from custom_components.bosch_buderus_heating.managed_pointt import (
        ManagedPointTCoordinator,
    )

    runtime = FakeRuntime([local(RETURN, 31.5, monotonic=10.0)])
    current = {"value": True}
    fake = SimpleNamespace(
        source=SimpleNamespace(current=lambda: current["value"]),
        local_selection=LocalSelection(runtime, TARGET),
        async_update_listeners=MagicMock(),
        local_retained=False,
        _local_faults_updated=lambda selection: None,
    )
    refresh = ManagedPointTCoordinator._local_updated
    refresh(fake)
    refresh(fake)
    assert fake.async_update_listeners.call_count == 1
    runtime.observations = [local(RETURN, 31.6, monotonic=70.0)]
    current["value"] = False
    refresh(fake)
    assert fake.async_update_listeners.call_count == 1
    current["value"] = True
    refresh(fake)
    assert fake.async_update_listeners.call_count == 2
    # P40: a stopped source whose view local readings keep showing.
    runtime.observations = [local(RETURN, 31.7, monotonic=130.0)]
    current["value"] = False
    fake.local_retained = True
    refresh(fake)
    assert fake.async_update_listeners.call_count == 3
    fake.local_retained = False
    fake.local_selection = None
    refresh(fake)
    assert fake.async_update_listeners.call_count == 3


def test_integral_local_value_keeps_the_cloud_integer_type() -> None:
    speed = cloud("/heatSources/actualModulation", 100, unit="%")
    shown = LocalSelection(
        FakeRuntime([local("/heatSources/actualModulation", 100, unitOfMeasure="%")]),
        TARGET,
    ).select(speed.resource.path, speed)
    assert shown.resource.value == 100 and type(shown.resource.value) is int
    fractional = LocalSelection(
        FakeRuntime([local("/heatSources/actualModulation", 100.5, unitOfMeasure="%")]),
        TARGET,
    ).select(speed.resource.path, speed)
    assert fractional.resource.value == 100.5


async def test_pressure_status_combines_cloud_readings_only(hass) -> None:
    from tests.test_sensor import _pressure_sensors

    pressure, status = _pressure_sensors(hass, pressure_value=2.0)
    assert status is not None
    before = status.native_value
    pressure.coordinator.local_selection = LocalSelection(
        FakeRuntime([local("/heatSources/systemPressure", 3.2, unitOfMeasure="bar")]),
        TARGET,
    )
    assert pressure.native_value == 3.2  # the plain reading may be local
    assert status.native_value == before  # the combined status stays cloud


async def test_local_values_menu_requires_a_linked_installation(
    hass, enable_custom_integrations
) -> None:
    from homeassistant.config_entries import ConfigEntryState

    from custom_components.bosch_buderus_heating.const import DOMAIN
    from tests.ha_test_helpers import MockConfigEntry
    from tests.test_local_setup import starting

    snapshot, _cloud, _target = starting(local=True)
    local_id = next(
        key for key, item in snapshot.connections.items() if item.kind == "k40_local"
    )

    async def menu(data, options):
        entry = MockConfigEntry(
            domain=DOMAIN, version=4, data=data, options=options, unique_id=None
        )
        entry.add_to_hass(hass)
        assert entry.state is ConfigEntryState.NOT_LOADED
        manager = hass.config_entries.options
        result = await manager.async_init(entry.entry_id)
        result = await manager.async_configure(
            result["flow_id"], {"next_step_id": "connections"}
        )
        result = await manager.async_configure(
            result["flow_id"], {"connection": local_id}
        )
        manager.async_abort(result["flow_id"])
        return result["menu_options"]

    data, options = snapshot.to_data(), snapshot.to_options()
    assert "local_values" in await menu(data, options)
    unbound = [
        key
        for key, binding in snapshot.bindings.items()
        if binding.connection_id == local_id
    ]
    data, options = snapshot.to_data(), snapshot.to_options()
    for key in unbound:
        data["bindings"].pop(key)
        options["bindings"].pop(key, None)
        for target in options["targets"].values():
            if key in target.get("read_order", []):
                target["read_order"].remove(key)
    items = await menu(data, options)
    assert "local_polling" in items and "local_values" not in items


def test_repeated_lookups_do_not_rescan_the_catalog() -> None:
    """Entity state asks for local values on every write; the scan is cached."""
    from custom_components.bosch_buderus_heating.observations import pair_for_path

    observations = [local(RETURN, float(i), monotonic=float(i)) for i in range(200)]
    selection = LocalSelection(FakeRuntime(observations), TARGET)
    snapshot = cloud(RETURN, 30.0)
    selection.select(RETURN, snapshot)
    misses = pair_for_path.cache_info().misses
    for _ in range(50):
        selection.select(RETURN, snapshot)
    assert pair_for_path.cache_info().misses == misses
