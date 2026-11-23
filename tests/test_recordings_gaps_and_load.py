"""Recordings: gaps, failing paths and the load on the cloud.

A slot without data (``c = 0``) is a gap, never energy, also for whole days
and months; a failing, slow or server-failing path concerns that path only;
a run with nothing to read locally never falls back to the cloud; the cloud
reads of installations without a local access are spread over hours and
stop at a rate limit or a pause; a time zone off the full hour imports
nothing. Synthetic answers in the shapes read from a real installation.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.local_read import (
    ReadProblem,
    ReadResult,
    ReadState,
)
from custom_components.bosch_buderus_heating.pointt import (
    RateLimited,
    UnexpectedHttpStatus,
)
from custom_components.bosch_buderus_heating.pointt.exceptions import (
    AccessTokenRejected,
    InvalidPayload,
    RequestTimeout,
    ServiceUnavailable,
    SignInUnavailable,
    TransportError,
)
from custom_components.bosch_buderus_heating.recordings import manager as module
from custom_components.bosch_buderus_heating.recordings import sources
from custom_components.bosch_buderus_heating.recordings.ledger import (
    CLOUD,
    LOCAL,
    Ledger,
)
from custom_components.bosch_buderus_heating.recordings.manager import (
    OPTION_IMPORT,
    on_the_hour,
    statistic_id,
)
from custom_components.bosch_buderus_heating.recordings.measures import CALCULATED
from custom_components.bosch_buderus_heating.recordings.parse import parse_cloud
from custom_components.bosch_buderus_heating.recordings.periods import (
    Period,
    PeriodValue,
    combine,
    month_value,
    period_value,
)
from custom_components.bosch_buderus_heating.recordings.series import (
    BY_KEY,
    CLOUD_PATHS,
    LOCAL_PATHS,
)
from custom_components.bosch_buderus_heating.recordings.sources import Outcome
from tests.ha_test_helpers import MockConfigEntry
from tests.test_recordings_core import Z, days, hours, local, months, read, utc
from tests.test_recordings_manager import Clock, Cloud, Driver, key, manager
from tests.test_start_counters import (
    recorder_runtime_annotations,  # noqa: F401 - fixture
)

HEAT = BY_KEY["total_heat"]
NOW = local(2026, 11, 14, 10) + timedelta(minutes=20)
POOL = "/recordings/heatSources/emon/pool/compressor"


def _cloud_day(rows: list[tuple[float, float]]) -> dict[str, Any]:
    return {
        "type": "yRecording",
        "unitOfMeasure": "kWh",
        "recording": [{"y": y, "c": c} for y, c in rows],
    }


# ------------------------------------------------- gaps of days and months
def test_a_day_without_data_is_no_energy_in_any_period() -> None:
    """Rule 7 for days: the plant's placeholder ``(1.0, 0)`` of a past day
    is a gap: no value for yesterday, the month from the known days or the
    plant's month value, and no statistics row for that day."""
    ledger = Ledger(HEAT)
    at = local(2026, 11, 25, 12)
    read(
        ledger,
        at,
        months(date(2026, 11, 1), [(30.0, 300)]),
        days(date(2026, 11, 1), [(2.0, 24)] * 11 + [(1.0, 0)] + [(2.0, 24)] * 13),
        full=True,
    )
    assert ledger.days["2026-11-12"][:2] == [0.0, 0.0]
    yesterday = period_value(ledger, Period.YESTERDAY, date(2026, 11, 13))
    assert yesterday is not None and yesterday.gap
    assert yesterday.value(False) is None
    month = month_value(ledger, date(2026, 11, 1), date(2026, 11, 25))
    assert month is not None and month.y == pytest.approx(48.0)
    assert ledger.start_import(0.0, Z)
    plan = ledger.plan(at.astimezone(UTC), Z)
    assert utc(local(2026, 11, 12)) not in [row[0] for row in plan.rows]
    # Days 1 to 22 are final (read once, 48 hours ago), the 12th a gap.
    assert plan.rows[-1][3] == pytest.approx(21 * 2.0)


