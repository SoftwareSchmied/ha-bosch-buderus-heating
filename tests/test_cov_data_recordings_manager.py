"""Failure paths of the recordings manager: damaged store, unreadable or
refused answers, failing steps of a run, unknown units, counters and the
read-only view for other features."""

from __future__ import annotations

import logging
from datetime import UTC, date, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.local_read import (
    ReadProblem,
    ReadResult,
    ReadState,
)
from custom_components.bosch_buderus_heating.pointt import UnexpectedHttpStatus
from custom_components.bosch_buderus_heating.recordings import (
    Period,
    RecordingsView,
    diagnostics,
    recordings_view,
)
from custom_components.bosch_buderus_heating.recordings import manager as module
from custom_components.bosch_buderus_heating.recordings.ledger import LOCAL, Ledger
from custom_components.bosch_buderus_heating.recordings.manager import (
    OPTION_IMPORT,
    RecordingsManager,
    clears_ranges,
    statistic_id,
)
from custom_components.bosch_buderus_heating.recordings.measures import CALCULATED
from custom_components.bosch_buderus_heating.recordings.series import (
    BY_KEY,
    BY_PATH,
    CLOUD_PATHS,
    LOCAL_PATHS,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.test_recordings_core import days, local, recording
from tests.test_recordings_manager import (
    GW,
    PLANT,
    Clock,
    Cloud,
    Coordinator,
    Driver,
    key,
    manager,
)
from tests.test_recordings_recorder import _rows
from tests.test_start_counters import (
    recorder_runtime_annotations,  # noqa: F401 - fixture
)

NOW = local(2026, 11, 14, 10) + timedelta(minutes=20)


def _clock() -> Clock:
    return Clock(NOW)


# ------------------------------------------------------------ the store
async def test_a_damaged_store_is_read_as_empty_and_logged(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def broken(self: Store[Any]) -> Any:
        raise OSError("disk")

    monkeypatch.setattr(Store, "async_load", broken)
    caplog.set_level(logging.WARNING)
    rec = await manager(hass, _clock(), [Driver(_clock())])
    assert rec.loaded and rec.ledgers == {} and rec.published == {}
    assert "The stored recordings could not be read" in caplog.text
    assert rec.waiting is None


@pytest.mark.parametrize("stored", [None, [], "text", 5, {"series": []}])
async def test_a_store_of_the_wrong_shape_gives_an_empty_state(
    hass: HomeAssistant, stored: object
) -> None:
    rec = await manager(hass, _clock())
    rec._load(stored)
    assert rec.ledgers == {} and rec.local_paths == {} and rec.cloud_paths == {}
    assert rec.published == {} and rec.cloud_done == set()
    assert rec.last_local_ok is None


async def test_only_valid_entries_of_the_store_are_taken_over(
    hass: HomeAssistant,
) -> None:
    rec = await manager(hass, _clock())
    path = BY_KEY["total_heat"].path
    rec._load(
        {
            "series": {"total_heat": {"days": {}}, "unknown_series": {}},
            "local": {
                path: ["ok", 5],
                "/recordings/unknown": ["ok", 5],
                BY_KEY["ch_heat"].path: ["maybe", 5],
                BY_KEY["total_starts"].path: ["missing"],
                BY_KEY["total_compressor"].path: ["unit", "x"],
            },
            "cloud": "nothing",
            "published": {
                "a": ["2026-11-01", "2026-12-01", 1.0, "x", "daily"],
                "b": ["too", "short"],
                3: ["2026-11-01", "2026-12-01", 1.0, "x", "daily"],
                "c": "no list",
            },
            "cloud_done": ["p|2026-10", 5],
            "last_local_ok": "yesterday",
        }
    )
    assert set(rec.ledgers) == {"total_heat"}
    assert rec.local_paths == {path: ["ok", 5]}
    assert rec.cloud_paths == {}
    assert set(rec.published) == {"a"}
    assert rec.cloud_done == {"p|2026-10"}
    assert rec.last_local_ok is None


async def test_a_published_value_that_cannot_be_read_back_is_not_shown(
    hass: HomeAssistant,
) -> None:
    rec = await manager(hass, _clock(), [Driver(_clock())])
    await rec.async_run("start")
    spec = next(s for s in rec.specs() if s.key == key("total_heat", Period.YESTERDAY))
    rec.published[spec.key] = ["not-a-date", "2026-11-14", 1.0, "x", "daily"]
    assert rec.shown(spec) is None
    rec.published[spec.key] = ["2026-11-13", "2026-11-14", "abc", "x", "daily"]
    assert rec.shown(spec) is None
    rec.published[spec.key] = ["2026-11-13", "2026-11-14", 3.5, "x", "daily"]
    shown = rec.shown(spec)
    assert shown is not None and shown.value == 3.5


# ------------------------------------------------------------ the demand
async def test_an_entity_asked_for_twice_stays_wanted_until_both_released(
    hass: HomeAssistant,
) -> None:
    rec = await manager(hass, _clock())
    name = key("total_heat", Period.YESTERDAY)
    first = rec.request(name)
    second = rec.request(name)
    first()
    assert name in rec._requested and rec.needed()
    second()
    assert name not in rec._requested


async def test_a_second_attempt_keeps_the_timers_until_the_last_one_ends(
    hass: HomeAssistant,
) -> None:
    clock = _clock()
    rec = await manager(hass, clock, [Driver(clock)])
    first = rec._test_coordinator
    second = Coordinator(hass, rec.entry, [Driver(clock)])
    rec.attach(second)
    rec._schedule_wanted = True
    rec._schedule()
    assert rec._unsubs and rec.next_run is not None
    rec.detach(first)
    assert rec._unsubs, "timers stay while another attempt is running"
    assert rec.coordinator() is second
    rec.detach(second)
    assert not rec._unsubs and rec.next_run is None
    assert rec.coordinator() is None
    # A new attempt resumes the timers with the same state.
    third = Coordinator(hass, rec.entry, [Driver(clock)])
    rec.attach(third)
    assert rec._unsubs
    rec.stop()
    assert not rec._unsubs and rec.stopped


async def test_a_coordinator_that_is_not_the_latest_is_still_found(
    hass: HomeAssistant,
) -> None:
    clock = _clock()
    rec = await manager(hass, clock, [Driver(clock)])
    first = rec._test_coordinator
    second = Coordinator(hass, rec.entry, [Driver(clock)])
    rec.attach(second)
    rec._latest = SimpleNamespace()  # type: ignore[assignment]
    assert rec.coordinator() in (first, second)


async def test_a_run_request_without_timers_does_nothing(hass: HomeAssistant) -> None:
    rec = await manager(hass, _clock())
    rec.request_run()
    assert rec._unsubs == []


async def test_a_stopped_or_running_manager_starts_no_run(hass: HomeAssistant) -> None:
    clock = _clock()
    driver = Driver(clock)
    rec = await manager(hass, clock, [driver])
    rec.running = True
    await rec.async_run("start")
    assert driver.requests == []
    rec.running = False
    rec.stopped = True
    await rec.async_run("start")
    rec._background("hourly")
    assert driver.requests == []
    rec.stopped = False
    rec.loaded = False
    await rec.async_run("start")
    assert driver.requests == []


# ------------------------------------------------------------- local reads
async def test_a_coordinator_without_a_local_source_reads_nothing_locally(
    hass: HomeAssistant,
) -> None:
    clock = _clock()
    rec = await manager(hass, clock, [], Cloud())
    assert rec._local_drivers() == []
    rec._test_coordinator.local_selection = SimpleNamespace(running_sources=5)
    assert rec._local_drivers() == []
    rec._test_coordinator.local_selection = SimpleNamespace(
        running_sources=lambda: [object(), Driver(clock)]
    )
    assert len(rec._local_drivers()) == 1
    del rec._test_coordinator.local_selection
    assert rec._local_drivers() == []


class _UnitDriver(Driver):
    """Answers every series in a unit the integration does not know."""

    async def read_explicit(self, request: Any) -> Any:
        self.requests.append((request.path, dict(request.query)))
        group = days(date(2026, 11, 1), [(1.0, 1)])
        return ReadResult(
            request,
            ReadState.SUCCESS,
            200,
            observation=SimpleNamespace(data=recording(group, unit="furlongs")),
        )


async def test_a_series_with_an_unknown_unit_is_marked_and_not_read_again(
    hass: HomeAssistant,
) -> None:
    clock = _clock()
    driver = _UnitDriver(clock)
    rec = await manager(hass, clock, [driver])
    rec.request(key("total_heat", Period.YESTERDAY))
    await rec.async_run("start")
    assert len(driver.requests) == len(LOCAL_PATHS)
    assert {state for state, _ in rec.local_paths.values()} == {"unit"}
    assert rec.available() == ()
    assert not rec.ledgers or all(not ledger.days for ledger in rec.ledgers.values())
    driver.requests.clear()
    clock.now += timedelta(hours=1)
    await rec.async_run("hourly")
    assert driver.requests == []


class _FlakyDriver(Driver):
    """Every path answers; the heat path is busy (the plant defers it)."""

    def __init__(self, clock: Clock, state: ReadState, problem: str) -> None:
        super().__init__(clock)
        self.state, self.problem = state, problem
        self.broken = True

    async def read_explicit(self, request: Any) -> Any:
        if request.path == BY_KEY["total_heat"].path and self.broken:
            self.requests.append((request.path, dict(request.query)))
            return ReadResult(request, self.state, problem=self.problem)
        return await super().read_explicit(request)


@pytest.mark.parametrize(
    ("state", "problem"),
    [(ReadState.DEFERRED, ReadProblem.BUSY)],
)
async def test_the_whole_history_is_read_again_until_every_path_answered(
    hass: HomeAssistant, state: ReadState, problem: str, monkeypatch
) -> None:
    from custom_components.bosch_buderus_heating.recordings import manager as module

    monkeypatch.setattr(module, "BUSY_RETRY_SECONDS", 0)
    clock = _clock()
    driver = _FlakyDriver(clock, state, problem)
    rec = await manager(hass, clock, [driver])
    rec.request(key("total_heat", Period.YESTERDAY))
    await rec.async_run("start")
    assert not rec._full_done
    assert "total_heat" not in rec.available()
    # A busy path is asked again within the run (twice), a failed one not.
    assert rec.runs[-1]["outcomes"].get(
        f"local_{'busy' if state is ReadState.DEFERRED else 'failed'}"
    ) == (1 + module.BUSY_RETRIES if state is ReadState.DEFERRED else 1)
    driver.requests.clear()
    driver.broken = False
    clock.now += timedelta(hours=1)
    await rec.async_run("hourly")
    heat = [q for path, q in driver.requests if path == BY_KEY["total_heat"].path]
    assert heat == [{"sampleRate": "all"}]
    assert rec._full_done and "total_heat" in rec.available()


async def test_a_forbidden_local_path_is_not_asked_again_in_this_run_series(
    hass: HomeAssistant,
) -> None:
    clock = _clock()
    driver = Driver(clock)
    rec = await manager(hass, clock, [driver])
    await rec.async_run("start")
    clock.now += timedelta(hours=40)
    driver.requests.clear()
    await rec.async_run("hourly")
    asked = {path for path, _ in driver.requests}
    assert module_forbidden() not in asked


def module_forbidden() -> str:
    from tests.test_recordings_manager import FORBIDDEN

    return FORBIDDEN


# --------------------------------------------------------- failing steps
class _BrokenDriver(Driver):
    async def read_explicit(self, request: Any) -> Any:
        raise RuntimeError("socket exploded")


async def test_a_reading_step_that_raises_is_logged_and_the_run_goes_on(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    clock = _clock()
    rec = await manager(hass, clock, [_BrokenDriver(clock)])
    rec.request(key("total_heat", Period.YESTERDAY))
    caplog.set_level(logging.WARNING)
    await rec.async_run("start")
    report = rec.runs[-1]
    assert report["failed"] is True
    assert "Reading the recordings failed" in caplog.text
    assert not rec.running
    # The next run starts normally.
    rec._test_coordinator.local_selection = SimpleNamespace(
        running_sources=lambda: (Driver(clock),)
    )
    await rec.async_run("hourly")
    assert not rec.runs[-1].get("failed")
    assert set(rec.available()) == set(PLANT)


async def test_a_failing_import_step_leaves_the_values_and_is_reported(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = _clock()
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={OPTION_IMPORT: True})
    rec = await manager(hass, clock, [Driver(clock)], entry=entry)
    shown = key("total_heat", Period.CURRENT_MONTH)
    rec.request(shown)

    async def boom(*args: Any) -> None:
        raise RuntimeError("recorder gone")

    monkeypatch.setattr(rec, "_import", boom)
    caplog.set_level(logging.WARNING)
    await rec.async_run("start")
    report = rec.runs[-1]
    assert report["import_failed"] is True and not report.get("failed")
    assert "Importing the recordings failed" in caplog.text
    assert shown in rec.published


async def test_a_failing_counting_step_is_reported_and_the_run_ends_cleanly(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = _clock()
    rec = await manager(hass, clock, [Driver(clock)])
    saved: list[bool] = []
    monkeypatch.setattr(rec, "_save", lambda: saved.append(True))
    notified: list[bool] = []
    rec.add_listener(lambda: notified.append(True))

    async def boom(*args: Any) -> None:
        raise RuntimeError("statistics gone")

    monkeypatch.setattr(rec, "_count", boom)
    caplog.set_level(logging.WARNING)
    await rec.async_run("start")
    assert rec.runs[-1]["failed"] is True
    assert "Working out the recordings' values failed" in caplog.text
    assert not rec.running and saved and notified
    assert not rec._detected


# ------------------------------------------------------------ cloud reads
async def test_a_cloud_answer_in_an_unknown_unit_is_marked_and_skipped(
    hass: HomeAssistant,
) -> None:
    clock = _clock()

    class Metric(Cloud):
        async def get_recording(self, gateway_id: str, path: str, interval: str) -> Any:
            answer = await super().get_recording(gateway_id, path, interval)
            answer["unitOfMeasure"] = "joule"
            return answer

    cloud = Metric()
    rec = await manager(hass, clock, [], cloud)
    await rec.async_run("start")
    states = {
        state
        for path, (state, _) in rec.cloud_paths.items()
        if path != "/recordings/heatSources/emon/total/eheater"
    }
    assert states == {"unit"}
    # Only the first interval of each path was asked: a path in an unknown
    # unit is left out of the plan from then on.
    first = len(cloud.calls)
    clock.now += timedelta(hours=1)
    await rec.async_run("hourly")
    assert len(cloud.calls) == first
    assert rec.available() == ()


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_a_cloud_server_error_ends_the_run_without_marking_paths(
    hass: HomeAssistant, status: int
) -> None:
    clock = _clock()
    cloud = Cloud(fail=UnexpectedHttpStatus(status))
    rec = await manager(hass, clock, [], cloud)
    await rec.async_run("start")
    failures = module.CLOUD_FAILURES_PER_RUN
    assert len(cloud.calls) == failures, "a few errors end the plan"
    assert rec.cloud_paths == {}
    assert rec.runs[-1]["outcomes"] == {"cloud_failed": failures}
    # Nothing answered: the cloud failed, no path is left out.
    assert rec._cloud_deferred == {}
    assert rec.runs[-1]["source"] is None
    assert rec.available() == ()


async def test_a_forbidden_cloud_path_is_skipped_for_the_rest_of_the_session(
    hass: HomeAssistant,
) -> None:
    clock = _clock()
    cloud = Cloud()
    rec = await manager(hass, clock, [], cloud)
    forbidden = (module.CLOUD, CLOUD_PATHS[0])
    rec._forbidden.add(forbidden)
    await rec.async_run("start")
    assert CLOUD_PATHS[0] not in {path for path, _ in cloud.calls}
    assert {path for path, _ in cloud.calls} >= set(CLOUD_PATHS[1:3])
    report = diagnostics(hass, GW)["series"]
    assert report[BY_PATH[CLOUD_PATHS[0]].key]["cloud"] == "forbidden"  # type: ignore[index]


async def test_the_cloud_is_not_read_for_a_gateway_without_a_coordinator(
    hass: HomeAssistant,
) -> None:
    clock = _clock()
    rec = await manager(hass, clock, [], Cloud())
    rec.detach(rec._test_coordinator)
    rec.attach(rec._test_coordinator)
    rec._coordinators.clear()
    rec._latest = None
    assert (
        await rec._run_cloud(clock.now, module.dt_util.get_default_time_zone(), {})
        is False
    )


async def test_old_cloud_markers_are_dropped_after_two_years(
    hass: HomeAssistant,
) -> None:
    clock = _clock()
    cloud = Cloud()
    rec = await manager(hass, clock, [], cloud)
    rec.cloud_done = {"p|2020-01", "p|2024-05", "p|2026-10", "p|2025"}
    await rec.async_run("start")
    assert "p|2020-01" not in rec.cloud_done
    assert {"p|2024-05", "p|2026-10", "p|2025"} <= rec.cloud_done


async def test_cloud_months_the_plant_still_knows_are_read_by_the_day(
    hass: HomeAssistant,
) -> None:
    clock = _clock()
    cloud = Cloud()
    rec = await manager(hass, clock, [], cloud)
    rec.acquire()  # the back-fill is only for needed series
    ledger = rec._ledger(BY_KEY["total_heat"])
    ledger.months["2026-04-01"] = [100.0, 1.0, LOCAL, 1, True]
    ledger.months["2026-10-01"] = [100.0, 1.0, LOCAL, 1, True]
    ledger.months["2026-03-01"] = [0.0, 0.0, LOCAL, 1, True]
    tasks = {
        (series.key, interval)
        for series, interval, _ in rec._cloud_tasks(date(2026, 11, 14))
    }
    assert ("total_heat", "2026-04") in tasks
    assert ("total_heat", "2026-03") not in tasks, "a month without data is no task"
    rec.cloud_done.add(f"{BY_KEY['total_heat'].path}|2026-04")
    again = {
        (series.key, interval)
        for series, interval, _ in rec._cloud_tasks(date(2026, 11, 14))
    }
    assert ("total_heat", "2026-04") not in again


async def test_a_cloud_interval_without_a_ledger_is_never_complete(
    hass: HomeAssistant,
) -> None:
    rec = await manager(hass, _clock())
    zone = module.dt_util.get_default_time_zone()
    heat = BY_KEY["total_heat"]
    assert not rec._cloud_complete(
        heat, "2026-10-01", module.Rate.HOUR, date(2026, 11, 14), zone
    )


# ---------------------------------------------------------------- values
async def test_a_current_period_without_a_value_is_withdrawn(
    hass: HomeAssistant,
) -> None:
    clock = _clock()
    rec = await manager(hass, clock, [Driver(clock)])
    month = key("total_heat", Period.CURRENT_MONTH)
    year_before = key("total_heat", Period.PREVIOUS_YEAR)
    rec.request(month)
    rec.request(year_before)
    await rec.async_run("start")
    assert month in rec.published
    assert year_before not in rec.published, "no data for last year"
    ledger = rec.ledgers["total_heat"]
    ledger.days.clear()
    ledger.months.clear()
    clock.now += timedelta(hours=1)
    rec._test_coordinator.local_selection = SimpleNamespace(running_sources=lambda: ())
    await rec.async_run("hourly")
    assert month not in rec.published


async def test_a_past_period_keeps_its_last_value_until_the_new_one_is_final(
    hass: HomeAssistant,
) -> None:
    clock = _clock()
    rec = await manager(hass, clock, [Driver(clock)])
    name = key("total_heat", Period.YESTERDAY)
    rec.request(name)
    await rec.async_run("start")
    rec.published[name] = ["2026-11-10", "2026-11-11", 7.0, "local_recording", "daily"]
    ledger = rec.ledgers["total_heat"]
    cell = ledger.days["2026-11-13"]
    cell[4], cell[3] = False, 0
    rec._test_coordinator.local_selection = SimpleNamespace(running_sources=lambda: ())
    rec._publish_all(clock.now, module.dt_util.get_default_time_zone())
    assert rec.published[name][2] == 7.0


async def test_a_mean_without_samples_is_not_published(hass: HomeAssistant) -> None:
    clock = _clock()
    rec = await manager(hass, clock, [Driver(clock)])
    name = key("outdoor_temperature", Period.YESTERDAY)
    rec.request(name)
    await rec.async_run("start")
    for _ in range(2):
        clock.now += timedelta(hours=1)
        await rec.async_run("hourly")
    assert name in rec.published
    del rec.published[name]
    cell = rec.ledgers["outdoor_temperature"].days["2026-11-13"]
    cell[:2] = [0.0, 0.0]
    cell[4] = True
    rec._publish_all(clock.now, module.dt_util.get_default_time_zone())
    assert name not in rec.published


async def test_the_values_of_a_measure_need_every_part_of_it(
    hass: HomeAssistant,
) -> None:
    clock = _clock()
    rec = await manager(hass, clock, [Driver(clock)])
    await rec.async_run("start")
    electricity = next(m for m in rec.measures() if m.key == "total_electricity")
    today = date(2026, 11, 14)
    assert rec.recorded(electricity, Period.YESTERDAY, today) is not None
    del rec.ledgers["total_eheater"]
    assert rec.recorded(electricity, Period.YESTERDAY, today) is None
    rec.ledgers["total_eheater"] = Ledger(BY_KEY["total_eheater"])
    assert rec.recorded(electricity, Period.YESTERDAY, today) is None


async def test_electricity_days_are_only_those_every_part_knows(
    hass: HomeAssistant,
) -> None:
    clock = _clock()
    rec = await manager(hass, clock, [Driver(clock)])
    await rec.async_run("start")
    first, last = date(2026, 11, 1), date(2026, 11, 8)
    both = rec.daily("total_electricity", first, last)
    assert len(both) == 8
    del rec.ledgers["total_eheater"].days["2026-11-03"]
    fewer = rec.daily("total_electricity", first, last)
    assert [item.day.day for item in fewer] == [1, 2, 4, 5, 6, 7, 8]
    assert rec.daily("unknown_measure", first, last) == ()


# ------------------------------------------------------------- the view
async def test_the_view_answers_nothing_for_unknown_measures(
    hass: HomeAssistant,
) -> None:
    clock = _clock()
    rec = await manager(hass, clock, [Driver(clock)])
    await rec.async_run("start")
    view = recordings_view(hass, GW)
    assert isinstance(view, RecordingsView) and view.loaded
    today = date(2026, 11, 14)
    assert "total_heat" in view.measure_keys()
    assert view.first_day("nope") is None
    assert view.monthly("nope", date(2026, 11, 1), today) is None
    assert view.period("nope", Period.YESTERDAY, today) is None
    assert view.daily("nope", today, today) == ()
    assert view.first_day("total_heat") == date(2026, 11, 1)
    assert view.first_day("total_electricity") == date(2026, 11, 1)
    month = view.monthly("total_electricity", date(2026, 11, 1), today)
    assert month is not None and not month.final
    # A month the plant has no value for.
    assert view.monthly("total_heat", date(2020, 1, 1), today) is None
    # One part without any day: the measure has no first day.
    rec.ledgers["total_eheater"].days.clear()
    rec.ledgers["total_eheater"].months.clear()
    assert view.first_day("total_electricity") is None
    assert recordings_view(hass, "another-gateway") is None
    assert diagnostics(hass, "another-gateway") == {"waiting": "not_started"}


async def test_a_view_listener_is_called_after_each_run_and_can_be_removed(
    hass: HomeAssistant,
) -> None:
    clock = _clock()
    rec = await manager(hass, clock, [Driver(clock)])
    view = recordings_view(hass, GW)
    assert view is not None
    calls: list[int] = []
    remove = view.add_listener(lambda: calls.append(1))
    release = view.acquire()
    await rec.async_run("start")
    assert calls
    remove()
    remove()
    count = len(calls)
    await rec.async_run("hourly")
    assert len(calls) == count
    release()


async def test_a_removed_manager_is_not_found_for_its_gateway(
    hass: HomeAssistant,
) -> None:
    rec = await manager(hass, _clock())
    assert recordings_view(hass, GW) is not None
    rec.stop()
    assert recordings_view(hass, GW) is None
    rec.stop()


# ------------------------------------------------------ recorder helpers
def test_a_recorder_that_cannot_be_inspected_clears_whole_statistics_only() -> None:
    assert clears_ranges(SimpleNamespace(async_clear_statistics=5)) is False
    assert (
        clears_ranges(SimpleNamespace(async_clear_statistics=lambda ids: None)) is False
    )
    assert (
        clears_ranges(
            SimpleNamespace(
                async_clear_statistics=lambda ids, start_time=None, end_time=None: None
            )
        )
        is True
    )


async def test_the_import_without_a_recorder_is_reported(hass: HomeAssistant) -> None:
    clock = _clock()
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={OPTION_IMPORT: True})
    rec = await manager(hass, clock, [Driver(clock)], entry=entry)
    assert not rec.recorder_ready()
    await rec.async_run("start")
    assert rec.runs[-1]["import"] == "recorder_unavailable"
    assert await rec.async_delete_statistics() == 0


# ------------------------------------------------- with the real recorder
async def test_a_series_without_any_recorded_value_imports_nothing(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass: HomeAssistant,
) -> None:
    clock = _clock()
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={OPTION_IMPORT: True})
    rec = await manager(hass, clock, [Driver(clock, reachable=False)], entry=entry)
    series = BY_KEY["total_heat"]
    rec.ledgers["total_heat"] = Ledger(series)
    rec.local_paths[series.path] = ["ok", clock.now.timestamp()]
    await rec.async_run("start")
    assert rec.runs[-1]["imported_rows"] == 0
    assert not rec.ledgers["total_heat"].importing
    assert not (await _rows(hass, statistic_id(rec.digest, series))).get(
        statistic_id(rec.digest, series)
    )


async def test_a_new_import_continues_an_earlier_sum_that_ends_before_the_history(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass: HomeAssistant,
) -> None:
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMeanType,
        StatisticMetaData,
    )
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
    )

    clock = _clock()
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={OPTION_IMPORT: True})
    rec = await manager(hass, clock, [Driver(clock)], entry=entry)
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
        [StatisticData(start=local(2026, 10, 20).astimezone(UTC), sum=700.0)],
    )
    await _rows(hass, heat)
    await rec.async_run("start")
    rows = (await _rows(hass, heat))[heat]
    assert rows[0]["start"] == local(2026, 10, 20).astimezone(UTC).timestamp()
    assert rows[0]["sum"] == 700.0
    assert rows[1]["start"] == local(2026, 11, 1).astimezone(UTC).timestamp()
    assert rows[1]["sum"] == pytest.approx(748.0)


