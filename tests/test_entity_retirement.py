"""Regression coverage for resource retirement after complete discovery."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import _remove_retired_entities
from custom_components.bosch_buderus_heating.binary_sensor import (
    BoschBuderusBinarySensor,
    build_binary_sensor_descriptions,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.data import tokens_to_data
from custom_components.bosch_buderus_heating.diagnostics import (
    _capability_diagnostics,
    _discovery_diagnostics,
)
from custom_components.bosch_buderus_heating.entry_sources import pointt_coordinators
from custom_components.bosch_buderus_heating.pointt import (
    AuthTokens,
    BatchItemResult,
    Gateway,
    Resource,
    ResourceMetadata,
    ResourceReference,
)
from custom_components.bosch_buderus_heating.pointt.parsers import resource_error
from custom_components.bosch_buderus_heating.resource_catalog import supports_entity
from custom_components.bosch_buderus_heating.sensor import (
    BoschBuderusSensor,
    build_sensor_descriptions,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.helpers.managed_runtime import wait_for_sources

_BASE = "custom_components.bosch_buderus_heating"
_SOLAR_FIELDS = (
    "collectorTemperature",
    "dhwTankBottomTemperature",
    "maxCylinderTemperature",
    "maxTemperatureReached",
    "pumpModulation",
    "solarYield",
)


def _parent(path: str, children: list[str]) -> Resource:
    return Resource(
        path=path,
        references=tuple(ResourceReference(child) for child in children),
    )


def _solar_tree(
    circuits: tuple[str, ...],
    limit_type: str,
    value_mode: str,
    root_references: bool,
) -> dict[str, Resource]:
    resources: dict[str, Resource] = {}
    for circuit in circuits:
        for field in _SOLAR_FIELDS:
            path = f"/solarCircuits/{circuit}/{field}"
            limit = field == "maxTemperatureReached"
            value = (
                (False if limit_type == "booleanValue" else "off")
                if limit
                else (1500.0 if field == "solarYield" else 42.5)
            )
            if value_mode == "zero" and not limit:
                value = 0
            if value_mode in {"missing", "null"}:
                value = None
            resources[path] = Resource(
                path=path,
                value=value,
                has_value=value_mode != "missing",
                metadata=ResourceMetadata(
                    resource_type=limit_type if limit else "floatValue",
                    unit=(
                        None
                        if limit
                        else "%"
                        if field == "pumpModulation"
                        else "Wh"
                        if field == "solarYield"
                        else "C"
                    ),
                    allowed_values=("off", "on") if limit else (),
                ),
            )
        parent_path = f"/solarCircuits/{circuit}"
        resources[parent_path] = _parent(
            parent_path,
            [path for path in resources if path.startswith(parent_path + "/")],
        )
    circuit_paths = [f"/solarCircuits/{circuit}" for circuit in circuits]
    resources["/solarCircuits"] = _parent(
        "/solarCircuits", circuit_paths if root_references else []
    )
    # Alternate references allow the circuit to be discovered if its root is
    # unreadable or returns an empty directory.
    resources["/system"] = _parent("/system", ["/system/brand", *circuit_paths])
    resources["/system/brand"] = Resource(
        path="/system/brand", value="Buderus", has_value=True
    )
    energy_path = "/heatSources/emon/totalConsumption"
    resources["/heatSources"] = _parent("/heatSources", [energy_path])
    resources[energy_path] = Resource(
        path=energy_path,
        metadata=ResourceMetadata(resource_type="emonValue", unit="kWh"),
        values=({"solar": 10.0},),
    )
    return resources


@pytest.mark.parametrize(
    (
        "circuits",
        "gateway_count",
        "limit_type",
        "value_mode",
        "root_status",
        "circuit_status",
        "root_references",
        "preseed",
    ),
    [
        pytest.param(
            ("sc1",),
            1,
            "stringValue",
            "normal",
            200,
            200,
            True,
            False,
            id="complete-tree",
        ),
        pytest.param(
            ("sc1", "sc2"),
            2,
            "booleanValue",
            "zero",
            200,
            200,
            True,
            False,
            id="two-gateways-two-circuits",
        ),
        pytest.param(
            ("sc1",),
            1,
            "stringValue",
            "normal",
            200,
            403,
            True,
            False,
            id="circuit-403",
        ),
        pytest.param(
            ("sc1",),
            1,
            "stringValue",
            "normal",
            200,
            404,
            True,
            False,
            id="circuit-404",
        ),
        pytest.param(
            ("sc1",),
            1,
            "stringValue",
            "normal",
            200,
            406,
            True,
            False,
            id="circuit-406",
        ),
        pytest.param(
            ("sc1",), 1, "stringValue", "normal", 403, 200, True, False, id="root-403"
        ),
        pytest.param(
            ("sc1",), 1, "stringValue", "normal", 404, 200, True, False, id="root-404"
        ),
        pytest.param(
            ("sc1",), 1, "stringValue", "normal", 406, 200, True, False, id="root-406"
        ),
        pytest.param(
            ("sc1",),
            1,
            "stringValue",
            "missing",
            200,
            200,
            True,
            False,
            id="missing-values",
        ),
        pytest.param(
            ("sc1",), 1, "stringValue", "null", 200, 200, True, False, id="null-values"
        ),
        pytest.param(
            ("sc1",),
            1,
            "stringValue",
            "normal",
            200,
            200,
            False,
            False,
            id="empty-root",
        ),
        pytest.param(
            ("sc1",),
            1,
            "stringValue",
            "normal",
            200,
            200,
            True,
            True,
            id="user-disabled",
        ),
    ],
)
async def test_solar_entities_survive_discovery_polling_and_reload(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    caplog: pytest.LogCaptureFixture,
    circuits: tuple[str, ...],
    gateway_count: int,
    limit_type: str,
    value_mode: str,
    root_status: int,
    circuit_status: int,
    root_references: bool,
    preseed: bool,
) -> None:
    gateways = tuple(
        Gateway(f"gateway-{number}", device_type="k30")
        for number in range(gateway_count)
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={
            "brand": "buderus",
            "gateway_ids": [gateway.gateway_id for gateway in gateways],
            **tokens_to_data(
                AuthTokens(
                    "synthetic-access", "synthetic-refresh", expires_at=9999999999
                )
            ),
        },
    )
    entry.add_to_hass(hass)
    resources = _solar_tree(circuits, limit_type, value_mode, root_references)
    statuses = {
        "/solarCircuits": root_status,
        **{f"/solarCircuits/{circuit}": circuit_status for circuit in circuits},
    }

    async def bulk_result(
        gateway_id: str, paths: tuple[str, ...], **kwargs: object
    ) -> tuple[BatchItemResult, ...]:
        results = []
        for path in paths:
            status = statuses.get(path, 200 if path in resources else 404)
            results.append(
                BatchItemResult(gateway_id, path, status, resource=resources[path])
                if status == 200
                else BatchItemResult(
                    gateway_id, path, status, error=resource_error(path, status)
                )
            )
        return tuple(results)

    registry = er.async_get(hass)

    def solar_entries() -> list[er.RegistryEntry]:
        return [
            item
            for item in er.async_entries_for_config_entry(registry, entry.entry_id)
            if ":solarCircuits:" in item.unique_id
        ]

    user_entity = None
    if preseed:
        user_entity = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            "gateway-0:solarCircuits:sc1:collectorTemperature",
            config_entry=entry,
            disabled_by=er.RegistryEntryDisabler.USER,
        )
        user_entity = registry.async_update_entity(
            user_entity.entity_id, name="My collector", icon="mdi:thermometer"
        )
    cleanup_counts: list[tuple[int, int]] = []

    def cleanup(
        hass: HomeAssistant,
        entry: MockConfigEntry,
        coordinators: tuple[BoschBuderusDataUpdateCoordinator, ...],
        **kwargs: bool,
    ) -> None:
        before = len(solar_entries())
        _remove_retired_entities(hass, entry, coordinators, **kwargs)
        cleanup_counts.append((before, len(solar_entries())))

    expected_count = 6 * len(circuits) * gateway_count
    with (
        patch(f"{_BASE}.PointTClient.get_gateways", AsyncMock(return_value=gateways)),
        patch(f"{_BASE}.PointTClient.get_resources_bulk", side_effect=bulk_result),
        patch(f"{_BASE}._remove_retired_entities", side_effect=cleanup),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await wait_for_sources(hass, entry, gateway_count)
        await hass.async_block_till_done()
        # 0.9.0-alpha.2: the managed views run the cleanup; it removes none.
        assert all(before == after for before, after in cleanup_counts)
        identities = {
            item.unique_id: (item.id, item.entity_id) for item in solar_entries()
        }
        assert len(identities) == expected_count
        for coordinator in pointt_coordinators(entry):
            discovery = _discovery_diagnostics(coordinator)
            assert discovery["completed"] and discovery["stop_reason"] == "complete"
            for circuit in circuits:
                group = discovery["groups"][f"/solarCircuits/{circuit}"]
                assert group["paths_requested"] == 7
                assert group["resources_discovered"] == (
                    7 if circuit_status == 200 else 6
                )
                assert group["paths_failed"] == (0 if circuit_status == 200 else 1)
                for field in _SOLAR_FIELDS:
                    path = f"/solarCircuits/{circuit}/{field}"
                    diagnostic = _capability_diagnostics(
                        coordinator.resources[path],
                        coordinator.data[path],
                        coordinator.capability_metrics(path),
                        0,
                    )
                    assert diagnostic["entity_supported"]
                    assert diagnostic["entity_enabled_by_default"]
                    assert diagnostic["available"]
                    assert diagnostic["freshness"] == "fresh"
                    assert diagnostic["last_error_category"] is None
            await coordinator.async_request_refresh()
        await hass.async_block_till_done()
        assert len(solar_entries()) == expected_count
        assert (
            len(
                [
                    item
                    for item in er.async_entries_for_config_entry(
                        registry, entry.entry_id
                    )
                    if ":heatSources:emon:totalConsumption:solar" in item.unique_id
                ]
            )
            == gateway_count
        )
        assert await hass.config_entries.async_reload(entry.entry_id)
        await wait_for_sources(hass, entry, gateway_count)
        await hass.async_block_till_done()
        assert cleanup_counts and all(
            before == after for before, after in cleanup_counts
        )
        assert {
            item.unique_id: (item.id, item.entity_id) for item in solar_entries()
        } == identities
        if user_entity is not None:
            retained = registry.async_get(user_entity.entity_id)
            assert retained is not None
            assert retained.name == "My collector"
            assert retained.icon == "mdi:thermometer"
            assert retained.disabled_by is er.RegistryEntryDisabler.USER
        assert not [record for record in caplog.records if record.levelno >= 40]
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("parent_path", "path", "resource_type", "unit"),
    [
        ("/pool", "/pool/currentTemp", "floatValue", "C"),
        ("/ventilation", "/ventilation/zone1/sensors/supplyTemp", "floatValue", "C"),
        ("/zones", "/zones/zone1/averageCurrentTemperature", "floatValue", "C"),
        ("/pv", "/pv/surplusAvailable", "booleanValue", None),
        ("/system/appliance", "/system/appliance/enabled", "stringValue", None),
        (
            "/system/seasonOptimizer",
            "/system/seasonOptimizer/heatingThreshold",
            "floatValue",
            "C",
        ),
        (
            "/heatingCircuits/hc1/cooling",
            "/heatingCircuits/hc1/cooling/roomTempSetpoint",
            "floatValue",
            "C",
        ),
        (
            "/dhwCircuits/dhw1/sensor",
            "/dhwCircuits/dhw1/sensor/externalTankTemperature",
            "floatValue",
            "C",
        ),
        (
            "/heatSources/hs1/brineCircuit",
            "/heatSources/hs1/brineCircuit/collectorInflowTemp",
            "floatValue",
            "C",
        ),
        (
            "/heatSources/hs1/emon",
            "/heatSources/hs1/emon/totalConsumption",
            "emonValue",
            "kWh",
        ),
        ("/heatSources/emon", "/heatSources/emon/totalConsumption", "emonValue", "kWh"),
    ],
)
def test_retirement_preserves_other_supported_descendants(
    hass: HomeAssistant,
    parent_path: str,
    path: str,
    resource_type: str,
    unit: str | None,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    resource = Resource(
        path=path,
        value=True
        if resource_type == "booleanValue"
        else "on"
        if resource_type == "stringValue"
        else 25.0,
        has_value=resource_type != "emonValue",
        values=(
            ({"compressor": 1.0, "eheater": 2.0, "outputProduced": 6.0},)
            if resource_type == "emonValue"
            else ()
        ),
        metadata=ResourceMetadata(resource_type=resource_type, unit=unit),
    )
    resources = {parent_path: _parent(parent_path, [path]), path: resource}
    assert supports_entity(resource)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-one"), entry
    )
    coordinator.resources = resources
    coordinator.data = {
        key: ResourceSnapshot(item, True, datetime.now(UTC))
        for key, item in resources.items()
    }
    entities = [
        ("sensor", BoschBuderusSensor(coordinator, description))
        for description in build_sensor_descriptions(resources)
    ] + [
        ("binary_sensor", BoschBuderusBinarySensor(coordinator, description))
        for description in build_binary_sensor_descriptions(resources)
    ]
    assert entities
    registry = er.async_get(hass)
    registered = [
        registry.async_get_or_create(
            domain, DOMAIN, entity.unique_id, config_entry=entry
        )
        for domain, entity in entities
    ]

    _remove_retired_entities(hass, entry, (coordinator,))

    assert all(registry.async_get(item.entity_id) == item for item in registered)


@pytest.mark.parametrize(
    ("parent_path", "entity_key"),
    [
        ("/solarCircuits", "solarCircuits:sc1:collectorTemperature"),
        ("/pool", "pool:currentTemp"),
        ("/pool", "pool:currentTemp:values"),
        ("/pool", "pool:setpointTemp:control"),
        ("/system/appliance", "system:appliance:enabled"),
        ("/system/appliance", "system:appliance:enabled:control"),
        (
            "/heatingCircuits/hc1/cooling",
            "heatingCircuits:hc1:cooling:roomTempSetpoint:control",
        ),
    ],
)
def test_retirement_keeps_catalogued_entities_missing_from_discovery(
    hass: HomeAssistant, parent_path: str, entity_key: str
) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registered = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"gateway-one:{entity_key}",
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    registered = registry.async_update_entity(
        registered.entity_id, name="My saved name", icon="mdi:thermometer"
    )
    coordinator = SimpleNamespace(
        gateway=Gateway("gateway-one"),
        resources={parent_path: Resource(path=parent_path)},
    )

    _remove_retired_entities(hass, entry, (coordinator,))

    assert registry.async_get(registered.entity_id) == registered


def test_retirement_only_removes_observed_resources_and_their_own_fields(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    foreign_entry = MockConfigEntry(domain=DOMAIN)
    foreign_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    prefix = "gateway-one:heatSources:vendorExtension"
    own_ids = (prefix, f"{prefix}:detail", f"{prefix}:progress.percent")
    retained_ids = (
        f"{prefix}:child:detail",
        f"{prefix}Extra:detail",
        "gateway-two:heatSources:vendorExtension:detail",
    )
    own = [
        registry.async_get_or_create("sensor", DOMAIN, key, config_entry=entry)
        for key in own_ids
    ]
    retained = [
        registry.async_get_or_create("sensor", DOMAIN, key, config_entry=entry)
        for key in retained_ids
    ]
    foreign = registry.async_get_or_create(
        "sensor", DOMAIN, f"{prefix}:foreign", config_entry=foreign_entry
    )
    path = "/heatSources/vendorExtension"
    coordinator = SimpleNamespace(
        gateway=Gateway("gateway-one"),
        resources={
            path: Resource(path=path, value={"detail": 1}, has_value=True),
        },
    )

    _remove_retired_entities(hass, entry, (coordinator,))

    assert all(registry.async_get(item.entity_id) is None for item in own)
    assert all(registry.async_get(item.entity_id) == item for item in retained)
    assert registry.async_get(foreign.entity_id) == foreign


# ------------------------------------------ 0.9.0-alpha.2 registry cleanups


def test_former_field_entries_keep_catalogued_and_present_children(
    hass: HomeAssistant,
) -> None:
    """The legacy field rule again spares catalogued children (old guard)."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    keys = {
        "field": "gateway-one:pool:other",
        "catalogued": "gateway-one:pool:currentTemp",
        "present": "gateway-one:heatSources:vendorExtension:detail",
    }
    created = {
        name: registry.async_get_or_create("sensor", DOMAIN, key, config_entry=entry)
        for name, key in keys.items()
    }
    child = "/heatSources/vendorExtension/detail"
    coordinator = SimpleNamespace(
        gateway=Gateway("gateway-one"),
        resources={
            "/pool": Resource(
                path="/pool", value={"currentTemp": 1, "other": 2}, has_value=True
            ),
            "/heatSources/vendorExtension": Resource(
                path="/heatSources/vendorExtension",
                value={"detail": 1},
                has_value=True,
            ),
            child: Resource(
                path=child,
                value=1.0,
                has_value=True,
                metadata=ResourceMetadata(resource_type="floatValue"),
            ),
        },
    )

    _remove_retired_entities(hass, entry, (coordinator,))

    assert registry.async_get(created["field"].entity_id) is None
    assert registry.async_get(created["catalogued"].entity_id) is not None
    assert registry.async_get(created["present"].entity_id) is not None