def test_a_stored_placeholder_of_an_earlier_version_is_read_as_a_gap() -> None:
    stored = {
        "days": {
            "2026-11-12": [1.0, 0.0, LOCAL, 2, True],
            "2026-11-13": [2.0, 24.0, LOCAL, 2, True],
        }
    }
    ledger = Ledger(HEAT, stored)
    assert ledger.days["2026-11-12"][:2] == [0.0, 0.0]
    assert ledger.days["2026-11-13"][:2] == [2.0, 24.0]
    assert ledger.start_import(0.0, Z)
    plan = ledger.plan(local(2026, 11, 20).astimezone(UTC), Z)
    assert [row[1] for row in plan.rows] == [2.0]


def test_a_month_without_data_is_no_rest_for_the_import() -> None:
    """A month the plant reports without data is no month value: its days
    before the first known day are gaps, not a negative rest."""
    ledger = Ledger(HEAT)
    ledger.months["2026-09-01"] = [30.0, 1.0, LOCAL, 2, True]
    ledger.months["2026-10-01"] = [0.0, 0.0, LOCAL, 2, True]
    for day in ("2026-10-20", "2026-10-21"):
        ledger.days[day] = [12.0, 24.0, LOCAL, 2, True]
    assert ledger.start_import(0.0, Z)
    plan = ledger.plan(local(2026, 12, 1).astimezone(UTC), Z)
    assert ledger.stats["negative_rest"] == 0
    assert ledger.stats["import_gap_days"] == 19
    assert [row[1] for row in plan.rows] == [30.0, 12.0, 12.0]


def test_a_day_of_gap_hours_only_has_no_value() -> None:
    """A day whose hours are all without data shows no value, not 0 kWh."""
    ledger = Ledger(HEAT)
    at = local(2026, 11, 14, 10) + timedelta(minutes=20)
    values = [(2.0, 1)] * 24 + [(1.0, 0)] * 24 + [(2.0, 1)] * 11
    read(ledger, at, hours(local(2026, 11, 12), values))
    read(
        ledger, at + timedelta(hours=1), hours(local(2026, 11, 12), [*values, (2.0, 1)])
    )
    value = period_value(ledger, Period.YESTERDAY, date(2026, 11, 14))
    assert value is not None and value.final and value.value(False) is None


def test_a_cloud_month_of_placeholders_uses_the_plants_month_value() -> None:
    """A month whose recording began on the 27th: 26 placeholder days and
    five real ones. The month is the plant's month value (31 kWh), not 26
    placeholders plus 30 kWh."""
    ledger = Ledger(HEAT)
    read_at = local(2026, 11, 15, 12).astimezone(UTC)
    july = parse_cloud(
        _cloud_day([(1.0, 0)] * 26 + [(6.0, 24)] * 5), HEAT, "2026-07", Z
    )
    year = parse_cloud(
        _cloud_day(
            [(1.0, 0)] * 6
            + [(31.0, 1), (107.0, 1), (89.0, 1), (712.0, 1), (753.51, 327), (1.0, 0)]
        ),
        HEAT,
        "2026",
        Z,
    )
    assert july is not None and year is not None
    for parsed in (july, year, july, year):
        ledger.merge(parsed, CLOUD, read_at=read_at, zone=Z)
    value = month_value(ledger, date(2026, 7, 1), date(2026, 11, 15))
    assert value is not None
    assert value.y == pytest.approx(31.0) and value.resolution == "monthly"
    current = period_value(ledger, Period.CURRENT_YEAR, date(2026, 11, 15))
    assert current is not None
    assert current.y == pytest.approx(31.0 + 107.0 + 89.0 + 712.0 + 753.51)


