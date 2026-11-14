"""The plant's recordings in Home Assistant's recorder (V3, rules 6, 18-20).

The hourly values become external statistics of their own: energy as
running sums in kWh, temperatures as means. A new import state continues
the sum of an earlier import; deleting removes the statistics; switched off
nothing more is imported. The counters of the integration stand in where
the plant has no recording (V1). The recorder is set up before Home
Assistant, so this module has no module-wide fixtures.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.recordings import Period
from custom_components.bosch_buderus_heating.recordings.manager import (
    OPTION_IMPORT,
    statistic_id,
)
from custom_components.bosch_buderus_heating.recordings.measures import CALCULATED
from custom_components.bosch_buderus_heating.recordings.series import BY_KEY
from tests.ha_test_helpers import MockConfigEntry
from tests.test_recordings_core import local
from tests.test_recordings_manager import GW, Clock, Driver, key, manager
from tests.test_start_counters import (
    recorder_runtime_annotations,  # noqa: F401 - fixture
)


async def _rows(hass, *ids, period="hour", types=("sum", "mean", "state")):
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    from tests.ha_recorder_helpers import async_wait_recording_done

    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    return await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 1, 1, tzinfo=UTC),
        None,
        set(ids),
        period,
        None,
        set(types),
    )


async def _importing(hass, clock, **kwargs):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={OPTION_IMPORT: True})
    rec = await manager(hass, clock, [Driver(clock)], entry=entry, **kwargs)
    return entry, rec


async def test_the_hourly_values_become_sums_and_means(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass: HomeAssistant,
) -> None:
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import get_metadata

    clock = Clock(local(2026, 11, 14, 10) + timedelta(minutes=20))
    _, rec = await _importing(hass, clock)
    await rec.async_run("start")
    heat = statistic_id(rec.digest, BY_KEY["total_heat"])
    outdoor = statistic_id(rec.digest, BY_KEY["outdoor_temperature"])
    rows = await _rows(hass, heat, outdoor)
    # Days 1 to 8 November as days, the hours ended more than 48 h ago.
    first = [datetime.fromtimestamp(row["start"], UTC) for row in rows[heat][:2]]
    assert first == [
        local(2026, 11, 1).astimezone(UTC),
        local(2026, 11, 2).astimezone(UTC),
    ]
    sums = [row["sum"] for row in rows[heat]]
    assert sums == sorted(sums) and sums[0] == pytest.approx(48.0)
    assert all(row["mean"] == pytest.approx(5.0) for row in rows[outdoor])
    metadata = await get_instance(hass).async_add_executor_job(get_metadata, hass)
    assert metadata[heat][1]["unit_of_measurement"] == "kWh"
    assert metadata[heat][1]["has_sum"] is True
    assert metadata[outdoor][1]["unit_of_measurement"] == "°C"
    assert "system recording" in metadata[heat][1]["name"]
    assert GW not in json.dumps({k: v[1] for k, v in metadata.items()}, default=str)
    # The next run adds the hours confirmed since, nothing twice.
    count = len(rows[heat])
    clock.now += timedelta(hours=1)
    await rec.async_run("hourly")
    later = (await _rows(hass, heat))[heat]
    assert len(later) > count
    starts = [row["start"] for row in later]
    assert len(starts) == len(set(starts))
    assert [row["sum"] for row in later] == sorted(row["sum"] for row in later)


async def test_a_new_import_state_continues_the_recorded_sum(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass: HomeAssistant,
) -> None:
    """Rule 18: the same statistic ID; rows from the first recorded day are
    written again on top of the sum before it."""
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMeanType,
        StatisticMetaData,
    )
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
    )

    clock = Clock(local(2026, 11, 14, 10) + timedelta(minutes=20))
    _, rec = await _importing(hass, clock)
    heat = statistic_id(rec.digest, BY_KEY["total_heat"])
    async_add_external_statistics(
        hass,
        StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name="earlier",
            source=DOMAIN,
            statistic_id=heat,
            unit_class="energy",
            unit_of_measurement="kWh",
        ),
        [
            StatisticData(start=local(2026, 10, 30).astimezone(UTC), sum=500.0),
            # An earlier import of a day the plant still has.
            StatisticData(start=local(2026, 11, 1).astimezone(UTC), sum=999.0),
        ],
    )
    await _rows(hass, heat)
    await rec.async_run("start")
    rows = (await _rows(hass, heat))[heat]
    assert rows[0]["sum"] == 500.0
    assert rows[1]["start"] == local(2026, 11, 1).astimezone(UTC).timestamp()
    assert rows[1]["sum"] == pytest.approx(548.0)


async def test_off_imports_nothing_on_goes_on_and_delete_removes_everything(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass: HomeAssistant,
) -> None:
    """Rules 19 and 20."""
    clock = Clock(local(2026, 11, 14, 10) + timedelta(minutes=20))
    entry, rec = await _importing(hass, clock)
    rec.request(key("total_heat", Period.YESTERDAY))
    await rec.async_run("start")
    heat = statistic_id(rec.digest, BY_KEY["total_heat"])
    count = len((await _rows(hass, heat))[heat])
    hass.config_entries.async_update_entry(entry, options={OPTION_IMPORT: False})
    clock.now += timedelta(hours=2)
    await rec.async_run("hourly")
    assert len((await _rows(hass, heat))[heat]) == count
    hass.config_entries.async_update_entry(entry, options={OPTION_IMPORT: True})
    await rec.async_run("hourly")
    rows = (await _rows(hass, heat))[heat]
    assert len(rows) > count
    sums = [row["sum"] for row in rows]
    assert sums == sorted(sums)
    assert await rec.async_delete_statistics() >= 1
    assert heat not in await _rows(hass, heat)
    assert not any(ledger.importing for ledger in rec.ledgers.values())


async def test_a_correction_writes_the_hours_again(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass: HomeAssistant,
) -> None:
    """Rule 6 end to end: the changed hour and all later sums."""
    clock = Clock(local(2026, 11, 14, 10) + timedelta(minutes=20))
    _, rec = await _importing(hass, clock)
    await rec.async_run("start")
    clock.now += timedelta(hours=1)
    await rec.async_run("hourly")
    heat = statistic_id(rec.digest, BY_KEY["total_heat"])
    before = {row["start"]: row["sum"] for row in (await _rows(hass, heat))[heat]}
    driver = rec._test_coordinator.local_selection.running_sources()[0]
    original = driver.read_explicit
    changed_hour = local(2026, 11, 14, 5).astimezone(UTC).timestamp()

    async def corrected(request):
        result = await original(request)
        data = result.observation.data if result.observation else None
        if data is None or request.path != BY_KEY["total_heat"].path:
            return result
        from types import SimpleNamespace

        from custom_components.bosch_buderus_heating.local_api.models import (
            RecordingGroup,
            RecordingSample,
        )

        groups = []
        for group in data.groups:
            samples = list(group.samples)
            if group.sample_rate == "PT1H":
                index = int((changed_hour - group.start.timestamp()) // 3600)
                samples[index] = RecordingSample(5.0, 1)
            groups.append(
                RecordingGroup(
                    group.sample_rate, group.start, group.end, tuple(samples)
                )
            )
        return replace(
            result,
            observation=SimpleNamespace(
                data=type(data)(
                    data.resource, data.recording_type, data.unit, tuple(groups), ()
                )
            ),
        )

    driver.read_explicit = corrected
    for _ in range(2):
        clock.now += timedelta(hours=1)
        await rec.async_run("hourly")
    after = {row["start"]: row["sum"] for row in (await _rows(hass, heat))[heat]}
    assert after[changed_hour] == pytest.approx(before[changed_hour] + 3.0)
    last = max(before)
    assert after[last] == pytest.approx(before[last] + 3.0)
    sums = [after[start] for start in sorted(after)]
    assert sums == sorted(sums)


async def test_the_counters_stand_in_and_compare(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass: HomeAssistant,
) -> None:
    """V1: the heat per mode from the counter's statistics first; the source
    and the counter entity are named."""
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMeanType,
        StatisticMetaData,
    )
    from homeassistant.components.recorder.statistics import async_import_statistics

    clock = Clock(local(2026, 11, 14, 10) + timedelta(minutes=20))
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    counter = registry.async_get_or_create(
        "sensor", DOMAIN, f"{GW}:gateway:heating_heat_produced", config_entry=entry
    )
    async_import_statistics(
        hass,
        StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=None,
            source="recorder",
            statistic_id=counter.entity_id,
            unit_class="energy",
            unit_of_measurement="kWh",
        ),
        [
            StatisticData(
                start=local(2026, 11, 12, 23).astimezone(UTC), sum=100.0, state=100.0
            ),
            StatisticData(
                start=local(2026, 11, 13, 23).astimezone(UTC), sum=140.0, state=140.0
            ),
        ],
    )
    await _rows(hass, counter.entity_id)
    rec = await manager(hass, clock, [Driver(clock)], entry=entry)
    calculated = key("ch_heat", Period.YESTERDAY, CALCULATED)
    recorded = key("ch_heat", Period.YESTERDAY)
    rec.request(calculated)
    rec.request(recorded)
    await rec.async_run("start")
    clock.now += timedelta(hours=2)
    await rec.async_run("hourly")
    specs = {spec.key: spec for spec in rec.specs()}
    shown = rec.shown(specs[calculated])
    assert shown is not None and shown.source == "counters"
    assert shown.value == pytest.approx(40.0)
    assert rec.counter_entities["ch_heat"] == counter.entity_id
    compared = rec.shown(specs[recorded])
    assert compared is not None and compared.source == "local_recording"
    assert compared.value == pytest.approx(36.0)
    assert shown.start == compared.start == date(2026, 11, 13)