# ------------------------------------------------------------- counters
async def _counter_manager(hass: HomeAssistant, clock: Clock) -> tuple[Any, str, str]:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    counter = er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, f"{GW}:gateway:heating_heat_produced", config_entry=entry
    )
    rec = await manager(hass, clock, [Driver(clock)], entry=entry)
    calculated = key("ch_heat", Period.YESTERDAY, CALCULATED)
    rec.request(calculated)
    return rec, calculated, counter.entity_id


@pytest.mark.parametrize(
    "outcome",
    [RuntimeError("no statistics"), {}, {"change": "n/a"}, {"change": None}],
    ids=["raises", "empty", "text", "none"],
)
async def test_a_counter_value_that_cannot_be_read_is_left_out(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    outcome: Any,
) -> None:
    from homeassistant.components.recorder import statistics

    clock = _clock()
    rec, calculated, entity_id = await _counter_manager(hass, clock)

    def fake(*args: Any) -> Any:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(statistics, "statistic_during_period", fake)
    await rec.async_run("start")
    assert not rec.runs[-1].get("failed")
    assert rec._counted == {}
    assert calculated not in rec.published
    assert rec.counter_entities["ch_heat"] == entity_id


async def test_a_counter_without_an_entity_is_skipped(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass: HomeAssistant,
) -> None:
    clock = _clock()
    rec = await manager(hass, clock, [Driver(clock)])
    calculated = key("ch_heat", Period.YESTERDAY, CALCULATED)
    rec.request(calculated)
    await rec.async_run("start")
    assert rec._counted == {} and rec.counter_entities == {}
    assert calculated not in rec.published


