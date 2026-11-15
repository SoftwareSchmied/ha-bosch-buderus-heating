"""The plant's recordings: answers, slots, statistics rows and periods.

Synthetic answers in the shapes read from a real installation (the local
``sampleRate`` form with offsets and the cloud ``interval`` form). Each test
names the rule of the recordings it checks (``docs/recordings.md``).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.bosch_buderus_heating.gateway_identity import (
    GatewayNamespace,
    IdentityContext,
)
from custom_components.bosch_buderus_heating.local_api.catalog import make_request
from custom_components.bosch_buderus_heating.local_api.decoder import decode_resource
from custom_components.bosch_buderus_heating.local_api.models import (
    Recording,
    RecordingGroup,
    RecordingSample,
    ResourceReference,
)
from custom_components.bosch_buderus_heating.recordings.ledger import (
    CLOUD,
    LOCAL,
    Ledger,
)
from custom_components.bosch_buderus_heating.recordings.parse import (
    parse_cloud,
    parse_local,
    valid_interval,
)
from custom_components.bosch_buderus_heating.recordings.periods import (
    Period,
    period_value,
)
from custom_components.bosch_buderus_heating.recordings.series import (
    BY_KEY,
    CLOUD_PATHS,
    LOCAL_PATHS,
    SERIES,
)

Z = ZoneInfo("Europe/Berlin")
HEAT = BY_KEY["total_heat"]
OUTDOOR = BY_KEY["outdoor_temperature"]
STARTS = BY_KEY["total_starts"]
WORKING = BY_KEY["total_working_time"]


def fixed(moment: datetime) -> datetime:
    """As the gateway writes it: local time with its offset."""
    offset = moment.utcoffset()
    assert offset is not None
    return moment.astimezone(timezone(offset))


def local(year, month, day, hour=0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=Z)


def hours(start: datetime, values, *, end: datetime | None = None) -> RecordingGroup:
    if end is None:
        end = (start.astimezone(UTC) + timedelta(hours=len(values) - 1)).astimezone(Z)
    return RecordingGroup(
        "PT1H",
        fixed(start),
        fixed(end),
        tuple(RecordingSample(y, c) for y, c in values),
    )


def days(first: date, values) -> RecordingGroup:
    last = first + timedelta(days=len(values) - 1)
    return RecordingGroup(
        "P1D",
        fixed(datetime.combine(first, datetime.min.time(), Z)),
        fixed(datetime.combine(last, datetime.min.time(), Z)),
        tuple(RecordingSample(y, c) for y, c in values),
    )


def months(first: date, values) -> RecordingGroup:
    index = first.year * 12 + first.month - 1 + len(values) - 1
    last = date(index // 12, index % 12 + 1, 1)
    return RecordingGroup(
        "P1M",
        fixed(datetime.combine(first, datetime.min.time(), Z)),
        fixed(datetime.combine(last, datetime.min.time(), Z)),
        tuple(RecordingSample(y, c) for y, c in values),
    )


def recording(*groups, unit: str | None = "kWh") -> Recording:
    return Recording(
        ResourceReference("/recordings/x", "https://gateway/recordings/x"),
        "actual",
        unit,
        tuple(groups),
        (),
    )


def read(ledger: Ledger, at: datetime, *groups, full=False, unit="kWh") -> None:
    parsed = parse_local(recording(*groups, unit=unit), ledger.series, Z)
    assert parsed is not None
    ledger.merge(parsed, LOCAL, read_at=at.astimezone(UTC), zone=Z, full_history=full)


def utc(moment: datetime) -> int:
    return int(moment.timestamp())


# ---------------------------------------------------------------- catalogue
def test_the_candidates_are_the_local_catalogue_paths_without_placeholders() -> None:
    """Rule 15: only fixed paths; gas, solar and hybrid paths are candidates."""
    assert len(LOCAL_PATHS) == 35
    assert all("{" not in path for path in LOCAL_PATHS)
    assert {"total_burner", "solar", "total_heat", "outdoor_temperature"} <= set(BY_KEY)
    assert len(CLOUD_PATHS) == 9
    assert {series.key for series in SERIES if series.cloud} == {
        f"{mode}_{kind}"
        for mode in ("total", "ch", "dhw")
        for kind in ("heat", "compressor", "eheater")
    }


def test_a_real_shaped_local_answer_decodes_and_is_placed() -> None:
    """The local ``sampleRate`` form passes the decoder, also without a unit
    (starts), and its hours are placed in UTC."""
    request = make_request(
        "/recordings/heatSources/emon/total/numberOfStarts",
        query={
            "sampleRate": "PT1H",
            "startDate": "2026-11-13",
            "endDate": "2026-11-13",
        },
    )
    body = {
        "id": "/recordings/heatSources/emon/total/numberOfStarts"
        "?sampleRate=PT1H&startDate=2026-11-13&endDate=2026-11-13",
        "type": "yRecordingExtended",
        "writeable": 0,
        "recordable": 0,
        "recordedResource": {
            "id": "/heatSources/numberOfStarts",
            "uri": "https://192.0.2.1/heatSources/numberOfStarts",
        },
        "recordingType": "actual",
        "values": [
            {
                "sampleRate": "PT1H",
                "startTime": "2026-11-13T00:00:00+01:00",
                "endTime": "2026-11-13T23:00:00+01:00",
                "recording": [{"y": index % 2, "c": 1} for index in range(24)],
            }
        ],
    }
    observation = decode_resource(
        request,
        json.dumps(body).encode(),
        context=IdentityContext(GatewayNamespace.K40_LOCAL, "owner", "local", 2),
        observed_at=datetime(2026, 11, 14, tzinfo=UTC),
        observed_monotonic=1.0,
    )
    assert isinstance(observation.data, Recording)
    parsed = parse_local(observation.data, STARTS, Z)
    assert parsed is not None and len(parsed.samples) == 24
    assert parsed.samples[0].hour == utc(datetime(2026, 11, 12, 23, tzinfo=UTC))
    assert parsed.complete_days == {date(2026, 11, 13)}


# --------------------------------------------------------- 1: summer time
def test_spring_change_hours_counted_as_elapsed_are_placed() -> None:
    """Rule 1: 23 hours on 28 March 2027, each placed in UTC."""
    start = local(2027, 3, 27)
    count = 24 + 23 + 24
    group = hours(start, [(1.0, 1)] * count)
    assert group.start.utcoffset() != group.end.utcoffset()
    parsed = parse_local(recording(group), HEAT, Z)
    assert parsed is not None
    assert not parsed.unplaced_days and not parsed.issues
    day = [s for s in parsed.samples if s.day == date(2027, 3, 28)]
    assert len(day) == 23
    assert len({s.hour for s in parsed.samples}) == count


def test_autumn_change_has_25_hours() -> None:
    parsed = parse_local(
        recording(hours(local(2026, 10, 25), [(1.0, 1)] * 25)), HEAT, Z
    )
    assert parsed is not None
    assert [s.day for s in parsed.samples].count(date(2026, 10, 25)) == 25
    assert parsed.complete_days == {date(2026, 10, 25)}


def test_hours_counted_on_the_clock_keep_the_day_only() -> None:
    """Rule 1: 24 values on a day of 23 hours cannot be placed; the day of
    the change is imported as a day, the other days hour by hour."""
    start = local(2027, 3, 27)
    end = local(2027, 3, 29, 23)
    group = RecordingGroup(
        "PT1H",
        fixed(start),
        fixed(end),
        tuple(RecordingSample(1.0, 1) for _ in range(72)),
    )
    parsed = parse_local(recording(group), HEAT, Z)
    assert parsed is not None
    assert parsed.unplaced_days == {date(2027, 3, 28)}
    assert parsed.issues["hours_counted_on_clock"] == 1
    placed = [s for s in parsed.samples if s.hour is not None]
    assert len(placed) == 48
    ledger = Ledger(HEAT)
    now = local(2027, 4, 5, 12)
    read(ledger, now, group)
    assert ledger.day(date(2027, 3, 28))[:2] == [24.0, 24.0]
    assert ledger.start_import(0.0, Z)
    plan = ledger.plan(now.astimezone(UTC), Z)
    starts = [row[0] for row in plan.rows]
    # 24 hours, one row for the day of the change, 24 hours.
    assert len(plan.rows) == 49
    assert utc(local(2027, 3, 28)) in starts
    assert plan.rows[-1][3] == pytest.approx(72.0)


def test_a_mismatching_hour_count_is_not_used() -> None:
    start = local(2026, 11, 13)
    group = RecordingGroup(
        "PT1H",
        fixed(start),
        fixed(local(2026, 11, 13, 23)),
        tuple(RecordingSample(1.0, 1) for _ in range(20)),
    )
    parsed = parse_local(recording(group), HEAT, Z)
    assert parsed is not None and not parsed.samples
    assert parsed.issues["hour_count_mismatch"] == 1


def test_a_cloud_day_of_23_hours_with_24_values_is_a_day_only() -> None:
    payload = {
        "type": "yRecording",
        "unitOfMeasure": "kWh",
        "recording": [{"y": 1.0, "c": 1}] * 24,
    }
    parsed = parse_cloud(payload, HEAT, "2027-03-28", Z)
    assert parsed is not None
    assert parsed.unplaced_days == {date(2027, 3, 28)}
    assert all(sample.hour is None for sample in parsed.samples)
    normal = parse_cloud(payload, HEAT, "2026-11-13", Z)
    assert normal is not None and not normal.unplaced_days
    assert normal.samples[0].hour == utc(local(2026, 11, 13))


@pytest.mark.parametrize(
    ("interval", "valid"),
    [
        ("2026-11-13", True),
        ("2026-11", True),
        ("2026", True),
        ("2026-13", False),
        ("2026-02-30", False),
        ("2026-11-13&x=1", False),
        ("", False),
    ],
)
def test_cloud_intervals(interval: str, valid: bool) -> None:
    assert valid_interval(interval) is valid


# ---------------------------------------------------------- 5: complete
def test_the_running_hour_is_never_imported_and_an_hour_needs_two_reads() -> None:
    """Rule 5: an hour counts once it ended 15 minutes ago and two reads
    gave the same values."""
    ledger = Ledger(HEAT)
    first = local(2026, 11, 14, 10) + timedelta(minutes=20)
    values = [(1.0, 1)] * 35  # 13 Nov 00:00 to 14 Nov 10:00 (running)
    read(ledger, first, hours(local(2026, 11, 13), values))
    assert ledger.start_import(0.0, Z)
    assert ledger.plan(first.astimezone(UTC), Z).rows == []
    second = first + timedelta(hours=1)
    read(ledger, second, hours(local(2026, 11, 13), [*values, (0.4, 1)]))
    plan = ledger.plan(second.astimezone(UTC), Z)
    assert len(plan.rows) == 34
    assert plan.rows[-1][0] == utc(local(2026, 11, 14, 9))
    assert [row[3] for row in plan.rows] == pytest.approx(range(1, 35))
    # Nothing twice.
    assert ledger.plan(second.astimezone(UTC), Z).rows == []


def test_a_slot_read_while_running_is_live_and_never_final() -> None:
    ledger = Ledger(HEAT)
    at = local(
        2026,
        11,
        14,
        10,
    ) + timedelta(minutes=5)
    read(ledger, at, hours(local(2026, 11, 14, 9), [(1.0, 1), (0.1, 1)]))
    cell = ledger.hours[str(utc(local(2026, 11, 14, 9)))]
    assert cell[3:] == [0, False]  # ended 5 minutes ago: still live


# ------------------------------------------------------ 6: late corrections
def test_a_changed_final_hour_is_imported_again_from_there() -> None:
    """Rule 6: the last 48 hours are read on every run; a change of an
    imported hour imports again from that hour, with consistent sums."""
    ledger = Ledger(HEAT)
    base = [(1.0, 1)] * 34
    at = local(2026, 11, 14, 10) + timedelta(minutes=20)
    for step in range(2):
        read(
            ledger,
            at + timedelta(hours=step),
            hours(local(2026, 11, 13), base + [(0.0, 1)] * (step + 1)),
        )
    assert ledger.start_import(100.0, Z)
    assert len(ledger.plan((at + timedelta(hours=1)).astimezone(UTC), Z).rows) == 34
    changed = list(base)
    changed[29] = (2.0, 1)  # 14 Nov 05:00
    for step in (2, 3):
        read(
            ledger,
            at + timedelta(hours=step),
            hours(local(2026, 11, 13), changed + [(0.0, 1)] * (step + 1)),
        )
    plan = ledger.plan((at + timedelta(hours=3)).astimezone(UTC), Z)
    assert plan.clear is not None
    assert plan.clear[0] == utc(local(2026, 11, 14, 5))
    assert plan.rows[0][0] == utc(local(2026, 11, 14, 5))
    assert plan.rows[0][3] == pytest.approx(100.0 + 29 + 2.0)
    assert plan.rows[-1][3] == pytest.approx(100.0 + 34 + 1.0)
    assert ledger.stats["imported_again"] == 1
    assert ledger.stats["late_corrections"] == 1


@pytest.mark.parametrize("can_clear", [True, False])
def test_an_hour_turned_into_a_gap_is_neutral_where_nothing_is_cleared(
    can_clear,
) -> None:
    """Rule 6 on a recorder that clears whole statistics only (before
    2026.11): an imported hour that is now a gap is not written again, so
    it gets a neutral row with the sum before it; with a range to clear
    it is simply gone. A plan is taken over only on ``commit``."""
    ledger = Ledger(HEAT)
    base = [(1.0, 1)] * 34
    at = local(2026, 11, 14, 10) + timedelta(minutes=20)
    for step in range(2):
        read(
            ledger,
            at + timedelta(hours=step),
            hours(local(2026, 11, 13), base + [(0.0, 1)] * (step + 1)),
        )
    assert ledger.start_import(100.0, Z)
    assert len(ledger.plan((at + timedelta(hours=1)).astimezone(UTC), Z).rows) == 34
    changed = list(base)
    changed[29] = (0.0, 0)  # 14 Nov 05:00 is now a gap
    for step in (2, 3):
        read(
            ledger,
            at + timedelta(hours=step),
            hours(local(2026, 11, 13), changed + [(0.0, 1)] * (step + 1)),
        )
    now = (at + timedelta(hours=3)).astimezone(UTC)
    before = json.dumps(ledger.to_dict())
    plan = ledger.plan(now, Z, can_clear=can_clear, commit=False)
    assert json.dumps(ledger.to_dict()) == before  # nothing taken over yet
    gap = utc(local(2026, 11, 14, 5))
    rows = {row[0]: row for row in plan.rows}
    assert plan.clear is not None and plan.clear[0] == gap
    if can_clear:
        assert gap not in rows
    else:
        assert rows[gap] == (gap, 0.0, 0.0, pytest.approx(100.0 + 29))
    assert rows[gap + 3600][3] == pytest.approx(100.0 + 30)
    ledger.commit(plan)
    assert ledger.stats["imported_neutral"] == (0 if can_clear else 1)
    assert ledger.stats["imported_again"] == 1
    assert ledger.plan(now, Z, can_clear=can_clear).rows == []
    assert not ledger.imp["stale"]
    # Stored and read back, the position is the same.
    assert Ledger(HEAT, json.loads(json.dumps(ledger.to_dict()))).imp == ledger.imp


# --------------------------------------------------------- 7: gaps, c = 0
def test_c_zero_is_a_gap_and_future_placeholders_are_skipped() -> None:
    """Rule 7: no data is never 0; a placeholder ahead is not kept."""
    ledger = Ledger(HEAT)
    at = local(2026, 11, 20, 12)
    values = [(1.0, 1), (0.0, 0), (1.0, 1)] + [(1.0, 1)] * 21
    read(ledger, at, hours(local(2026, 11, 13), values))
    assert ledger.start_import(0.0, Z)
    plan = ledger.plan(at.astimezone(UTC), Z)
    assert utc(local(2026, 11, 13, 1)) not in [row[0] for row in plan.rows]
    assert len(plan.rows) == 23
    payload = {
        "type": "yRecording",
        "unitOfMeasure": "kWh",
        "recording": [{"y": 2.0, "c": 1}] * 14 + [{"y": 1.0, "c": 0}] * 16,
    }
    cloud = Ledger(HEAT)
    parsed = parse_cloud(payload, HEAT, "2026-11", Z)
    assert parsed is not None
    cloud.merge(parsed, CLOUD, read_at=local(2026, 11, 14, 20).astimezone(UTC), zone=Z)
    assert len(cloud.days) == 14  # the 16 days ahead are no gaps either
    assert not cloud.stats


# ----------------------------------------------- 8, 10: coarser resolution
def test_back_fill_uses_months_then_days_then_hours() -> None:
    """Rules 8 and 10: hours where the plant still has them, days before,
    the rest of a month before the first day; a rounded month is taken as
    it is."""
    ledger = Ledger(HEAT)
    at = local(2026, 11, 14, 20) + timedelta(minutes=20)
    read(
        ledger,
        at,
        months(date(2026, 10, 1), [(20.0, 1), (5.0, 90)]),
        days(date(2026, 10, 20), [(1.0, 24)] * 26),  # 20 Oct to 14 Nov (part)
        hours(local(2026, 11, 10), [(0.1, 1)] * (4 * 24 + 21)),
        full=True,
    )
    assert ledger.floor == "2026-10-01"
    assert ledger.start_import(0.0, Z)
    plan = ledger.plan(at.astimezone(UTC), Z)
    starts = [row[0] for row in plan.rows]
    # The rest of October before its first day (20 - 12 days) at 1 October.
    assert plan.rows[0][0] == utc(local(2026, 10, 1))
    assert plan.rows[0][1] == pytest.approx(8.0)
    assert utc(local(2026, 10, 20)) in starts
    assert utc(local(2026, 11, 9)) in starts  # the last day as a day
    # Hours from 10 November that ended more than 48 hours ago.
    assert utc(local(2026, 11, 10, 0)) in starts
    assert utc(local(2026, 11, 12, 20)) not in starts
    assert ledger.stats["imported_m"] == 1


def test_a_negative_rest_of_a_rounded_month_is_left_out() -> None:
    ledger = Ledger(HEAT)
    at = local(2026, 11, 20, 12)
    read(
        ledger,
        at,
        months(date(2026, 10, 1), [(10.0, 1)]),
        days(date(2026, 10, 20), [(0.85, 24)] * 12),  # 10.2 > 10
        full=True,
    )
    assert ledger.start_import(0.0, Z)
    plan = ledger.plan(at.astimezone(UTC), Z)
    assert plan.rows[0][0] == utc(local(2026, 10, 20))
    assert ledger.stats["negative_rest"] == 1


def test_a_long_gap_is_filled_by_the_day_and_a_missing_day_is_a_gap() -> None:
    """Rule 10: after a long stop the hours are gone: the days come in."""
    ledger = Ledger(HEAT)
    at = local(2026, 11, 20, 12)
    read(
        ledger,
        at,
        days(date(2026, 11, 1), [(2.0, 24), (2.0, 24)]),
        days(date(2026, 11, 4), [(3.0, 24)]),
        full=True,
    )
    assert ledger.start_import(0.0, Z)
    plan = ledger.plan(at.astimezone(UTC), Z)
    assert [row[1] for row in plan.rows] == [2.0, 2.0, 3.0]
    assert ledger.stats["import_gap_days"] == 1


# --------------------------------------------------------- 3, 13, 17
def test_a_wrong_clock_negative_and_implausible_values_are_discarded() -> None:
    """Rules 3 and 13: ahead by more than an hour, before the first known
    day, below 0 or above 1.5 x 60 kW per hour."""
    ledger = Ledger(HEAT)
    at = local(2026, 11, 14, 12)
    read(ledger, at, days(date(2026, 11, 10), [(1.0, 24)]), full=True)
    read(
        ledger,
        at,
        hours(local(2026, 11, 14, 14), [(1.0, 1)]),  # ahead
        days(date(2026, 11, 1), [(1.0, 24)]),  # before the first day
        hours(local(2026, 11, 13, 3), [(-1.0, 1), (500.0, 1)]),
    )
    assert ledger.stats["discarded_future"] == 1
    assert ledger.stats["discarded_before_history"] == 1
    assert ledger.stats["discarded_negative"] == 1
    assert ledger.stats["discarded_implausible"] == 1
    assert set(ledger.days) == {"2026-11-10"}
    assert not ledger.hours


def test_units_are_converted_and_an_unknown_unit_keeps_no_series() -> None:
    """Rule 17."""
    parsed = parse_local(
        recording(hours(local(2026, 11, 13), [(1500.0, 1)]), unit="Wh"), HEAT, Z
    )
    assert parsed is not None and parsed.samples[0].y == pytest.approx(1.5)
    seconds = parse_local(
        recording(hours(local(2026, 11, 13), [(1800.0, 1)]), unit="s"), WORKING, Z
    )
    assert seconds is not None and seconds.samples[0].y == pytest.approx(0.5)
    assert parse_local(recording(unit="J"), HEAT, Z) is None
    assert parse_local(recording(unit=None), HEAT, Z) is None
    assert parse_local(recording(unit=None), STARTS, Z) is not None
    cloud = {"type": "yRecording", "unitOfMeasure": "GJ", "recording": []}
    assert parse_cloud(cloud, HEAT, "2026", Z) is None


# ------------------------------------------------------ 12: data vanish
def test_a_shorter_history_deletes_nothing() -> None:
    ledger = Ledger(HEAT)
    at = local(2026, 11, 20, 12)
    read(ledger, at, days(date(2026, 11, 1), [(1.0, 24)] * 5), full=True)
    read(ledger, at, days(date(2026, 11, 4), [(1.0, 24)] * 2), full=True)
    assert ledger.stats["history_shrunk"] == 1
    assert len(ledger.days) == 5


# -------------------------------------------------- 14: local and cloud
def test_local_wins_over_the_cloud_and_differences_are_counted() -> None:
    ledger = Ledger(HEAT)
    at = local(2026, 11, 20, 12)
    payload = {
        "type": "yRecording",
        "unitOfMeasure": "kWh",
        "recording": [{"y": 2.0, "c": 1}] * 24,
    }
    parsed = parse_cloud(payload, HEAT, "2026-11-13", Z)
    assert parsed is not None
    ledger.merge(parsed, CLOUD, read_at=at.astimezone(UTC), zone=Z)
    read(ledger, at, hours(local(2026, 11, 13), [(2.5, 1)] + [(2.0, 1)] * 23))
    first = ledger.hours[str(utc(local(2026, 11, 13)))]
    assert first[:3] == [2.5, 1.0, LOCAL]
    # A later cloud value never replaces a local one.
    ledger.merge(parsed, CLOUD, read_at=at.astimezone(UTC), zone=Z)
    assert ledger.hours[str(utc(local(2026, 11, 13)))][2] == LOCAL
    assert ledger.stats["deviations_local_cloud"] >= 2


# ------------------------------------------------------ 9: a restart
def test_the_import_position_survives_a_restart() -> None:
    """Rule 9: stored per series; only missing hours follow."""
    ledger = Ledger(HEAT)
    at = local(2026, 11, 20, 12)
    read(ledger, at, hours(local(2026, 11, 13), [(1.0, 1)] * 24))
    assert ledger.start_import(0.0, Z)
    assert len(ledger.plan(at.astimezone(UTC), Z).rows) == 24
    stored = json.loads(json.dumps(ledger.to_dict()))
    again = Ledger(HEAT, stored)
    assert again.plan(at.astimezone(UTC), Z).rows == []
    read(again, at, hours(local(2026, 11, 14), [(1.0, 1)] * 2))
    plan = again.plan(at.astimezone(UTC), Z)
    assert [row[3] for row in plan.rows] == [25.0, 26.0]
    assert Ledger(HEAT, {"import": {"next": "x"}}).imp is None


def test_rows_beyond_the_window_are_dropped_with_their_sum() -> None:
    ledger = Ledger(HEAT)
    at = local(2026, 11, 14, 12)
    read(ledger, at, hours(local(2026, 11, 1), [(1.0, 1)] * 24))
    assert ledger.start_import(0.0, Z)
    ledger.plan(at.astimezone(UTC), Z)
    ledger.prune((at + timedelta(days=20)).astimezone(UTC))
    assert ledger.imp is not None and ledger.imp["rows"] == []
    assert ledger.imp["base"][0] == pytest.approx(24.0)
    assert not ledger.hours


# --------------------------------------------------- 22: temperatures
def test_temperatures_are_hourly_means() -> None:
    """Rule 22: the mean is y / c, never the raw sum."""
    ledger = Ledger(OUTDOOR)
    at = local(2026, 11, 20, 12)
    read(
        ledger,
        at,
        hours(local(2026, 11, 13), [(264.0, 60)] + [(300.0, 60)] * 23),
        unit="C",
    )
    assert ledger.start_import(0.0, Z)
    plan = ledger.plan(at.astimezone(UTC), Z)
    assert plan.rows[0][1] / plan.rows[0][2] == pytest.approx(4.4)
    value = period_value(ledger, Period.YESTERDAY, date(2026, 11, 14))
    assert value is not None
    assert value.value(True) == pytest.approx((264 + 300 * 23) / (60 * 24))
    bad = Ledger(OUTDOOR)
    read(bad, at, hours(local(2026, 11, 13), [(9000.0, 60)]), unit="C")
    assert bad.stats["discarded_implausible"] == 1


# ------------------------------------------------- 2, 4: periods
def test_periods_follow_the_plant_days_and_months() -> None:
    ledger = Ledger(HEAT)
    at = local(2026, 11, 14, 20)
    read(
        ledger,
        at,
        months(
            date(2026, 7, 1), [(31.0, 1), (100.0, 1), (90.0, 1), (700.0, 1), (60.0, 9)]
        ),
        days(date(2026, 7, 27), [(1.0, 24)] * (5 + 31 + 30 + 31 + 14)),
        full=True,
    )
    # The next hourly read confirms yesterday.
    read(ledger, at + timedelta(hours=1), days(date(2026, 11, 13), [(1.0, 24)] * 2))
    today = date(2026, 11, 14)
    yesterday = period_value(ledger, Period.YESTERDAY, today)
    assert yesterday is not None and yesterday.final
    assert (yesterday.start, yesterday.end) == (date(2026, 11, 13), today)
    # July only from its month value (days from the 27th); August from days.
    july = period_value(ledger, Period.PREVIOUS_MONTH, date(2026, 8, 3))
    assert july is not None and july.resolution == "monthly" and july.y == 31.0
    october = period_value(ledger, Period.PREVIOUS_MONTH, today)
    assert october is not None and october.resolution == "daily"
    assert october.y == 31.0 and october.final
    current = period_value(ledger, Period.CURRENT_MONTH, today)
    assert current is not None and not current.final and current.y == 14.0
    year = period_value(ledger, Period.CURRENT_YEAR, today)
    assert year is not None and year.y == pytest.approx(31 + 31 + 30 + 31 + 14)
    assert year.resolution == "monthly" and not year.final
    assert period_value(ledger, Period.PREVIOUS_YEAR, today) is None


def test_a_month_just_ended_is_final_only_after_a_confirmed_read() -> None:
    """Rule 4: the previous month switches once a read after its end
    confirms it."""
    ledger = Ledger(HEAT)
    at = local(
        2026,
        11,
        30,
        23,
    ) + timedelta(minutes=20)
    read(ledger, at, days(date(2026, 11, 1), [(1.0, 24)] * 30), full=True)
    december = date(2026, 12, 1)
    value = period_value(ledger, Period.PREVIOUS_MONTH, december)
    assert value is not None and not value.final
    after = local(2026, 12, 1, 0) + timedelta(minutes=20)
    # The hourly reads of the days before (yesterday and today each time).
    three = [(1.0, 24)] * 3
    read(ledger, after, days(date(2026, 11, 28), [*three, (0.1, 1)]))
    value = period_value(ledger, Period.PREVIOUS_MONTH, december)
    assert value is not None and not value.final  # read once after its end
    read(
        ledger, after + timedelta(hours=1), days(date(2026, 11, 28), [*three, (0.2, 2)])
    )
    value = period_value(ledger, Period.PREVIOUS_MONTH, december)
    assert value is not None and value.final and value.y == 30.0


# ------------------------------------------------------ cloud client, docs
async def test_the_cloud_client_reads_a_recording_with_a_checked_interval() -> None:
    from pathlib import Path
    from unittest.mock import AsyncMock, MagicMock

    from custom_components.bosch_buderus_heating.pointt import PointTClient

    client = PointTClient(MagicMock(), "token")
    client._request = AsyncMock(return_value={"type": "yRecording"})  # type: ignore[method-assign]
    assert await client.get_recording(
        "gw", "/recordings/heatSources/emon/total/outputProduced", "2026-11-13"
    ) == {"type": "yRecording"}
    url = client._request.await_args.args[1]
    assert url == (
        "gateways/gw/resource/recordings/heatSources/emon/total/outputProduced"
        "?interval=2026-11-13"
    )
    assert client._request.await_args.kwargs["resource_path"] == (
        "/recordings/heatSources/emon/total/outputProduced"
    )
    for path, interval in (
        ("/heatSources/emon/totalConsumption", "2026"),
        ("/recordings/heatSources/emon/total/outputProduced", "2026-11-13&a=b"),
        ("/recordings/heatSources/emon/total/outputProduced", "1999"),
        ("/recordings/heatSources/emon/total/outputProduced", "2026-02-30"),
    ):
        with pytest.raises(ValueError):
            await client.get_recording("gw", path, interval)
    docs = (Path(__file__).parents[1] / "docs" / "recordings.md").read_text("utf-8")
    # Rule 21: the energy dashboard must not count the same energy twice.
    assert "**either** these statistics **or** the counter entities" in docs


def test_a_slot_read_once_is_final_once_it_is_48_hours_old() -> None:
    """Rule 6: corrections come within 48 hours; a slot read once and not
    read again (an outage) counts after that and is imported."""
    ledger = Ledger(HEAT)
    at = local(2026, 11, 14, 1) + timedelta(minutes=20)
    read(ledger, at, hours(local(2026, 11, 13), [(1.0, 1)] * 24))
    assert ledger.start_import(0.0, Z)
    assert ledger.plan(at.astimezone(UTC), Z).rows == []
    later = at + timedelta(hours=48)
    plan = ledger.plan(later.astimezone(UTC), Z)
    assert len(plan.rows) == 24