def test_a_month_without_data_adds_nothing_to_its_year() -> None:
    ledger = Ledger(HEAT)
    for month, cell in (
        ("2026-01-01", [10.0, 1.0]),
        ("2026-02-01", [1.0, 0.0]),  # a placeholder kept by an older version
        ("2026-03-01", [5.0, 1.0]),
    ):
        ledger.months[month] = [*cell, LOCAL, 2, True]
    value = period_value(ledger, Period.CURRENT_YEAR, date(2026, 3, 20))
    assert value is not None and value.y == pytest.approx(15.0)
    gap = period_value(ledger, Period.PREVIOUS_MONTH, date(2026, 3, 20))
    assert gap is not None and gap.gap and gap.value(False) is None


def test_electricity_with_one_part_without_data_is_a_gap() -> None:
    def part(y: float, c: float) -> PeriodValue:
        return PeriodValue(
            date(2026, 11, 13), date(2026, 11, 14), y, c, True, "daily", frozenset()
        )

    both = combine([part(10.0, 24), part(0.0, 0)])
    assert both is not None and both.gap and both.value(False) is None
    full = combine([part(10.0, 24), part(0.0, 24)])
    assert full is not None and full.value(False) == pytest.approx(10.0)


async def test_yesterday_without_data_shows_no_value(hass: HomeAssistant) -> None:
    """A confirmed day without data: the entity shows no value rather than
    the day before or zero."""
    clock = Clock(NOW)
    rec = await manager(hass, clock, [Driver(clock)])
    name = key("total_heat", Period.YESTERDAY)
    rec.request(name)
    await rec.async_run("start")
    clock.now += timedelta(hours=1)
    await rec.async_run("hourly")
    assert name in rec.published
    rec.ledgers["total_heat"].days["2026-11-13"] = [0.0, 0.0, LOCAL, 2, True]
    rec._publish_all(clock.now, Z)
    assert name not in rec.published
    # Where the entity has the counters to fall back on, they stand in.
    spec = next(item for item in rec.specs() if item.key == name)
    rec._counted[(spec.measure.key, Period.YESTERDAY)] = PeriodValue(
        date(2026, 11, 13),
        date(2026, 11, 14),
        5.0,
        1.0,
        True,
        "hourly",
        frozenset({"counters"}),
    )
    rec._publish_all(clock.now, Z)
    assert rec.published[name][2:4] == [5.0, "counters"]
    rec._counted.clear()
    # Not confirmed yet: the last value stays until it is.
    rec.published[name] = ["2026-11-12", "2026-11-13", 7.0, "local_recording", "daily"]
    rec.ledgers["total_heat"].days["2026-11-13"] = [0.0, 0.0, LOCAL, 1, False]
    rec._publish_all(clock.now, Z)
    assert rec.published[name][2] == 7.0


# ------------------------------------------------------- local failures
class _FailingDriver(Driver):
    """One path answers 406 (neither 200 nor 404/403)."""

    def __init__(self, clock: Clock, path: str = POOL, **kwargs: Any) -> None:
        super().__init__(clock, **kwargs)
        self.failing = path
        self.problem: tuple[ReadState, str] = (
            ReadState.PATH_ERROR,
            ReadProblem.NOT_ACCEPTABLE,
        )

    async def read_explicit(self, request: Any) -> Any:
        if request.path == self.failing:
            self.requests.append((request.path, dict(request.query)))
            state, problem = self.problem
            return ReadResult(request, state, 406, problem)
        return await super().read_explicit(request)


async def test_a_path_that_keeps_failing_neither_blocks_nor_repeats_the_history(
    hass: HomeAssistant,
) -> None:
    clock = Clock(NOW)
    driver = _FailingDriver(clock)
    rec = await manager(hass, clock, [driver])
    rec.acquire()
    await rec.async_run("start")
    assert rec._full_done
    assert rec.local_paths[POOL][0] == "failed"
    for _ in range(3):
        driver.requests.clear()
        clock.now += timedelta(hours=1)
        await rec.async_run("hourly")
        assert driver.requests
        assert all(query["sampleRate"] == "PT1H" for _, query in driver.requests)
        assert POOL not in {path for path, _ in driver.requests}
    # Tried again once a day, with its whole history.
    driver.requests.clear()
    clock.now += timedelta(days=1)
    await rec.async_run("hourly")
    assert (POOL, {"sampleRate": "all"}) in driver.requests


