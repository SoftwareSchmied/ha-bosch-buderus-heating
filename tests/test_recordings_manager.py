"""The reads of the plant's recordings: request plan, sources and values.

A manager with a fake local source and a fake cloud client; the answers
follow the shapes read from a real installation. Expected, untested on a
real installation: the request plan over days, the switch to the cloud and
the change of summer time.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
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
    ResourceForbidden,
    ResourceNotFound,
)
from custom_components.bosch_buderus_heating.recordings import (
    Period,
    recordings_view,
)
from custom_components.bosch_buderus_heating.recordings.manager import (
    _MANAGERS,
    CLOUD_BUDGET,
    OPTION_IMPORT,
    RecordingsManager,
    statistic_id,
)
from custom_components.bosch_buderus_heating.recordings.measures import (
    CALCULATED,
    RECORDING,
)
from custom_components.bosch_buderus_heating.recordings.parse import Rate
from custom_components.bosch_buderus_heating.recordings.series import (
    BY_KEY,
    BY_PATH,
    CLOUD_PATHS,
    LOCAL_PATHS,
)
from custom_components.bosch_buderus_heating.rrc_energy_statistics import (
    async_gateway_digest,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.test_recordings_core import days, fixed, hours, local, recording

Z = ZoneInfo("Europe/Berlin")
GW = "gateway-recordings-1"
PLANT = {
    "total_heat": 2.0,
    "total_compressor": 0.5,
    "total_eheater": 0.0,
    "ch_heat": 1.5,
    "outdoor_temperature": 300.0,
}
FORBIDDEN = "/recordings/heatSources/emon/total/burner"
# Units of series that are no energy (a test adds them to ``PLANT``).
UNITS: dict[str, str] = {}


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start.astimezone(UTC)

    def __call__(self) -> datetime:
        return self.now


class Driver:
    """A local source: the plant's paths answer, the others 404, one 403."""

    def __init__(self, clock: Clock, *, reachable: bool = True) -> None:
        self.clock, self.reachable = clock, reachable
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def read_explicit(self, request):
        self.requests.append((request.path, dict(request.query)))
        if not self.reachable:
            return ReadResult(request, ReadState.ACCESS_ERROR, problem="connect")
        series = BY_PATH[request.path]
        if request.path == FORBIDDEN:
            return ReadResult(request, ReadState.PATH_ERROR, 403, ReadProblem.FORBIDDEN)
        if series.key not in PLANT:
            return ReadResult(request, ReadState.PATH_ERROR, 404, ReadProblem.NOT_FOUND)
        value = PLANT[series.key]
        count = 60 if series.mean else 1
        now = self.clock().astimezone(Z)
        current = now.replace(minute=0, second=0, microsecond=0)
        query = dict(request.query)
        groups = []
        if query["sampleRate"] == "all":
            first = date(2026, 11, 1)
            groups.append(
                days(
                    first, [(value * 24, count * 24)] * ((now.date() - first).days + 1)
                )
            )
            start = datetime.combine(
                now.date() - timedelta(days=5), datetime.min.time(), Z
            )
        else:
            start = datetime.combine(
                date.fromisoformat(query["startDate"]), datetime.min.time(), Z
            )
        elapsed = int((current - start).total_seconds() // 3600) + 1
        groups.append(hours(start, [(value, count)] * elapsed, end=current))
        unit = UNITS.get(series.key, "C" if series.mean else "kWh")
        return ReadResult(
            request,
            ReadState.SUCCESS,
            200,
            observation=SimpleNamespace(data=recording(*groups, unit=unit)),
        )


class Cloud:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    async def get_recording(self, gateway_id: str, path: str, interval: str):
        assert gateway_id == GW
        self.calls.append((path, interval))
        if self.fail is not None:
            raise self.fail
        if path.endswith("/total/eheater"):
            raise ResourceNotFound(path, 404)
        if len(interval) == 10:
            rows = [{"y": 1.0, "c": 1}] * 24
        elif len(interval) == 7:
            rows = [{"y": 24.0, "c": 24}] * 30
        else:
            rows = [{"y": 700.0, "c": 1}] * 12
        return {"type": "yRecording", "unitOfMeasure": "kWh", "recording": rows}


class Coordinator:
    def __init__(self, hass, entry, drivers=(), cloud=None, *, cloud_down=False):
        self.hass, self.config_entry = hass, entry
        self.gateway = SimpleNamespace(gateway_id=GW)
        self.local_selection = SimpleNamespace(running_sources=lambda: tuple(drivers))
        self.client = cloud
        self._down = cloud_down
        self.resources = {"/heatSources/emon/totalConsumption": object()}

    def cloud_unavailable(self) -> bool:
        return self._down


async def manager(hass, clock, drivers=(), cloud=None, **kwargs):
    await hass.config.async_update(time_zone="Europe/Berlin")
    entry = kwargs.pop("entry", None) or MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    result = RecordingsManager(hass, entry, GW, now=clock)
    coordinator = Coordinator(hass, entry, drivers, cloud, **kwargs)
    result.attach(coordinator)
    result._test_coordinator = coordinator  # keep it alive (weak set)
    hass.data.setdefault(_MANAGERS, {})[(entry.entry_id, GW)] = result
    await result.async_start(schedule=False)
    return result


def module_backfill() -> int:
    from custom_components.bosch_buderus_heating.recordings import manager as module

    return module.CLOUD_BACKFILL


def key(measure: str, period: Period, method: str = RECORDING) -> str:
    from custom_components.bosch_buderus_heating.recordings.measures import (
        measures,
    )

    found = next(item for item in measures(PLANT) if item.key == measure)
    suffix = ":calculated" if method == CALCULATED else ""
    return f"{found.path_id}:{period.value}{suffix}"


# ---------------------------------------------------------- request plan
async def test_the_start_reads_the_whole_history_then_hourly_the_last_two_days(
    hass: HomeAssistant,
) -> None:
    """V5 and rule 15: ``all`` once per path at the start; then hourly the
    hours of yesterday and today of the paths that answered; 404 again
    after a day, 403 not before the next start."""
    clock = Clock(
        local(
            2026,
            11,
            14,
            10,
        )
        + timedelta(minutes=20)
    )
    driver = Driver(clock)
    rec = await manager(hass, clock, [driver])
    await rec.async_run("start")
    assert len(driver.requests) == len(LOCAL_PATHS)
    assert all(query == {"sampleRate": "all"} for _, query in driver.requests)
    assert set(rec.available()) == set(PLANT)
    driver.requests.clear()
    clock.now += timedelta(hours=1)
    rec.request(key("total_heat", Period.YESTERDAY))
    await rec.async_run("hourly")
    # Hourly only what something needs: here the one enabled entity.
    assert [path for path, _ in driver.requests] == [BY_KEY["total_heat"].path]
    assert all(
        query
        == {"sampleRate": "PT1H", "startDate": "2026-11-12", "endDate": "2026-11-14"}
        for _, query in driver.requests
    )
    # Another feature (performance factors) adds the energy series.
    driver.requests.clear()
    release = recordings_view(hass, GW).acquire()
    await rec.async_run("hourly")
    assert {path for path, _ in driver.requests} == {
        BY_KEY[item].path for item in PLANT if BY_KEY[item].cloud
    }
    release()
    driver.requests.clear()
    clock.now += timedelta(hours=25)
    await rec.async_run("hourly")
    again = {path for path, query in driver.requests if query == {"sampleRate": "all"}}
    assert len(again) == len(LOCAL_PATHS) - len(PLANT) - 1
    assert FORBIDDEN not in {path for path, _ in driver.requests}


async def test_nothing_is_read_when_nothing_needs_the_recordings(
    hass: HomeAssistant,
) -> None:
    clock = Clock(local(2026, 11, 14, 10))
    driver = Driver(clock)
    rec = await manager(hass, clock, [driver])
    await rec.async_run("start")  # once after the start, to find the series
    driver.requests.clear()
    await rec.async_run("hourly")
    assert driver.requests == []
    assert rec.waiting == "not_needed"
    release = recordings_view(hass, GW).acquire()
    await rec.async_run("hourly")
    assert driver.requests
    release()
    release()  # once only
    assert rec._consumers == 0


async def test_without_the_local_access_the_cloud_reads_the_energy_paths(
    hass: HomeAssistant,
) -> None:
    """Rules 11 and 16, V5: local unreachable, so the cloud; only the nine
    energy paths. The start run reads yesterday once per path to find the
    series (no today without a running period, no back-fill, no second
    run five minutes later); hourly runs go on with the back-fill of the
    needed series, a few intervals per run."""
    clock = Clock(
        local(
            2026,
            11,
            14,
            10,
        )
        + timedelta(minutes=20)
    )
    driver = Driver(clock, reachable=False)
    cloud = Cloud()
    rec = await manager(hass, clock, [driver], cloud)
    await rec.async_run("start")
    assert len(driver.requests) == 1  # stopped at the first unreachable read
    assert sorted(cloud.calls) == sorted((path, "2026-11-13") for path in CLOUD_PATHS)
    assert rec.cloud_paths["/recordings/heatSources/emon/total/eheater"][0] == "missing"
    assert rec.runs[-1]["source"] == "cloud"
    first = len(cloud.calls)
    # The next run reads the needed series: yesterday until it is final and
    # a few older intervals (back-fill), not the same again.
    clock.now += timedelta(hours=1)
    rec.request(key("total_heat", Period.YESTERDAY))
    await rec.async_run("hourly")
    later = cloud.calls[first:]
    heat = BY_KEY["total_heat"].path
    assert later[0] == (heat, "2026-11-13")
    assert {path for path, _ in later} == {heat}
    assert len(later) == 1 + module_backfill()
    assert len(later) <= CLOUD_BUDGET


async def test_the_cloud_reads_yesterday_only_until_it_is_final(
    hass: HomeAssistant,
) -> None:
    """V5: cloud only; yesterday of the needed series until a second read
    confirmed it, then like an older day; today only for a running period
    (none enabled here)."""
    clock = Clock(local(2026, 11, 14, 10) + timedelta(minutes=20))
    cloud = Cloud()
    rec = await manager(hass, clock, [], cloud)
    rec.request(key("total_heat", Period.YESTERDAY))
    await rec.async_run("start")
    yesterday = date(2026, 11, 13)
    paths = {BY_KEY["total_heat"].path}
    opened = []
    for _ in range(4):
        open_paths = {
            ledger.series.path
            for ledger in rec.ledgers.values()
            if not (ledger.day(yesterday) or [0, 0, "", 0, False])[4]
            and ledger.series.path in paths
        }
        opened.append(open_paths)
        done = len(cloud.calls)
        clock.now += timedelta(hours=1)
        await rec.async_run("hourly")
        calls = cloud.calls[done:]
        assert {path for path, day in calls if day == "2026-11-13"} == open_paths
        assert not [day for _, day in calls if day == "2026-11-14"]
    # Open after the start (read once), confirmed by the next run.
    assert opened[0] and not any(opened[1:])
    # Twelve hours after its last read (23:20) yesterday is read once more
    # (late corrections).
    clock.now += timedelta(hours=9)
    done = len(cloud.calls)
    await rec.async_run("hourly")
    again = {path for path, day in cloud.calls[done:] if day == "2026-11-13"}
    assert again == paths


async def test_a_cloud_interval_counts_as_complete_only_with_every_slot(
    hass: HomeAssistant,
) -> None:
    """A month answered with the wrong number of days (or a year without
    its months) left no values: it is read again, not marked complete."""
    clock = Clock(local(2026, 11, 14, 10) + timedelta(minutes=20))
    rec = await manager(hass, clock, [], Cloud())
    series = BY_KEY["total_heat"]
    today = date(2026, 11, 14)
    ledger = rec._ledger(series)
    assert not rec._cloud_complete(series, "2026-10", Rate.DAY, today, Z)
    assert not rec._cloud_complete(series, "2025", Rate.MONTH, today, Z)
    for offset in range(31):
        value = 24.0 if offset else 0.0  # the first day a gap
        day = (date(2026, 10, 1) + timedelta(days=offset)).isoformat()
        ledger.days[day] = [value, value, "cloud", 1, True]
    assert rec._cloud_complete(series, "2026-10", Rate.DAY, today, Z)
    ledger.days["2026-10-31"][4] = False
    assert not rec._cloud_complete(series, "2026-10", Rate.DAY, today, Z)
    for month in range(1, 13):
        ledger.months[date(2025, month, 1).isoformat()] = [1.0, 1.0, "cloud", 1, True]
    assert rec._cloud_complete(series, "2025", Rate.MONTH, today, Z)
    del ledger.months["2025-12-01"]
    assert not rec._cloud_complete(series, "2025", Rate.MONTH, today, Z)


@pytest.mark.parametrize(
    "error", [RateLimited(60), ResourceForbidden("/recordings/x", 403)]
)
async def test_a_rate_limit_or_refusal_ends_or_skips_cloud_reads(
    hass: HomeAssistant, error: Exception
) -> None:
    clock = Clock(local(2026, 11, 14, 10))
    cloud = Cloud(fail=error)
    rec = await manager(hass, clock, [], cloud)
    await rec.async_run("start")
    if isinstance(error, RateLimited):
        assert len(cloud.calls) == 1
    else:
        # A refused path is not asked again before the next start.
        assert len({path for path, _ in cloud.calls}) == len(CLOUD_PATHS)
        assert len(cloud.calls) == len(CLOUD_PATHS)


async def test_no_cloud_reads_while_the_cloud_is_unavailable_or_for_no_heat_source(
    hass: HomeAssistant,
) -> None:
    clock = Clock(local(2026, 11, 14, 10))
    cloud = Cloud()
    rec = await manager(hass, clock, [], cloud, cloud_down=True)
    await rec.async_run("start")
    assert cloud.calls == [] and rec.runs[-1]["cloud"] == "unavailable"
    rec._test_coordinator._down = False
    rec._test_coordinator.resources = {"/system/info": object()}
    rec._detected = False
    await rec.async_run("start")
    # No heat source reading at all: no run (the anchor), so no cloud read.
    assert cloud.calls == [] and rec.waiting == "no_heat_source_anchor"


# ----------------------------------------------------------------- values
async def test_yesterday_switches_only_once_the_new_day_is_final(
    hass: HomeAssistant,
) -> None:
    """Rule 4 and V2: the electricity is compressor plus heater; a past
    period keeps its value until the new one is confirmed."""
    clock = Clock(
        local(
            2026,
            11,
            14,
            10,
        )
        + timedelta(minutes=20)
    )
    rec = await manager(hass, clock, [Driver(clock)])
    heat = key("total_heat", Period.YESTERDAY)
    power = key("total_electricity", Period.YESTERDAY)
    rec.request(heat)
    rec.request(power)
    await rec.async_run("start")
    clock.now += timedelta(hours=1)
    await rec.async_run("hourly")
    specs = {spec.key: spec for spec in rec.specs()}
    assert specs[heat].enabled_default and specs[power].enabled_default
    shown = rec.shown(specs[heat])
    assert shown is not None and shown.value == pytest.approx(48.0)
    assert shown.start == date(2026, 11, 13) and shown.source == "local_recording"
    assert rec.shown(specs[power]).value == pytest.approx(12.0)
    # Just after midnight: the new yesterday is read once only.
    clock.now = (local(2026, 11, 15, 0) + timedelta(minutes=20)).astimezone(UTC)
    await rec.async_run("hourly")
    assert rec.shown(specs[heat]).start == date(2026, 11, 13)
    clock.now += timedelta(hours=1)
    await rec.async_run("hourly")
    assert rec.shown(specs[heat]).start == date(2026, 11, 14)


async def test_entities_default_and_comparison_methods(hass: HomeAssistant) -> None:
    clock = Clock(local(2026, 11, 14, 10))
    rec = await manager(hass, clock, [Driver(clock)])
    await rec.async_run("start")
    specs = rec.specs()
    enabled = sorted(spec.key for spec in specs if spec.enabled_default)
    assert enabled == sorted(
        key(measure, period)
        for measure in ("total_heat", "total_electricity")
        for period in Period
    )
    # Not the parts of the electricity, not a mode, not a comparison copy.
    assert key("total_compressor", Period.CURRENT_MONTH) not in enabled
    assert key("ch_heat", Period.CURRENT_MONTH) not in enabled
    assert key("total_heat", Period.CURRENT_MONTH, CALCULATED) not in enabled
    by_key = {spec.key: spec for spec in specs}
    # The heat per mode comes from the counters first.
    assert by_key[key("ch_heat", Period.YESTERDAY, CALCULATED)].primary
    assert not by_key[key("ch_heat", Period.YESTERDAY)].primary
    # No comparison for temperatures.
    assert key("outdoor_temperature", Period.YESTERDAY, CALCULATED) not in by_key
    assert len({spec.key for spec in specs}) == len(specs)


async def test_the_state_survives_a_restart(hass: HomeAssistant, hass_storage) -> None:
    """Rule 9: the series and the shown values are stored per gateway."""
    clock = Clock(
        local(
            2026,
            11,
            14,
            10,
        )
        + timedelta(minutes=20)
    )
    entry = MockConfigEntry(domain=DOMAIN, data={})
    rec = await manager(hass, clock, [Driver(clock)], entry=entry)
    rec.request(key("total_heat", Period.CURRENT_MONTH))
    await rec.async_run("start")
    await hass.async_block_till_done()
    await rec._store.async_save(rec._data())
    again = RecordingsManager(hass, entry, GW, now=clock)
    await again.async_start(schedule=False)
    assert set(again.available()) == set(PLANT)
    spec = next(
        s for s in again.specs() if s.key == key("total_heat", Period.CURRENT_MONTH)
    )
    assert again.shown(spec) is not None
    stored = json.dumps(hass_storage)
    assert GW not in stored


async def test_gateways_have_separate_statistics(hass: HomeAssistant) -> None:
    """Rule 23 and 18: a stable ID per gateway, keyed, never the gateway ID."""
    one = await async_gateway_digest(hass, "gateway-a")
    two = await async_gateway_digest(hass, "gateway-b")
    series = BY_KEY["total_heat"]
    assert statistic_id(one, series) != statistic_id(two, series)
    assert statistic_id(one, series) == statistic_id(
        await async_gateway_digest(hass, "gateway-a"), series
    )
    assert "gateway" not in statistic_id(one, series)


async def test_diagnostics_hold_states_and_counts_only(hass: HomeAssistant) -> None:
    clock = Clock(local(2026, 11, 14, 10))
    rec = await manager(hass, clock, [Driver(clock)])
    await rec.async_run("start")
    report = rec.diagnostics()
    text = json.dumps(report)
    assert GW not in text and "48.0" not in text
    assert report["series"]["total_heat"]["local"] == "ok"
    assert report["series"]["total_burner"]["local"] == "forbidden"
    assert report["import_enabled"] is False
    assert report["last_run"]["source"] == "local"


async def test_the_daily_interface_for_other_features(hass: HomeAssistant) -> None:
    clock = Clock(local(2026, 11, 14, 10))
    await manager(hass, clock, [Driver(clock)])
    view = recordings_view(hass, GW)
    assert view is not None and view.loaded
    rec = view._manager
    await rec.async_run("start")
    assert "total_electricity" in view.measure_keys()
    rows = view.daily("total_electricity", date(2026, 11, 1), date(2026, 11, 3))
    assert [row.value for row in rows] == [12.0, 12.0, 12.0]
    temperature = view.daily(
        "outdoor_temperature", date(2026, 11, 1), date(2026, 11, 1)
    )
    assert temperature[0].value == pytest.approx(5.0)
    assert view.first_day("total_heat") == date(2026, 11, 1)
    month = view.monthly("total_heat", date(2026, 11, 1), date(2026, 11, 14))
    assert month is not None and not month.final
    assert (
        view.period("total_heat", Period.CURRENT_YEAR, date(2026, 11, 14)) is not None
    )
    assert view.daily("unknown", date(2026, 11, 1), date(2026, 11, 2)) == ()
    assert recordings_view(hass, "other") is None


async def test_the_import_option_counts_as_a_need(hass: HomeAssistant) -> None:
    clock = Clock(local(2026, 11, 14, 10))
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={OPTION_IMPORT: True})
    rec = await manager(hass, clock, [Driver(clock)], entry=entry)
    rec._detected = True
    assert rec.needed()
    # Without a recorder nothing is imported, the reads go on.
    await rec.async_run("hourly")
    assert rec.runs[-1]["import"] == "recorder_unavailable"


