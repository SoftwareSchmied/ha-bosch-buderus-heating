"""W6: settings and valve details of the radio devices of an RRC controller.

Pure parts first (type to leaves, the valve status in both forms, the
sensitivity levels, the budget), then real setups and coordinator cycles
against the fake cloud with ``device_settings.json``: eight radiator
thermostats, a room thermostat, the controller and a repeater. Everything is
synthetic; nothing here is confirmed on hardware.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from types import MappingProxyType

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import (
    rrc,
    rrc_contract_devices,
    rrc_contracts,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import Freshness
from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.discovery import (
    MAX_RRC_DEVICE_SETTING_PROBES,
    DiscoveryDiagnostics,
    async_discover_resources,
)
from custom_components.bosch_buderus_heating.ha_compat import device_by_identifier
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    Resource,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.rrc_device_settings import (
    DEVICE_LEAVES,
    offered,
    sensitivity_check,
    sensitivity_level,
    split,
    valve_status,
)
from custom_components.bosch_buderus_heating.writes import (
    GENERIC_CONTEXT,
    RRC_CONTEXT,
    ContractRejected,
    assess_control,
)
from tests.helpers.rrc_cloud import (
    Clock,
    RrcCloud,
    cloud_patches,
    load_tree,
    with_containers,
)
from tests.test_entry import _entry

PACKAGE = "custom_components.bosch_buderus_heating"
GATEWAY = Gateway("gateway-one", device_type="rrc2")
FIXTURE = "rrc/device_settings.json"
ETRV = "/devices/device{}/etrv"
LOCK = "/devices/device2/etrv/childLock/enabled"
OFFSET = "/devices/device2/etrv/offset"
SENSITIVITY = "/devices/device2/etrv/openWindowDetection/sensitivity"
# Never read: radio keys, the repeater switch, the calibration trigger and
# leaves no entity shows.
NEVER = re.compile(
    r"^/devices/dev[0-9]+(?:/|$)|/homematicip(?:/|$)|/repeater/toggle$"
    r"|/valveCalibration$|/temperatureGradient$"
)
MARKERS = ("MARKER",)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture(autouse=True)
def legacy_setup_path():
    """Reach the format 3 setup without migrating, as test_startup_inventory."""
    from unittest.mock import AsyncMock, patch

    with patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)):
        yield


def wire(path: str, **fields: object) -> Resource:
    return parse_resource({"id": path, "writeable": 1, "recordable": 0, **fields})


def tree() -> dict[str, Resource]:
    return with_containers(load_tree(FIXTURE))


def listed(*types: str) -> Resource:
    return wire(
        rrc.DEVICE_LIST,
        type="deviceArray",
        writeable=0,
        value=[
            {"id": number, "type": kind, "zone": 1}
            for number, kind in enumerate(types, start=1)
        ],
    )


# ------------------------------------------------------------ pure parts


def test_each_type_reads_its_own_subfolder() -> None:
    assert rrc_contracts.device_leaves("thermostat_valve") == (
        "etrv/status",
        "etrv/childLock/enabled",
        "etrv/offset",
        "etrv/openWindowDetection/sensitivity",
        "etrv/valvePosition",
        "etrv/temperatureActual",
        "etrv/valveState",
        "etrv/maxValvePosition",
        "etrv/whisperMode/enabled",
    )
    for kind, subfolder in (
        ("room_thermostat", "wth"),
        ("device_underfloor_heating", "bwth"),
        ("thermostat", "thermostat"),
    ):
        assert rrc_contracts.device_leaves(kind) == (
            f"{subfolder}/childLock/enabled",
            f"{subfolder}/offset",
        )
    for kind in ("repeater", "appliance_link", "easy_receiver", "bwth", "unknown"):
        assert rrc_contracts.device_leaves(kind) == ()
    assert set(DEVICE_LEAVES) == {
        "thermostat_valve",
        "room_thermostat",
        "device_underfloor_heating",
        "thermostat",
    }
    paths = rrc.device_setting_paths(
        listed("thermostat", "repeater", "room_thermostat")
    )
    # Leaf by leaf over the devices, each round in list order.
    assert paths == (
        "/devices/device1/thermostat/childLock/enabled",
        "/devices/device3/wth/childLock/enabled",
        "/devices/device1/thermostat/offset",
        "/devices/device3/wth/offset",
    )
    # No two contract entries for one fixture path.
    rrc_contracts.registry().check_paths(load_tree(FIXTURE))


def test_only_the_package_leaves_are_split() -> None:
    assert split("/devices/device2/etrv/offset") is not None
    assert split("/devices/device2/wth/offset") is not None
    for path in (
        "/devices/device2/wth/valvePosition",  # only below etrv
        "/devices/device2/etrv/valveCalibration",
        "/devices/device2/etrv/temperatureGradient",
        "/devices/device2/homematicip/dlk",
        "/devices/device02/etrv/offset",
        "/devices/dev2/etrv/offset",
        "/devices/device2/repeater/toggle",
    ):
        assert split(path) is None, path


@pytest.mark.parametrize(
    ("value", "shape", "problems", "unknown"),
    [
        (["VALVE_ADAPTION_SUCESSFUL"], "strings", (), 0),
        (["valve_too_tight "], "strings", ("VALVE_TOO_TIGHT",), 0),
        (
            [{"id": 3, "valve": "VALVE_ADJUSTMENT_RANGE_TOO_BIG", "battery": "ok"}],
            "objects",
            ("VALVE_ADJUSTMENT_RANGE_TOO_BIG",),
            0,
        ),
        (
            [
                {"id": 3, "valve": "VALVE_ADAPTION_IN_PROGRESS"},
                "VALVE_ADJUSTMENT_RANGE_TOO_SMALL",
            ],
            "mixed",
            ("VALVE_ADJUSTMENT_RANGE_TOO_SMALL",),
            0,
        ),
        (["VALVE_ERROR_POSITION", "VALVE_ERROR_POSITION"], "strings", None, 0),
        (["SOMETHING_NEW"], "strings", (), 1),
        ([], "empty", (), 0),
    ],
)
def test_valve_status_reads_both_forms(
    value: list[object], shape: str, problems: tuple[str, ...] | None, unknown: int
) -> None:
    status = valve_status(wire("/x", type="stringArray", writeable=0, value=value))
    assert status is not None
    assert status.shape == shape
    if problems is None:
        problems = ("VALVE_ERROR_POSITION",)
    assert status.problems == problems
    assert status.problem is bool(problems)
    assert status.unknown == unknown


@pytest.mark.parametrize(
    "value",
    [
        "VALVE_TOO_TIGHT",
        [1],
        [{"id": 1}],
        [{"valve": 4}],
        [None],
        ["VALVE_TOO_TIGHT"] * 17,
    ],
)
def test_malformed_valve_status_is_unknown(value: object) -> None:
    assert valve_status(wire("/x", type="stringArray", value=value)) is None
    assert valve_status(None) is None


@pytest.mark.parametrize(
    ("value", "level"),
    [
        (0.5, "high"),
        (1.0, "high"),
        (1.5, "medium"),
        (2.0, "medium"),
        (2.2, "low"),
        (2.5, "low"),
        (-1.0, "high"),
    ],
)
def test_sensitivity_levels_follow_the_thresholds(value: float, level: str) -> None:
    reading = wire("/x", type="floatValue", unitOfMeasure="C", value=value)
    assert sensitivity_level(reading) == level


def test_unreadable_sensitivity_has_no_level() -> None:
    for fields in (
        {"type": "floatValue", "value": "1.5"},
        {"type": "stringValue", "value": 1.5},
        {"type": "floatValue", "value": True},
        {"type": "floatValue"},
    ):
        assert sensitivity_level(wire("/x", **fields)) is None
    assert sensitivity_level(None) is None
    assert sensitivity_check(wire("/x"), 2.5, {}) is None
    for value in (2.4, 1, True, "low"):
        assert sensitivity_check(wire("/x"), value, {}) == (
            "rrc_window_sensitivity_level"
        )


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ({}, True),
        ({"used": "true"}, True),
        ({"used": "false"}, False),
        ({"available": "false"}, False),
        ({"used": "true", "available": "false"}, True),  # shown and locked (S8)
        ({"used": "maybe"}, False),
    ],
)
def test_a_setting_is_offered_only_while_used(
    flags: dict[str, str], expected: bool
) -> None:
    assert offered(wire(OFFSET, type="floatValue", value=0.0, **flags)) is expected


def _offset(**fields: object) -> Resource:
    data: dict[str, object] = {
        "type": "floatValue",
        "unitOfMeasure": "C",
        "minValue": -2.0,
        "maxValue": 2.0,
        "stepSize": 0.5,
        "value": 0.0,
    }
    data.update(fields)
    return wire(OFFSET, **{key: value for key, value in data.items() if value})


def test_contract_policies_apply_only_on_a_controller() -> None:
    """S11: the same paths at another gateway get no control."""
    lock = wire(LOCK, type="stringValue", value="false")
    offset = _offset()
    sensitivity = wire(
        SENSITIVITY,
        type="floatValue",
        unitOfMeasure="C",
        minValue=0.0,
        maxValue=3.0,
        value=1.0,
    )
    assert assess_control(lock, RRC_CONTEXT).policy is (
        rrc_contract_devices.CHILD_LOCK_POLICY
    )
    assert assess_control(lock, RRC_CONTEXT).supports_switch
    assert assess_control(offset, RRC_CONTEXT).eligible
    assert assess_control(sensitivity, RRC_CONTEXT).eligible
    for resource in (lock, offset, sensitivity):
        assert assess_control(resource, GENERIC_CONTEXT).policy is None
        assert assess_control(resource, None).policy is None


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"writeable": 0}, "not_writable"),
        ({"minValue": None}, "missing_bounds"),
        ({"maxValue": None}, "missing_bounds"),
        ({"stepSize": -1}, "invalid_step"),
        ({"stepSize": 10}, "invalid_step"),
        ({"unitOfMeasure": "%"}, "unsupported_unit"),
        ({"type": "stringValue"}, "unsupported_resource_type"),
    ],
)
def test_offset_needs_complete_metadata(fields: dict[str, object], reason: str) -> None:
    """S1, S3, S4: missing or malformed metadata leaves no control."""
    resource = _offset(**fields)
    if "writeable" in fields:
        resource = parse_resource(
            {
                "id": OFFSET,
                "writeable": 0,
                "type": "floatValue",
                "unitOfMeasure": "C",
                "minValue": -2.0,
                "maxValue": 2.0,
                "value": 0.0,
            }
        )
    assert assess_control(resource, RRC_CONTEXT).rejection_reason == reason


@pytest.mark.parametrize(
    "fields",
    [
        {"value": "on"},
        {"value": "true", "allowedValues": ["on", "off"]},
        {"value": "true", "allowedValues": ["true", "false", "auto"]},
        {"value": "true", "type": "booleanValue"},
        {"value": True},
    ],
)
def test_child_lock_needs_exactly_the_two_strings(fields: dict[str, object]) -> None:
    """S2, P38: only "true"/"false"; anything else gets no switch."""
    resource = wire(LOCK, **{"type": "stringValue", **fields})
    assessment = assess_control(resource, RRC_CONTEXT)
    assert not (assessment.eligible and assessment.supports_switch)


def test_valve_calibration_and_whisper_mode_are_never_written() -> None:
    """W6-E1, W6-E2: refused on every gateway, whatever the metadata."""
    for path, reason in (
        ("/devices/device2/etrv/valveCalibration", "rrc_valve_calibration_not_offered"),
        ("/devices/device2/etrv/whisperMode/enabled", "rrc_whisper_mode_read_only"),
    ):
        resource = wire(path, type="stringValue", value="false")
        for context in (RRC_CONTEXT, GENERIC_CONTEXT):
            assert assess_control(resource, context).rejection_reason == reason


# ----------------------------------------------------------------- budget


def _budget_tree(devices: int) -> dict[str, Resource]:
    resources: dict[str, Resource] = {
        "/gateway/productType": wire(
            "/gateway/productType", type="stringValue", writeable=0, value="RRC 2.0"
        ),
        "/zones/list": wire("/zones/list", type="zoneArray", value=[{"id": 1}]),
        "/zones/zn1/temperatureActual": wire(
            "/zones/zn1/temperatureActual",
            type="floatValue",
            unitOfMeasure="C",
            value=20.0,
        ),
        "/devices/list": listed(*["thermostat_valve"] * devices),
    }
    for number in range(1, devices + 1):
        resources[f"{ETRV.format(number)}/status"] = wire(
            f"{ETRV.format(number)}/status", type="stringArray", value=[]
        )
    return with_containers(resources)


async def _discover(cloud: RrcCloud, **kwargs: object):
    report = DiscoveryDiagnostics()
    found = await async_discover_resources(
        cloud,  # type: ignore[arg-type]
        "gw",
        diagnostics=report,
        gateway=Gateway("gw", device_type="rrc2"),
        **kwargs,  # type: ignore[arg-type]
    )
    return found, report


async def test_24_valves_stay_within_the_device_cap_and_come_last() -> None:
    cloud = RrcCloud(_budget_tree(24))
    found, report = await _discover(cloud)
    assert report.stop_reason == "complete"
    order = [path for paths in cloud.requests for path in paths] + cloud.single
    settings = [path for path in dict.fromkeys(order) if split(path) is not None]
    assert len(settings) == MAX_RRC_DEVICE_SETTING_PROBES == 192
    # Nine leaves per valve, leaf by leaf: the status of every valve first,
    # then the next leaf of every valve; the cap drops the last leaf of all
    # 24 valves instead of every leaf of the last ones.
    leaves = DEVICE_LEAVES["thermostat_valve"]
    assert settings[:24] == [f"/devices/device{n}/{leaves[0]}" for n in range(1, 25)]
    assert settings[-1] == f"/devices/device24/{leaves[7]}"
    limited = [
        path
        for path, item in report.paths.items()
        if item.read_policy == "rrc_device_setting_limit"
    ]
    assert len(limited) == 24 * 9 - 192
    assert sorted(limited) == sorted(
        f"/devices/device{n}/{leaves[8]}" for n in range(1, 25)
    )
    assert report.snapshot()["paths_not_read"]["rrc_device_setting_limit"] == 24
    # Missing leaves answer 500: probably missing, the discovery goes on.
    assert report.paths["/devices/device1/etrv/offset"].read_policy == (
        "rrc_5xx_assumed_missing"
    )
    assert "/devices/device1/etrv/status" in found
    # The lists come first; no other path shares a request with a leaf.
    first = next(index for index, path in enumerate(order) if split(path))
    assert {"/zones/list", "/devices/list"} <= set(order[:first])
    for paths in cloud.requests:
        if any(split(path) for path in paths):
            assert all(split(path) for path in paths)


async def test_the_device_leaves_have_their_own_cap_beyond_the_budget() -> None:
    """Where the overall budget is used up by the other paths, the device
    leaves are still read: they count only against their own cap, so the
    leaves of the last devices of a large controller are not lost."""
    cloud = RrcCloud(_budget_tree(8))
    _found, unbounded = await _discover(cloud)
    assert unbounded.stop_reason == "complete"
    leaves = [path for path in cloud.all_requested() if split(path) is not None]
    other = len(cloud.all_requested()) - len(leaves)
    cloud = RrcCloud(_budget_tree(8))
    found, report = await _discover(cloud, maximum_resources=other)
    assert report.stop_reason == "complete"
    assert report.completed
    assert not [
        path
        for path, item in report.paths.items()
        if item.read_policy == "rrc_device_setting_limit"
    ]
    assert set(leaves) <= cloud.all_requested()
    assert "/devices/device8/etrv/status" in found
    assert "/zones/zn1/temperatureActual" in found
    # One path fewer than the others need still stops the discovery.
    cloud = RrcCloud(_budget_tree(8))
    _found, short = await _discover(cloud, maximum_resources=other - 1)
    assert short.stop_reason == "resource_limit"
    assert not [path for path in cloud.all_requested() if split(path) is not None]


async def test_no_devices_no_leaves() -> None:
    cloud = RrcCloud(_budget_tree(0))
    _found, report = await _discover(cloud)
    assert report.stop_reason == "complete"
    assert not [path for path in cloud.all_requested() if split(path)]


# ------------------------------------------------------------ real setups


async def _setup(hass: HomeAssistant, cloud: RrcCloud, clock: Clock, gateway=GATEWAY):
    entry = _entry(hass)
    stack = cloud_patches(cloud, clock, gateway)
    stack.__enter__()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    _no_wait(entry)
    return entry, stack


def _no_wait(entry) -> list[float]:
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    entry.runtime_data.coordinators[0]._write_service._sleep = sleep
    return sleeps


async def _teardown(hass: HomeAssistant, entry, stack) -> None:
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stack.close()


def _entries(hass: HomeAssistant, entry) -> dict[tuple[str, str], er.RegistryEntry]:
    return {
        (item.domain, item.unique_id): item
        for item in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }


async def _enable(hass: HomeAssistant, entry, *keys: tuple[str, str]) -> list[float]:
    registry = er.async_get(hass)
    entries = _entries(hass, entry)
    for key in keys:
        registry.async_update_entity(entries[key].entity_id, disabled_by=None)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    return _no_wait(entry)


def _state(hass: HomeAssistant, entry, domain: str, unique_id: str):
    item = _entries(hass, entry)[(domain, unique_id)]
    return hass.states.get(item.entity_id)


LOCK_ID = ("switch", "gateway-one:devices:device2:etrv:childLock:enabled:control")
OFFSET_ID = ("number", "gateway-one:devices:device2:etrv:offset:control")
SENSITIVITY_ID = (
    "select",
    "gateway-one:devices:device2:etrv:openWindowDetection:sensitivity:control",
)


async def test_settings_and_valve_details_reach_their_devices(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=PACKAGE)
    cloud, clock = RrcCloud(tree()), Clock()
    entry, stack = await _setup(hass, cloud, clock)
    try:
        coordinator = entry.runtime_data.coordinators[0]
        assert coordinator.discovery_diagnostics.stop_reason == "complete"
        requested = cloud.all_requested()
        assert not [path for path in requested if NEVER.search(path)]
        assert set(rrc.device_setting_paths(cloud.tree[rrc.DEVICE_LIST])) <= requested
        entries = _entries(hass, entry)

        # Child lock: a switch per device with a subfolder, disabled, config.
        locks = {
            key: item
            for key, item in entries.items()
            if key[0] == "switch" and ":childLock:enabled" in key[1]
        }
        assert len(locks) == 10  # controller, eight valves, room thermostat
        assert all(
            item.disabled_by is er.RegistryEntryDisabler.INTEGRATION
            and item.entity_category is EntityCategory.CONFIG
            and item.translation_key == "rrc_child_lock"
            for item in locks.values()
        )
        # Offset: not for the valve that does not use it (device 3).
        offsets = {
            key[1]
            for key in entries
            if key[0] == "number" and key[1].endswith(":offset:control")
        }
        assert "gateway-one:devices:device3:etrv:offset:control" not in offsets
        assert len(offsets) == 9
        sensitivities = {
            key[1]
            for key in entries
            if key[0] == "select" and ":sensitivity:" in key[1]
        }
        assert len(sensitivities) == 8
        # Valve problem: enabled, on its device; not without a status leaf
        # (device 8, older firmware).
        problems = {
            key[1]: item
            for key, item in entries.items()
            if key[0] == "binary_sensor" and key[1].endswith(":valve_problem")
        }
        assert sorted(problems) == sorted(
            f"gateway-one:devices:device{n}:valve_problem"
            for n in (2, 3, 4, 5, 6, 7, 9)
        )
        assert all(item.disabled_by is None for item in problems.values())

        def problem(n: int) -> str:
            item = problems[f"gateway-one:devices:device{n}:valve_problem"]
            return hass.states.get(item.entity_id).state

        assert [problem(n) for n in (2, 3, 4, 5, 6, 7, 9)] == [
            "off",  # adaption successful (string array)
            "on",  # valve too tight
            "off",  # list of objects, successful
            "on",  # list of objects, error position
            "off",  # empty list
            "off",  # an unknown state is no problem
            "unknown",  # malformed
        ]
        tight = hass.states.get(
            problems["gateway-one:devices:device3:valve_problem"].entity_id
        )
        assert tight.attributes["valve_problems"] == ["valve_too_tight"]
        assert tight.attributes["device_class"] == "problem"
        assert (
            tight.attributes["friendly_name"] == "Radiator thermostat 3 Valve problem"
        )

        # The readings: generic sensors, disabled, named by the contract.
        sensors = {key[1]: item for key, item in entries.items() if key[0] == "sensor"}
        for leaf, key, category in (
            ("valvePosition", "rrc_valve_position", None),
            ("maxValvePosition", "rrc_max_valve_position", EntityCategory.DIAGNOSTIC),
            ("temperatureActual", "rrc_valve_temperature", None),
            ("valveState", "rrc_valve_state", EntityCategory.DIAGNOSTIC),
            ("whisperMode:enabled", "rrc_whisper_mode", EntityCategory.DIAGNOSTIC),
        ):
            item = sensors[f"gateway-one:devices:device2:etrv:{leaf}"]
            assert item.disabled_by is er.RegistryEntryDisabler.INTEGRATION, leaf
            assert item.translation_key == key, leaf
            assert item.entity_category is category, leaf
        assert (
            sensors[
                "gateway-one:devices:device2:etrv:valvePosition"
            ].unit_of_measurement
            == "%"
        )
        assert (
            sensors[
                "gateway-one:devices:device2:etrv:temperatureActual"
            ].original_device_class
            == "temperature"
        )
        # The generic sensors of the settings are disabled diagnostics.
        for leaf in ("offset", "openWindowDetection:sensitivity", "childLock:enabled"):
            mirror = sensors[f"gateway-one:devices:device2:etrv:{leaf}"]
            assert mirror.entity_category is EntityCategory.DIAGNOSTIC
            assert mirror.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        # No entity for the repeater switch or the status list itself.
        assert not [key for key in entries if "repeater" in key[1]]
        assert ("sensor", "gateway-one:devices:device2:etrv:status") not in entries

        # E07: on the radio device; the controller's own on the controller.
        devices = dr.async_get(hass)
        gateway = device_by_identifier(devices, (DOMAIN, "gateway-one"), entry.entry_id)
        valve = device_by_identifier(
            devices, (DOMAIN, "gateway-one:devices:device2"), entry.entry_id
        )
        assert entries[LOCK_ID].device_id == valve.id
        assert entries[OFFSET_ID].device_id == valve.id
        assert (
            problems["gateway-one:devices:device2:valve_problem"].device_id == valve.id
        )
        controller_lock = entries[
            (
                "switch",
                "gateway-one:devices:device1:thermostat:childLock:enabled:control",
            )
        ]
        assert controller_lock.device_id == gateway.id
        assert controller_lock.original_name == "Room controller \N{EN DASH} Child lock"

        # Diagnostics: counted, without values, never confirmed.
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        features = diagnostics["gateways"][0]["rrc_features"]["contracts"]["devices"]
        assert features["confirmation"] == "unconfirmed"
        assert features["device_settings_read"] == len(
            [path for path in coordinator.resources if split(path)]
        )
        assert features["leaves"]["etrv/status"] == 7
        assert features["valve_status_shapes"] == {
            "empty": 1,
            "invalid": 1,
            "objects": 2,
            "strings": 3,
        }
        assert features["valves_with_problem"] == 2
        assert features["unknown_valve_states"] == 1
        assert features["window_sensitivity_levels"] == {
            "high": 4,
            "low": 2,
            "medium": 2,
        }
        assert features["settings_not_offered"] == 1

        # P14: nothing private in diagnostics, states, the store or logs.
        dumped = json.dumps(diagnostics)
        stored = json.dumps(coordinator.inventory._data())
        shown = json.dumps(
            [(state.state, dict(state.attributes)) for state in hass.states.async_all()]
        )
        for text in (dumped, stored, shown, caplog.text):
            for marker in MARKERS:
                assert marker not in text
    finally:
        await _teardown(hass, entry, stack)


async def test_device_leaves_poll_slowly(hass: HomeAssistant) -> None:
    """P20, CT-H01: the device leaves at most every 15 minutes."""
    cloud, clock = RrcCloud(tree()), Clock()
    entry, stack = await _setup(hass, cloud, clock)
    try:
        coordinator = entry.runtime_data.coordinators[0]
        cloud.requests.clear()
        cloud.single.clear()
        clock.now += 120
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert not [path for path in cloud.all_requested() if split(path)]
        clock.now += 15 * 60
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        polled = {path for path in cloud.all_requested() if split(path)}
        assert "/devices/device2/etrv/status" in polled
        assert LOCK in polled
    finally:
        await _teardown(hass, entry, stack)


async def test_settings_write_exact_wire_values_once(hass: HomeAssistant) -> None:
    """S2, S6, S7: strings for the lock, floats on the grid, exact levels;
    one PUT each, confirmed by reading back."""
    cloud, clock = RrcCloud(tree()), Clock()
    entry, stack = await _setup(hass, cloud, clock)
    try:
        sleeps = await _enable(hass, entry, LOCK_ID, OFFSET_ID, SENSITIVITY_ID)
        lock = _state(hass, entry, *LOCK_ID).entity_id
        assert hass.states.get(lock).state == "off"
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": lock}, blocking=True
        )
        assert cloud.written() == [("PUT", LOCK, "true")]
        assert hass.states.get(lock).state == "on"
        assert sleeps == [1.0]

        number = _state(hass, entry, *OFFSET_ID)
        assert number.attributes["min"] == -2.0
        assert number.attributes["max"] == 2.0
        assert number.attributes["step"] == 0.5
        assert number.attributes["unit_of_measurement"] == "°C"
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": number.entity_id, "value": 1.5},
            blocking=True,
        )
        assert cloud.written()[-1] == ("PUT", OFFSET, 1.5)
        assert isinstance(cloud.written()[-1][2], float)
        # Off the grid: refused by the write path; beyond the limit: by
        # Home Assistant already. Nothing is sent.
        for value, key in ((0.3, "write_validation_failed"), (2.5, "out_of_range")):
            with pytest.raises(ServiceValidationError) as refused:
                await hass.services.async_call(
                    "number",
                    "set_value",
                    {"entity_id": number.entity_id, "value": value},
                    blocking=True,
                )
            assert refused.value.translation_key == key

        select = _state(hass, entry, *SENSITIVITY_ID)
        assert select.state == "high"  # 1.0
        assert select.attributes["options"] == ["high", "medium", "low"]
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": select.entity_id, "option": "low"},
            blocking=True,
        )
        assert cloud.written()[-1] == ("PUT", SENSITIVITY, 2.5)
        assert _state(hass, entry, *SENSITIVITY_ID).state == "low"
        assert len(cloud.written()) == 3

        coordinator = entry.runtime_data.coordinators[0]
        stats = coordinator.rrc_write_stats.snapshot("devices")
        assert stats["writes_sent"] == 3
        assert stats["writes_confirmed"] == 3
        assert stats["rejection_reasons"] == {"validation": 1}
    finally:
        await _teardown(hass, entry, stack)


async def test_a_level_outside_the_limits_is_refused_unsent(
    hass: HomeAssistant,
) -> None:
    """S3: device 9 advertises up to 2.0, so "low" (2.5) is never sent."""
    cloud, clock = RrcCloud(tree()), Clock()
    entry, stack = await _setup(hass, cloud, clock)
    select_id = (
        "select",
        "gateway-one:devices:device9:etrv:openWindowDetection:sensitivity:control",
    )
    try:
        await _enable(hass, entry, select_id)
        select = _state(hass, entry, *select_id).entity_id
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": select, "option": "low"},
                blocking=True,
            )
        assert cloud.written() == []
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": select, "option": "medium"},
            blocking=True,
        )
        assert cloud.written() == [
            ("PUT", "/devices/device9/etrv/openWindowDetection/sensitivity", 1.5)
        ]
    finally:
        await _teardown(hass, entry, stack)


async def test_only_the_exact_value_confirms_the_sensitivity(
    hass: HomeAssistant,
) -> None:
    """The controller keeps 2.4 instead of 2.5: shown as "low" by the
    thresholds, but never confirmed; exactly one PUT."""
    cloud, clock = RrcCloud(tree()), Clock()
    entry, stack = await _setup(hass, cloud, clock)
    try:
        sleeps = await _enable(hass, entry, SENSITIVITY_ID)
        settle = cloud._settle

        def rounding(path: str, value: object) -> None:
            settle(path, 2.4 if path == SENSITIVITY else value)

        cloud._settle = rounding  # type: ignore[method-assign]
        select = _state(hass, entry, *SENSITIVITY_ID).entity_id
        with pytest.raises(HomeAssistantError) as unconfirmed:
            await hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": select, "option": "low"},
                blocking=True,
            )
        assert unconfirmed.value.translation_key == "rrc_write_pending_confirmation"
        assert cloud.calls["PUT", SENSITIVITY] == 1
        assert sleeps == [1.0, 2.0, 4.0, 8.0]
        coordinator = entry.runtime_data.coordinators[0]
        assert coordinator.data[SENSITIVITY].freshness is Freshness.STALE
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(select).state == "low"
        assert cloud.calls["PUT", SENSITIVITY] == 1
        assert coordinator.rrc_write_stats.snapshot("devices")["late_confirmed"] == 0
    finally:
        await _teardown(hass, entry, stack)


async def test_write_failure_paths_in_real_cycles(hass: HomeAssistant) -> None:
    """Timeout after the PUT, 204 without effect, late adoption, 403, 429
    with Retry-After and 5xx: never a second PUT."""
    cloud, clock = RrcCloud(tree()), Clock()
    entry, stack = await _setup(hass, cloud, clock)
    try:
        await _enable(hass, entry, LOCK_ID)
        lock = _state(hass, entry, *LOCK_ID).entity_id

        async def turn(service: str) -> None:
            await hass.services.async_call(
                "switch", service, {"entity_id": lock}, blocking=True
            )

        # Timeout after an applied PUT: confirmed by reading alone.
        cloud.timeout_after_write[LOCK] = True
        await turn("turn_on")
        assert cloud.calls["PUT", LOCK] == 1
        # Timeout after a PUT that was not applied: not confirmed, not resent.
        cloud.timeout_after_write[LOCK] = False
        with pytest.raises(HomeAssistantError) as lost:
            await turn("turn_off")
        assert lost.value.translation_key == "rrc_write_pending_confirmation"
        assert cloud.calls["PUT", LOCK] == 2
        del cloud.timeout_after_write[LOCK]
        coordinator = entry.runtime_data.coordinators[0]
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        # 204 without effect: not confirmed.
        cloud.ignore_writes.add(LOCK)
        with pytest.raises(HomeAssistantError) as ignored:
            await turn("turn_off")
        assert ignored.value.translation_key == "rrc_write_pending_confirmation"
        assert cloud.calls["PUT", LOCK] == 3
        cloud.ignore_writes.clear()
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        # Adopted after the read-back ended: the next regular poll confirms
        # it late (CT-C07).
        cloud.adopt_after[LOCK] = 5
        with pytest.raises(HomeAssistantError):
            await turn("turn_off")
        assert cloud.calls["PUT", LOCK] == 4
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(lock).state == "off"
        del cloud.adopt_after[LOCK]

        # 403: refused, never repeated.
        cloud.forbidden.add(LOCK)
        with pytest.raises(HomeAssistantError) as refused:
            await turn("turn_on")
        assert refused.value.translation_key == "write_failed"
        assert cloud.calls["PUT", LOCK] == 5
        cloud.forbidden.discard(LOCK)
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        # 5xx on the PUT: the outcome is unknown, so only reading follows
        # (C32); not confirmed, no second PUT.
        record = cloud._write

        def unavailable(method: str, path: str, value: object) -> None:
            record(method, path, value)
            raise ServiceUnavailable(503)

        cloud._write = unavailable  # type: ignore[method-assign]
        with pytest.raises(HomeAssistantError) as failed:
            await turn("turn_on")
        assert failed.value.translation_key == "rrc_write_pending_confirmation"
        assert cloud.calls["PUT", LOCK] == 6
        cloud._write = record  # type: ignore[method-assign]
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        # 429 with Retry-After: the brake stops the next write unsent.
        cloud.rate_limit_writes[LOCK] = 120
        with pytest.raises(HomeAssistantError) as limited:
            await turn("turn_on")
        assert limited.value.translation_key == "write_rate_limited"
        del cloud.rate_limit_writes[LOCK]
        with pytest.raises(HomeAssistantError) as braked:
            await turn("turn_on")
        assert braked.value.translation_key == "write_rate_limited"
        assert cloud.calls["PUT", LOCK] == 7

        stats = coordinator.rrc_write_stats.snapshot("devices")
        assert stats["late_confirmed"] == 1
        assert stats["writes_unconfirmed"] >= 3
    finally:
        await _teardown(hass, entry, stack)


async def test_unused_and_locked_settings(hass: HomeAssistant) -> None:
    """An offset the device stops using goes unavailable and is refused
    unsent; a used but unavailable one is locked (S8)."""
    cloud, clock = RrcCloud(tree()), Clock()
    entry, stack = await _setup(hass, cloud, clock)
    try:
        await _enable(hass, entry, OFFSET_ID)
        coordinator = entry.runtime_data.coordinators[0]
        number = _state(hass, entry, *OFFSET_ID).entity_id
        assert hass.states.get(number).state == "0.0"
        cloud.tree[OFFSET] = _offset(used="false")
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(number).state == "unavailable"
        with pytest.raises(ContractRejected) as unused:
            await coordinator.async_write_control(
                OFFSET, 1.0, rrc_contract_devices.OFFSET_POLICY
            )
        assert unused.value.translation_key == "rrc_device_setting_unused"

        cloud.tree[OFFSET] = _offset(used="true", available="false")
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        with pytest.raises(ServiceValidationError) as locked:
            await hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": number, "value": 1.0},
                blocking=True,
            )
        assert locked.value.translation_key == "rrc_device_setting_locked"
        assert cloud.written() == []
    finally:
        await _teardown(hass, entry, stack)


async def test_a_device_that_leaves_or_changes_type_goes_unavailable(
    hass: HomeAssistant,
) -> None:
    """Entities stay registered; a type change under the same ID shows
    nothing from the former subfolder."""
    cloud, clock = RrcCloud(tree()), Clock()
    entry, stack = await _setup(hass, cloud, clock)
    wth_offset = ("number", "gateway-one:devices:device10:wth:offset:control")
    try:
        await _enable(hass, entry, OFFSET_ID, SENSITIVITY_ID, wth_offset)
        coordinator = entry.runtime_data.coordinators[0]
        problem = _state(
            hass, entry, "binary_sensor", "gateway-one:devices:device3:valve_problem"
        ).entity_id
        assert hass.states.get(problem).state == "on"
        listed_devices = cloud.tree[rrc.DEVICE_LIST]
        items = [
            {**item, "type": "thermostat_valve"} if item["id"] == 10 else item
            for item in listed_devices.value
            if item["id"] not in (2, 3)
        ]
        cloud.tree[rrc.DEVICE_LIST] = replace(listed_devices, value=items)
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        for unique in (OFFSET_ID, SENSITIVITY_ID, wth_offset):
            assert _state(hass, entry, *unique).state == "unavailable", unique
        assert hass.states.get(problem).state == "unavailable"
        assert len(_entries(hass, entry)) > 50  # nothing removed
    finally:
        await _teardown(hass, entry, stack)


async def test_an_outage_of_the_leaves_keeps_them_within_validity(
    hass: HomeAssistant,
) -> None:
    cloud, clock = RrcCloud(tree()), Clock()
    entry, stack = await _setup(hass, cloud, clock)
    status = "/devices/device3/etrv/status"
    try:
        coordinator = entry.runtime_data.coordinators[0]
        problem = _state(
            hass, entry, "binary_sensor", "gateway-one:devices:device3:valve_problem"
        ).entity_id
        cloud.down[status] = 503
        clock.now += 15 * 60 + 1
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(problem).state == "on"
        # The controller no longer knows the leaf: unavailable, kept.
        cloud.down[status] = 404
        for _ in range(3):
            clock.now += 4 * 3600
            await coordinator.async_refresh()
            await hass.async_block_till_done()
        assert hass.states.get(problem).state == "unavailable"
        del cloud.down[status]
        clock.now += 4 * 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(problem).state == "on"
    finally:
        await _teardown(hass, entry, stack)


async def test_another_gateway_with_the_same_paths_gets_nothing(
    hass: HomeAssistant,
) -> None:
    """S11: without RRC evidence no leaf is probed and no W6 entity exists."""
    resources = {
        path: resource
        for path, resource in tree().items()
        if not path.startswith(("/zones", "/gateway"))
    }
    cloud = RrcCloud(resources, missing_as_500=False)
    entry, stack = await _setup(hass, cloud, Clock(), Gateway("gateway-one"))
    try:
        entries = _entries(hass, entry)
        assert not [key for key in entries if key[1].endswith(":valve_problem")]
        assert not [
            key
            for key in entries
            if key[0] in ("switch", "number", "select") and ":devices:" in key[1]
        ]
        coordinator = entry.runtime_data.coordinators[0]
        assert not coordinator.control_context().rrc_plant
    finally:
        await _teardown(hass, entry, stack)


def test_contract_names_have_both_languages() -> None:
    names = dict(rrc_contract_devices.CONTRACT.names)
    assert names[rrc_contract_devices.CHILD_LOCK_PATTERN] == (
        "Kindersicherung",
        "Child lock",
    )
    assert all(german and english for german, english in names.values())
    assert isinstance(rrc_contract_devices.CONTRACT.device_leaves, MappingProxyType)
