"""Energy history rules without Home Assistant: malformed answers, bad
numbers, a corrupt stored state and the sums across gaps and folds."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from custom_components.bosch_buderus_heating import rrc_energy
from custom_components.bosch_buderus_heating.rrc_energy import (
    FIRST_FILL_DAYS,
    KEYS,
    RECOMPUTE_DAYS,
    HistoryState,
    history_items,
    merge,
    parse_records,
)
from tests.test_rrc_energy_history import (
    NOW,
    OLD_FIRMWARE,
    TODAY,
    Sink,
    Source,
    cycle,
    day_record,
)

ENERGY = KEYS[0].slug  # heat_pump_electricity_heating
TEMPERATURE = KEYS[-1].slug  # outdoor_temperature


# ------------------------------------------------------------ the records
def test_values_that_are_no_plausible_numbers_are_left_out_of_a_record():
    (record,), invalid = parse_records(
        [
            {
                "d": "01-10-2026",
                "eCh": True,  # a flag is no amount
                "eHw": "n/a",
                "eCo": -3.0,  # a sum must never run backwards
                "T": 99.0,  # not a temperature
                "pS": "2.5",
                "pChH": float("inf"),
            }
        ]
    )
    assert invalid == 0
    assert dict(record.values) == {"solar_electricity_produced": 2.5}


def test_entries_that_are_no_records_are_counted_as_invalid():
    records, invalid = parse_records(
        ["x", 3, None, {"d": "31-02-2026"}, {"d": "01-10-2026", "eCh": 1.0}]
    )
    assert len(records) == 1 and invalid == 4


def test_a_temperature_below_the_plausible_range_is_left_out():
    (record,), _ = parse_records([{"d": "01-10-2026", "T": -61.0, "pS": 1.0}])
    assert TEMPERATURE not in record.values
    (record,), _ = parse_records([{"d": "01-10-2026", "T": -59.5}])
    assert record.values[TEMPERATURE] == -59.5


def test_the_record_list_is_found_in_every_answer_form():
    items = [{"d": "01-10-2026"}]
    assert history_items(items) == items
    assert history_items(None, [items[0]]) == items
    assert history_items({"value": items}) == items
    assert history_items({"value": "x"}) == []
    assert history_items("text") == []


# ------------------------------------------------------- the stored state
def test_a_stored_state_with_damaged_parts_keeps_only_the_usable_ones():
    stored = HistoryState().to_dict()
    stored.update(
        {
            "last_day": "2026-10-10",
            "recent": "not a mapping",
            "digests": {"2026-10-01": "abcd1234", "bad": "x", "2026-10-02": 5},
            "base": {ENERGY: 5.0, "unknown_slug": 1.0, TEMPERATURE: -2.0},
            "active": {ENERGY: -1.0, "unknown_slug": 3.0},
        }
    )
    state = HistoryState.from_dict(stored)
    assert state.recent == {}
    assert state.digests == {date(2026, 10, 1): "abcd1234"}
    # A negative energy amount never comes back; a negative temperature may.
    assert state.base == {ENERGY: 5.0, TEMPERATURE: -2.0}
    assert state.active == {}


def test_recent_days_without_a_last_day_cannot_be_continued():
    stored = HistoryState().to_dict()
    stored["recent"] = {"2026-10-01": {ENERGY: 1.0}}
    assert HistoryState.from_dict(stored) == HistoryState()


# ------------------------------------------------------------- the sums
def test_a_day_that_would_run_a_sum_backwards_is_not_written():
    state = HistoryState(
        last_day=date(2026, 10, 2),
        active={ENERGY: 100.0},
        recent={date(2026, 10, 1): {ENERGY: 1.0}, date(2026, 10, 2): {ENERGY: 1.0}},
    )
    assert rrc_energy._rows(state, date(2026, 10, 1)) == {}


def test_old_days_are_folded_into_the_base_and_their_digests_expire():
    last = TODAY - timedelta(days=1)
    old = last - timedelta(days=RECOMPUTE_DAYS + 5)
    ancient = TODAY - timedelta(days=FIRST_FILL_DAYS + 5)
    state = HistoryState(
        last_day=last,
        recent={
            old: {ENERGY: 2.0, TEMPERATURE: 4.0},
            last: {ENERGY: 1.0},
        },
        digests={ancient: "00000000", TODAY - timedelta(days=100): "11111111"},
    )
    rrc_energy._fold(state, TODAY)
    assert old not in state.recent and last in state.recent
    # Energy goes into the base, a temperature has no sum.
    assert state.base == {ENERGY: 2.0}
    assert old in state.digests
    assert ancient not in state.digests
    assert TODAY - timedelta(days=100) in state.digests


def test_every_day_older_than_the_window_is_folded_in_one_go():
    last = TODAY - timedelta(days=1)
    days = [last - timedelta(days=RECOMPUTE_DAYS + n) for n in range(1, 4)]
    state = HistoryState(last_day=last, recent={day: {ENERGY: 1.0} for day in days})
    rrc_energy._fold(state, TODAY)
    assert state.recent == {}
    assert state.base == {ENERGY: 3.0}


def test_merge_of_an_answer_without_new_days_changes_nothing():
    state = HistoryState()
    assert merge(state, [], TODAY) == {}
    assert state == HistoryState()


# ----------------------------------------------------------- the cycle
async def test_an_async_writer_is_awaited_and_gets_the_rows_once():
    seen: list[dict[str, list[tuple[date, float]]]] = []

    async def write(rows):
        seen.append(rows)

    items = [day_record(date(2026, 10, 10) + timedelta(days=n)) for n in range(3)]
    state = HistoryState()
    result = await rrc_energy.run_cycle(
        state,
        Source(items, page=10),
        today=TODAY,
        now=NOW,
        firmware=OLD_FIRMWARE,
        write=write,
    )
    assert result.reason is None
    assert len(seen) == 1 and ENERGY in seen[0]


@pytest.mark.parametrize("entries", [-1, True, "12"])
async def test_an_unusable_entry_count_ends_the_cycle_without_a_guess(entries):
    source = Source([day_record(date(2026, 10, 10))])
    source.entries_value = entries  # type: ignore[assignment]
    state = HistoryState()
    sink = Sink()
    await cycle(state, source, sink)
    assert state.reason == "history_entries_invalid"
    assert sink.writes == 0
    assert state.position is None
    assert [name for name, _ in source.calls if name == "page"] == []