def test_fixed_offsets_helper() -> None:
    assert fixed(local(2026, 7, 1)).utcoffset() == timedelta(hours=2)


@pytest.mark.parametrize(("value", "discarded"), [(16.0, False), (91.0, True)])
async def test_one_fixed_plausibility_limit_for_every_recording(
    hass: HomeAssistant, monkeypatch, value, discarded
) -> None:
    """Rule 13: 90 kWh per hour (1.5 x 60 kW) for every energy recording,
    whatever the heat generator; an hour with a heater above the nominal
    output of the outdoor unit stays."""
    monkeypatch.setitem(PLANT, "total_heat", value)
    monkeypatch.setitem(PLANT, "total_eheater", 9.0)
    clock = Clock(local(2026, 11, 14, 10))
    rec = await manager(hass, clock, [Driver(clock)])
    await rec.async_run("start")
    assert rec.diagnostics()["plausibility_limit_kwh_per_hour"] == 90.0
    stats = rec.ledgers["total_heat"].stats
    assert (stats["discarded_implausible"] > 0) is discarded
    assert rec.ledgers["total_eheater"].stats["discarded_implausible"] == 0


# ------------------------------------- findings of a real installation
class BusyDriver(Driver):
    """The local access is busy for some paths at first (as at a start)."""

    def __init__(self, clock: Clock, busy: dict[str, int]) -> None:
        super().__init__(clock)
        self.busy = dict(busy)

    async def read_explicit(self, request):
        left = self.busy.get(request.path, 0)
        if left > 0:
            self.busy[request.path] = left - 1
            self.requests.append((request.path, dict(request.query)))
            return ReadResult(request, ReadState.DEFERRED, problem=ReadProblem.BUSY)
        return await super().read_explicit(request)


