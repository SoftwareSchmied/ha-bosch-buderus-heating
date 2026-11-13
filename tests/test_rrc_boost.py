"""W2: boost of an RRC 2.0 controller, written only via ``boostShortcut``.

The structure is parsed strictly and written as a whole with exactly one
PUT (W2-E1); its limits come from the boost leaves (S3). Real coordinator
cycles cover the read-back, a timeout after the PUT, 204 without effect, a
late confirmation and the refusals. Expected, untested on hardware.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import rrc_boost
from custom_components.bosch_buderus_heating.pointt import WriteValidationError
from custom_components.bosch_buderus_heating.rrc_absence_entities import (
    build_calendar,
    build_numbers,
    build_switch,
)
from custom_components.bosch_buderus_heating.writes import (
    GENERIC_CONTEXT,
    RRC_CONTEXT,
    ContractRejected,
)
from tests.helpers.rrc_absence import (
    BOOST,
    EMS_GATEWAY,
    absence_tree,
    entity_id,
    set_value,
    setup,
    teardown,
    variants,
    wire,
)
from tests.helpers.rrc_cloud import RrcCloud

PACKAGE = "custom_components.bosch_buderus_heating"
SWITCH = "gateway-one:heatingCircuits:hc1:boostShortcut:switch"
TEMPERATURE = "gateway-one:heatingCircuits:hc1:boostShortcut:temperature"
DURATION = "gateway-one:heatingCircuits:hc1:boostShortcut:duration"

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture(autouse=True)
def legacy_setup_path():
    with patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)):
        yield


def _struct(value, **fields):
    return wire(
        BOOST, type="boostShortcutStruct", value=value, **{"writeable": 1, **fields}
    )


def _leaf(path: str, unit: str, minimum, maximum, step=None, **fields):
    extra = {"stepSize": step} if step is not None else {}
    return wire(
        path,
        type="floatValue",
        writeable=1,
        unitOfMeasure=unit,
        minValue=minimum,
        maxValue=maximum,
        value=minimum,
        **extra,
        **fields,
    )


# ------------------------------------------------------------------ rules
def test_the_structure_is_read_strictly() -> None:
    off = rrc_boost.parse(_struct(absence_tree()[BOOST].value))
    assert off == rrc_boost.Boost(20.0, 2, "off", (1, 2), (1, 2, 3, 4))
    assert off.body() == [
        {"temperature": 20.0, "duration": 2, "mode": "off", "zones": [1, 2]}
    ]
    assert rrc_boost.parse(_struct(variants()["boost_extra_field"])).extra_fields
    for value in (
        [],
        [{}, {}],
        [{"duration": 2, "mode": "off", "temperature": 20}],
        [{"duration": 2.5, "mode": "off", "temperature": 20, "zones": [1]}],
        [{"duration": 2, "mode": "boost", "temperature": 20, "zones": [1]}],
        [{"duration": 2, "mode": "on", "temperature": True, "zones": [1]}],
        [{"duration": 2, "mode": "on", "temperature": 20, "zones": [1, 1]}],
        [{"duration": 2, "mode": "on", "temperature": 20, "zones": ["1"]}],
        [
            {
                "duration": 2,
                "mode": "on",
                "temperature": 20,
                "zones": [1],
                "allowedZones": 3,
            }
        ],
        {"duration": 2, "mode": "on", "temperature": 20, "zones": [1]},
    ):
        assert rrc_boost.parse(_struct(value)) is None, value
    # P41: available "false" without used "true" is no structure; used
    # "true" and available "false" is shown and locked (S8).
    value = absence_tree()[BOOST].value
    assert rrc_boost.parse(_struct(value, available="false")) is None
    locked = _struct(value, used="true", available="false")
    assert rrc_boost.parse(locked) is not None
    assert rrc_boost.write_issue(locked) == "locked"
    assert rrc_boost.write_issue(_struct(value, writeable=0)) == "not_writable"
    assert rrc_boost.write_issue(_struct(variants()["boost_extra_field"])) == (
        "rrc_boost_unsupported_shape"
    )


def test_limits_only_from_valid_leaf_metadata() -> None:
    temperature = rrc_boost.BOOST_TEMPERATURE_PATH
    duration = rrc_boost.BOOST_DURATION_PATH
    assert rrc_boost.limits(_leaf(temperature, "C", 5.0, 30.0, 0.5), "temperature") == (
        rrc_boost.Limits(5.0, 30.0, 0.5)
    )
    assert rrc_boost.limits(_leaf(temperature, "C", 5.0, 30.0), "temperature") == (
        rrc_boost.Limits(5.0, 30.0, 0.5)
    )
    assert rrc_boost.limits(_leaf(duration, "hour", 1.0, 8.0), "duration") == (
        rrc_boost.Limits(1.0, 8.0, 1.0)
    )
    for resource, setting in (
        (_leaf(temperature, "F", 41.0, 86.0), "temperature"),
        (_leaf(temperature, "C", 30.0, 5.0), "temperature"),
        (_leaf(temperature, "C", 5.0, 6553.5), "temperature"),
        (_leaf(temperature, "C", 5.0, 30.0, 100), "temperature"),
        (_leaf(duration, "mins", 60.0, 480.0), "duration"),
        (_leaf(duration, "hour", 1.0, 8.0, 0.5), "duration"),
        (_leaf(duration, "hour", 1.0, 8.0, available="false"), "duration"),
        (
            wire(temperature, type="floatValue", writeable=1, unitOfMeasure="C"),
            "temperature",
        ),
        (None, "temperature"),
    ):
        assert rrc_boost.limits(resource, setting) is None, resource
    bounds = rrc_boost.Limits(5.0, 30.0, 0.5)
    bounds.check(22.5)
    for value in (22.2, 4.5, 30.5, float("nan")):
        with pytest.raises(WriteValidationError):
            bounds.check(value)


def test_a_start_heats_only_existing_zones_it_may_heat() -> None:
    """AWAY-08: the set zones that still exist and are allowed; without
    ``allowedZones`` or without such a zone nothing starts."""
    boost = rrc_boost.Boost(20.0, 2, "off", (1, 2), (1, 2, 3, 4))
    assert rrc_boost.started(boost, (1, 2, 3)) == rrc_boost.Boost(
        20.0, 2, "on", (1, 2), (1, 2, 3, 4)
    )
    # A deleted zone (2) and one it may not heat (5) are left out.
    mixed = rrc_boost.Boost(20.0, 2, "off", (5, 2, 1), (1, 2, 3, 4))
    assert rrc_boost.started(mixed, (1, 3, 5)).zones == (1,)
    for zones, allowed, existing in (
        ((), (1, 2), (1, 2)),
        ((5,), (1, 2), (1, 2, 5)),
        ((1, 2), None, (1, 2)),  # no allowedZones
        ((1, 2), (1, 2), (3,)),  # both deleted
        ((1, 2), (1, 2), None),  # no valid zone list
    ):
        with pytest.raises(ContractRejected) as refused:
            rrc_boost.started(rrc_boost.Boost(20.0, 2, "off", zones, allowed), existing)
        assert refused.value.reason == "rrc_boost_no_zones"
    # Off needs no zone and keeps everything else.
    running = rrc_boost.Boost(22.5, 3, "on", (), None)
    assert rrc_boost.stopped(running) == rrc_boost.Boost(22.5, 3, "off", (), None)


def test_a_start_sends_the_preset() -> None:
    """AWAY-06: temperature and duration go only with mode on."""
    boost = rrc_boost.Boost(20.0, 2, "off", (1, 2), (1, 2))
    assert rrc_boost.started(boost, (1, 2), rrc_boost.Preset(22.5, None)).body() == [
        {"temperature": 22.5, "duration": 2, "mode": "on", "zones": [1, 2]}
    ]
    assert rrc_boost.started(boost, (1, 2), rrc_boost.Preset(None, 4)).body() == [
        {"temperature": 20.0, "duration": 4, "mode": "on", "zones": [1, 2]}
    ]
    bounds = rrc_boost.Limits(1.0, 8.0, 1.0)
    assert rrc_boost.preset_value("duration", 3.0, bounds) == 3.0
    with pytest.raises(WriteValidationError):
        rrc_boost.preset_value("duration", 3.5, bounds)


# ------------------------------------------------------------- real cycles
async def _turn(hass: HomeAssistant, on: bool) -> None:
    await hass.services.async_call(
        "switch",
        "turn_on" if on else "turn_off",
        {"entity_id": entity_id(hass, "switch", SWITCH)},
        blocking=True,
    )


async def _number(hass: HomeAssistant, unique_id: str, value: float) -> None:
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": entity_id(hass, "number", unique_id), "value": value},
        blocking=True,
    )


async def test_boost_switch_and_settings_write_the_whole_structure(
    hass: HomeAssistant,
) -> None:
    cloud = RrcCloud(absence_tree())
    cloud.adopt_after[BOOST] = 2
    entry, coordinator, _clock, sleeps, stack = await setup(hass, cloud)
    registry = er.async_get(hass)
    try:
        switch = entity_id(hass, "switch", SWITCH)
        temperature = entity_id(hass, "number", TEMPERATURE)
        duration = entity_id(hass, "number", DURATION)
        assert switch and temperature and duration
        state = hass.states.get(switch)
        assert state.state == "off" and state.attributes["zones"] == [1, 2]
        number = hass.states.get(temperature)
        assert (number.state, number.attributes["min"], number.attributes["max"]) == (
            "20.0",
            5.0,
            30.0,
        )
        assert number.attributes["step"] == 0.5
        assert hass.states.get(duration).attributes["unit_of_measurement"] == "h"
        assert registry.async_get(temperature).entity_category is EntityCategory.CONFIG
        # The structure's generic sensor stays a disabled diagnostic one.
        mirror = registry.async_get(
            entity_id(hass, "sensor", "gateway-one:heatingCircuits:hc1:boostShortcut")
        )
        assert mirror.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert mirror.entity_category is EntityCategory.DIAGNOSTIC

        # AWAY-06: while the boost is off, a new temperature or duration is
        # kept for the next start and shown; nothing is sent.
        await _number(hass, TEMPERATURE, 22.5)
        await _number(hass, DURATION, 3)
        assert cloud.written() == []
        assert hass.states.get(temperature).state == "22.5"
        assert hass.states.get(duration).state == "3.0"
        # The controller's value again drops the preset.
        await _number(hass, DURATION, 2)
        await _number(hass, DURATION, 3)
        assert cloud.written() == []

        await _turn(hass, True)
        assert cloud.written() == [
            (
                "PUT",
                BOOST,
                [{"temperature": 22.5, "duration": 3, "mode": "on", "zones": [1, 2]}],
            )
        ]
        assert isinstance(cloud.written()[0][2][0]["duration"], int)
        assert sleeps[-2:] == [1.0, 2.0]
        assert hass.states.get(switch).state == "on"
        assert hass.states.get(temperature).state == "22.5"
        # Already running: nothing is sent.
        await _turn(hass, True)
        assert len(cloud.written()) == 1
        # While it runs, a change is refused and nothing is sent or kept.
        with pytest.raises(ServiceValidationError) as running:
            await _number(hass, TEMPERATURE, 25.0)
        assert running.value.translation_key == "rrc_boost_running"
        assert len(cloud.written()) == 1
        await _turn(hass, False)
        assert cloud.written()[-1] == (
            "PUT",
            BOOST,
            [{"temperature": 22.5, "duration": 3, "mode": "off", "zones": [1, 2]}],
        )
        # The preset was sent with the start: none is left.
        assert hass.states.get(temperature).state == "22.5"
        assert hass.states.get(duration).state == "3.0"
        # Already off: nothing is sent.
        await _turn(hass, False)
        assert len(cloud.written()) == 2
        # The single leaves are never written (W2-E1).
        assert {path for _method, path, _value in cloud.written()} == {BOOST}
        stats = coordinator.rrc_write_stats.snapshot("absence")
        assert stats["writes_sent"] == 2 and stats["writes_confirmed"] == 2
        # A value changed on the controller later shows: no preset is left.
        set_value(
            cloud,
            BOOST,
            [{**absence_tree()[BOOST].value[0], "temperature": 21.0}],
        )
        _clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(temperature).state == "21.0"
        assert hass.states.get(duration).state == "2.0"
    finally:
        await teardown(hass, entry, stack)


async def test_a_preset_waits_for_a_confirmed_start(hass: HomeAssistant) -> None:
    """AWAY-06: an unconfirmed start keeps the preset; a preset outside the
    current limits is not sent."""
    cloud = RrcCloud(absence_tree())
    entry, coordinator, clock, _sleeps, stack = await setup(hass, cloud)
    temperature = entity_id(hass, "number", TEMPERATURE)
    try:
        await _number(hass, TEMPERATURE, 24.0)
        cloud.ignore_writes.add(BOOST)
        with pytest.raises(HomeAssistantError) as unconfirmed:
            await _turn(hass, True)
        assert unconfirmed.value.translation_key == "rrc_write_pending_confirmation"
        assert cloud.written()[-1][2][0]["temperature"] == 24.0
        cloud.ignore_writes.clear()
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(temperature).state == "24.0"
        # The limits shrink meanwhile: the preset is refused at the start.
        cloud.tree[rrc_boost.BOOST_TEMPERATURE_PATH] = _leaf(
            rrc_boost.BOOST_TEMPERATURE_PATH, "C", 5.0, 23.0, 0.5
        )
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        with pytest.raises(ServiceValidationError) as outside:
            await _turn(hass, True)
        assert outside.value.translation_key == "write_validation_failed"
        assert cloud.calls["PUT", BOOST] == 1
    finally:
        await teardown(hass, entry, stack)


async def test_the_preset_survives_a_reload(hass: HomeAssistant) -> None:
    """The kept value is restored with the entity, not lost on a reload."""
    cloud = RrcCloud(absence_tree())
    entry, _coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    try:
        temperature = entity_id(hass, "number", TEMPERATURE)
        await _number(hass, TEMPERATURE, 23.5)
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        reloaded = entry.runtime_data.coordinators[0]

        async def no_sleep(_delay: float) -> None:
            return None

        reloaded.rrc_struct_writes._sleep = no_sleep
        assert hass.states.get(temperature).state == "23.5"
        await _turn(hass, True)
        assert cloud.written() == [
            (
                "PUT",
                BOOST,
                [{"temperature": 23.5, "duration": 2, "mode": "on", "zones": [1, 2]}],
            )
        ]
    finally:
        await teardown(hass, entry, stack)


async def test_a_start_leaves_out_deleted_zones(hass: HomeAssistant) -> None:
    """AWAY-08: zone 2 was deleted; the start heats only zone 1. Without
    ``allowedZones`` nothing starts."""
    cloud = RrcCloud(absence_tree())
    entry, coordinator, clock, _sleeps, stack = await setup(hass, cloud)
    try:
        zones = cloud.tree["/zones/list"]
        set_value(
            cloud,
            "/zones/list",
            [item for item in zones.value if item["id"] != 2],
        )
        clock.now += 2 * 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        await _turn(hass, True)
        assert cloud.written() == [
            (
                "PUT",
                BOOST,
                [{"temperature": 20.0, "duration": 2, "mode": "on", "zones": [1]}],
            )
        ]
        await _turn(hass, False)
        set_value(
            cloud,
            BOOST,
            [{"duration": 2, "mode": "off", "temperature": 20, "zones": [1]}],
        )
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        with pytest.raises(ServiceValidationError) as no_zones:
            await _turn(hass, True)
        assert no_zones.value.translation_key == "rrc_boost_no_zones"
        assert len(cloud.written()) == 2
    finally:
        await teardown(hass, entry, stack)


async def test_refusals_send_nothing(hass: HomeAssistant) -> None:
    cloud = RrcCloud(absence_tree())
    entry, coordinator, clock, _sleeps, stack = await setup(hass, cloud)
    try:
        # Off the grid: not kept (the numbers send nothing, AWAY-06).
        with pytest.raises(ServiceValidationError) as off_grid:
            await _number(hass, TEMPERATURE, 22.2)
        assert off_grid.value.translation_key == "write_validation_failed"
        assert hass.states.get(entity_id(hass, "number", TEMPERATURE)).state == ("20.0")
        # The zones were cleared in the app meanwhile: the fresh read decides.
        set_value(cloud, BOOST, variants()["boost_without_zones"])
        with pytest.raises(ServiceValidationError) as no_zones:
            await _turn(hass, True)
        assert no_zones.value.translation_key == "rrc_boost_no_zones"
        # The mode changed, seen only by the fresh read (S5, AWAY-09).
        set_value(cloud, BOOST, variants()["boost_on"])
        with pytest.raises(ServiceValidationError) as changed:
            await _turn(hass, False)
        assert changed.value.translation_key == "rrc_boost_changed"
        # Unknown fields: shown, never written.
        set_value(cloud, BOOST, variants()["boost_extra_field"])
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(entity_id(hass, "switch", SWITCH)).state == "off"
        with pytest.raises(ServiceValidationError) as shape:
            await _turn(hass, True)
        assert shape.value.translation_key == "rrc_boost_unsupported_shape"
        # S8: used but unavailable.
        cloud.tree[BOOST] = _struct(
            absence_tree()[BOOST].value, used="true", available="false"
        )
        with pytest.raises(ServiceValidationError) as locked:
            await _turn(hass, True)
        assert locked.value.translation_key == "rrc_boost_locked"
        assert cloud.written() == []
        reasons = coordinator.rrc_write_stats.snapshot("absence")["rejection_reasons"]
        assert reasons == {
            "rrc_boost_changed": 1,
            "rrc_boost_no_zones": 1,
            "rrc_boost_unsupported_shape": 1,
            "locked": 1,
        }
    finally:
        await teardown(hass, entry, stack)


async def test_timeout_after_the_put_reads_only_and_counts_late(
    hass: HomeAssistant,
) -> None:
    """S6/S7: the PUT times out after it was applied; the controller shows
    it only after the read-back. No second PUT; the next regular poll
    confirms it late (CT-C07)."""
    cloud = RrcCloud(absence_tree())
    cloud.timeout_after_write[BOOST] = True
    cloud.adopt_after[BOOST] = 5
    entry, coordinator, clock, sleeps, stack = await setup(hass, cloud)
    switch = entity_id(hass, "switch", SWITCH)
    try:
        with pytest.raises(HomeAssistantError) as unconfirmed:
            await _turn(hass, True)
        assert unconfirmed.value.translation_key == "rrc_write_pending_confirmation"
        assert cloud.calls["PUT", BOOST] == 1
        assert sleeps[-4:] == [1.0, 2.0, 4.0, 8.0]
        # The reading is stale until the next poll.
        assert hass.states.get(switch).state == "unavailable"
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(switch).state == "on"
        stats = coordinator.rrc_write_stats.snapshot("absence")
        assert stats["writes_unconfirmed"] == 1 and stats["late_confirmed"] == 1
        assert cloud.calls["PUT", BOOST] == 1
    finally:
        await teardown(hass, entry, stack)


async def test_a_put_without_effect_is_not_confirmed(hass: HomeAssistant) -> None:
    cloud = RrcCloud(absence_tree())
    cloud.ignore_writes.add(BOOST)
    entry, _coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    try:
        with pytest.raises(HomeAssistantError) as unconfirmed:
            await _turn(hass, True)
        assert unconfirmed.value.translation_key == "rrc_write_pending_confirmation"
        assert cloud.calls["PUT", BOOST] == 1
    finally:
        await teardown(hass, entry, stack)


@pytest.mark.parametrize(
    ("failure", "key"),
    [("forbidden", "write_failed"), ("rate_limit", "write_rate_limited")],
)
async def test_403_and_429_end_the_write(hass: HomeAssistant, failure, key) -> None:
    cloud = RrcCloud(absence_tree())
    entry, coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    try:
        if failure == "forbidden":
            cloud.forbidden.add(BOOST)
            # The fresh read is refused already.
        else:
            cloud.rate_limit_writes[BOOST] = 120
        with pytest.raises(HomeAssistantError) as failed:
            await _turn(hass, True)
        assert failed.value.translation_key == key
        assert cloud.calls["PUT", BOOST] <= 1
        if failure == "rate_limit":
            assert cloud.calls["PUT", BOOST] == 1
            assert coordinator._cloud_backoff_until > 0
    finally:
        await teardown(hass, entry, stack)


async def test_no_numbers_without_limits(
    hass: HomeAssistant,
) -> None:
    tree = absence_tree()
    del tree[rrc_boost.BOOST_TEMPERATURE_PATH]
    tree[rrc_boost.BOOST_DURATION_PATH] = _leaf(
        rrc_boost.BOOST_DURATION_PATH, "hour", 1.0, 6553.5
    )
    cloud = RrcCloud(tree)
    entry, _coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    try:
        assert entity_id(hass, "switch", SWITCH) is not None
        assert entity_id(hass, "number", TEMPERATURE) is None
        assert entity_id(hass, "number", DURATION) is None
    finally:
        await teardown(hass, entry, stack)


def test_the_builders_need_a_room_controller() -> None:
    """S11 in the builders themselves, not only in the tracker."""
    tree = absence_tree()
    for build in (build_calendar, build_switch, build_numbers):
        assert build(tree, RRC_CONTEXT)
        assert build(tree, GENERIC_CONTEXT) == ()


async def test_no_boost_entities_on_other_gateways(hass: HomeAssistant) -> None:
    """S11: a heat pump gateway with the same structure gets no boost
    entity."""
    ems_tree = {
        path: resource
        for path, resource in absence_tree().items()
        if not path.startswith(("/zones", "/programs", "/devices", "/events"))
        and path != "/gateway/productType"
    }
    cloud = RrcCloud(ems_tree, forbidden=set(), missing_as_500=False)
    entry, _coordinator, _clock, _sleeps, stack = await setup(hass, cloud, EMS_GATEWAY)
    try:
        unique_ids = {
            item.unique_id
            for item in er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
        }
        assert not {uid for uid in unique_ids if "boostShortcut" in uid}
        # The structure is read there, only no entity follows from it.
        assert BOOST in cloud.all_requested()
    finally:
        await teardown(hass, entry, stack)