def _managed_view(hass, entry, **changes):
    private = "/heatSources/hs1/installerPinCode"
    values = {
        "config_entry": entry,
        "hass": hass,
        "gateway": Gateway("gateway-one"),
        "resources": {
            private: Resource(
                path=private,
                value=1234,
                has_value=True,
                metadata=ResourceMetadata(resource_type="integerValue"),
            )
        },
    }
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("case", ["managed", "legacy", "read_only"])
def test_managed_views_remove_entries_that_can_never_be_entities(
    hass: HomeAssistant, case: str
) -> None:
    from custom_components.bosch_buderus_heating import remove_never_entities

    entry = MockConfigEntry(domain=DOMAIN, version=3 if case == "legacy" else 4)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    plain = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "gateway-one:heatSources:hs1:installerPinCode",
        config_entry=entry,
    )
    named = registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        "gateway-one:heatSources:hs1:installerPinCode",
        config_entry=entry,
    )
    registry.async_update_entity(named.entity_id, name="My code")
    kept = registry.async_get_or_create(
        "sensor", DOMAIN, "gateway-one:heatSources:hs1:supplyTemp", config_entry=entry
    )
    extra = {"read_only_view": True} if case == "read_only" else {}

    remove_never_entities(_managed_view(hass, entry, **extra))

    removed = case == "managed"
    assert (registry.async_get(plain.entity_id) is None) is removed
    assert registry.async_get(named.entity_id) is not None  # customised: kept
    assert registry.async_get(kept.entity_id) is not None