async def test_busy_paths_are_asked_again_within_the_start_run(
    hass: HomeAssistant, monkeypatch
) -> None:
    """Every series (and so every period entity) is known after the first
    run, also when the local access was busy for some paths at first."""
    from custom_components.bosch_buderus_heating.recordings import manager as module

    monkeypatch.setattr(module, "BUSY_RETRY_SECONDS", 0)
    clock = Clock(local(2026, 11, 14, 10) + timedelta(minutes=20))
    dhw = BY_KEY["ch_heat"].path
    driver = BusyDriver(clock, {dhw: 2, BY_KEY["outdoor_temperature"].path: 1})
    rec = await manager(hass, clock, [driver])
    await rec.async_run("start")
    assert set(rec.available()) == set(PLANT)
    assert rec._full_done
    assert [query for path, query in driver.requests if path == dhw] == [
        {"sampleRate": "all"}
    ] * 3
    assert "local_busy_left" not in rec.runs[-1]
    # Still busy after the retries: the run says so, the next one reads all.
    driver = BusyDriver(clock, {dhw: module.BUSY_RETRIES + 1})
    again = await manager(hass, clock, [driver], entry=MockConfigEntry(domain=DOMAIN))
    await again.async_run("start")
    assert again.runs[-1]["local_busy_left"] == 1
    assert not again._full_done and "ch_heat" not in again.available()


