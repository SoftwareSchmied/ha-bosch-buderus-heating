"""W5 (CT-G05): the energy history in Home Assistant's recorder.

A real setup of an RRC 2.0 room controller (``ct200-shapes.json``) with a
recorder: the history days become hourly rows at local midnight, energy as
running sums in kWh, the outdoor temperature as a mean. Expected, untested
on real hardware. The recorder is set up before Home Assistant, so this
module has no module-wide fixtures.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.bosch_buderus_heating.rrc_energy_statistics import (
    START_DELAY,
    day_start,
    history_of,
)
from tests.test_rrc_energy_statistics import (
    GW,
    advance,
    day_record,
    history_cloud,
    legacy_setup_path,  # noqa: F401 - fixture
    sid,
    start,
    stop,
)
from tests.test_start_counters import (
    recorder_runtime_annotations,  # noqa: F401 - fixture
)


async def test_the_history_becomes_sums_and_means_in_the_recorder(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    enable_custom_integrations,
    hass: HomeAssistant,
    freezer,
) -> None:
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import (
        get_metadata,
        statistics_during_period,
    )

    from tests.ha_recorder_helpers import async_wait_recording_done

    await hass.config.async_update(time_zone="Europe/Berlin")
    freezer.move_to(datetime(2026, 10, 28, 7, 0, tzinfo=UTC))
    # Across the end of summer time (25 October) and a gap (27 October).
    items = [
        day_record(date(2026, 10, 24), eCh=2.0, gCh=1.0, T=8.0),
        day_record(date(2026, 10, 25), eCh=3.0, gCh=1.5, T=6.0),
        day_record(date(2026, 10, 26), eCh=4.0, gCh=0.5, T=-1.0),
        day_record(date(2026, 10, 28), eCh=9.0, gCh=9.0, T=0.0),  # today
    ]
    cloud = history_cloud(items, page=10)
    entry, stack = await start(hass, cloud)
    try:
        await advance(hass, freezer, START_DELAY + timedelta(minutes=1))
        # The cycle is a background task; it reads the recorder first.
        await hass.async_block_till_done(wait_background_tasks=True)
        await async_wait_recording_done(hass)
        heating = await sid(hass, "heat_pump_electricity_heating")
        gas = await sid(hass, "gas_heating")
        temperature = await sid(hass, "outdoor_temperature")
        rows = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            datetime(2026, 10, 20, tzinfo=UTC),
            None,
            {heating, gas, temperature},
            "hour",
            None,
            {"sum", "mean", "state"},
        )
        starts = [dt_util.utc_from_timestamp(row["start"]) for row in rows[heating]]
        assert starts == [
            datetime(2026, 10, 23, 22, 0, tzinfo=UTC),
            datetime(2026, 10, 24, 22, 0, tzinfo=UTC),
            datetime(2026, 10, 25, 23, 0, tzinfo=UTC),
        ]
        assert [row["sum"] for row in rows[heating]] == [2.0, 5.0, 9.0]
        assert [row["sum"] for row in rows[gas]] == [1.0, 2.5, 3.0]
        assert [row["mean"] for row in rows[temperature]] == [8.0, 6.0, -1.0]
        metadata = await get_instance(hass).async_add_executor_job(get_metadata, hass)
        assert metadata[heating][1]["unit_of_measurement"] == "kWh"
        assert metadata[heating][1]["has_sum"] is True
        assert "expected" in metadata[heating][1]["name"]
        assert metadata[temperature][1]["unit_of_measurement"] == "°C"
        assert GW not in json.dumps(
            {key: value[1] for key, value in metadata.items()}, default=str
        )
        # Today is never imported: it is not complete yet.
        assert history_of(entry.runtime_data.coordinators[0]).state.last_day == (
            date(2026, 10, 26)
        )
    finally:
        await stop(hass, entry, stack)


async def test_an_empty_import_state_continues_the_recorded_sums(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    enable_custom_integrations,
    hass: HomeAssistant,
    freezer,
) -> None:
    """SYSTEM-05: rows of an earlier import whose state is gone (a new entry,
    an unreadable store) are continued, never run backwards or rewritten."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMeanType,
        StatisticMetaData,
    )
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
        statistics_during_period,
    )

    from tests.ha_recorder_helpers import async_wait_recording_done

    await hass.config.async_update(time_zone="Europe/Berlin")
    freezer.move_to(datetime(2026, 10, 28, 7, 0, tzinfo=UTC))
    zone = dt_util.get_default_time_zone()
    heating = await sid(hass, "heat_pump_electricity_heating")
    temperature = await sid(hass, "outdoor_temperature")
    async_add_external_statistics(
        hass,
        StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name="Heat pump electricity heating (kWh expected)",
            source="bosch_buderus_heating",
            statistic_id=heating,
            unit_class="energy",
            unit_of_measurement="kWh",
        ),
        [
            StatisticData(start=day_start(date(2026, 10, 24), zone), sum=480.0),
            StatisticData(start=day_start(date(2026, 10, 25), zone), sum=500.0),
        ],
    )
    await async_wait_recording_done(hass)
    items = [
        day_record(date(2026, 10, 24), eCh=2.0, T=8.0),
        day_record(date(2026, 10, 25), eCh=3.0, T=6.0),
        day_record(date(2026, 10, 26), eCh=4.0, T=-1.0),
        day_record(date(2026, 10, 28), eCh=9.0, T=0.0),  # today
    ]
    cloud = history_cloud(items, page=10)
    entry, stack = await start(hass, cloud)
    try:
        await advance(hass, freezer, START_DELAY + timedelta(minutes=1))
        # The cycle is a background task; it reads the recorder first.
        await hass.async_block_till_done(wait_background_tasks=True)
        await async_wait_recording_done(hass)
        rows = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            datetime(2026, 10, 20, tzinfo=UTC),
            None,
            {heating, temperature},
            "hour",
            None,
            {"sum", "mean"},
        )
        # The recorded days stay as they were; the sum goes on from 500.
        assert [row["sum"] for row in rows[heating]] == [480.0, 500.0, 504.0]
        assert [row["mean"] for row in rows[temperature]] == [-1.0]
        state = history_of(entry.runtime_data.coordinators[0]).state
        assert state.statistics_checked
        assert state.floor == date(2026, 10, 25)
        assert state.last_day == date(2026, 10, 26)
    finally:
        await stop(hass, entry, stack)
