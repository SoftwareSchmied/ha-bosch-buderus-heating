"""The restored compressor speed continues its long-term statistics.

Statistics belong to the entity ID. The local-only entity restored at setup
gets its old entity ID back, so the recorder adds to the same statistics;
a cloud entity in use that continues as the local-only entity keeps its
entity ID and statistics the same way. The recorder is set up before Home
Assistant, so this module has no module-wide fixtures.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.setup import async_setup_component

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.local_only_heal import cloud_unique_id
from custom_components.bosch_buderus_heating.local_only_sensor import (
    local_only_unique_id,
)
from tests.test_local_only import by_path, coordinator_with, number, track
from tests.test_local_only_heal import (
    UNIT_GATEWAY,
    cloud_speed,
    removed_local_speed,
    speed_resource,
)
from tests.test_local_only_upgrade import SPEED
from tests.test_start_counters import (
    recorder_runtime_annotations,  # noqa: F401 - fixture
)


def _platform(hass, entry) -> EntityPlatform:
    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain="sensor",
        platform_name=DOMAIN,
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    platform.config_entry = entry
    platform.async_prepare()
    return platform


async def test_the_restored_entity_continues_its_statistics(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass,
    freezer,
) -> None:
    from homeassistant.components.recorder.statistics import (
        get_metadata,
        statistics_during_period,
        validate_statistics,
    )

    from tests.ha_recorder_helpers import (
        async_wait_recording_done,
        do_adhoc_statistics,
        get_start_time,
    )

    start = get_start_time(datetime.now(UTC)) + timedelta(minutes=5)
    freezer.move_to(start)
    assert await async_setup_component(hass, "sensor", {})
    registry = er.async_get(hass)
    # 0.9.1: the local-only entity records its statistics.
    before = coordinator_with(
        hass, [number(SPEED, 38, unit="%", kind="integerValue")], speed_resource()
    )
    entry = before.config_entry
    # Disabled by default; this user enabled it.
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        local_only_unique_id(UNIT_GATEWAY, SPEED),
        config_entry=entry,
        suggested_object_id="heat_pump_compressor_speed",
        disabled_by=None,
    )
    first = by_path(track(before))[SPEED]
    platform = _platform(hass, entry)
    await platform.async_add_entities([first])
    old = first.entity_id
    registry.async_update_entity(old, name="Verdichter")
    await async_wait_recording_done(hass)
    freezer.move_to(start + timedelta(minutes=5))
    do_adhoc_statistics(hass, start=start)
    await async_wait_recording_done(hass)
    (metadata_id, _) = (
        await recorder_mock.async_add_executor_job(
            lambda: get_metadata(hass, statistic_ids={old})
        )
    )[old]
    # The faulty candidate: the entry is removed, a cloud entry is added.
    await first.async_remove(force_remove=True)
    registry.async_remove(old)
    cloud_speed(registry, entry, UNIT_GATEWAY)
    await before.async_shutdown()
    # The next setup restores the local-only entity under its entity ID.
    freezer.move_to(start + timedelta(minutes=6))
    after = coordinator_with(
        hass, [number(SPEED, 44, unit="%", kind="integerValue")], speed_resource(44)
    )
    after.config_entry = entry
    second = by_path(track(after))[SPEED]
    await platform.async_add_entities([second])
    assert second.entity_id == old
    assert registry.async_get(old).name == "Verdichter"
    await async_wait_recording_done(hass)
    freezer.move_to(start + timedelta(minutes=10))
    do_adhoc_statistics(hass, start=start + timedelta(minutes=5))
    await async_wait_recording_done(hass)
    rows = await recorder_mock.async_add_executor_job(
        lambda: statistics_during_period(
            hass, start, None, {old}, "5minute", None, {"max"}
        )
    )
    # The second period already has the restored entity's reading.
    assert [row["max"] for row in rows[old]] == [38.0, 44.0]
    metadata = await recorder_mock.async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids={old})
    )
    assert metadata[old][0] == metadata_id  # the same statistics
    assert metadata[old][1]["unit_of_measurement"] == "%"
    issues = await recorder_mock.async_add_executor_job(validate_statistics, hass)
    assert old not in issues
    await after.async_shutdown()


class _CloudSpeed(SensorEntity):
    """Stands in for the cloud's sensor of the compressor speed."""

    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_should_poll = False

    def __init__(self, value: int) -> None:
        self._attr_unique_id = cloud_unique_id(UNIT_GATEWAY, SPEED)
        self._attr_native_value = value


async def test_a_cloud_entity_in_use_continues_its_statistics(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass,
    freezer,
) -> None:
    """The user renamed the cloud entity's ID and it has statistics; the
    local-only entity that continues it adds to the same statistics."""
    from homeassistant.components.recorder.statistics import (
        get_metadata,
        statistics_during_period,
        validate_statistics,
    )

    from tests.ha_recorder_helpers import (
        async_wait_recording_done,
        do_adhoc_statistics,
        get_start_time,
    )

    start = get_start_time(datetime.now(UTC)) + timedelta(minutes=5)
    freezer.move_to(start)
    assert await async_setup_component(hass, "sensor", {})
    registry = er.async_get(hass)
    before = coordinator_with(hass, [], speed_resource())
    entry = before.config_entry
    # The local-only entry was removed earlier (with the local access).
    removed_local_speed(registry, entry, UNIT_GATEWAY)
    platform = _platform(hass, entry)
    cloud = _CloudSpeed(38)
    await platform.async_add_entities([cloud])
    mine = registry.async_update_entity(
        cloud.entity_id, new_entity_id="sensor.my_compressor_speed"
    ).entity_id
    await async_wait_recording_done(hass)
    freezer.move_to(start + timedelta(minutes=5))
    do_adhoc_statistics(hass, start=start)
    await async_wait_recording_done(hass)
    (metadata_id, _) = (
        await recorder_mock.async_add_executor_job(
            lambda: get_metadata(hass, statistic_ids={mine})
        )
    )[mine]
    await cloud.async_remove(force_remove=True)
    await before.async_shutdown()
    # The local access is set up again: the next setup continues the entity.
    freezer.move_to(start + timedelta(minutes=6))
    after = coordinator_with(
        hass, [number(SPEED, 44, unit="%", kind="integerValue")], speed_resource(44)
    )
    after.config_entry = entry
    second = by_path(track(after))[SPEED]
    await platform.async_add_entities([second])
    assert second.entity_id == mine
    assert registry.async_get(mine).unique_id == local_only_unique_id(
        UNIT_GATEWAY, SPEED
    )
    await async_wait_recording_done(hass)
    freezer.move_to(start + timedelta(minutes=10))
    do_adhoc_statistics(hass, start=start + timedelta(minutes=5))
    await async_wait_recording_done(hass)
    rows = await recorder_mock.async_add_executor_job(
        lambda: statistics_during_period(
            hass, start, None, {mine}, "5minute", None, {"max"}
        )
    )
    assert [row["max"] for row in rows[mine]] == [38.0, 44.0]
    metadata = await recorder_mock.async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids={mine})
    )
    assert metadata[mine][0] == metadata_id  # the same statistics
    issues = await recorder_mock.async_add_executor_job(validate_statistics, hass)
    assert mine not in issues
    await after.async_shutdown()