def _snapshot(*targets, bindings=()):
    return SimpleNamespace(
        targets={
            f"t{index}": SimpleNamespace(
                kind=kind,
                to_data=lambda identities=identities: {"identities": identities},
            )
            for index, (kind, identities) in enumerate(targets)
        },
        bindings={
            f"b{index}": SimpleNamespace(
                to_data=lambda value=value: {
                    "locator": {"namespace": "pointt_gateway", "value": value}
                }
            )
            for index, value in enumerate(bindings)
        },
    )


_PLANT = ("pointt_plant", [{"namespace": "pointt_gateway", "value": "gateway-one"}])


@pytest.mark.parametrize("case", ["clean", "unknown_target", "unreadable"])
def test_managed_entries_lose_only_gateways_no_system_has(
    hass: HomeAssistant, case: str
) -> None:
    from custom_components.bosch_buderus_heating import (
        _remove_unconfigured_gateway_entries,
    )

    entry = MockConfigEntry(domain=DOMAIN, version=4)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)

    def create(unique_id, **changes):
        item = registry.async_get_or_create(
            "sensor", DOMAIN, unique_id, config_entry=entry
        )
        if changes:
            registry.async_update_entity(item.entity_id, **changes)
        return item.entity_id

    present = create("gateway-one:heatSources:supplyTemp")
    bound = create("gateway-two:heatSources:supplyTemp")
    gone = create("gateway-old:heatSources:supplyTemp")
    renamed = create("gateway-old:heatSources:returnTemp", name="Mine")
    disabled = create(
        "gateway-old:heatSources:power",
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    others = [
        create("bacon:abc:power"),
        create(f"{entry.entry_id}:request_metrics:total"),
        create(f"{entry.entry_id}:connection:c1:request_metrics:total"),
        create("no-colon"),
    ]
    targets = [_PLANT, ("bacon_device", [{"namespace": "bacon_device", "value": "x"}])]
    if case == "unknown_target":
        targets.append(("other", [{"namespace": "something_new", "value": "y"}]))
    load = (
        patch(f"{_BASE}.configuration.load_configuration", side_effect=ValueError)
        if case == "unreadable"
        else patch(
            f"{_BASE}.configuration.load_configuration",
            return_value=_snapshot(*targets, bindings=("gateway-two",)),
        )
    )
    with load:
        _remove_unconfigured_gateway_entries(hass, entry)

    assert (registry.async_get(gone) is None) is (case == "clean")
    for entity_id in (present, bound, renamed, disabled, *others):
        assert registry.async_get(entity_id) is not None