async def test_a_path_that_answered_before_fails_only_after_two_failures(
    hass: HomeAssistant,
) -> None:
    clock = Clock(NOW)
    heat = HEAT.path
    driver = _FailingDriver(clock, path="/nothing")
    rec = await manager(hass, clock, [driver])
    rec.request(key("total_heat", Period.YESTERDAY))
    await rec.async_run("start")
    driver.failing = heat
    for state in ("ok", "failed"):
        clock.now += timedelta(hours=1)
        await rec.async_run("hourly")
        assert rec.local_paths[heat][0] == state
    # What was read stays shown; the path is not asked for a day.
    assert "total_heat" in rec.available()
    driver.requests.clear()
    clock.now += timedelta(hours=1)
    await rec.async_run("hourly")
    assert heat not in {path for path, _ in driver.requests}


async def test_a_local_server_error_pauses_once_and_leaves_that_path_out(
    hass: HomeAssistant,
) -> None:
    clock = Clock(NOW)
    driver = _FailingDriver(clock)
    driver.problem = (ReadState.ACCESS_ERROR, ReadProblem.SERVICE_UNAVAILABLE)
    cloud = Cloud()
    rec = await manager(hass, clock, [driver], cloud)
    await rec.async_run("start")
    assert rec.runs[-1]["outcomes"]["local_server_error"] == 1
    assert rec.local_paths[POOL][0] == "failed"
    assert not rec._full_done and rec.runs[-1]["source"] == LOCAL
    assert cloud.calls == []
    driver.requests.clear()
    clock.now += timedelta(hours=1)
    rec.request(key("total_heat", Period.YESTERDAY))
    await rec.async_run("hourly")
    asked = {path for path, _ in driver.requests}
    assert POOL not in asked
    assert rec._full_done
    assert sorted(LOCAL_PATHS).index(POOL) < len(LOCAL_PATHS) - 1
    assert asked >= {path for path in LOCAL_PATHS if path > POOL} - {
        "/recordings/heatSources/emon/total/burner"
    }


class _SlowDriver(Driver):
    """The whole history of the heat path takes too long."""

    async def read_explicit(self, request: Any) -> Any:
        if request.path == HEAT.path and dict(request.query)["sampleRate"] == "all":
            await asyncio.sleep(0.2)
        return await super().read_explicit(request)


async def test_a_history_that_never_comes_in_time_is_read_by_its_days(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sources, "READ_SECONDS", 0.05)
    clock = Clock(NOW)
    driver = _SlowDriver(clock)
    rec = await manager(hass, clock, [driver])
    rec.request(key("total_heat", Period.YESTERDAY))
    await rec.async_run("start")
    assert rec.runs[-1]["outcomes"]["local_timeout"] == 1
    assert not rec._full_done
    driver.requests.clear()
    clock.now += timedelta(hours=1)
    await rec.async_run("hourly")
    asked = dict(driver.requests)
    assert asked[HEAT.path]["sampleRate"] == "PT1H"
    assert rec._full_done and "total_heat" in rec.available()
    assert rec.diagnostics()["local_paths_read_by_days_only"] == 1
    driver.requests.clear()
    clock.now += timedelta(hours=1)
    await rec.async_run("hourly")
    assert all(query["sampleRate"] == "PT1H" for _, query in driver.requests)