async def test_an_entity_added_after_a_run_gets_its_value_without_a_read(
    hass: HomeAssistant,
) -> None:
    """The start run finds the series; their entities are added afterwards
    and get their values from what is stored, not an hour later."""
    from tests.ha_test_helpers import async_fire_time_changed

    clock = Clock(local(2026, 11, 14, 10) + timedelta(minutes=20))
    driver = Driver(clock)
    rec = await manager(hass, clock, [driver])
    await rec.async_run("start")
    month = key("total_heat", Period.CURRENT_MONTH)
    yesterday = key("total_heat", Period.YESTERDAY)
    spec = {item.key: item for item in rec.specs()}
    assert rec.shown(spec[month]) is None
    driver.requests.clear()
    rec.request(month)
    rec.request(yesterday)
    async_fire_time_changed(hass, dt_util_now() + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert driver.requests == []  # no read
    assert rec.shown(spec[month]) is not None
    # Yesterday needs a second read (rule 5): not shown yet.
    assert rec.shown(spec[yesterday]) is None


def dt_util_now() -> datetime:
    from homeassistant.util import dt as dt_util

    return dt_util.utcnow()


async def test_the_start_run_is_confirmed_soon_after(hass: HomeAssistant) -> None:
    """Yesterday follows a second read of the needed series a few minutes
    after the start run, unless the hourly run comes first anyway."""
    from custom_components.bosch_buderus_heating.recordings import manager as module
    from tests.ha_test_helpers import async_fire_time_changed

    clock = Clock(local(2026, 11, 14, 10) + timedelta(minutes=30))
    driver = Driver(clock)
    rec = await manager(hass, clock, [driver])
    yesterday = key("total_heat", Period.YESTERDAY)
    rec.request(yesterday)
    # Scheduled runs (the start and hourly timers themselves stay out).
    rec._unsubs.append(lambda: None)
    try:
        await rec.async_run("start")
        spec = {item.key: item for item in rec.specs()}[yesterday]
        assert rec.shown(spec) is None
        due = rec.next_run
        assert due is not None
        assert (due - clock.now).total_seconds() == module.CONFIRM_DELAY
        driver.requests.clear()
        clock.now += timedelta(seconds=module.CONFIRM_DELAY)
        async_fire_time_changed(
            hass, dt_util_now() + timedelta(seconds=module.CONFIRM_DELAY + 1)
        )
        await hass.async_block_till_done()
        await hass.async_block_till_done()
        assert rec.runs[-1]["reason"] == "confirm"
        # Only the needed series, the last two days.
        assert [path for path, _ in driver.requests] == [BY_KEY["total_heat"].path]
        assert rec.shown(spec) is not None
        # The diagnostics name the hourly run as the next one (minute 20).
        assert rec.next_run is not None and rec.next_run.minute == 20
        assert rec.diagnostics()["next_run_in_seconds"] > 0
        # Close before the hourly run: no extra confirmation.
        clock.now = (local(2026, 11, 14, 11) + timedelta(minutes=12)).astimezone(UTC)
        count = len(rec._unsubs)
        rec._schedule_confirm()
        assert len(rec._unsubs) == count
    finally:
        rec.stop()


async def test_a_series_needed_again_after_a_while_is_read_whole(
    hass: HomeAssistant,
) -> None:
    """V5: series nothing needs are not read hourly; one needed again after
    more than a day and a half gets its whole history once."""
    clock = Clock(local(2026, 11, 14, 10) + timedelta(minutes=20))
    driver = Driver(clock)
    rec = await manager(hass, clock, [driver])
    heat = key("total_heat", Period.YESTERDAY)
    rec.request(heat)
    await rec.async_run("start")
    for _ in range(40):
        clock.now += timedelta(hours=1)
        driver.requests.clear()
        await rec.async_run("hourly")
        assert BY_KEY["outdoor_temperature"].path not in {
            path for path, query in driver.requests if query.get("sampleRate") == "PT1H"
        }
    driver.requests.clear()
    clock.now += timedelta(hours=1)
    rec.request(key("outdoor_temperature", Period.YESTERDAY))
    await rec.async_run("hourly")
    asked = dict(driver.requests)
    assert asked[BY_KEY["outdoor_temperature"].path] == {"sampleRate": "all"}
    assert asked[BY_KEY["total_heat"].path]["sampleRate"] == "PT1H"


async def test_the_import_reads_every_series_hourly(hass: HomeAssistant) -> None:
    clock = Clock(local(2026, 11, 14, 10) + timedelta(minutes=20))
    driver = Driver(clock)
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={OPTION_IMPORT: True})
    rec = await manager(hass, clock, [driver], entry=entry)
    await rec.async_run("start")
    driver.requests.clear()
    clock.now += timedelta(hours=1)
    await rec.async_run("hourly")
    assert {path for path, _ in driver.requests} >= {
        BY_KEY[item].path for item in PLANT
    }


