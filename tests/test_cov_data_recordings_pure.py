"""Edge cases of the recordings building blocks: answers that are malformed or
partial, unreadable sources, names and counters of the period entities."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.bosch_buderus_heating.local_api.models import (
    Recording,
    RecordingGroup,
    RecordingSample,
    ResourceReference,
)
from custom_components.bosch_buderus_heating.local_read import (
    ReadProblem,
    ReadResult,
    ReadState,
)
from custom_components.bosch_buderus_heating.pointt import (
    RateLimited,
    ResourceForbidden,
    ResourceNotFound,
    UnexpectedHttpStatus,
)
from custom_components.bosch_buderus_heating.recordings import sources
from custom_components.bosch_buderus_heating.recordings.ledger import (
    CLOUD,
    LOCAL,
    Ledger,
)
from custom_components.bosch_buderus_heating.recordings.measures import (
    CALCULATED,
    RECORDING,
    EntitySpec,
    entity_name,
    entity_specs,
    measure_name,
    measures,
    statistic_name,
)
from custom_components.bosch_buderus_heating.recordings.parse import (
    Rate,
    Sample,
    interval_text,
    parse_cloud,
    parse_local,
    rejection,
)
from custom_components.bosch_buderus_heating.recordings.periods import (
    Period,
    PeriodValue,
    combine,
    period_value,
)
from custom_components.bosch_buderus_heating.recordings.series import (
    BY_KEY,
    SERIES,
    Quantity,
)
from custom_components.bosch_buderus_heating.recordings.sources import (
    Answer,
    Outcome,
)

Z = ZoneInfo("Europe/Berlin")
HEAT = BY_KEY["total_heat"]
OUTDOOR = BY_KEY["outdoor_temperature"]
DASH = "\N{EN DASH}"


def _group(rate: str, start: datetime, end: datetime, values: list[Any]) -> Any:
    return RecordingGroup(
        rate,
        start,
        end,
        tuple(RecordingSample(y, c) for y, c in values),
    )


def _recording(*groups: Any, unit: str | None = "kWh") -> Recording:
    return Recording(
        ResourceReference("/recordings/x", "https://gateway/recordings/x"),
        "actual",
        unit,
        tuple(groups),
        (),
    )


def _at(day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=Z)


# ------------------------------------------------------------ local parsing
def test_an_hour_group_that_does_not_start_on_the_hour_is_not_used() -> None:
    group = _group("PT1H", _at(13, 0, 30), _at(13, 1, 30), [(1.0, 1), (1.0, 1)])
    parsed = parse_local(_recording(group), HEAT, Z)
    assert parsed is not None
    assert not parsed.samples
    assert parsed.issues["hour_group_not_aligned"] == 1


@pytest.mark.parametrize(
    "bad",
    [
        (None, 1),
        ("abc", 1),
        (True, 1),
        (float("nan"), 1),
        (float("inf"), 1),
        (1.0, None),
        (1.0, -1),
    ],
)
def test_an_invalid_hour_value_is_skipped_and_counted(bad: tuple[Any, Any]) -> None:
    group = _group("PT1H", _at(13, 0), _at(13, 2), [(1.0, 1), bad, (2.0, 1)])
    parsed = parse_local(_recording(group), HEAT, Z)
    assert parsed is not None
    assert [s.y for s in parsed.samples] == [1.0, 2.0]
    assert parsed.issues["invalid_value"] == 1


def test_calendar_groups_with_a_wrong_count_or_bad_values_are_handled() -> None:
    wrong_days = _group("P1D", _at(1), _at(5), [(1.0, 24)] * 3)
    wrong_months = _group(
        "P1M", datetime(2026, 1, 1, tzinfo=Z), datetime(2026, 4, 1, tzinfo=Z), [(1, 1)]
    )
    parsed = parse_local(_recording(wrong_days, wrong_months), HEAT, Z)
    assert parsed is not None and not parsed.samples
    assert parsed.issues["day_count_mismatch"] == 1
    assert parsed.issues["month_count_mismatch"] == 1
    bad = _group("P1D", _at(1), _at(3), [(1.0, 24), (None, 24), (3.0, -1)])
    parsed = parse_local(_recording(bad), HEAT, Z)
    assert parsed is not None
    assert [s.day for s in parsed.samples] == [date(2026, 11, 1)]
    assert parsed.issues["invalid_value"] == 2


def test_an_unknown_sample_rate_is_counted_and_an_unknown_unit_gives_nothing() -> None:
    odd = _group("PT5M", _at(13), _at(13), [(1.0, 1)])
    parsed = parse_local(_recording(odd), HEAT, Z)
    assert parsed is not None and parsed.offset is None
    assert parsed.issues["unknown_rate"] == 1
    assert parse_local(_recording(odd, unit="BTU"), HEAT, Z) is None


# ------------------------------------------------------------ cloud parsing
@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "text",
        {"type": "yRecordingExtended", "recording": []},
        {"type": "yRecording"},
        {"type": "yRecording", "recording": "x"},
        {"type": "yRecording", "recording": [], "unitOfMeasure": "BTU"},
        {"type": "yRecording", "recording": [], "unitOfMeasure": 5},
    ],
)
def test_a_cloud_answer_that_is_no_recording_gives_nothing(payload: object) -> None:
    assert parse_cloud(payload, HEAT, "2026-11-13", Z) is None


def test_a_cloud_answer_for_an_invalid_interval_gives_nothing() -> None:
    payload = {"type": "yRecording", "unitOfMeasure": "kWh", "recording": []}
    assert parse_cloud(payload, HEAT, "2026-13", Z) is None


def _cloud(rows: list[Any]) -> dict[str, Any]:
    return {"type": "yRecording", "unitOfMeasure": "kWh", "recording": rows}


def test_cloud_rows_that_are_not_usable_are_skipped_in_every_interval() -> None:
    rows: list[Any] = [{"y": 1.0, "c": 1}, "x", {"y": "a", "c": 1}, {"y": 1, "c": -1}]
    day = parse_cloud(_cloud(rows * 6), HEAT, "2026-11-13", Z)
    assert day is not None
    assert len(day.samples) == 6 and day.issues["invalid_value"] == 18
    assert day.complete_days == {date(2026, 11, 13)}
    month = parse_cloud(_cloud(rows * 7 + rows[:2]), HEAT, "2026-11", Z)
    assert month is not None
    assert all(s.rate is Rate.DAY for s in month.samples)
    assert len(month.samples) == 8 and month.issues["invalid_value"] == 22
    year = parse_cloud(_cloud(rows * 3), HEAT, "2026", Z)
    assert year is not None
    assert [s.day for s in year.samples] == [
        date(2026, 1, 1),
        date(2026, 5, 1),
        date(2026, 9, 1),
    ]
    assert year.issues["invalid_value"] == 9


def test_cloud_answers_with_a_wrong_number_of_values_are_not_placed() -> None:
    rows = [{"y": 1.0, "c": 1}]
    month = parse_cloud(_cloud(rows * 29), HEAT, "2026-11", Z)
    assert month is not None and not month.samples
    assert month.issues["day_count_mismatch"] == 1
    year = parse_cloud(_cloud(rows * 11), HEAT, "2026", Z)
    assert year is not None and not year.samples
    assert year.issues["month_count_mismatch"] == 1
    day = parse_cloud(_cloud(rows * 20), HEAT, "2026-11-13", Z)
    assert day is not None
    assert day.unplaced_days == {date(2026, 11, 13)}
    assert day.samples and all(s.hour is None for s in day.samples)


def test_interval_text_names_day_month_and_year() -> None:
    value = date(2026, 3, 7)
    assert interval_text(value, Rate.HOUR) == "2026-03-07"
    assert interval_text(value, Rate.DAY) == "2026-03"
    assert interval_text(value, Rate.MONTH) == "2026"


# ------------------------------------------------------------ plausibility
def _sample(rate: Rate, y: float, c: float, day: date = date(2026, 11, 13)) -> Sample:
    return Sample(rate, day, None, y, c)


def _reject(sample: Sample, series: Any = HEAT, floor: date | None = None) -> Any:
    now = datetime(2026, 12, 1, tzinfo=UTC)
    return rejection(sample, series, now=now, zone=Z, floor=floor)


@pytest.mark.parametrize(
    ("rate", "y", "reason"),
    [
        (Rate.HOUR, -1.0, "negative"),
        (Rate.HOUR, 91.0, "implausible"),
        (Rate.HOUR, 90.0, None),
        (Rate.DAY, 90.0 * 25 + 1, "implausible"),
        (Rate.MONTH, 90.0 * (24 * 31 + 1) + 1, "implausible"),
        (Rate.MONTH, 100.0, None),
    ],
)
def test_energy_sums_are_checked_against_the_length_of_their_slot(
    rate: Rate, y: float, reason: str | None
) -> None:
    assert _reject(_sample(rate, y, 1)) == reason


def test_starts_and_working_time_have_their_own_limits() -> None:
    starts = BY_KEY["total_starts"]
    working = BY_KEY["total_working_time"]
    assert starts.quantity is Quantity.COUNT
    assert _reject(_sample(Rate.HOUR, 61, 1), starts) == "implausible"
    assert _reject(_sample(Rate.HOUR, 60, 1), starts) is None
    assert _reject(_sample(Rate.HOUR, 1.0, 1), working) is None
    assert _reject(_sample(Rate.HOUR, 1.5, 1), working) == "implausible"


def test_samples_from_the_future_or_before_the_history_are_rejected() -> None:
    now = datetime(2026, 11, 13, 12, tzinfo=UTC)
    future = Sample(
        Rate.HOUR, date(2026, 11, 13), int((now + timedelta(hours=3)).timestamp()), 1, 1
    )
    assert rejection(future, HEAT, now=now, zone=Z, floor=None) == "future"
    assert _reject(_sample(Rate.DAY, 1, 1, date(2009, 12, 31))) == "before_history"
    assert (
        _reject(_sample(Rate.DAY, 1, 1, date(2026, 10, 1)), floor=date(2026, 11, 1))
        == "before_history"
    )


def test_temperature_means_outside_the_range_are_rejected() -> None:
    assert _reject(_sample(Rate.HOUR, 9000, 60), OUTDOOR) == "implausible"
    assert _reject(_sample(Rate.HOUR, -4000, 60), OUTDOOR) == "implausible"
    assert _reject(_sample(Rate.HOUR, 600, 60), OUTDOOR) is None


# ------------------------------------------------------------ the sources
class _Driver:
    def __init__(self, result: Any = None, *, delay: float = 0.0) -> None:
        self.result = result
        self.delay = delay
        self.requests: list[Any] = []

    async def read_explicit(self, request: Any) -> Any:
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.result


def _result(state: ReadState, problem: str | None = None, data: Any = None) -> Any:
    observation = SimpleNamespace(data=data) if data is not None else None
    return ReadResult(
        None,  # type: ignore[arg-type]
        state,
        problem=problem,
        observation=observation,  # type: ignore[arg-type]
    )


def test_the_local_query_for_a_full_or_a_recent_read() -> None:
    assert sources.local_query(True, date(2026, 11, 14)) == {"sampleRate": "all"}
    assert sources.local_query(False) == {"sampleRate": "all"}
    assert sources.local_query(False, date(2026, 11, 14)) == {
        "sampleRate": "PT1H",
        "startDate": "2026-11-12",
        "endDate": "2026-11-14",
    }


async def test_an_invalid_local_query_is_a_failure_without_a_request() -> None:
    driver = _Driver()
    answer = await sources.read_local(driver, HEAT, {"bogus": "x"})
    assert answer == Answer(Outcome.FAILED)
    assert driver.requests == []


async def test_a_local_read_that_hangs_is_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sources, "READ_SECONDS", 0.01)
    driver = _Driver(delay=1.0)
    answer = await sources.read_local(driver, HEAT, {"sampleRate": "all"})
    assert answer.outcome is Outcome.TIMEOUT


@pytest.mark.parametrize(
    ("result", "outcome"),
    [
        (None, Outcome.UNREACHABLE),
        (_result(ReadState.SUCCESS), Outcome.FAILED),
        (_result(ReadState.SUCCESS, data={"not": "a recording"}), Outcome.FAILED),
        (_result(ReadState.DEFERRED, ReadProblem.BUSY), Outcome.BUSY),
        (_result(ReadState.PATH_ERROR, ReadProblem.NOT_FOUND), Outcome.NOT_FOUND),
        (_result(ReadState.PATH_ERROR, ReadProblem.FORBIDDEN), Outcome.FORBIDDEN),
        (_result(ReadState.PATH_ERROR, ReadProblem.HTTP_ERROR), Outcome.FAILED),
        (
            _result(ReadState.ACCESS_ERROR, ReadProblem.AUTH_REQUIRED),
            Outcome.UNREACHABLE,
        ),
    ],
)
async def test_every_local_answer_maps_to_one_outcome(
    result: Any, outcome: Outcome
) -> None:
    driver = _Driver(result)
    answer = await sources.read_local(driver, HEAT, {"sampleRate": "all"})
    assert answer.outcome is outcome
    assert len(driver.requests) == 1
    assert answer.data is None


async def test_a_local_success_carries_the_recording() -> None:
    recording = _recording()
    driver = _Driver(_result(ReadState.SUCCESS, data=recording))
    answer = await sources.read_local(driver, HEAT, {"sampleRate": "all"})
    assert answer == Answer(Outcome.OK, recording)


class _Client:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str, str]] = []

    async def get_recording(self, gateway: str, path: str, interval: str) -> Any:
        self.calls.append((gateway, path, interval))
        if self.error is not None:
            raise self.error
        return {"type": "yRecording"}


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (ResourceNotFound("/x", 404), Outcome.NOT_FOUND),
        (ResourceForbidden("/x", 403), Outcome.FORBIDDEN),
        (RateLimited(30.0), Outcome.RATE_LIMITED),
        (UnexpectedHttpStatus(500), Outcome.FAILED),
        (ValueError("bad interval"), Outcome.FAILED),
        (None, Outcome.OK),
    ],
)
async def test_every_cloud_error_maps_to_one_outcome(
    error: BaseException | None, outcome: Outcome
) -> None:
    client = _Client(error)
    answer = await sources.read_cloud(client, "gw", HEAT, "2026-11-13")
    assert answer.outcome is outcome
    assert client.calls == [("gw", HEAT.path, "2026-11-13")]
    assert (answer.data is not None) is (outcome is Outcome.OK)


# ------------------------------------------------------------------ measures
def _all() -> dict[str, Any]:
    return {m.key: m for m in measures({s.key for s in SERIES})}


def test_electricity_of_a_plant_without_that_series_sums_the_parts() -> None:
    found = {m.key: m for m in measures(["total_compressor", "total_eheater", "bogus"])}
    assert set(found) == {"total_compressor", "total_eheater", "total_electricity"}
    total = found["total_electricity"]
    assert total.parts == ("total_compressor", "total_eheater")
    assert total.derived_from() == [BY_KEY[p].path for p in total.parts]
    assert total.path_id == "heatSources:emon:total:electricity"
    assert measures(["ch_heat"])[0].primary == CALCULATED
    assert measures(["total_heat"])[0].primary == RECORDING


def test_a_mode_without_electricity_parts_has_no_electricity_measure() -> None:
    keys = {m.key for m in measures(["ch_heat", "cooling_heat"])}
    assert keys == {"ch_heat", "cooling_heat"}


def test_a_recorded_electricity_series_is_not_added_twice() -> None:
    if "total_electricity" not in BY_KEY:
        pytest.skip("the catalogue has no recorded electricity series")
    keys = [m.key for m in measures(["total_electricity", "total_compressor"])]
    assert keys.count("total_electricity") == 1


def test_counters_follow_the_kind_and_mode_of_the_series() -> None:
    found = _all()

    def suffixes(key: str) -> tuple[str, ...] | None:
        counter = found[key].counter
        return None if counter is None else counter.suffixes

    assert suffixes("outdoor_temperature") == ("gateway:outdoor_temperature",)
    assert suffixes("supply_temperature") == ("gateway:supply_temperature",)
    assert suffixes("total_starts") == (
        "heatSources:numberOfStarts:total",
        "heatSources:numberOfStarts",
    )
    assert suffixes("ch_starts") == ("heatSources:numberOfStarts:ch",)
    assert suffixes("total_working_time") == ("heatSources:workingTime:totalSystem",)
    assert suffixes("ch_working_time") is None
    assert suffixes("total_heat") == ("gateway:total_heat_produced",)
    assert suffixes("ch_compressor") == ("heatSources:emon:chConsumption:compressor",)
    assert suffixes("cooling_heat") == (
        "heatSources:emon:coolingConsumption:outputProduced",
    )
    assert suffixes("total_burner") is None


def test_the_anchor_of_a_measure_places_it_with_the_matching_counter() -> None:
    found = _all()
    assert found["outdoor_temperature"].anchor.endswith("outdoor_t1")
    assert found["supply_temperature"].anchor.endswith("actualSupplyTemperature")
    assert found["total_starts"].anchor.endswith("numberOfStarts")
    assert found["total_working_time"].anchor.endswith("totalSystem")
    assert found["total_heat"].anchor.endswith("totalConsumption")
    assert found["outdoor_temperature"].mean
    assert not found["total_heat"].mean


def test_only_energy_with_a_counter_has_a_comparison_entity() -> None:
    found = _all()
    assert found["total_heat"].methods == (RECORDING, CALCULATED)
    assert found["ch_heat"].methods == (CALCULATED, RECORDING)
    assert found["outdoor_temperature"].methods == (RECORDING,)
    assert found["total_starts"].methods == (RECORDING,)
    specs = entity_specs([found["total_heat"]])
    assert len(specs) == 2 * len(Period)
    assert sum(spec.key.endswith(":calculated") for spec in specs) == len(Period)
    enabled = [spec for spec in specs if spec.enabled_default]
    assert len(enabled) == 1
    assert enabled[0].period is Period.YESTERDAY and enabled[0].primary


def test_every_series_has_a_statistic_name_in_both_languages() -> None:
    for series in SERIES:
        assert statistic_name("de", series).endswith("(Anlagenaufzeichnung)")
        assert statistic_name("en", series).endswith("(system recording)")


def test_names_of_modes_and_kinds() -> None:
    found = _all()
    assert measure_name("en", found["cooling_heat"]) == (
        f"Cooling {DASH} Cooling energy produced"
    )
    assert measure_name("de", found["dhw_compressor"]) == (
        f"Warmwasser {DASH} Strom Verdichter"
    )
    assert measure_name("en", found["total_eheater"]) == (
        f"Total {DASH} Electric heater electricity"
    )
    assert measure_name("en", found["outdoor_temperature"]) == (
        "Outdoor temperature (mean)"
    )
    assert measure_name("en", found["supply_temperature"]) == (
        "Supply temperature (mean)"
    )
    assert measure_name("en", found["total_starts"]) == f"Total {DASH} Starts"
    assert measure_name("de", found["total_working_time"]) == f"Gesamt {DASH} Laufzeit"
    for key, text in (
        ("pool_heat", f"Pool {DASH} Heat produced"),
        ("total_burner", f"Total {DASH} Burner energy"),
        ("ventilation_heat", f"Ventilation {DASH} Heat produced"),
        ("ventilation_heat_recovered", f"Ventilation {DASH} Heat recovered"),
        ("solar", "Solar yield"),
        ("ventilation", "Ventilation energy"),
    ):
        if key in found:
            assert measure_name("en", found[key]) == text


def test_entity_names_carry_the_period_and_the_method() -> None:
    measure = _all()["total_heat"]
    expected = {
        Period.YESTERDAY: "yesterday",
        Period.PREVIOUS_MONTH: "previous month",
        Period.PREVIOUS_YEAR: "previous year",
        Period.CURRENT_MONTH: "this month",
        Period.CURRENT_YEAR: "this year",
    }
    for period, text in expected.items():
        recorded = entity_name("en", EntitySpec(measure, period, RECORDING))
        calculated = entity_name("en", EntitySpec(measure, period, CALCULATED))
        assert recorded == f"Total {DASH} Heat produced {text} (system recording)"
        assert calculated == f"Total {DASH} Heat produced {text} (calculated)"
        german = entity_name("de", EntitySpec(measure, period, RECORDING))
        assert german.endswith("(Anlagenaufzeichnung)")
        assert entity_name("de", EntitySpec(measure, period, CALCULATED)).endswith(
            "(berechnet)"
        )


# ------------------------------------------------------------------- periods
def _ledger_with_days(first: date, count: int, *, final: bool = True) -> Ledger:
    ledger = Ledger(HEAT)
    read_at = datetime(2026, 11, 20, 12, tzinfo=Z).astimezone(UTC)
    group = _group(
        "P1D",
        datetime.combine(first, datetime.min.time(), Z),
        datetime.combine(first + timedelta(days=count - 1), datetime.min.time(), Z),
        [(1.0, 24)] * count,
    )
    parsed = parse_local(_recording(group), HEAT, Z)
    assert parsed is not None
    ledger.merge(parsed, LOCAL, read_at=read_at, zone=Z)
    if final:
        ledger.merge(parsed, LOCAL, read_at=read_at, zone=Z)
    return ledger


def test_the_period_of_a_series_without_data_is_none() -> None:
    ledger = Ledger(HEAT)
    for period in Period:
        assert period_value(ledger, period, date(2026, 11, 14)) is None
    assert combine([]) is None


def test_a_month_with_a_gap_that_has_no_month_value_is_unknown() -> None:
    ledger = _ledger_with_days(date(2026, 10, 1), 10)
    del ledger.days["2026-10-05"]
    assert period_value(ledger, Period.PREVIOUS_MONTH, date(2026, 11, 14)) is None
    assert period_value(ledger, Period.PREVIOUS_YEAR, date(2027, 2, 3)) is None


def test_a_running_month_with_a_gap_uses_the_known_days() -> None:
    ledger = _ledger_with_days(date(2026, 11, 1), 10)
    del ledger.days["2026-11-05"]
    value = period_value(ledger, Period.CURRENT_MONTH, date(2026, 11, 12))
    assert value is not None
    assert value.resolution == "daily" and not value.final
    assert value.y == pytest.approx(9.0)
    # The plant's month value wins where it is larger.
    ledger.months["2026-11-01"] = [40.0, 1.0, LOCAL, 1, False]
    larger = period_value(ledger, Period.CURRENT_MONTH, date(2026, 11, 12))
    assert larger is not None
    assert larger.resolution == "monthly" and larger.y == 40.0 and not larger.final


def test_a_running_month_known_only_by_its_month_value() -> None:
    ledger = Ledger(HEAT)
    ledger.months["2026-10-01"] = [5.0, 1.0, LOCAL, 1, True]
    ledger.months["2026-11-01"] = [12.0, 1.0, LOCAL, 1, False]
    value = period_value(ledger, Period.CURRENT_MONTH, date(2026, 11, 12))
    assert value is not None and value.y == 12.0 and value.resolution == "monthly"
    assert not value.final


def test_a_running_month_without_any_known_day_or_month_value_is_none() -> None:
    ledger = Ledger(HEAT)
    ledger.months["2026-10-01"] = [5.0, 1.0, LOCAL, 1, True]
    assert period_value(ledger, Period.CURRENT_MONTH, date(2026, 11, 12)) is None


def test_a_mean_month_ignores_a_larger_month_value() -> None:
    ledger = Ledger(OUTDOOR)
    ledger.days["2026-11-01"] = [240.0, 24.0, LOCAL, 1, True]
    ledger.days["2026-11-03"] = [480.0, 24.0, LOCAL, 1, True]
    ledger.months["2026-11-01"] = [99999.0, 100.0, LOCAL, 1, False]
    value = period_value(ledger, Period.CURRENT_MONTH, date(2026, 11, 5))
    assert value is not None and value.resolution == "daily"
    assert value.value(True) == pytest.approx(15.0)
    empty = PeriodValue(
        date(2026, 11, 1), date(2026, 11, 2), 5, 0, True, "daily", frozenset()
    )
    assert empty.value(True) is None


def test_the_year_is_unknown_when_a_month_in_it_is_missing() -> None:
    ledger = _ledger_with_days(date(2026, 1, 1), 31)
    ledger.months["2026-03-01"] = [3.0, 1.0, LOCAL, 1, True]
    assert period_value(ledger, Period.CURRENT_YEAR, date(2026, 11, 14)) is None


def test_combined_values_are_final_only_when_every_part_is() -> None:
    first = PeriodValue(
        date(2026, 11, 1), date(2026, 11, 2), 1, 1, True, "hourly", frozenset({LOCAL})
    )
    second = PeriodValue(
        date(2026, 11, 1), date(2026, 11, 2), 2, 1, False, "monthly", frozenset({CLOUD})
    )
    both = combine([first, second])
    assert both is not None
    assert both.y == 3 and both.c == 2 and not both.final
    assert both.resolution == "monthly"
    assert both.sources == frozenset({LOCAL, CLOUD})