async def test_nothing_to_read_locally_never_asks_the_cloud(
    hass: HomeAssistant,
) -> None:
    """V5: with the local access answering, a run without local work (only
    a comparison entity from the counters) reads nothing from the cloud."""
    clock = Clock(NOW)
    driver = Driver(clock)
    cloud = Cloud()
    rec = await manager(hass, clock, [driver], cloud)
    await rec.async_run("start")
    rec.request(key("total_heat", Period.YESTERDAY, CALCULATED))
    driver.requests.clear()
    clock.now += timedelta(hours=1)
    await rec.async_run("hourly")
    assert driver.requests == [] and cloud.calls == []
    assert rec.runs[-1]["source"] == LOCAL
    # Without the local access the cloud still stands in.
    driver.reachable = False
    rec.request(key("total_heat", Period.YESTERDAY))
    clock.now += timedelta(hours=1)
    await rec.async_run("hourly")
    assert cloud.calls


# ------------------------------------------------------------ cloud load
async def test_cloud_reads_stay_far_below_the_normal_load(
    hass: HomeAssistant,
) -> None:
    """Cloud only, default entities and the performance factors: the start
    run reads yesterday once per path, no second run five minutes later,
    no today without a running period; the back-fill comes a few intervals
    per hourly run."""
    clock = Clock(NOW)
    cloud = Cloud()
    rec = await manager(hass, clock, [], cloud)
    rec.acquire()
    rec.request(key("total_heat", Period.YESTERDAY))
    rec.request(key("total_electricity", Period.YESTERDAY))
    confirmed: list[str] = []
    rec._unsubs.append(lambda: None)
    rec._schedule_confirm = lambda: confirmed.append("x")  # type: ignore[method-assign]
    await rec.async_run("start")
    assert len(cloud.calls) == len(CLOUD_PATHS) and confirmed == []
    per_run = []
    for _ in range(36):
        done = len(cloud.calls)
        clock.now += timedelta(hours=1)
        await rec.async_run("hourly")
        calls = cloud.calls[done:]
        per_run.append(len(calls))
        today = clock.now.astimezone(Z).date().isoformat()
        assert today not in {day for _, day in calls}
    assert max(per_run) <= len(CLOUD_PATHS) - 1 + module.CLOUD_BACKFILL
    assert sum(per_run[:24]) / 24 < 10
    # Locally the start run is confirmed soon after.
    local_rec = await manager(
        hass, clock, [Driver(clock)], entry=MockConfigEntry(domain=DOMAIN)
    )
    local_rec._unsubs.append(lambda: None)
    local_rec._schedule_confirm = lambda: confirmed.append("y")  # type: ignore[method-assign]
    await local_rec.async_run("start")
    assert confirmed == ["y"]


async def test_today_is_read_from_the_cloud_only_for_a_running_period(
    hass: HomeAssistant,
) -> None:
    clock = Clock(NOW)
    cloud = Cloud()
    rec = await manager(hass, clock, [], cloud)
    await rec.async_run("start")
    assert not [day for _, day in cloud.calls if day == "2026-11-14"]
    rec.request(key("total_heat", Period.CURRENT_MONTH))
    done = len(cloud.calls)
    clock.now += timedelta(hours=1)
    await rec.async_run("hourly")
    today = [path for path, day in cloud.calls[done:] if day == "2026-11-14"]
    assert today == [HEAT.path]


class _LimitedCloud(Cloud):
    """Answers a rate limit for the first older interval."""

    def __init__(self) -> None:
        super().__init__()
        self.limited = False

    async def get_recording(self, gateway_id: str, path: str, interval: str) -> Any:
        if len(interval) < 10 and not self.limited:
            self.limited = True
            self.calls.append((path, interval))
            raise RateLimited(60)
        return await super().get_recording(gateway_id, path, interval)