# ------------------------------------------------------------ more edge cases
def test_the_source_of_a_value_is_named_for_the_shown_attribute() -> None:
    assert module._source_text(frozenset({"counters"})) == "counters"
    assert module._source_text(frozenset({module.LOCAL})) == "local_recording"
    assert module._source_text(frozenset({module.CLOUD})) == "cloud_recording"
    assert (
        module._source_text(frozenset({module.LOCAL, module.CLOUD}))
        == "local_and_cloud_recording"
    )


async def test_a_start_with_timers_waits_for_the_first_attempt_of_the_gateway(
    hass: HomeAssistant,
) -> None:
    clock = _clock()
    await hass.config.async_update(time_zone="Europe/Berlin")
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    rec = RecordingsManager(hass, entry, GW, now=clock)
    await rec.async_start()
    assert rec._schedule_wanted and rec._unsubs == []
    rec.attach(Coordinator(hass, entry, [Driver(clock)]))
    assert len(rec._unsubs) == 2 and rec.next_run is not None
    rec.stop()
    assert rec._unsubs == [] and rec.stopped
    rec.stop()  # a second stop finds nothing to remove


async def test_a_series_only_known_as_missing_is_not_available(
    hass: HomeAssistant,
) -> None:
    rec = await manager(hass, _clock())
    heat = BY_KEY["total_heat"]
    rec.ledgers["total_heat"] = Ledger(heat)
    rec.local_paths[heat.path] = ["missing", 5]
    assert rec.available() == ()
    rec.cloud_paths[heat.path] = ["ok", 5]
    assert rec.available() == ("total_heat",)


async def test_a_manager_is_only_started_for_an_entry_that_is_being_set_up_or_runs(
    hass: HomeAssistant,
) -> None:
    from homeassistant.config_entries import ConfigEntryState

    clock = _clock()
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    coordinator = Coordinator(hass, entry, [Driver(clock)])
    coordinator.config_entry = None
    assert module.manager_for(coordinator) is None  # type: ignore[arg-type]
    coordinator.config_entry = entry
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert module.manager_for(coordinator) is None  # type: ignore[arg-type]
    assert module.managers_of(hass, entry.entry_id) == []


async def test_removing_an_entry_deletes_its_stored_recordings(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    clock = _clock()
    rec = await manager(hass, clock, [Driver(clock)])
    await rec.async_run("start")
    await rec._store.async_save(rec._data())
    stored = module.store_key(rec.entry.entry_id, rec.digest)
    assert stored in hass_storage
    await module.async_remove_recordings_store(hass, rec.entry.entry_id, GW)
    assert stored not in hass_storage
