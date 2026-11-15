"""The plant's recordings in Home Assistant's recorder (V3, rules 6, 18-20).

The hourly values become external statistics of their own: energy as
running sums in kWh, temperatures as means. A new import state continues
the sum of an earlier import; deleting removes the statistics; switched off
nothing more is imported. The counters of the integration stand in where
the plant has no recording (V1). The recorder is set up before Home
Assistant, so this module has no module-wide fixtures.
"""

from __future__ import annotations

import itertools
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


def _old_recorder(hass, monkeypatch) -> list[list[str]]:
    """The recorder of Home Assistant before 2026.11: it clears whole
    statistics only. Returns the cleared statistic IDs."""
    from homeassistant.components.recorder import get_instance

    instance = get_instance(hass)
    real = instance.async_clear_statistics
    cleared: list[list[str]] = []

    def clear(statistic_ids, *, on_done=None):
        cleared.append(list(statistic_ids))
        real(statistic_ids, on_done=on_done)

    monkeypatch.setattr(instance, "async_clear_statistics", clear)
    return cleared


def _correct(rec, changed_hour: float, value: float = 5.0) -> None:
    """From now on the plant answers ``value`` for one hour of the heat."""
    driver = rec._test_coordinator.local_selection.running_sources()[0]
    original = driver.read_explicit

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
                if 0 <= index < len(samples):
                    samples[index] = RecordingSample(value, 1)
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


def _sums(rows) -> dict[float, float]:
    return {row["start"]: row["sum"] for row in rows}


@pytest.mark.parametrize("ranges", [True, False], ids=["2026.11", "older"])
async def test_a_correction_writes_the_hours_again(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass: HomeAssistant,
    monkeypatch,
    ranges,
) -> None:
    """Rule 6 end to end: the changed hour and all later sums; on a recorder
    that clears whole statistics only, the rows are written over, nothing
    is cleared, and no hour goes missing in the runs after."""
    cleared = [] if ranges else _old_recorder(hass, monkeypatch)
    clock = Clock(local(2026, 11, 14, 10) + timedelta(minutes=20))
    _, rec = await _importing(hass, clock)
    await rec.async_run("start")
    clock.now += timedelta(hours=1)
    await rec.async_run("hourly")
    heat = statistic_id(rec.digest, BY_KEY["total_heat"])
    before = _sums((await _rows(hass, heat))[heat])
    changed_hour = local(2026, 11, 14, 5).astimezone(UTC).timestamp()
    _correct(rec, changed_hour)
    for _ in range(6):
        clock.now += timedelta(hours=1)
        await rec.async_run("hourly")
        assert not rec.runs[-1].get("failed")
        assert "import_failed_series" not in rec.runs[-1]
    after = _sums((await _rows(hass, heat))[heat])
    assert after[changed_hour] == pytest.approx(before[changed_hour] + 3.0)
    last = max(before)
    assert after[last] == pytest.approx(before[last] + 3.0)
    # Every hour since the hours began, none missing, and each hour adds
    # the plant's 2 kWh (5 kWh for the corrected one).
    first_hour = local(2026, 11, 9).astimezone(UTC).timestamp()
    hourly = sorted(start for start in after if start >= first_hour)
    assert hourly == [first_hour + 3600 * i for i in range(len(hourly))]
    assert hourly[-1] > last
    steps = {
        start: after[start] - after[previous]
        for previous, start in itertools.pairwise(hourly)
    }
    assert steps[changed_hour] == pytest.approx(5.0)
    assert all(
        step == pytest.approx(2.0)
        for start, step in steps.items()
        if start != changed_hour
    )
    assert cleared == []


async def test_a_failed_write_is_not_marked_as_imported(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass: HomeAssistant,
    monkeypatch,
) -> None:
    """The import position moves on only once the rows are handed to the
    recorder: a failure while importing a correction is tried again."""
    from homeassistant.components.recorder import get_instance

    clock = Clock(local(2026, 11, 14, 10) + timedelta(minutes=20))
    _, rec = await _importing(hass, clock)
    await rec.async_run("start")
    clock.now += timedelta(hours=1)
    await rec.async_run("hourly")
    heat = statistic_id(rec.digest, BY_KEY["total_heat"])
    before = _sums((await _rows(hass, heat))[heat])
    changed_hour = local(2026, 11, 14, 5).astimezone(UTC).timestamp()
    _correct(rec, changed_hour)
    instance = get_instance(hass)
    real = instance.async_clear_statistics
    position = rec.ledgers["total_heat"].imp["next"]

    def broken(statistic_ids, *, on_done=None, start_time=None, end_time=None):
        raise RuntimeError("clear failed")

    monkeypatch.setattr(instance, "async_clear_statistics", broken)
    positions = []
    for _ in range(3):
        clock.now += timedelta(hours=1)
        await rec.async_run("hourly")
        positions.append(rec.ledgers["total_heat"].imp["next"])
    # Confirmed by the second read, the corrected hour is imported again;
    # that fails each time. The other series went on, the heat stayed.
    assert [run.get("import_failed_series") for run in rec.runs][-3:] == [None, 1, 1]
    assert rec.runs[-1]["imported_rows"] > 0
    assert positions[0] > position and positions[1] == positions[2] == positions[0]
    assert not rec.runs[-1].get("failed")
    monkeypatch.setattr(instance, "async_clear_statistics", real)
    for _ in range(2):
        clock.now += timedelta(hours=1)
        await rec.async_run("hourly")
    after = _sums((await _rows(hass, heat))[heat])
    assert after[changed_hour] == pytest.approx(before[changed_hour] + 3.0)
    last = max(before)
    assert after[last] == pytest.approx(before[last] + 3.0)
    first_hour = local(2026, 11, 9).astimezone(UTC).timestamp()
    hourly = sorted(start for start in after if start >= first_hour)
    assert hourly == [first_hour + 3600 * i for i in range(len(hourly))]


