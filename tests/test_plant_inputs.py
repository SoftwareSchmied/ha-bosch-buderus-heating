"""The plant model in real coordinator cycles: stored row, outages, restarts,
diagnostics (schema 49) and the shared interface for later stages."""

from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating import (
    derived_store as derived_store_module,
)
from custom_components.bosch_buderus_heating import plant_detection, plant_inputs
from custom_components.bosch_buderus_heating.component_rows import ComponentRow
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.derived_store import (
    STORAGE_KEY,
    DerivedStore,
    derived_store,
)
from custom_components.bosch_buderus_heating.diagnostics import (
    _contains_technical_values,
    _gateway_diagnostics,
)
from custom_components.bosch_buderus_heating.inventory import (
    build_anonymized_inventory,
)
from custom_components.bosch_buderus_heating.local_selection import LocalSelection
from custom_components.bosch_buderus_heating.plant_inputs import (
    PlantTracker,
    SourceRows,
    choose_rows,
    components,
    current_plant,
    describe,
    existing_tracker,
    gateway_class,
    parse_row,
    plant_diagnostics,
    row_key,
    set_probe_results,
    track_plant,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    BatchItemResult,
    Gateway,
    RateLimited,
    RequestMetrics,
    Resource,
    ResourceMetadata,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.resource_catalog import PollGroup
from tests.ha_test_helpers import MockConfigEntry, flush_store
from tests.test_coordinator_outages import clock
from tests.test_inventory import _diagnostics as inventory_diagnostics
from tests.test_local_api import read
from tests.test_local_selection import TARGET, FakeRuntime
from tests.test_plant_detection import load_case

GATEWAY = "gateway-one"
SCHEMAS = Path(__file__).parents[1] / "docs" / "schemas"
MARKERS = ("SERIAL-MARKER", "UUID-MARKER")
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------- helpers


def _resources(case: Mapping[str, Any], *, info: bool = True) -> dict[str, Resource]:
    """The cloud resources of a plant case; ``info=False``: as the stored
    inventory keeps them (``/system/info`` without its rows)."""
    allowed = case.get("allowed_values", {})
    resources = {
        path: Resource(
            path,
            value=value,
            has_value=True,
            metadata=ResourceMetadata(
                resource_type="stringValue",
                allowed_values=tuple(allowed.get(path, ())),
            ),
        )
        for path, value in case["values"].items()
    }
    for path in case.get("named_paths", ()):
        resources[path] = Resource(
            path, metadata=ResourceMetadata(resource_type="refEnum")
        )
    if case["cloud"] is not None:
        resources["/system/info"] = Resource(
            "/system/info",
            values=tuple(copy.deepcopy(case["cloud"])) if info else (),
            has_values=info,
            metadata=ResourceMetadata(resource_type="systeminfo"),
        )
    return resources


def _local(rows: Iterable[Mapping[str, Any]], observed_at: datetime = NOW) -> Any:
    return read(
        {
            "id": "/system/basicInfo",
            "type": "systeminfo",
            "writeable": 0,
            "status": "completed",
            "timeStamp": "1684201667",
            "gatewayId": "101012902",
            "values": [dict(row) for row in rows],
        },
        observed_at=observed_at,
    )


def _scalar(path: str, value: str, observed_at: datetime = NOW) -> Any:
    return read(
        {"id": path, "type": "stringValue", "writeable": 0, "value": value},
        observed_at=observed_at,
    )


def _coordinator(
    hass: HomeAssistant,
    case: Mapping[str, Any],
    *,
    client: AsyncMock | None = None,
    info: bool = True,
    local: Iterable[Any] = (),
    gateway_id: str = GATEWAY,
) -> BoschBuderusDataUpdateCoordinator:
    client = client or AsyncMock()
    client.metrics = RequestMetrics()
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    gateway = case["gateway"]
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass,
        client,
        Gateway(
            gateway_id,
            device_type=gateway["device_type"],
            firmware_version=gateway.get("firmware"),
        ),
        entry,
    )
    coordinator.update_interval = None
    coordinator.resources = _resources(case, info=info)
    coordinator.data = {
        path: ResourceSnapshot(resource, True, NOW)
        for path, resource in coordinator.resources.items()
        if resource.has_value or resource.values
    }
    coordinator._paths_by_group = {PollGroup.FAST: tuple(coordinator.resources)}
    coordinator.last_update_success = True
    coordinator.local_selection = LocalSelection(FakeRuntime(list(local)), TARGET)
    return coordinator


def _answers(
    coordinator: BoschBuderusDataUpdateCoordinator,
    changes: Mapping[str, object] | None = None,
) -> tuple[BatchItemResult, ...]:
    """A successful cycle: every path answered (new objects), with changes."""
    answers = []
    for path, resource in coordinator.resources.items():
        value = (changes or {}).get(path, resource.value)
        answers.append(
            BatchItemResult(
                GATEWAY,
                path,
                200,
                resource=replace(resource, value=value, has_value=resource.has_value),
            )
        )
    return tuple(answers)