async def test_after_a_rate_limit_no_back_fill_until_the_next_day(
    hass: HomeAssistant,
) -> None:
    clock = Clock(NOW)
    cloud = _LimitedCloud()
    rec = await manager(hass, clock, [], cloud)
    rec.acquire()
    await rec.async_run("start")
    clock.now += timedelta(hours=1)
    await rec.async_run("hourly")
    assert cloud.limited and rec._cloud_limited == date(2026, 11, 14)
    done = len(cloud.calls)
    for _ in range(3):
        clock.now += timedelta(hours=1)
        await rec.async_run("hourly")
    assert not [interval for _, interval in cloud.calls[done:] if len(interval) < 10]
    clock.now = (local(2026, 11, 15, 0) + timedelta(minutes=20)).astimezone(UTC)
    done = len(cloud.calls)
    await rec.async_run("hourly")
    assert [interval for _, interval in cloud.calls[done:] if len(interval) < 10]


async def test_a_cloud_pause_during_a_run_ends_it(hass: HomeAssistant) -> None:
    clock = Clock(NOW)

    class Pausing(Cloud):
        async def get_recording(self, gateway_id: str, path: str, interval: str) -> Any:
            rec._test_coordinator._down = True  # the polling hit a limit
            return await super().get_recording(gateway_id, path, interval)

    cloud = Pausing()
    rec = await manager(hass, clock, [], cloud)
    await rec.async_run("start")
    assert len(cloud.calls) == 1
    assert rec.runs[-1]["cloud"] == "unavailable"


class _HalfCloud(Cloud):
    """The first cloud path fails; ``broken`` chooses how."""

    def __init__(self, broken: object) -> None:
        super().__init__()
        self.broken = broken

    async def get_recording(self, gateway_id: str, path: str, interval: str) -> Any:
        if path == sorted(CLOUD_PATHS)[0]:
            self.calls.append((path, interval))
            if isinstance(self.broken, Exception):
                raise self.broken
            return self.broken
        return await super().get_recording(gateway_id, path, interval)


@pytest.mark.parametrize(
    "broken",
    [UnexpectedHttpStatus(500), {"type": "error"}, {"type": "yRecording"}],
    ids=["server_error", "no_recording", "no_list"],
)
async def test_one_failing_cloud_path_does_not_block_the_others(
    hass: HomeAssistant, broken: object
) -> None:
    clock = Clock(NOW)
    cloud = _HalfCloud(broken)
    failing = sorted(CLOUD_PATHS)[0]
    rec = await manager(hass, clock, [], cloud)
    rec.acquire()
    await rec.async_run("start")
    assert len({path for path, _ in cloud.calls}) == len(CLOUD_PATHS)
    assert rec.available()
    # Not marked as a unit problem: read again after a while.
    assert failing not in rec.cloud_paths
    assert rec.runs[-1]["cloud_paths_left_out"] == 1
    assert rec.diagnostics()["cloud_paths_left_out"] == 1
    clock.now += timedelta(hours=1)
    done = len(cloud.calls)
    await rec.async_run("hourly")
    assert failing not in {path for path, _ in cloud.calls[done:]}
    clock.now += module.CLOUD_FAIL_PAUSE
    done = len(cloud.calls)
    await rec.async_run("hourly")
    assert failing in {path for path, _ in cloud.calls[done:]}


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (ServiceUnavailable(503), Outcome.FAILED),
        (InvalidPayload("bad"), Outcome.FAILED),
        (SignInUnavailable(), Outcome.UNREACHABLE),
        (AccessTokenRejected("no"), Outcome.UNREACHABLE),
        (RequestTimeout("slow"), Outcome.UNREACHABLE),
        (TransportError("no connection"), Outcome.UNREACHABLE),
    ],
)
async def test_cloud_errors_of_a_path_and_of_the_cloud_are_told_apart(
    error: Exception, outcome: Outcome
) -> None:
    class Client:
        async def get_recording(self, *_: Any) -> Any:
            raise error

    answer = await sources.read_cloud(Client(), "gw", HEAT, "2026-11-13")
    assert answer.outcome is outcome