async def test_a_failing_series_leaves_the_others_and_the_values(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass: HomeAssistant,
    monkeypatch,
) -> None:
    from custom_components.bosch_buderus_heating.recordings import manager as module

    real = module.statistic_name

    def name(language, series):
        if series.key == "outdoor_temperature":
            raise ValueError("broken")
        return real(language, series)

    monkeypatch.setattr(module, "statistic_name", name)
    clock = Clock(local(2026, 11, 14, 10) + timedelta(minutes=20))
    _, rec = await _importing(hass, clock)
    shown = key("total_heat", Period.CURRENT_MONTH)
    rec.request(shown)
    await rec.async_run("start")
    report = rec.runs[-1]
    assert report["import_failed_series"] == 1
    assert not report.get("failed")
    heat = statistic_id(rec.digest, BY_KEY["total_heat"])
    outdoor = statistic_id(rec.digest, BY_KEY["outdoor_temperature"])
    rows = await _rows(hass, heat, outdoor)
    assert rows.get(heat) and not rows.get(outdoor)
    assert not rec.ledgers["outdoor_temperature"].imp["rows"]
    assert shown in rec.published


@pytest.mark.parametrize(
    ("ranges", "earlier_day"),
    [(True, False), (True, True), (False, False), (False, True)],
    ids=["2026.11", "2026.11-earlier-day", "older", "older-earlier-day"],
)
async def test_a_new_import_replaces_the_hours_of_an_earlier_one(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass: HomeAssistant,
    monkeypatch,
    ranges,
    earlier_day,
) -> None:
    """Rule 18: a day the plant now only has as a day is one row at its
    midnight; the earlier import's hours of that day leave no energy behind
    (gone, or neutral where the recorder cannot clear a range)."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMeanType,
        StatisticMetaData,
    )
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
    )

    from custom_components.bosch_buderus_heating.recordings.manager import (
        clears_ranges,
    )

    cleared = None if ranges else _old_recorder(hass, monkeypatch)
    # Home Assistant before 2026.11 clears whole statistics only.
    ranges = clears_ranges(get_instance(hass))
    clock = Clock(local(2026, 11, 14, 10) + timedelta(minutes=20))
    _, rec = await _importing(hass, clock)
    heat = statistic_id(rec.digest, BY_KEY["total_heat"])
    base = 500.0 if earlier_day else 0.0
    earlier = (
        [StatisticData(start=local(2026, 10, 31).astimezone(UTC), sum=base)]
        if earlier_day
        else []
    )
    total = base
    for hour in range(24):  # the earlier import still had the hours
        total += 1.0
        earlier.append(
            StatisticData(
                start=local(2026, 11, 1, hour).astimezone(UTC), sum=total, state=total
            )
        )
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
        earlier,
    )
    await _rows(hass, heat)
    await rec.async_run("start")
    rows = _sums((await _rows(hass, heat))[heat])
    midnight = local(2026, 11, 1).astimezone(UTC).timestamp()
    assert rows[midnight] == pytest.approx(base + 48.0)
    day = [start for start in rows if midnight < start < midnight + 24 * 3600]
    assert all(rows[start] == pytest.approx(base + 48.0) for start in day)
    assert bool(day) is (not ranges and earlier_day)
    sums = [rows[start] for start in sorted(rows)]
    assert sums == sorted(sums)
    if cleared is not None:
        assert cleared == ([] if earlier_day else [[heat]])


async def test_switched_off_while_a_run_waits_writes_nothing(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass: HomeAssistant,
    monkeypatch,
) -> None:
    """Switched off (or deleted) while a run waits for the recorder: the
    run neither writes nor marks anything as imported."""
    from homeassistant.components.recorder import get_instance

    clock = Clock(local(2026, 11, 14, 10) + timedelta(minutes=20))
    entry, rec = await _importing(hass, clock)
    instance = get_instance(hass)
    real = instance.async_add_executor_job

    async def job(target, *args):
        hass.config_entries.async_update_entry(entry, options={OPTION_IMPORT: False})
        return await real(target, *args)

    monkeypatch.setattr(instance, "async_add_executor_job", job)
    await rec.async_run("start")
    heat = statistic_id(rec.digest, BY_KEY["total_heat"])
    assert heat not in await _rows(hass, heat)
    assert not any(ledger.importing for ledger in rec.ledgers.values())
    assert rec.runs[-1]["imported_rows"] == 0


async def test_deleting_right_after_the_setup_finds_the_statistics(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass: HomeAssistant,
) -> None:
    """Before the start has finished, the digest is worked out on demand."""
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMeanType,
        StatisticMetaData,
    )
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
    )

    from custom_components.bosch_buderus_heating.recordings.manager import (
        RecordingsManager,
    )
    from custom_components.bosch_buderus_heating.rrc_energy_statistics import (
        async_gateway_digest,
    )

    entry = MockConfigEntry(domain=DOMAIN, data={}, options={OPTION_IMPORT: False})
    entry.add_to_hass(hass)
    heat = statistic_id(await async_gateway_digest(hass, GW), BY_KEY["total_heat"])
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
        [StatisticData(start=local(2026, 11, 1).astimezone(UTC), sum=1.0)],
    )
    await _rows(hass, heat)
    rec = RecordingsManager(hass, entry, GW)
    assert rec.digest == ""
    assert await rec.async_delete_statistics() == 1
    assert heat not in await _rows(hass, heat)


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