async def _start(hass: HomeAssistant, coordinator: Any) -> PlantTracker:
    tracker = track_plant(coordinator.config_entry, coordinator)
    assert tracker is not None
    await hass.async_block_till_done()
    return tracker


async def _cycle(
    coordinator: BoschBuderusDataUpdateCoordinator, now: Any, *, seconds: int = 60
) -> None:
    now.now += seconds
    await coordinator.async_refresh()


async def _restart(hass: HomeAssistant) -> None:
    """Write the saved values and read them anew, as after a restart."""
    await flush_store(derived_store(hass)._store)
    hass.data.pop(derived_store_module._STORE)


def _validate(
    instance: object, schema: Mapping[str, Any], root: Mapping[str, Any], at: str = "$"
) -> None:
    """A small JSON schema check (the subset the published schemas use)."""
    if "$ref" in schema:
        target: Any = root
        for part in schema["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        _validate(instance, target, root, at)
        return
    kinds = schema.get("type")
    if kinds is not None:
        names = [kinds] if isinstance(kinds, str) else kinds
        checks = {
            "object": lambda v: isinstance(v, dict),
            "array": lambda v: isinstance(v, list),
            "string": lambda v: isinstance(v, str),
            "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
            "boolean": lambda v: isinstance(v, bool),
            "null": lambda v: v is None,
        }
        assert any(checks[name](instance) for name in names), f"{at}: type {kinds}"
    if "const" in schema:
        assert instance == schema["const"], f"{at}: const"
    if "enum" in schema:
        assert instance in schema["enum"], f"{at}: {instance!r} not in enum"
    if isinstance(instance, str):
        if "pattern" in schema:
            assert re.search(schema["pattern"], instance), f"{at}: pattern"
        if "maxLength" in schema:
            assert len(instance) <= schema["maxLength"], f"{at}: maxLength"
    if (
        isinstance(instance, int)
        and not isinstance(instance, bool)
        and "minimum" in schema
    ):
        assert instance >= schema["minimum"], f"{at}: minimum"
    if isinstance(instance, dict):
        for key in schema.get("required", ()):
            assert key in instance, f"{at}: {key} missing"
        if "maxProperties" in schema:
            assert len(instance) <= schema["maxProperties"], f"{at}: maxProperties"
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, value in instance.items():
            if key in properties:
                _validate(value, properties[key], root, f"{at}.{key}")
            elif extra is False:
                pytest.fail(f"{at}: unexpected key {key}")
            elif isinstance(extra, dict):
                _validate(value, extra, root, f"{at}.{key}")
    if isinstance(instance, list):
        if "maxItems" in schema:
            assert len(instance) <= schema["maxItems"], f"{at}: maxItems"
        if "minItems" in schema:
            assert len(instance) >= schema["minItems"], f"{at}: minItems"
        prefix = schema.get("prefixItems", [])
        for index, value in enumerate(instance):
            if index < len(prefix):
                _validate(value, prefix[index], root, f"{at}[{index}]")
            elif schema.get("items") is False:
                pytest.fail(f"{at}: too many items")
            elif isinstance(schema.get("items"), dict):
                _validate(value, schema["items"], root, f"{at}[{index}]")


def _check(instance: object, name: str) -> None:
    schema = json.loads((SCHEMAS / name).read_text(encoding="utf-8"))
    _validate(instance, schema, schema)


def _no_markers(text: str) -> None:
    for marker in MARKERS:
        assert marker not in text


# ---------------------------------------------------- cycles and the stored row


async def test_the_own_k40_is_worked_out_once_and_written_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    case = load_case("k40_own")
    client = AsyncMock()
    local = _local(case["local"])
    coordinator = _coordinator(hass, case, client=client, local=[local])
    calls: list[object] = []
    original = plant_inputs.resolve

    def counted(*args: Any) -> Any:
        calls.append(args)
        return original(*args)

    with patch.object(plant_inputs, "resolve", counted), clock() as now:
        tracker = await _start(hass, coordinator)
        result = current_plant(coordinator)
        assert result is not None
        assert result.plant_kind == "heat_pump"
        assert result.evidence == (
            "reported_heat_pump_type",
            "reported_heat_source_type",
            "reported_system_type",
        )
        assert len(calls) == 1 and tracker.writes == 1
        for _ in range(3):
            client.get_resources_bulk.return_value = _answers(coordinator)
            await _cycle(coordinator, now)
            assert coordinator.last_update_success
        # New snapshots with the same content: no new result, no write.
        assert len(calls) == 1
        assert tracker.writes == 1
        # Each new answer of the module list is parsed once.
        parsed = tracker._readings.parsed
        coordinator.async_update_listeners()
        coordinator.async_update_listeners()
        assert tracker._readings.parsed == parsed
    row = derived_store(hass).get(row_key(GATEWAY))
    assert row is not None
    _check(row, "plant-store-row.v1.json")
    assert [item[:2] for item in row["sources"]["cloud"]["rows"]] == [
        [8, 8],
        [16, 253],
        [72, 17],
        [75, 17],
        [8, 65281],
    ]
    assert row["sources"]["local"]["rows"] == row["sources"]["cloud"]["rows"]
    assert row["sources"]["cloud"]["rows"][0][2:] == [
        "1.2.3-abc",
        "Example indoor unit 12",
        "XCU_THH",
        "can_uds_example",
        "example.rhp:hyd.5.aw",
        "1000000001",
    ]
    assert row["inputs"]["allowed_values"]["/heatSources/hs1/type"] == [
        "Heatpump",
        "GasBoiler",
        "OilBoiler",
        "eBoiler",
    ]
    assert row["inputs"]["gateway_firmware"] == "12.34.56"
    assert row["shown"] == "cloud" and row["probes"] == []
    assert set(row["components"]) == {"8/8", "16/253", "72/17", "75/17", "8/65281"}
    rendered = json.dumps(row)
    _no_markers(rendered)
    # Versions never reach the log.
    _no_markers(caplog.text)
    for version in ("1.2.3-abc", "12.34.56", "7.8.9-def"):
        assert version not in caplog.text
    await coordinator.async_shutdown()


async def test_one_update_pass_works_the_plant_out_once(hass: HomeAssistant) -> None:
    case = load_case("k40_own")
    coordinator = _coordinator(hass, case)
    tracker = await _start(hass, coordinator)
    selection = coordinator.local_selection
    assert selection is not None
    with patch.object(plant_inputs, "_gather", wraps=plant_inputs._gather) as gather:
        with selection.update_pass():
            coordinator.async_update_listeners()
            for _ in range(3):
                assert current_plant(coordinator) is tracker.result
                assert components(coordinator) is not None
        assert gather.call_count == 1
        current_plant(coordinator)
        assert gather.call_count == 2
    await coordinator.async_shutdown()


async def test_outages_and_a_lost_sign_in_change_nothing(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    case = load_case("issue_138_hybrid_manager_new")
    client = AsyncMock()
    coordinator = _coordinator(hass, case, client=client)
    with (
        clock() as now,
        # A lost sign-in starts the sign-in dialog; not part of this test.
        patch.object(coordinator.config_entry, "async_start_reauth"),
    ):
        tracker = await _start(hass, coordinator)
        client.get_resources_bulk.return_value = _answers(coordinator)
        await _cycle(coordinator, now)
        assert coordinator.last_update_success
        assert current_plant(coordinator).plant_kind == "hybrid"  # type: ignore[union-attr]
        inputs = tracker._inputs
        assert inputs is not None
        assert (inputs.system_type, inputs.heat_source_types) == (
            "boiler",
            (("hs1", "GasBoiler"),),
        )
        writes = tracker.writes
        paths = tuple(coordinator.resources)
        failures: list[dict[str, Any]] = [
            {
                "return_value": tuple(
                    BatchItemResult(GATEWAY, p, 503, server_status=503) for p in paths
                )
            },
            {
                "return_value": tuple(
                    BatchItemResult(GATEWAY, p, 504, server_status=504) for p in paths
                )
            },
            {"side_effect": ServiceUnavailable(503)},
            {"side_effect": RateLimited(60)},
            {"side_effect": TimeoutError()},
            {"side_effect": AuthenticationError("signed out")},
        ]
        for failure in failures:
            client.get_resources_bulk.reset_mock(return_value=True, side_effect=True)
            client.get_resource.side_effect = ServiceUnavailable(503)
            for key, value in failure.items():
                setattr(client.get_resources_bulk, key, value)
            # Long enough for every reading to expire (M41: never the
            # current readings only).
            freezer.tick(timedelta(days=1))
            await _cycle(coordinator, now, seconds=86400)
            assert not coordinator.last_update_success
            snapshot = coordinator.display_snapshot("/system/type")
            assert snapshot is not None and snapshot.freshness is not Freshness.FRESH
            result = current_plant(coordinator)
            assert result is not None and result.plant_kind == "hybrid"
            assert "hybrid_manager_component" in result.evidence
            # The inputs are the readings of this run, not the stored ones.
            assert tracker.inputs_restored == frozenset()
            assert tracker._inputs == inputs
        assert tracker.writes == writes
        assert tracker.changes == 0
        await hass.async_block_till_done()
    await coordinator.async_shutdown()


async def test_a_start_from_the_inventory_keeps_the_stored_result(
    hass: HomeAssistant,
) -> None:
    """M45: the stored row is read before the first result; no flicker."""
    case = load_case("issue_138_xcu_hybrid")
    first = _coordinator(hass, case)
    await _start(hass, first)
    assert current_plant(first).plant_kind == "hybrid"  # type: ignore[union-attr]
    await first.async_shutdown()
    await _restart(hass)

    client = AsyncMock()
    again = _coordinator(hass, case, client=client, info=False)
    seen: list[str | None] = []
    tracker = track_plant(again.config_entry, again)
    assert tracker is not None
    # Before the stored row is read there is no result at all; the row is
    # read at once here.
    seen.append(None if (result := current_plant(again)) is None else result.plant_kind)
    await hass.async_block_till_done()
    result = current_plant(again)
    assert result is not None
    seen.append(result.plant_kind)
    assert result.evidence == ("hybrid_manager_component", "stored")
    assert tracker.restored and tracker.inputs_restored == frozenset({"components"})
    shown = components(again)
    assert shown is not None and shown.restored and shown.source == "cloud"
    assert [row.key for row in shown.rows] == [(8, 195), (83, 16), (72, 17)]
    # The module list is read again: the same result, now from the reading.
    again.resources = _resources(case)
    with clock() as now:
        client.get_resources_bulk.return_value = _answers(again)
        await _cycle(again, now)
    result = current_plant(again)
    assert result is not None
    seen.append(result.plant_kind)
    assert result.evidence == ("hybrid_manager_component",)
    assert not tracker.restored
    assert seen[0] in (None, "hybrid")
    assert seen[1:] == ["hybrid", "hybrid"]
    assert tracker.changes == 0
    await again.async_shutdown()


async def test_there_is_no_result_before_the_row_is_read(hass: HomeAssistant) -> None:
    case = load_case("k40_own")
    coordinator = _coordinator(hass, case)
    tracker = PlantTracker(coordinator, derived_store(hass))
    plant_inputs._TRACKERS[coordinator] = tracker
    assert current_plant(coordinator) is None
    assert components(coordinator) is None
    assert tracker.refresh() is None
    await tracker.async_load()
    result = current_plant(coordinator)
    assert result is not None and result.plant_kind == "heat_pump"
    del plant_inputs._TRACKERS[coordinator]
    await coordinator.async_shutdown()


async def test_a_start_without_a_stored_row_uses_the_inventory_values(
    hass: HomeAssistant,
) -> None:
    """M41: the stored inventory's type values count without a fresh read."""
    case = load_case("k40_own")
    coordinator = _coordinator(hass, case, info=False)
    for path, snapshot in list(coordinator.data.items()):
        coordinator.data[path] = ResourceSnapshot(snapshot.resource, False, None)
    await _start(hass, coordinator)
    result = current_plant(coordinator)
    assert result is not None and result.plant_kind == "heat_pump"
    assert existing_tracker(coordinator).inputs_restored == frozenset()  # type: ignore[union-attr]
    await coordinator.async_shutdown()


async def test_a_fresh_contradicting_reading_changes_the_result(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    case = load_case("k40_own")
    client = AsyncMock()
    coordinator = _coordinator(hass, case, client=client)
    with clock() as now:
        tracker = await _start(hass, coordinator)
        writes = tracker.writes
        hs2 = "/heatSources/hs2/type"
        coordinator.resources[hs2] = Resource(
            hs2, value="GasBoiler", has_value=True, metadata=ResourceMetadata()
        )
        coordinator._paths_by_group = {PollGroup.FAST: tuple(coordinator.resources)}
        client.get_resources_bulk.return_value = _answers(coordinator)
        await _cycle(coordinator, now)
    result = current_plant(coordinator)
    assert result is not None and result.plant_kind == "hybrid"
    assert "heat_source_types_combined" in result.evidence
    assert tracker.changes == 1 and tracker.writes == writes + 1
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == plant_inputs.__name__ and record.levelno == logging.INFO
    ]
    assert messages == ["The detected plant kind changed from heat_pump to hybrid"]
    assert derived_store(hass).get(row_key(GATEWAY))["changes"] == 1  # type: ignore[index]
    await coordinator.async_shutdown()


async def test_a_bus_still_detecting_keeps_the_known_kind(hass: HomeAssistant) -> None:
    case = load_case("k40_own")
    client = AsyncMock()
    coordinator = _coordinator(hass, case, client=client)
    with clock() as now:
        tracker = await _start(hass, coordinator)
        client.get_resources_bulk.return_value = _answers(
            coordinator, {"/system/bus": "Detecting"}
        )
        await _cycle(coordinator, now)
    result = current_plant(coordinator)
    assert result is not None and result.plant_kind == "heat_pump"
    assert "stored" in result.evidence
    assert tracker.changes == 0
    await coordinator.async_shutdown()


async def test_a_new_model_version_works_the_stored_inputs_out_again(
    hass: HomeAssistant,
) -> None:
    case = load_case("issue_138_xcu_hybrid")
    first = _coordinator(hass, case)
    await _start(hass, first)
    await first.async_shutdown()
    row = derived_store(hass).get(row_key(GATEWAY))
    assert row is not None
    row["model_version"] = 0
    row["kind"] = "boiler"
    derived_store(hass).set(row_key(GATEWAY), row)
    await _restart(hass)
    again = _coordinator(hass, case, info=False)
    tracker = await _start(hass, again)
    result = current_plant(again)
    assert result is not None and result.plant_kind == "hybrid"
    stored = derived_store(hass).get(row_key(GATEWAY))
    assert stored is not None
    assert stored["model_version"] == plant_detection.MODEL_VERSION
    assert stored["kind"] == "hybrid"
    assert tracker.changes == 1
    await again.async_shutdown()


async def test_a_local_view_after_30_days_uses_the_local_list(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """P49b: the cloud is gone for a month; the local readings and the
    stored row keep the plant as it was, nothing flickers."""
    freezer.move_to(NOW)
    case = load_case("k40_own")
    first = _coordinator(hass, case)
    await _start(hass, first)
    await first.async_shutdown()
    await _restart(hass)
    freezer.tick(timedelta(days=31))
    later = NOW + timedelta(days=31)
    local = [
        _local(case["local"][:4], observed_at=later),
        _scalar("/system/type", "heatpump_single", observed_at=later),
    ]
    view = _coordinator(hass, case, local=local)
    view.resources = {}
    view.data = {}
    tracker = await _start(hass, view)
    result = current_plant(view)
    assert result is not None and result.plant_kind == "heat_pump"
    shown = components(view)
    assert shown is not None and shown.source == "local" and not shown.restored
    assert len(shown.rows) == 4
    row = derived_store(hass).get(row_key(GATEWAY))
    assert row is not None
    # The cloud's list stays as stored; the outdoor unit is marked removed.
    assert len(row["sources"]["cloud"]["rows"]) == 5
    assert row["components"]["8/65281"]["removed_at"] is not None
    assert tracker.inputs_restored >= {"heat_source_types", "heat_pump_types"}
    freezer.tick(timedelta(days=31))
    tracker.refresh()
    row = derived_store(hass).get(row_key(GATEWAY))
    assert row is not None and "8/65281" not in row["components"]
    await view.async_shutdown()


async def test_a_component_back_again_is_no_longer_removed(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    case = load_case("k40_own")
    coordinator = _coordinator(hass, case)
    tracker = await _start(hass, coordinator)
    first_seen = derived_store(hass).get(row_key(GATEWAY))["components"]["8/8"][
        "first_seen"
    ]  # type: ignore[index]
    info = coordinator.data["/system/info"]
    shorter = replace(info.resource, values=info.resource.values[1:])
    coordinator.data["/system/info"] = ResourceSnapshot(shorter, True, NOW)
    freezer.tick(timedelta(days=1))
    tracker.refresh()
    entry = derived_store(hass).get(row_key(GATEWAY))["components"]["8/8"]  # type: ignore[index]
    assert entry["removed_at"] is not None
    coordinator.data["/system/info"] = info
    freezer.tick(timedelta(days=1))
    tracker.refresh()
    entry = derived_store(hass).get(row_key(GATEWAY))["components"]["8/8"]  # type: ignore[index]
    assert entry == {"first_seen": first_seen, "removed_at": None}
    await coordinator.async_shutdown()


# ------------------------------------------------------------ source choice


def _source(
    name: str, *keys: tuple[int, int], at: int = 0, current: bool = True
) -> SourceRows:
    return SourceRows(
        name,
        tuple(ComponentRow(type_id, tok) for tok, type_id in keys),
        NOW + timedelta(minutes=at),
        current,
    )


def test_the_display_source_follows_the_heat_generator_rule() -> None:
    cloud = _source("cloud", (8, 8), (8, 65281), (72, 17), (75, 17))
    local = _source("local", (8, 8), (8, 65281), (72, 17), at=1)
    # Agreeing lists (the gateway's rows are not compared): never fewer rows.
    assert choose_rows(cloud, local, None) == (cloud, False)
    assert choose_rows(cloud, local, local) == (cloud, False)
    assert choose_rows(cloud, local, cloud) == (cloud, False)
    same = _source("local", (8, 8), (8, 65281), (72, 17), (75, 17), at=1)
    assert choose_rows(cloud, same, cloud) == (cloud, False)
    assert choose_rows(cloud, same, same) == (same, False)
    assert choose_rows(cloud, same, None) == (same, False)
    # Disagreeing lists: the one matching the last shown, else the newer.
    other = _source("local", (8, 8), at=1)
    assert choose_rows(cloud, other, cloud) == (cloud, True)
    assert choose_rows(cloud, other, other) == (other, True)
    assert choose_rows(cloud, other, None) == (other, True)
    # One source alone, or none.
    assert choose_rows(None, other, cloud) == (other, False)
    assert choose_rows(None, None, cloud) == (cloud, False)


async def test_disagreeing_sources_are_a_conflict(hass: HomeAssistant) -> None:
    case = load_case("k40_own")
    local = _local(case["local"][:1], observed_at=NOW + timedelta(minutes=5))
    coordinator = _coordinator(hass, case, local=[local])
    await _start(hass, coordinator)
    result = current_plant(coordinator)
    assert result is not None
    assert "cloud_local_components_differ" in result.conflicts
    report = plant_diagnostics(coordinator)
    assert report is not None
    assert report["components_source"] == "local"
    assert report["sources"]["cloud"]["rows"] == 5
    assert report["sources"]["local"]["rows"] == 1
    await coordinator.async_shutdown()


# ---------------------------------------------------------------- diagnostics


async def test_the_diagnostics_block(hass: HomeAssistant) -> None:
    case = load_case("k40_own")
    coordinator = _coordinator(hass, case, local=[_local(case["local"])])
    await _start(hass, coordinator)
    gateway = _gateway_diagnostics(1, coordinator)
    report = gateway["plant"]
    _check(report, "diagnostics-plant.v1.json")
    assert report["plant_kind"] == "heat_pump"
    assert report["components_source"] == "cloud"
    assert report["stored"] is True and report["restored"] is False
    assert report["inputs"]["system_type"] == "heatpump_single"
    assert report["inputs"]["heat_source_types"] == {"hs1": "Heatpump"}
    assert report["gates"]["hybrid_manager_place"]["result"] == "not_applicable"
    indoor = report["components"][0]
    assert indoor == {
        "id": 8,
        "token": 8,
        "place": "heat_generator_control",
        "place_name": indoor["place_name"],
        "name": indoor["name"],
        "role": "heat_source",
        "basis": "observed",
        "gateway": False,
        "version": "1.2.3-abc",
        "hardware_type": "XCU_THH",
        "product_name": "Example indoor unit 12",
        "has_product_name": True,
        "sw_update_type": "can_uds_example",
        "sw_ident": "example.rhp:hyd.5.aw",
        "article_number": "1000000001",
    }
    assert [item["gateway"] for item in report["components"]] == [
        False,
        False,
        True,
        True,
        False,
    ]
    assert report["unmapped_components"] == []
    _no_markers(json.dumps(gateway, default=str))
    assert _contains_technical_values([{**gateway, "technical_metadata": []}])
    assert "icom" not in gateway
    await coordinator.async_shutdown()


async def test_unmapped_components_are_bounded(hass: HomeAssistant) -> None:
    case = copy.deepcopy(load_case("k40_own"))
    case["cloud"] = [{"Id": str(1000 + number), "Tok": "8"} for number in range(64)]
    coordinator = _coordinator(hass, case)
    report = plant_diagnostics(coordinator)
    assert report is not None
    assert len(report["components"]) == 64
    assert len(report["unmapped_components"]) == plant_inputs.MAX_UNMAPPED
    assert report["stored"] is False
    await coordinator.async_shutdown()


async def test_an_icom_gateway_keeps_its_own_block(hass: HomeAssistant) -> None:
    case = load_case("issue_467_icom_heat_pump")
    coordinator = _coordinator(hass, case)
    await _start(hass, coordinator)
    gateway = _gateway_diagnostics(1, coordinator)
    assert gateway["icom"]["system_components"] == [
        {"id": "172", "token": "8"},
        {"id": "158", "token": "16"},
        {"id": "165", "token": "56"},
    ]
    plant = gateway["plant"]
    assert plant["plant_kind"] == "heat_pump"
    assert plant["inputs"]["platform"] == "icom"
    assert [item["role"] for item in plant["components"]] == [None, None, None]
    assert plant["components"][0]["name"] == describe(ComponentRow(172, 8)).type_name
    await coordinator.async_shutdown()


async def test_a_room_controller_reports_its_full_rows(hass: HomeAssistant) -> None:
    case = copy.deepcopy(load_case("issue_122_rrc_hybrid"))
    case["cloud"] = [
        {
            "Id": "1",
            "Tok": "1",
            "Ver": "2.0",
            "ModuleHwIdentStr": "RRC",
            "No": "SERIAL-MARKER-R",
        }
    ]
    coordinator = _coordinator(hass, case)
    with patch(
        "custom_components.bosch_buderus_heating.device.is_rrc_plant", return_value=True
    ):
        report = plant_diagnostics(coordinator)
    assert report is not None
    assert report["inputs"]["platform"] == "rrc"
    assert report["components"][0]["version"] == "2.0"
    assert report["components"][0]["hardware_type"] == "RRC"
    _no_markers(json.dumps(report))
    await coordinator.async_shutdown()


async def test_water_softeners_and_other_devices_get_no_block(
    hass: HomeAssistant,
) -> None:
    case = copy.deepcopy(load_case("water_softener"))
    case["gateway"] = {"device_type": "WaterSoftener"}
    coordinator = _coordinator(hass, case)
    assert plant_diagnostics(coordinator) is None
    assert track_plant(coordinator.config_entry, coordinator) is None
    assert "plant" not in _gateway_diagnostics(1, coordinator)
    assert plant_diagnostics(SimpleNamespace()) is None
    assert current_plant(SimpleNamespace()) is None
    assert components(SimpleNamespace()) is None
    await coordinator.async_shutdown()


def test_a_failing_block_gives_a_fixed_note() -> None:
    broken = SimpleNamespace(gateway=SimpleNamespace(device_type="k40", model=None))
    with patch.object(plant_inputs, "_gather", side_effect=RuntimeError("x")):
        assert plant_diagnostics(broken) == {"error": "unavailable"}


async def test_markers_never_reach_any_channel(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    case = copy.deepcopy(load_case("issue_138_hybrid_manager_new"))
    for row in case["cloud"]:
        row.update(
            {
                "Ver": row.get("Ver", "1.0"),
                "No": "SERIAL-MARKER-N",
                "ModuleSerialNumber": "SERIAL-MARKER-M",
                "ProductSerialNumber": "SERIAL-MARKER-P",
                "ProductUuid": "UUID-MARKER-U",
            }
        )
    case["cloud"].append(
        {
            "Id": "8",
            "Tok": "8",
            "Ver": "SERIAL-MARKER-V",
            "ProductName": "SERIAL-MARKER-V",
            "ModuleHwIdentStr": "SERIAL-MARKER-V",
            "SwIdenStr": "SERIAL-MARKER-V",
            "ProductTtn": "SERIAL-MARKER-V",
            "ModuleSerialNumber": "SERIAL-MARKER-V",
        }
    )
    coordinator = _coordinator(hass, case)
    await _start(hass, coordinator)
    gateway = _gateway_diagnostics(1, coordinator)
    row = derived_store(hass).get(row_key(GATEWAY))
    diagnostics = inventory_diagnostics()
    diagnostics["diagnostics_schema"] = 49
    diagnostics["privacy"].update(
        contains_raw_resource_values=True,
        raw_resource_values_scope="allowlisted_technical_metadata",
    )
    diagnostics["gateways"][0]["plant"] = gateway["plant"]
    inventory = build_anonymized_inventory(diagnostics)
    for text in (
        json.dumps(gateway, default=str),
        json.dumps(row),
        json.dumps(inventory),
        caplog.text,
    ):
        _no_markers(text)
    assert inventory["gateways"][0]["plant_kind"] == "hybrid"  # type: ignore[index]
    rendered = json.dumps(inventory)
    for text in ("HM200", "06.02.00-x", "16.00.09", "GasBoiler", "hybrid_manager"):
        assert text not in rendered
    await coordinator.async_shutdown()


def test_the_anonymized_inventory_keeps_only_the_plant_kind() -> None:
    diagnostics = inventory_diagnostics()
    diagnostics["diagnostics_schema"] = 49
    diagnostics["gateways"][0]["plant"] = {
        "plant_kind": "unexpected",
        "components": [1],
    }
    assert build_anonymized_inventory(diagnostics)["gateways"][0]["plant_kind"] is None  # type: ignore[index]
    diagnostics["gateways"][0]["plant"] = {"plant_kind": "boiler"}
    gateway = build_anonymized_inventory(diagnostics)["gateways"][0]  # type: ignore[index]
    assert gateway["plant_kind"] == "boiler"
    del diagnostics["gateways"][0]["plant"]
    assert "plant_kind" not in build_anonymized_inventory(diagnostics)["gateways"][0]  # type: ignore[index]


# --------------------------------------------------------- shared interface


async def test_probe_results_are_kept_in_the_row(hass: HomeAssistant) -> None:
    case = load_case("k40_own")
    coordinator = _coordinator(hass, case)
    tracker = await _start(hass, coordinator)
    writes = tracker.writes
    results = [{"rule": "r2", "path": "/pool/enabled", "result": "404", "at": 3}]
    assert set_probe_results(coordinator, results) is True
    assert tracker.writes == writes + 1
    assert set_probe_results(coordinator, results) is True
    assert tracker.writes == writes + 1
    assert derived_store(hass).get(row_key(GATEWAY))["probes"] == results  # type: ignore[index]
    assert plant_diagnostics(coordinator)["probes"] == results  # type: ignore[index]
    assert set_probe_results(coordinator, [{"path": "with space"}]) is False
    assert set_probe_results(coordinator, [{"rule": "r"}] * 49) is False
    assert set_probe_results(coordinator, "x") is False  # type: ignore[arg-type]
    assert set_probe_results(SimpleNamespace(), results) is False
    await coordinator.async_shutdown()


async def test_the_row_goes_with_its_gateway(hass: HomeAssistant) -> None:
    case = load_case("k40_own")
    first = _coordinator(hass, case)
    await _start(hass, first)
    # A gateway exchange gives the new gateway a row of its own.
    second = _coordinator(hass, case, gateway_id="gateway-two")
    await _start(hass, second)
    store = derived_store(hass)
    assert store.get(row_key(GATEWAY)) is not None
    assert store.get(row_key("gateway-two")) is not None
    await store.async_forget_gateways([GATEWAY])
    assert store.get(row_key(GATEWAY)) is None
    assert store.get(row_key("gateway-two")) is not None
    await first.async_shutdown()
    await second.async_shutdown()


async def test_a_file_with_the_row_loads_as_before(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Rollback: the loader of the saved values reads a file with the plant
    row like any other row; the other rows stay usable."""
    case = load_case("k40_own")
    coordinator = _coordinator(hass, case)
    await _start(hass, coordinator)
    await flush_store(derived_store(hass)._store)
    saved = hass_storage[STORAGE_KEY]
    assert row_key(GATEWAY) in saved["data"]["rows"]
    saved["data"]["rows"][f"{GATEWAY}:defrost"] = {"defrosts": 3.0}
    store = DerivedStore(hass)
    await store.async_load()
    assert store.get(f"{GATEWAY}:defrost") == {"defrosts": 3.0}
    assert parse_row(store.get(row_key(GATEWAY))) is not None
    assert (
        derived_store_module.stored_number(store.get(row_key(GATEWAY)), "kind") is None
    )
    await coordinator.async_shutdown()


def test_invalid_stored_rows_are_ignored() -> None:
    assert parse_row(None) is None
    assert parse_row({"v": 2}) is None
    stored = parse_row(
        {
            "v": 1,
            "sources": {"cloud": {"rows": [["x"]]}, "local": "x"},
            "kind": "dragon",
            "evidence": ["stored", "made_up", 3],
            "changes": -1,
            "probes": [{"a b": 1}],
            "components": {"8/8": {"first_seen": "never"}, "x": 3},
            "inputs": {
                "system_type": "with space",
                "bus": "EMS2_0",
                "gateway_firmware": "SERIAL MARKER",
                "heat_source_types": {"hs1": "Heatpump", "bad key": "x", "hs2": 3},
                "allowed_values": {"/system/bus": ["EMS2_0", "bad value", 3]},
                "hybrid_evidence": ["hybrid_resources", "bad code"],
            },
        }
    )
    assert stored is not None
    assert stored.sources == {}
    assert stored.kind is None and stored.evidence == ("stored",)
    assert stored.changes == 0 and stored.probes == () and stored.components == {}
    assert stored.inputs["bus"] == "EMS2_0"
    assert "system_type" not in stored.inputs
    assert "gateway_firmware" not in stored.inputs
    assert stored.inputs["heat_source_types"] == {"hs1": "Heatpump"}
    assert stored.inputs["allowed_values"] == {"/system/bus": ("EMS2_0",)}
    assert stored.inputs["hybrid_evidence"] == ("hybrid_resources",)


def test_the_gateway_class_matches_the_diagnostics() -> None:
    from custom_components.bosch_buderus_heating.diagnostics import _gateway_class

    for device_type, model in (
        ("k40", None),
        ("K40RF", None),
        ("MX300", None),
        ("mx400", "Example"),
        ("K30RF", None),
        ("icom", "KM200"),
        ("rrc2", None),
        (None, "Heat pump gateway"),
        ("WaterSoftener", None),
        (None, None),
    ):
        gateway = Gateway("x", device_type=device_type, model=model)
        assert gateway_class(gateway) == _gateway_class(gateway)


def test_a_coordinator_without_a_store_is_worked_out_on_request() -> None:
    coordinator = Mock()
    coordinator.gateway = Gateway(GATEWAY, device_type="k40")
    coordinator.data = {}
    coordinator.resources = {}
    coordinator.local_selection = None
    tracker = PlantTracker(coordinator, None)
    assert tracker.loaded
    result = tracker.refresh()
    assert result is not None and result.plant_kind is None
    assert tracker.writes == 0
