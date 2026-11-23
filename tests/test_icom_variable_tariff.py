"""The variable tariff, read only (0.9.2).

An iCom gateway: two leaves probed once during discovery, shown as a
diagnostic binary sensor and sensor (disabled by default), never written.
A K30/K40: the children of ``/system/variableTariff`` (since 0.9.0) give
read-only sensors. The shapes follow ``fixtures/contracts/icom/tbso.json``
and ``fixtures/contracts/connectkey/variable-tariff-k40.json``; every value
is constructed. Nothing here confirms behaviour on a real installation.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import EntityCategory

from custom_components.bosch_buderus_heating.binary_sensor import (
    build_binary_sensor_descriptions,
)
from custom_components.bosch_buderus_heating.circuit_control_writes import (
    circuit_control_diagnostics,
)
from custom_components.bosch_buderus_heating.discovery import (
    DiscoveryDiagnostics,
    async_discover_resources,
)
from custom_components.bosch_buderus_heating.number import build_number_descriptions
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    ResourceError,
    ResourceNotFound,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.pointt.models import BatchItemResult
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.resource_catalog import (
    PollGroup,
    poll_group,
)
from custom_components.bosch_buderus_heating.select import build_select_descriptions
from custom_components.bosch_buderus_heating.sensor import build_sensor_descriptions
from custom_components.bosch_buderus_heating.switch import build_switch_descriptions
from custom_components.bosch_buderus_heating.variable_tariff import (
    ICOM_TARIFF_PATHS,
    TARIFF_ENABLE_PATH,
    TARIFF_ID_PATH,
    tariff_enabled,
    tariff_id,
)
from custom_components.bosch_buderus_heating.variable_tariff_entities import (
    TariffIdSensor,
    VariableTariffActiveBinarySensor,
    track_tariff_binary_sensor,
    track_tariff_sensor,
)
from custom_components.bosch_buderus_heating.writes import assess_control
from tests.circuit_control_helpers import coordinator_for, variant
from tests.test_write_guards import _Lifecycle

ICOM = "icom/tbso.json"
K40 = "connectkey/variable-tariff-k40.json"
COMPONENT = Path(__file__).parents[1] / "custom_components/bosch_buderus_heating"


# K30/K40: the children of /system/variableTariff ----------------------------------


def test_every_k40_tariff_child_is_a_disabled_reading():
    resources = variant(K40, "base")
    children = {path for path in resources if path != "/system/variableTariff"}
    assert len(children) == 15
    sensors = {item.key: item for item in build_sensor_descriptions(resources)}
    keys = {path.strip("/").replace("/", ":") for path in children}
    # supportStatus is an array: its sensor carries the ":values" suffix.
    assert {key.removesuffix(":values") for key in sensors} == keys
    assert all(not item.entity_registry_enabled_default for item in sensors.values())
    assert all(
        item.entity_category is EntityCategory.DIAGNOSTIC for item in sensors.values()
    )
    status = sensors["system:variableTariff:supportStatus:values"]
    assert status.device_class == "enum"
    for delta in ("highPriceDelta", "lowPriceDelta"):
        assert sensors[f"system:variableTariff:ch:{delta}"].device_class == (
            "temperature_delta"
        )
    # Nothing to operate: no number, select, switch or binary sensor.
    for build in (
        build_number_descriptions,
        build_select_descriptions,
        build_switch_descriptions,
        build_binary_sensor_descriptions,
    ):
        assert build(resources) == ()


@pytest.mark.parametrize(
    "path",
    [
        "/system/variableTariff/tariffId",
        "/system/variableTariff/ch/optimization",
        "/system/variableTariff/dhw/highPriceEnable",
        "/system/variableTariff/ch/highPriceDelta",
        TARIFF_ENABLE_PATH,
        TARIFF_ID_PATH,
    ],
)
def test_tariff_paths_have_no_write_policy(path):
    resources = {**variant(K40, "base"), **variant(ICOM, "on")}
    assert assess_control(resources[path]).rejection_reason == (
        "no_scalar_control_policy"
    )


# iCom: the readings ------------------------------------------------------------------


def test_parsing_of_the_tariff_values():
    on = variant(ICOM, "on")
    off = variant(ICOM, "off")
    assert tariff_enabled(on[TARIFF_ENABLE_PATH]) is True
    assert tariff_enabled(off[TARIFF_ENABLE_PATH]) is False
    assert tariff_id(on[TARIFF_ID_PATH]) == 3
    assert tariff_id(off[TARIFF_ID_PATH]) == 5
    for broken in (3.5, -1, float("nan"), True, "3"):
        item = parse_resource({"id": TARIFF_ID_PATH, "type": "floatValue", "value": 1})
        from dataclasses import replace

        assert tariff_id(replace(item, value=broken)) is None
    assert tariff_enabled(None) is None


@pytest.mark.parametrize(
    ("key", "state", "number", "chosen"),
    [
        ("on", True, 3, True),
        ("off", False, 5, False),
    ],
)
async def test_entities_show_state_and_number(hass, key, state, number, chosen):
    coordinator = coordinator_for(hass, variant(ICOM, key))
    binary = VariableTariffActiveBinarySensor(coordinator)
    sensor = TariffIdSensor(coordinator)
    for entity in (binary, sensor):
        assert entity.entity_category is EntityCategory.DIAGNOSTIC
        assert not entity.entity_registry_enabled_default
    assert binary.translation_key == "variable_tariff_active"
    assert sensor.translation_key == "tariff_id"
    assert binary.unique_id == "gateway:application:tbso:enable"
    assert sensor.unique_id == "gateway:application:tbso:electricity:tariffID"
    assert binary.available and binary.is_on is state
    assert sensor.available and sensor.native_value == number
    # 5 means "no tariff".
    assert sensor.extra_state_attributes == {"tariff_selected": chosen}


async def test_entities_are_added_only_with_the_paths(hass):
    for key, expected in (("on", 1), ("missing", 0)):
        coordinator = coordinator_for(hass, variant(ICOM, key))
        added: list = []
        lifecycle = _Lifecycle()
        track_tariff_binary_sensor(lifecycle, coordinator, added.extend)
        track_tariff_sensor(lifecycle, coordinator, added.extend)
        assert len(added) == 2 * expected
        lifecycle.unload()


@pytest.mark.parametrize("path", ICOM_TARIFF_PATHS)
def test_tariff_paths_are_read_once(path):
    assert poll_group(variant(ICOM, "on")[path]) is PollGroup.STATIC


def test_diagnostics_show_types_only(hass):
    coordinator = coordinator_for(hass, variant(ICOM, "on"))
    report = circuit_control_diagnostics(coordinator)
    assert report["variable_tariff"] == {
        "enable": "stringValue",
        "tariff_id": "floatValue",
    }
    text = json.dumps(report)
    assert "3.0" not in text and '"on"' not in text
    missing = coordinator_for(hass, variant(ICOM, "missing"))
    assert circuit_control_diagnostics(missing)["variable_tariff"] == {
        "enable": "missing",
        "tariff_id": "missing",
    }
    other = coordinator_for(hass, variant(ICOM, "missing"), device_type="K40")
    assert circuit_control_diagnostics(other)["variable_tariff"] == {
        "enable": "not_probed",
        "tariff_id": "not_probed",
    }


# iCom: the discovery probes ---------------------------------------------------------


def _client(tree: dict[str, object]) -> AsyncMock:
    client = AsyncMock()

    def answer(gateway, path):
        item = tree.get(path, 404)
        if isinstance(item, int):
            return BatchItemResult(gateway, path, item, error=ResourceError(path, item))
        return BatchItemResult(gateway, path, 200, item)

    client.get_resources_bulk.side_effect = lambda gateway, chunk: tuple(
        answer(gateway, path) for path in chunk
    )

    async def single(gateway, path, **_kwargs):
        item = tree.get(path, 404)
        if isinstance(item, int):
            if item >= 500:
                raise ServiceUnavailable(item)
            raise ResourceNotFound(path, item)
        return item

    client.get_resource.side_effect = single
    return client


def _requested(client: AsyncMock) -> set[str]:
    paths: set[str] = set()
    for call in client.get_resources_bulk.await_args_list:
        paths.update(call.args[1])
    for call in client.get_resource.await_args_list:
        paths.add(call.args[1])
    return paths


async def test_icom_discovery_probes_the_tariff_once():
    tree = dict(variant(ICOM, "on"))
    tree["/system"] = parse_resource(
        {"id": "/system", "type": "refEnum", "references": []}
    )
    client = _client(tree)
    discovered = await async_discover_resources(
        client,
        "gateway",
        roots=("/system",),
        gateway=Gateway("gateway", device_type="iCom"),
    )
    assert set(ICOM_TARIFF_PATHS) <= set(discovered)


@pytest.mark.parametrize("status", [403, 404])
async def test_refused_or_missing_tariff_changes_nothing(status):
    tree: dict[str, object] = {path: status for path in ICOM_TARIFF_PATHS}
    tree["/system"] = parse_resource(
        {"id": "/system", "type": "refEnum", "references": []}
    )
    report = DiscoveryDiagnostics()
    discovered = await async_discover_resources(
        _client(tree),
        "gateway",
        roots=("/system",),
        gateway=Gateway("gateway", device_type="icom"),
        diagnostics=report,
    )
    assert not set(ICOM_TARIFF_PATHS) & set(discovered)
    assert "/system" in discovered
    assert report.completed


async def test_server_errors_on_the_tariff_do_not_end_the_discovery():
    """Two roots fail with 5xx (two in the outage series); the tariff leaves
    answering 5xx as well would make it four and end the discovery, but they
    count as missing without an individual read."""
    tree: dict[str, object] = {path: 503 for path in ICOM_TARIFF_PATHS}
    tree["/r1"] = 503
    tree["/r2"] = 503
    report = DiscoveryDiagnostics()
    client = _client(tree)
    await async_discover_resources(
        client,
        "gateway",
        roots=("/r1", "/r2"),
        gateway=Gateway("gateway", device_type="icom"),
        diagnostics=report,
    )
    assert report.completed
    singles = {call.args[1] for call in client.get_resource.await_args_list}
    assert not singles & set(ICOM_TARIFF_PATHS)
    for path in ICOM_TARIFF_PATHS:
        assert report.paths[path].read_policy == "optional_5xx_assumed_missing"
    assert report.snapshot()["paths_not_read"] == {"optional_5xx_assumed_missing": 2}


@pytest.mark.parametrize("device_type", ["K40", "rrc2", None])
async def test_other_gateways_are_not_probed(device_type):
    tree = dict(variant(ICOM, "on"))
    tree["/system"] = parse_resource(
        {"id": "/system", "type": "refEnum", "references": []}
    )
    client = _client(tree)
    await async_discover_resources(
        client,
        "gateway",
        roots=("/system",),
        gateway=Gateway("gateway", device_type=device_type),
    )
    assert not any(path.startswith("/application") for path in _requested(client))


async def test_rediscovery_does_not_probe_again():
    tree = dict(variant(ICOM, "on"))
    tree["/system"] = parse_resource(
        {"id": "/system", "type": "refEnum", "references": []}
    )
    client = _client(tree)
    await async_discover_resources(
        client,
        "gateway",
        roots=("/system",),
        gateway=Gateway("gateway", device_type="icom"),
        rediscovery=True,
    )
    assert not any(path.startswith("/application") for path in _requested(client))


# Translations ----------------------------------------------------------------------


def test_names_in_every_language():
    for path in sorted((COMPONENT / "translations").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["entity"]["binary_sensor"]["variable_tariff_active"]["name"]
        assert data["entity"]["sensor"]["tariff_id"]["name"]
