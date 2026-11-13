"""Room controller deep review: the shared write path, the flag rule and the
discovery (0.9.0-alpha.5).

- The flag rule of every contract setting (ZONE-01, AWAY-02, CIRCUIT-05):
  ``available`` false gives no control, ``available`` true with ``used``
  false keeps the control and refuses every write unsent with its own
  message; the heating circuit keeps the earlier rule (``unused_writable``).
- S5 before every contract PUT (AWAY-03, CIRCUIT-06) and for the boost only
  the fields a write depends on (AWAY-09).
- A failed preflight keeps the reading (SYSTEM-11); an unconfirmed write is
  read again with the next cycle and a reading that ended it stays valid
  (ZONE-03); a request that never left is not counted as sent (SYSTEM-17).
- The 5xx-means-missing rule needs a cloud that answers (SYSTEM-12); event
  entries are never read (AWAY-01).

Real coordinator cycles against ``tests/helpers/rrc_cloud.py``; everything
is expected from data shapes, untested on hardware.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.components.climate.const import ClimateEntityFeature
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating import (
    discovery,
    rrc,
    rrc_contract_absence,
    rrc_contract_circuit,
    rrc_contract_dhw,
    rrc_contract_zone,
)
from custom_components.bosch_buderus_heating.control import unused_translation_key
from custom_components.bosch_buderus_heating.coordinator import Freshness
from custom_components.bosch_buderus_heating.discovery import (
    DiscoveryDiagnostics,
    DiscoveryPathSource,
)
from custom_components.bosch_buderus_heating.discovery_inventory import _not_kept
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Gateway,
    RequestTimeout,
    Resource,
    ResourceError,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.rrc_write import RrcStructWrite
from custom_components.bosch_buderus_heating.writes import (
    GENERIC_CONTEXT,
    RRC_CONTEXT,
    ControlLocked,
    ControlUnused,
    assess_control,
    validate_control_value,
)
from tests.helpers.rrc_absence import (
    AWAY,
    BOOST,
    RRC_GATEWAY,
    absence_tree,
    entity_id,
    set_value,
    setup,
    teardown,
    variants,
)
from tests.helpers.rrc_cloud import Clock, RrcCloud, cloud_patches
from tests.test_entry import _entry
from tests.test_rrc_dhw import EXTRA
from tests.test_rrc_dhw import _setup as dhw_setup
from tests.test_rrc_dhw import _teardown as dhw_teardown
from tests.test_rrc_dhw import _uid as dhw_uid
from tests.test_rrc_dhw import tree as dhw_tree
from tests.test_rrc_zone_climate import (
    MANUAL,
    OVERRIDE,
    climate,
    path,
    refresh,
    set_temperature,
    zone_cloud,
    zone_tree,
)
from tests.test_rrc_zone_climate import setup as zone_setup
from tests.test_rrc_zone_climate import teardown as zone_teardown

PACKAGE = "custom_components.bosch_buderus_heating"
AWAY_SWITCH = "gateway-one:system:awayMode:enabled:control"
BOOST_SWITCH = "gateway-one:heatingCircuits:hc1:boostShortcut:switch"
BOOST_TEMPERATURE = "gateway-one:heatingCircuits:hc1:boostShortcut:temperature"
WINDOW = path(2, "openWindowDetection/enabled")
SETPOINT_OPTIMIZATION = "/heatingCircuits/hc1/setpointOptimization"
NEW_KEYS = (
    "rrc_setting_unused",
    "rrc_away_unused",
    "rrc_window_detection_unused",
    "rrc_dhw_setting_unused",
    "rrc_boost_changed",
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture(autouse=True)
def legacy_setup_path():
    with patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)):
        yield


def flagged(path_: str, value: Any, **fields: Any) -> Resource:
    return parse_resource(
        {
            "id": path_,
            "type": "stringValue",
            "writeable": 1,
            "recordable": 0,
            "value": value,
            **fields,
        }
    )


# ------------------------------------------------------------ flag rule


@pytest.mark.parametrize(
    ("flags", "state"),
    [
        ({}, "usable"),
        ({"used": "true", "available": "true"}, "usable"),
        ({"used": "false"}, "unused"),
        ({"used": "false", "available": "true"}, "unused"),
        ({"available": "false"}, "unavailable"),
        ({"used": "true", "available": "false"}, "unavailable"),
        ({"used": "maybe"}, "invalid"),
    ],
)
def test_flag_state(flags: dict[str, str], state: str) -> None:
    assert rrc.flag_state(flagged(AWAY, "false", **flags)) == state


# (path, current value, a value to write) of contract settings that follow
# the flag rule.
FOLLOWING = (
    (AWAY, "false", "true"),
    ("/system/fireplace/enabled", "false", "true"),
    (WINDOW, "off", "on"),
    ("/system/openWindowDetection/enabled", "off", "on"),
    ("/dhwCircuits/dhw1/operationMode", "high", "Off"),
    ("/dhwCircuits/dhw1/extraDhw", "off", "on"),
)


@pytest.mark.parametrize(("path_", "value", "target"), FOLLOWING)
def test_unused_settings_are_shown_and_never_written(
    path_: str, value: str, target: str
) -> None:
    """ZONE-01, AWAY-02, CIRCUIT-05."""
    unused = flagged(path_, value, used="false", available="true")
    assessment = assess_control(unused, RRC_CONTEXT)
    assert assessment.eligible, assessment.rejection_reason
    with pytest.raises(ControlUnused):
        validate_control_value(unused, target, RRC_CONTEXT)
    for flags in ({"used": "true", "available": "false"}, {"available": "false"}):
        hidden = flagged(path_, value, **flags)
        assert assess_control(hidden, RRC_CONTEXT).rejection_reason == (
            "rrc_unavailable"
        )
    usable = flagged(path_, value, used="true", available="true")
    validate_control_value(usable, target, RRC_CONTEXT)


def test_unused_numbers_are_refused_too() -> None:
    temperature = parse_resource(
        {
            "id": "/system/awayMode/temperature",
            "type": "floatValue",
            "writeable": 1,
            "unitOfMeasure": "C",
            "minValue": 5.0,
            "maxValue": 30.0,
            "stepSize": 0.5,
            "value": 12.0,
            "used": "false",
        }
    )
    assert assess_control(temperature, RRC_CONTEXT).eligible
    with pytest.raises(ControlUnused):
        validate_control_value(temperature, 15.0, RRC_CONTEXT)


def test_the_heating_circuit_keeps_the_earlier_rule() -> None:
    """CIRCUIT-05: the circuit settings do not depend on ``used``, except
    the season mode and the setpoint optimization (E3), which follow the
    general rule."""
    general = {
        rrc_contract_circuit.SEASON_MODE_POLICY,
        rrc_contract_circuit.SETPOINT_OPTIMIZATION_POLICY,
    }
    for policy in (
        *rrc_contract_circuit.ENUM_POLICIES,
        *rrc_contract_circuit.NUMBER_POLICIES,
    ):
        assert policy.unused_writable is (policy not in general), policy
    unused = flagged(SETPOINT_OPTIMIZATION, "off", used="false")
    assert assess_control(unused, RRC_CONTEXT).eligible
    with pytest.raises(ControlUnused):
        validate_control_value(unused, "on", RRC_CONTEXT)
    hidden = flagged(SETPOINT_OPTIMIZATION, "off", used="true", available="false")
    assert assess_control(hidden, RRC_CONTEXT).rejection_reason == "rrc_unavailable"
    hidden = flagged(SETPOINT_OPTIMIZATION, "off", used="false", available="false")
    assert assess_control(hidden, RRC_CONTEXT).rejection_reason == "rrc_unavailable"


def test_only_contract_policies_on_a_room_controller_follow_the_rule() -> None:
    """S11: the generic policies and other gateways are not affected."""
    away = flagged(AWAY, "off", used="false", allowedValues=["on", "off"])
    assert assess_control(away, GENERIC_CONTEXT).eligible
    validate_control_value(away, "on", GENERIC_CONTEXT)
    # A zone mode keeps its own rule (#113).
    mode = flagged("/zones/zn1/userMode", "manual", used="true", available="false")
    with pytest.raises(ControlLocked) as raised:
        validate_control_value(mode, "clock", RRC_CONTEXT)
    assert not isinstance(raised.value, ControlUnused)


@pytest.mark.parametrize(
    ("path_", "key"),
    [
        (AWAY, "rrc_away_unused"),
        (WINDOW, "rrc_window_detection_unused"),
        ("/system/openWindowDetection/duration", "rrc_window_detection_unused"),
        ("/dhwCircuits/dhw1/extraDhw", "rrc_dhw_setting_unused"),
        ("/devices/device3/etrv/offset", "rrc_device_setting_unused"),
        ("/system/fireplace/enabled", "rrc_setting_unused"),
    ],
)
def test_each_unused_setting_has_its_message(path_: str, key: str) -> None:
    assert unused_translation_key(path_) == key


def test_the_new_messages_exist_in_every_language() -> None:
    root = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"
    files = [root / "strings.json", *sorted((root / "translations").glob("*.json"))]
    assert len(files) == 20
    english = json.loads(files[0].read_text("utf-8"))["exceptions"]
    for file in files:
        exceptions = json.loads(file.read_text("utf-8"))["exceptions"]
        for key in NEW_KEYS:
            message = exceptions[key]["message"]
            assert message and "{" not in message, (file.name, key)
            if file.name not in ("strings.json", "en.json"):
                assert message != english[key]["message"], (file.name, key)


# ---------------------------------------------------- real write cycles


async def _switch(hass: HomeAssistant, unique_id: str, on: bool) -> None:
    await hass.services.async_call(
        "switch",
        "turn_on" if on else "turn_off",
        {"entity_id": entity_id(hass, "switch", unique_id)},
        blocking=True,
    )


async def test_an_unused_away_switch_is_shown_and_refused_unsent(
    hass: HomeAssistant,
) -> None:
    """AWAY-02 (review proof reversed): no PUT, its own message."""
    tree = absence_tree()
    tree[AWAY] = flagged(AWAY, "false", used="false")
    cloud = RrcCloud(tree)
    entry, coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    try:
        switch = entity_id(hass, "switch", AWAY_SWITCH)
        assert hass.states.get(switch).state == "off"
        with pytest.raises(ServiceValidationError) as refused:
            await _switch(hass, AWAY_SWITCH, True)
        assert refused.value.translation_key == "rrc_away_unused"
        assert cloud.written() == []
        reasons = coordinator.rrc_write_stats.snapshot("absence")["rejection_reasons"]
        assert reasons == {"unused": 1}
    finally:
        await teardown(hass, entry, stack)


async def test_unused_window_detection_is_refused_unsent(hass: HomeAssistant) -> None:
    """ZONE-01 (review proof reversed): shown, greyed out by the refusal."""
    tree = zone_tree()
    tree[WINDOW] = flagged(WINDOW, "off", used="false")
    cloud = zone_cloud(tree)
    entry, _coordinator, _sleeps, stack, _clock = await zone_setup(hass, cloud)
    try:
        unique_id = "gateway-one:zones:zn2:openWindowDetection:enabled:control"
        assert hass.states.get(entity_id(hass, "switch", unique_id)).state == "off"
        with pytest.raises(ServiceValidationError) as refused:
            await _switch(hass, unique_id, True)
        assert refused.value.translation_key == "rrc_window_detection_unused"
        assert cloud.written("PUT") == []
    finally:
        await zone_teardown(hass, entry, stack)


async def test_a_contract_write_reads_the_controller_first(
    hass: HomeAssistant,
) -> None:
    """AWAY-03 (review proof reversed): the controller hid away mode after
    the last poll; the fresh read in one bulk request finds it, nothing is
    sent and the switch goes unavailable at once."""
    cloud = RrcCloud(absence_tree())
    entry, _coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    try:
        switch = entity_id(hass, "switch", AWAY_SWITCH)
        assert hass.states.get(switch).state == "off"
        cloud.tree[AWAY] = flagged(AWAY, "false", used="true", available="false")
        bulks = len(cloud.requests)
        with pytest.raises(ServiceValidationError):
            await _switch(hass, AWAY_SWITCH, True)
        assert cloud.written() == []
        assert any(AWAY in paths for paths in cloud.requests[bulks:])
        assert hass.states.get(switch).state == "unavailable"
    finally:
        await teardown(hass, entry, stack)


async def test_a_value_changed_meanwhile_is_not_refused(hass: HomeAssistant) -> None:
    """S5 reads the flags, limits and value; a value already as requested
    sends nothing, another changed value does not block a write."""
    cloud = RrcCloud(absence_tree())
    entry, _coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    try:
        set_value(cloud, AWAY, "true")
        await _switch(hass, AWAY_SWITCH, True)
        assert cloud.written() == []
        assert hass.states.get(entity_id(hass, "switch", AWAY_SWITCH)).state == "on"
        await _switch(hass, AWAY_SWITCH, False)
        assert cloud.written() == [("PUT", AWAY, "false")]
    finally:
        await teardown(hass, entry, stack)


@pytest.mark.parametrize(
    "failure", [ServiceUnavailable(503), RequestTimeout("preflight timed out")]
)
async def test_a_failed_fresh_read_sends_nothing_and_keeps_the_reading(
    hass: HomeAssistant, failure: Exception
) -> None:
    """C33: nothing was written, the reading stays as it is."""
    cloud = RrcCloud(absence_tree())
    entry, coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    try:
        cloud.down[AWAY] = failure
        with pytest.raises(HomeAssistantError):
            await _switch(hass, AWAY_SWITCH, True)
        assert cloud.written() == []
        snapshot = coordinator.data[AWAY]
        assert snapshot.available and snapshot.freshness is Freshness.FRESH
    finally:
        await teardown(hass, entry, stack)


async def test_an_unused_hot_water_switch_found_by_the_fresh_read(
    hass: HomeAssistant,
) -> None:
    """CIRCUIT-06 (review proof reversed) with CIRCUIT-05."""
    cloud, clock = RrcCloud(dhw_tree()), Clock()
    entry, _coordinator, _sleeps, stack = await dhw_setup(hass, cloud, clock)
    try:
        current = cloud.tree[EXTRA]
        cloud.tree[EXTRA] = replace(
            current, metadata=replace(current.metadata, used=False, available=True)
        )
        with pytest.raises(ServiceValidationError) as refused:
            await _switch(hass, dhw_uid(EXTRA), True)
        assert refused.value.translation_key == "rrc_dhw_setting_unused"
        assert [w for w in cloud.written("PUT") if w[1] == EXTRA] == []
        # Shown while not used: the entity stays available.
        state = hass.states.get(entity_id(hass, "switch", dhw_uid(EXTRA)))
        assert state.state == "off"
    finally:
        await dhw_teardown(hass, entry, stack)


async def test_boost_compares_only_what_the_write_depends_on(
    hass: HomeAssistant,
) -> None:
    """AWAY-09: a temperature changed on the controller does not block
    switching; changed zones do, with their own message."""
    cloud = RrcCloud(absence_tree())
    entry, _coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    try:
        shown = absence_tree()[BOOST].value
        assert isinstance(shown, list)
        set_value(cloud, BOOST, [{**shown[0], "temperature": 23.0, "duration": 4}])
        await _switch(hass, BOOST_SWITCH, True)
        assert cloud.written() == [
            (
                "PUT",
                BOOST,
                [{"temperature": 23.0, "duration": 4, "mode": "on", "zones": [1, 2]}],
            )
        ]
        # Shown now: on, zones 1 and 2. The zones change on the controller.
        set_value(cloud, BOOST, [{**shown[0], "mode": "on", "zones": [3]}])
        with pytest.raises(ServiceValidationError) as changed:
            await _switch(hass, BOOST_SWITCH, False)
        assert changed.value.translation_key == "rrc_boost_changed"
        assert len(cloud.written()) == 1
        # Stopping depends on the mode and the zones only: a duration changed
        # on the controller goes along as read.
        set_value(cloud, BOOST, [{**shown[0], "mode": "on", "duration": 5}])
        await _switch(hass, BOOST_SWITCH, False)
        assert cloud.written()[-1][2][0] == {
            "temperature": 20,
            "duration": 5,
            "mode": "off",
            "zones": [1, 2],
        }
        # AWAY-06: the numbers send nothing themselves.
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity_id(hass, "number", BOOST_TEMPERATURE), "value": 21},
            blocking=True,
        )
        assert len(cloud.written()) == 2
        assert variants()  # the fixture variants stay as they were
    finally:
        await teardown(hass, entry, stack)


# ------------------------------------------- failed and unconfirmed writes


async def test_a_failed_preflight_keeps_the_reading(hass: HomeAssistant) -> None:
    """SYSTEM-11 (review proof reversed)."""
    cloud = RrcCloud(absence_tree())
    entry, coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    try:

        async def operation() -> tuple[Resource, ...]:
            raise RequestTimeout("preflight read timed out")

        with pytest.raises(RequestTimeout):
            await coordinator.async_run_rrc_operation(BOOST, operation)
        snapshot = coordinator.data[BOOST]
        assert snapshot.available and snapshot.freshness is Freshness.FRESH
        assert not cloud.writes
        assert BOOST not in coordinator._urgent_reads
    finally:
        await teardown(hass, entry, stack)


async def test_an_unconfirmed_structure_is_read_with_the_next_cycle(
    hass: HomeAssistant,
) -> None:
    """ZONE-03 for structured writes: stale until read, read within a
    minute rather than with its five-minute group."""
    cloud = RrcCloud(absence_tree())
    entry, coordinator, clock, _sleeps, stack = await setup(hass, cloud)
    try:
        cloud.ignore_writes.add(BOOST)
        with pytest.raises(HomeAssistantError) as pending:
            await _switch(hass, BOOST_SWITCH, True)
        assert pending.value.translation_key == "rrc_write_pending_confirmation"
        assert coordinator.data[BOOST].freshness is Freshness.STALE
        reads = cloud.requested(BOOST)
        clock.now += 61
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert cloud.requested(BOOST) == reads + 1
        assert coordinator.data[BOOST].freshness is Freshness.FRESH
        assert len(cloud.written()) == 1
    finally:
        await teardown(hass, entry, stack)


@pytest.mark.parametrize(("leaf", "zone"), [(OVERRIDE, 3), (MANUAL, 1)])
async def test_an_unconfirmed_setpoint_comes_back_within_a_minute(
    hass: HomeAssistant, leaf: str, zone: int
) -> None:
    """ZONE-03 (review proof reversed): the target control is back with the
    next cycle, not after the five-minute group; nothing is sent again."""
    cloud = zone_cloud()
    entry, coordinator, _sleeps, stack, clock = await zone_setup(hass, cloud)
    try:
        cloud.ignore_writes.add(path(zone, leaf))
        with pytest.raises(HomeAssistantError) as pending:
            await set_temperature(hass, climate(hass, zone), temperature=22.5)
        assert pending.value.translation_key == "rrc_write_pending_confirmation"
        await refresh(hass, coordinator, clock, 61)
        state = hass.states.get(climate(hass, zone))
        assert state.attributes["supported_features"] & (
            ClimateEntityFeature.TARGET_TEMPERATURE
        )
        assert state.attributes["temperature"] is not None
        assert coordinator.data[path(zone, leaf)].freshness is Freshness.FRESH
        assert cloud.calls["PUT", path(zone, leaf)] == 1
    finally:
        await zone_teardown(hass, entry, stack)


async def test_an_ended_temporary_setpoint_keeps_its_valid_reading(
    hass: HomeAssistant,
) -> None:
    """ZONE-03 (b): after ``rrc_override_ended`` the reading of 0 is current."""
    cloud = zone_cloud()
    entry, coordinator, _sleeps, stack, _clock = await zone_setup(hass, cloud)
    try:
        cloud.end_on_write.add(path(2, OVERRIDE))
        with pytest.raises(HomeAssistantError) as ended:
            await set_temperature(hass, climate(hass, 2), temperature=22.0)
        assert ended.value.translation_key == "rrc_override_ended"
        snapshot = coordinator.data[path(2, OVERRIDE)]
        assert snapshot.available and snapshot.freshness is Freshness.FRESH
        assert snapshot.resource.value == 0
        state = hass.states.get(climate(hass, 2))
        assert state.attributes["supported_features"] & (
            ClimateEntityFeature.TARGET_TEMPERATURE
        )
        assert not state.attributes.get("temporary_override")
    finally:
        await zone_teardown(hass, entry, stack)


async def _struct_write(send_error: BaseException | None) -> list[int]:
    struct = RrcStructWrite(sleep=AsyncMock(), delays=(0.0,))
    sent: list[int] = []

    async def preflight() -> str:
        return "state"

    async def send(_state: str) -> None:
        if send_error is not None:
            raise send_error

    async def confirm(_state: str) -> str:
        return "done"

    with contextlib.suppress(BaseException):
        await struct.async_write(
            preflight=preflight,
            send=send,
            confirm=confirm,
            on_sent=lambda: sent.append(1),
        )
    return sent


async def test_a_request_that_never_left_is_not_counted_as_sent() -> None:
    """SYSTEM-17."""
    never = ServiceUnavailable()
    never.__cause__ = aiohttp.ClientConnectorError(
        connection_key=None,  # type: ignore[arg-type]
        os_error=OSError("refused"),
    )
    assert await _struct_write(never) == []
    assert await _struct_write(None) == [1]
    assert await _struct_write(RequestTimeout("lost answer")) == [1]
    assert await _struct_write(ResourceError("/x", 400)) == [1]


# ------------------------------------------------------------ discovery


OPTIONAL = ("/system/a", "/system/b", "/system/c")


def _diagnostics(*paths: str) -> DiscoveryDiagnostics:
    report = DiscoveryDiagnostics()
    for item in paths:
        report.scheduled(item, DiscoveryPathSource.OPTIONAL)
    return report


def _item_5xx(item: str) -> BatchItemResult:
    return BatchItemResult("gw", item, 500, error=ResourceError(item, 500))


@pytest.mark.parametrize(
    ("answering", "missing"), [(True, True), (False, False), (None, True)]
)
async def test_only_an_answering_cloud_makes_5xx_parts_missing(
    answering: bool | None, missing: bool
) -> None:
    """SYSTEM-12: without an answer to the path sent along, a bulk without
    any resource counts as an outage: read one by one, three 5xx answers in
    a row end the discovery. Without a path sent along (a batch with roots
    or references, which keep the outage rule themselves) as before."""
    cloud = RrcCloud({})
    for item in OPTIONAL:
        cloud.down[item] = ServiceUnavailable(500)
    report = _diagnostics(*OPTIONAL)
    results = tuple(_item_5xx(item) for item in OPTIONAL)
    if missing:
        await discovery._recover_invalid_bulk_results(
            cloud,  # type: ignore[arg-type]
            "gw",
            results,
            diagnostics=report,
            rrc_profile=True,
            answering=answering,
        )
        assert cloud.single == []
        assert {report.paths[item].read_policy for item in OPTIONAL} == {
            "rrc_5xx_assumed_missing"
        }
        return
    with pytest.raises(ServiceUnavailable):
        await discovery._recover_invalid_bulk_results(
            cloud,  # type: ignore[arg-type]
            "gw",
            results,
            diagnostics=report,
            rrc_profile=True,
            answering=answering,
        )
    assert cloud.single == list(OPTIONAL)
    assert "rrc_5xx_assumed_missing" not in {
        item.read_policy for item in report.paths.values()
    }


async def test_a_bulk_with_a_resource_needs_nothing_more() -> None:
    known = "/gateway/uuid"
    cloud = RrcCloud({known: flagged(known, "id")})
    report = _diagnostics(*OPTIONAL, known)
    results = (
        BatchItemResult("gw", known, 200, resource=cloud.tree[known]),
        *(_item_5xx(item) for item in OPTIONAL),
    )
    await discovery._recover_invalid_bulk_results(
        cloud,  # type: ignore[arg-type]
        "gw",
        results,
        diagnostics=report,
        rrc_profile=True,
    )
    assert cloud.single == []
    assert report.paths[OPTIONAL[0]].read_policy == "rrc_5xx_assumed_missing"


class _OutageAtTheEnd(RrcCloud):
    """Answers until the radio device settings are read; from then on every
    path answers HTTP 500, as a gateway that went offline."""

    def __init__(self, resources: dict[str, Resource]) -> None:
        super().__init__(resources)
        self.offline = False
        self.offline_reads: list[str] = []

    async def get_resources_bulk(self, gateway_id, paths, **kwargs):
        if any(
            item.startswith("/devices/device") and "/etrv/" in item for item in paths
        ):
            self.offline = True
        if self.offline:
            self.requests.append(tuple(paths))
            return tuple(_item_5xx(item) for item in paths)
        return await super().get_resources_bulk(gateway_id, paths, **kwargs)

    async def get_resource(self, gateway_id, path_, **kwargs):
        if self.offline:
            self.offline_reads.append(path_)
            raise ServiceUnavailable(500)
        return await super().get_resource(gateway_id, path_, **kwargs)


async def _discover_devices(cloud: RrcCloud) -> DiscoveryDiagnostics:
    report = DiscoveryDiagnostics()
    await discovery.async_discover_resources(
        cloud,  # type: ignore[arg-type]
        "gw",
        diagnostics=report,
        gateway=Gateway("gw", device_type="rrc2"),
    )
    return report


async def test_an_outage_before_the_device_settings_is_not_missing() -> None:
    """SYSTEM-12 in a real discovery: the device setting batches carry a
    path read before. While the cloud answers, missing leaves stay missing
    without extra requests; when it went away, the discovery ends instead
    of completing without the settings."""
    from tests.test_rrc_device_settings import tree as device_tree

    answering = RrcCloud(device_tree())
    report = await _discover_devices(answering)
    assert report.stop_reason == "complete"
    carried = [
        paths
        for paths in answering.requests
        if any("/etrv/" in item for item in paths) and "/etrv/" not in paths[-1]
    ]
    assert carried, "a device setting batch carries a known path"
    assert all(len(paths) <= 30 for paths in answering.requests)

    offline = _OutageAtTheEnd(device_tree())
    with pytest.raises(ServiceUnavailable):
        await _discover_devices(offline)
    # Three device leaves read one by one, each 500: the outage rule.
    assert len(offline.offline_reads) == 3
    assert all(item.startswith("/devices/device") for item in offline.offline_reads), (
        offline.offline_reads
    )


def _events_tree(*failing: str) -> dict[str, Resource]:
    tree = absence_tree()
    tree["/events"] = parse_resource(
        {
            "id": "/events",
            "type": "refEnum",
            "references": [
                {"id": f"/events/{name}"}
                for name in ("activeEvent", "ev1", "ev2", "ev3", *("list1", "list2"))
            ]
            + [{"id": "/events/list3"}],
        }
    )
    tree["/events/ev3"] = parse_resource(
        {"id": "/events/ev3", "type": "eventArray", "writeable": 1, "value": []}
    )
    for name in failing:
        tree.pop(f"/events/{name}", None)
    return tree


@pytest.mark.parametrize(
    "failing", [("ev1", "ev2", "ev3"), ("ev1", "ev2", "ev3", "list1", "list2", "list3")]
)
async def test_event_entries_are_never_read_and_never_block_setup(
    hass: HomeAssistant, failing: tuple[str, ...]
) -> None:
    """AWAY-01 (review proof reversed): the controller names its entries,
    each answers HTTP 500; the setup loads and reads none of them."""
    cloud = RrcCloud(_events_tree(*failing))
    entry = _entry(hass)
    stack = cloud_patches(cloud, Clock(), RRC_GATEWAY)
    try:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert not any(
            rrc.is_unread_event_entry(item) for item in cloud.all_requested()
        )
        coordinator = entry.runtime_data.coordinators[0]
        assert not any(rrc.is_unread_event_entry(item) for item in coordinator.data)
        assert entity_id(hass, "sensor", "gateway-one:events:activeEvent") is None
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        stack.close()


def test_stored_event_entries_are_dropped() -> None:
    tree = absence_tree()
    dropped = _not_kept(Gateway("gateway-one", device_type="rrc2"), tree)
    assert {"/events/ev1", "/events/ev2", "/events/activeEvent"} <= dropped
    assert not any(item.startswith("/events/list") for item in dropped)
    # Other gateways keep their paths.
    other = _not_kept(
        Gateway("gateway-one", device_type="k40"), {"/events/ev1": tree["/events/ev1"]}
    )
    assert "/events/ev1" not in other


def test_the_hooks_name_the_contract_paths() -> None:
    assert rrc.is_unread_event_entry("/events/ev12")
    assert rrc.is_unread_event_entry("/events/activeEvent")
    assert not rrc.is_unread_event_entry("/events/list1")
    assert not rrc.is_unread_event_entry("/events")
    assert rrc_contract_absence.EVENT_ENTRIES
    assert rrc_contract_dhw.EXTRA_DHW_POLICY.unused_writable is False
    assert rrc_contract_zone.ZONE_WINDOW_POLICY.unused_writable is False