async def test_a_comparison_copy_alone_needs_no_hourly_read(
    hass: HomeAssistant,
) -> None:
    """The calculated comparison comes from the counters, not the recording."""
    clock = Clock(local(2026, 11, 14, 10) + timedelta(minutes=20))
    driver = Driver(clock)
    rec = await manager(hass, clock, [driver])
    await rec.async_run("start")
    rec.request(key("total_heat", Period.YESTERDAY, CALCULATED))
    assert rec.hourly_series() == frozenset()
    rec.request(key("ch_heat", Period.YESTERDAY, CALCULATED))  # primary
    assert rec.hourly_series() == {"ch_heat"}


async def test_values_wanted_during_a_run_follow_after_it(
    hass: HomeAssistant,
) -> None:
    from tests.ha_test_helpers import async_fire_time_changed

    clock = Clock(local(2026, 11, 14, 10) + timedelta(minutes=20))
    driver = Driver(clock)
    rec = await manager(hass, clock, [driver])
    await rec.async_run("start")
    month = key("total_heat", Period.CURRENT_MONTH)
    rec.running = True  # a run is going on
    rec.request(month)
    async_fire_time_changed(hass, dt_util_now() + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert rec._values_after_run and month not in rec.published
    rec.running = False
    # The run publishes the requested values; a later need follows it.
    await rec.async_run("manual")
    assert month in rec.published
    assert rec._values_unsub is not None  # scheduled after the run
    async_fire_time_changed(hass, dt_util_now() + timedelta(seconds=4))
    await hass.async_block_till_done()
    assert rec._values_unsub is None
    # And the start timer names the hourly run as the next one.
    rec.running = True
    rec._due_start(dt_util_now())
    assert rec.next_run is not None and rec.next_run.minute == 20
    rec.running = False