async def test_a_local_server_error_is_its_own_outcome() -> None:
    class LocalDriver:
        async def read_explicit(self, request: Any) -> Any:
            return ReadResult(
                request, ReadState.ACCESS_ERROR, 503, ReadProblem.SERVICE_UNAVAILABLE
            )

    answer = await sources.read_local(LocalDriver(), HEAT, {"sampleRate": "all"})
    assert answer.outcome is Outcome.SERVER_ERROR


# ------------------------------------------------- time zones off the hour
@pytest.mark.parametrize(
    ("zone", "expected"),
    [
        ("Europe/Berlin", True),
        ("Asia/Kolkata", False),
        ("Asia/Kathmandu", False),
        ("Australia/Adelaide", False),
        ("UTC", True),
    ],
)
def test_time_zones_off_the_full_hour_are_recognised(zone: str, expected: bool) -> None:
    assert on_the_hour(datetime(2026, 11, 14, 12, tzinfo=UTC), ZoneInfo(zone)) is (
        expected
    )


async def test_a_time_zone_off_the_full_hour_imports_nothing(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock(NOW)
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={OPTION_IMPORT: True})
    rec = await manager(hass, clock, [Driver(clock)], entry=entry)
    monkeypatch.setattr(rec, "recorder_ready", lambda: True)
    report: dict[str, Any] = {}
    await rec._import(clock.now, ZoneInfo("Asia/Kolkata"), report)
    assert report == {"import": "time_zone_not_on_the_hour"}
    assert rec.diagnostics()["time_zone_on_the_hour"] is False


async def test_rows_off_the_full_hour_are_never_written(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plant clock off the full hour in a zone on it: that series writes
    nothing (the recorder would refuse it), the others are imported."""
    from tests.test_recordings_recorder import _rows

    clock = Clock(NOW)
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={OPTION_IMPORT: True})
    rec = await manager(hass, clock, [Driver(clock)], entry=entry)
    real = Ledger.plan

    def shifted(self: Ledger, *args: Any, **kwargs: Any) -> Any:
        plan = real(self, *args, **kwargs)
        if self.series.key == "total_heat":
            plan.rows = [(start + 1800, *rest) for start, *rest in plan.rows]
        return plan

    monkeypatch.setattr(Ledger, "plan", shifted)
    await rec.async_run("start")
    assert rec.runs[-1]["import_rows_not_on_the_hour"] == 1
    heat = statistic_id(rec.digest, HEAT)
    outdoor = statistic_id(rec.digest, BY_KEY["outdoor_temperature"])
    rows = await _rows(hass, heat, outdoor)
    assert heat not in rows and rows[outdoor]
    assert rec.ledgers["total_heat"].imp is not None
    assert rec.ledgers["total_heat"].imp["rows"] == []


async def test_the_performance_factors_take_no_month_without_data(
    hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A past month the plant reports without data is no month of 0 kWh
    for the performance factors."""
    from tests.test_performance import View, _at, _engine, air
    from tests.test_performance import local as plant_local

    class GapView(View):
        def monthly(self, key: str, first: date, today: date) -> PeriodValue | None:
            value = super().monthly(key, first, today)
            if value is None or first != date(2026, 9, 1):
                return value
            return PeriodValue(
                value.start, value.end, 0.0, 0.0, True, "monthly", value.sources
            )

    plant = air(hass, freezer)
    _at(plant, plant_local(2026, 11, 15, 12, 0))
    months = {date(2026, 9, 1): 300.0, date(2026, 10, 1): 400.0}
    view = GapView(
        {"total_heat": {}, "total_electricity": {}},
        months={"total_heat": dict(months), "total_electricity": dict(months)},
    )
    monkeypatch.setattr(
        "custom_components.bosch_buderus_heating.recordings.recordings_view",
        lambda _hass, _gateway: view,
    )
    engine = await _engine(plant)
    await engine.async_refresh(full=True)
    recorded = engine._recorded_months[("total", "heat")]
    assert date(2026, 10, 1) in recorded
    assert date(2026, 9, 1) not in recorded
