"""W2: the events of an RRC 2.0 controller as a Home Assistant calendar.

The rules of ``rrc_events`` (names, dates, overlaps, list shapes) and the
calendar in real coordinator cycles against the fake cloud: reading, the
controller's time zone, and creating, changing and deleting with exactly one
request, a late read-back and the pending guard after a timeout. Everything
is expected, untested on hardware.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating import rrc_events
from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.pointt import (
    RateLimited,
    ResourceError,
    UnexpectedHttpStatus,
)
from custom_components.bosch_buderus_heating.rrc_events import EventFields, EventState
from custom_components.bosch_buderus_heating.writes import ContractRejected
from tests.helpers.rrc_absence import (
    EMS_GATEWAY,
    LISTS,
    EventCloud,
    absence_tree,
    entity_id,
    set_value,
    setup,
    teardown,
    variants,
    wire,
)
from tests.helpers.rrc_cloud import RrcCloud

PACKAGE = "custom_components.bosch_buderus_heating"
CALENDAR = "gateway-one:events:calendar"
MARKER = "PRIVATE-MARKER-EVENT"
SKI = base64.b64encode(b"Skiurlaub").decode()

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture(autouse=True)
def legacy_setup_path():
    with patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)):
        yield


@pytest.fixture(autouse=True)
def today(freezer):
    # 2026-12-01 13:00 in Berlin, the controller's zone of the fixture.
    freezer.move_to("2026-12-01T12:00:00+00:00")
    return freezer


def _event(identifier: int, start: str, end: str, kind: str = "away") -> dict:
    return {"id": identifier, "name": SKI, "start": start, "end": end, "type": kind}


def _list(path: str, entries: list, **fields: Any):
    return wire(path, type="eventArray", writeable=0, value=entries, **fields)


# ------------------------------------------------------------------ rules
def test_names_are_trimmed_printable_and_at_most_32_bytes() -> None:
    assert rrc_events.encode_name("  Skiurlaub ") == SKI
    assert rrc_events.encode_name("x" * 32) == base64.b64encode(b"x" * 32).decode()
    # 16 umlauts are 32 bytes; one more is too long.
    assert rrc_events.encode_name("ä" * 16)
    for name in ("", "   ", "x" * 33, "ä" * 17, "tab\there", None, 7):
        with pytest.raises(ContractRejected) as refused:
            rrc_events.encode_name(name)
        assert refused.value.reason == "rrc_event_invalid_name"


def test_dates_need_an_end_not_before_the_start_or_today() -> None:
    day = date(2026, 12, 1)
    rrc_events.check_dates(day, day, day)  # ends today: allowed
    rrc_events.check_dates(day - timedelta(days=3), day, day)  # running
    for start, end in (
        (day, day - timedelta(days=1)),
        (day - timedelta(2), day - timedelta(1)),
    ):
        with pytest.raises(ContractRejected) as refused:
            rrc_events.check_dates(start, end, day)
        assert refused.value.reason == "rrc_event_invalid_dates"


def test_dates_stay_between_2016_and_2099() -> None:
    """AWAY-10: the controller's calendar offers 2016-01-01 to 2099-12-31."""
    rrc_events.check_dates(date(2016, 1, 1), date(2099, 12, 31), date(2016, 1, 1))
    for start, end in (
        (date(2015, 12, 31), date(2016, 1, 2)),
        (date(2099, 12, 30), date(2100, 1, 1)),
        (date(2200, 1, 1), date(2200, 1, 2)),
    ):
        with pytest.raises(ContractRejected) as refused:
            rrc_events.check_dates(start, end, date(2015, 12, 1))
        assert refused.value.reason == "rrc_event_invalid_dates"


@pytest.mark.parametrize(
    ("start", "end", "overlaps"),
    [
        ("2027-01-01", "2027-01-09", False),
        ("2027-01-01", "2027-01-10", True),  # shares the first day
        ("2027-01-17", "2027-01-20", True),  # shares the last day
        ("2027-01-18", "2027-01-20", False),
        ("2027-01-12", "2027-01-13", True),  # inside
        ("2027-01-01", "2027-02-01", True),  # around
    ],
)
def test_no_two_events_share_a_day(start: str, end: str, overlaps: bool) -> None:
    state = rrc_events.parse_state(
        {
            "/events/list1": _list(
                "/events/list1", [_event(1, "2027-01-10", "2027-01-17")]
            )
        },
        ("/events/list1",),
    )
    fields = EventFields(
        SKI, date.fromisoformat(start), date.fromisoformat(end), "away"
    )
    if overlaps:
        with pytest.raises(ContractRejected) as refused:
            rrc_events.check_overlap(fields, state)
        assert refused.value.reason == "rrc_event_overlap"
        # The event itself does not count when it is changed.
        rrc_events.check_overlap(fields, state, ignore=1)
    else:
        rrc_events.check_overlap(fields, state)


def test_lists_are_read_strictly_and_hidden_events_stop_writing() -> None:
    placeholder = variants()["event_placeholder_dates"]
    known = LISTS
    good = _list("/events/list1", [_event(1, "2027-01-10", "2027-01-17")])
    state = rrc_events.parse_state(
        {
            "/events/list1": good,
            "/events/list2": _list(
                "/events/list2", [_event(2, "2026-12-25", "2026-12-26", "Su")]
            ),
            "/events/list3": _list("/events/list3", []),
        },
        known,
    )
    assert [event.identifier for event in state.events] == [2, 1]
    assert state.writable
    # P01: a list "used" "false" is not read; a missing reading neither.
    unused = _list(
        "/events/list2", [_event(2, "2026-12-25", "2026-12-26")], used="false"
    )
    for reading in (unused, None):
        state = rrc_events.parse_state(
            {"/events/list1": good, "/events/list2": reading},
            ("/events/list1", "/events/list2"),
        )
        assert [event.identifier for event in state.events] == [1]
        assert state.issue == "list_unreadable"
    # Placeholder dates and unknown types are left out and counted.
    for entry in (
        placeholder,
        _event(3, "2027-02-01", "2027-02-02", "holiday"),
        _event(4, "2027-02-05", "2027-02-01"),
        {**_event(5, "2027-03-01", "2027-03-02"), "id": True},
        {"id": 6, "start": "2027-03-01", "end": "2027-03-02", "type": "away"},
    ):
        state = rrc_events.parse_state(
            {
                "/events/list1": _list(
                    "/events/list1", [_event(1, "2027-01-10", "2027-01-17"), entry]
                )
            },
            ("/events/list1",),
        )
        assert [event.identifier for event in state.events] == [1]
        assert state.skipped == 1 and state.issue == "invalid_entries"
    # The same ID in two lists: shown, never written.
    state = rrc_events.parse_state(
        {
            "/events/list1": good,
            "/events/list2": _list(
                "/events/list2", [_event(1, "2027-05-01", "2027-05-02")]
            ),
        },
        ("/events/list1", "/events/list2"),
    )
    assert len(state.events) == 2 and state.issue == "duplicate_ids"
    assert rrc_events.parse_state({}, ()).issue == "no_readable_list"
    assert (
        rrc_events.list_issue(wire("/events/list1", type="stringValue", value="x"))
        == "type"
    )


def test_read_back_rules_compare_fields_not_identifiers() -> None:
    old = rrc_events.parse_entry(_event(1, "2027-01-10", "2027-01-17"), "/events/list1")
    assert old is not None
    new = EventFields(SKI, date(2027, 1, 11), date(2027, 1, 17), "away")
    moved = EventState(
        (rrc_events.RrcEvent(1, "/events/list1", new),), ("/events/list1",)
    )
    assert rrc_events.changed(old, new, moved)
    assert not rrc_events.changed(old, new, EventState((old,), ("/events/list1",)))
    assert rrc_events.deleted(old, EventState((), ("/events/list1",)))
    assert not rrc_events.deleted(old, EventState((old,), ("/events/list1",)))
    created = EventState(
        (old, rrc_events.RrcEvent(11, "/events/list1", new)), ("/events/list1",)
    )
    assert rrc_events.created((old.fields,), new, created) is not None
    assert rrc_events.created((old.fields, new), new, created) is None
    # AWAY-05: the controller gives the new event the old one's ID and
    # renumbers that one; the fields still confirm the creation.
    renumbered = EventState(
        (
            rrc_events.RrcEvent(1, "/events/list1", new),
            rrc_events.RrcEvent(2, "/events/list1", old.fields),
        ),
        ("/events/list1",),
    )
    found = rrc_events.created((old.fields,), new, renumbered)
    assert found is not None and found.identifier == 1
    # AWAY-11: a deletion needs only its own list, a change only the lists
    # that can be read.
    assert rrc_events.deleted(
        old, EventState((), ("/events/list1",), ("/events/list2",))
    )
    assert not rrc_events.deleted(old, EventState((), ("/events/list2",)))
    assert rrc_events.changed(
        old, new, EventState(moved.events, ("/events/list1",), ("/events/list2",))
    )


# --------------------------------------------------------------- calendar
def _calendar(hass: HomeAssistant) -> Any:
    entity = entity_id(hass, "calendar", CALENDAR)
    assert entity is not None
    return hass.data["entity_components"]["calendar"].get_entity(entity)


async def _events(hass: HomeAssistant, start: str, end: str) -> list[dict]:
    entity = entity_id(hass, "calendar", CALENDAR)
    response = await hass.services.async_call(
        "calendar",
        "get_events",
        {"entity_id": entity, "start_date_time": start, "end_date_time": end},
        blocking=True,
        return_response=True,
    )
    return response[entity]["events"]


async def _create(hass: HomeAssistant, summary: str, start: str, end: str, **extra):
    await hass.services.async_call(
        "calendar",
        "create_event",
        {
            "entity_id": entity_id(hass, "calendar", CALENDAR),
            "summary": summary,
            "start_date": start,
            "end_date": end,
            **extra,
        },
        blocking=True,
    )


async def test_calendar_shows_whole_days_with_an_exclusive_end(
    hass: HomeAssistant,
) -> None:
    await hass.config.async_update(language="de")
    cloud = EventCloud()
    entry, coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    try:
        state = hass.states.get(entity_id(hass, "calendar", CALENDAR))
        assert state.state == "off"
        assert state.attributes["message"] == "Weihnachten"
        assert (
            state.attributes["description"] == "Der Tag wird wie ein Sonntag geregelt"
        )
        assert state.attributes["supported_features"] == 7
        events = await _events(
            hass, "2026-12-01T00:00:00+01:00", "2027-02-01T00:00:00+01:00"
        )
        assert [(item["summary"], item["start"], item["end"]) for item in events] == [
            ("Weihnachten", "2026-12-25", "2026-12-27"),
            (MARKER, "2027-01-10", "2027-01-18"),
        ]
        # A name that is no text gets the type's title.
        set_value(
            cloud,
            "/events/list2",
            [{**_event(2, "2026-12-25", "2026-12-26", "Su"), "name": ""}],
        )
        set_value(
            cloud,
            "/events/list1",
            [{**_event(1, "2027-01-10", "2027-01-17"), "name": ""}],
        )
        _clock.now += 3600
        await coordinator.async_refresh()
        events = await _events(
            hass, "2026-12-01T00:00:00+01:00", "2027-02-01T00:00:00+01:00"
        )
        assert [item["summary"] for item in events] == ["Wie Sonntag", "Abwesenheit"]
    finally:
        await teardown(hass, entry, stack)


async def test_dates_count_in_the_controllers_time_zone(
    hass: HomeAssistant, today
) -> None:
    """Home Assistant runs in US/Pacific, the controller in Berlin; the
    event spans the change to summer time there."""
    tree = absence_tree()
    tree["/events/list1"] = _list(
        "/events/list1", [_event(1, "2027-03-27", "2027-03-28")]
    )
    cloud = EventCloud(tree)
    entry, _coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    try:
        assert hass.config.time_zone == "US/Pacific"
        berlin = ZoneInfo("Europe/Berlin")
        inside = await _events(
            hass,
            datetime(2027, 3, 28, 23, 30, tzinfo=berlin).isoformat(),
            datetime(2027, 3, 28, 23, 45, tzinfo=berlin).isoformat(),
        )
        after = await _events(
            hass,
            datetime(2027, 3, 29, 0, 0, tzinfo=berlin).isoformat(),
            datetime(2027, 3, 29, 1, 0, tzinfo=berlin).isoformat(),
        )
        before = await _events(
            hass,
            datetime(2027, 3, 26, 23, 0, tzinfo=berlin).isoformat(),
            datetime(2027, 3, 27, 0, 0, tzinfo=berlin).isoformat(),
        )
        assert [item["start"] for item in inside] == ["2027-03-27"]
        assert after == [] and before == []
        # "Today" is the controller's: 00:30 in Berlin is still the day
        # before in Home Assistant's zone.
        today.move_to("2027-03-28T23:30:00+00:00")
        # Months later: the lists are read again (a reading three poll
        # intervals old is no longer shown).
        _clock.now += 3600
        await _coordinator.async_refresh()
        await hass.async_block_till_done()
        calendar = _calendar(hass)
        assert calendar._today == date(2027, 3, 29)
        with pytest.raises(ServiceValidationError) as refused:
            await _create(hass, "Spät", "2027-03-28", "2027-03-29")
        assert refused.value.translation_key == "rrc_event_invalid_dates"
        assert cloud.written() == []
    finally:
        await teardown(hass, entry, stack)


async def test_create_sends_one_post_and_waits_for_the_list(
    hass: HomeAssistant,
) -> None:
    cloud = EventCloud()
    cloud.adopt_after["/events/list1"] = 3
    entry, coordinator, _clock, sleeps, stack = await setup(hass, cloud)
    try:
        await _create(hass, " Skiurlaub ", "2027-02-01", "2027-02-03")
        assert cloud.written() == [
            (
                "POST",
                "/events",
                [
                    {
                        "name": SKI,
                        "start": "2027-02-01",
                        "end": "2027-02-02",
                        "type": "away",
                    }
                ],
            )
        ]
        # S7: shown at the third read after the POST.
        assert sleeps[-3:] == [1.0, 2.0, 4.0]
        events = await _events(
            hass, "2027-02-01T00:00:00+01:00", "2027-02-02T00:00:00+01:00"
        )
        assert [item["summary"] for item in events] == ["Skiurlaub"]
        stats = coordinator.rrc_write_stats.snapshot("absence")
        assert stats["writes_sent"] == 1 and stats["writes_confirmed"] == 1
    finally:
        await teardown(hass, entry, stack)


async def test_a_timed_out_creation_is_never_sent_twice(hass: HomeAssistant) -> None:
    """C36: the POST times out after the controller took it; the list shows
    it only after seven reads. Until then a second creation reads the lists
    and is refused without sending; once it shows, the same event is an
    overlap and another one is sent."""
    cloud = EventCloud()
    cloud.timeout_after_write["/events"] = True
    cloud.adopt_after["/events/list1"] = 7
    entry, coordinator, clock, _sleeps, stack = await setup(hass, cloud)
    try:
        with pytest.raises(HomeAssistantError) as unconfirmed:
            await _create(hass, "Skiurlaub", "2027-02-01", "2027-02-03")
        assert unconfirmed.value.translation_key == "rrc_write_pending_confirmation"
        assert cloud.calls["POST", "/events"] == 1
        reads = cloud.calls["BULK", "/events/list1"]
        with pytest.raises(ServiceValidationError) as pending:
            await _create(hass, "Skiurlaub", "2027-02-01", "2027-02-03")
        assert pending.value.translation_key == "rrc_event_pending"
        # The guard read the lists again before refusing.
        assert cloud.calls["BULK", "/events/list1"] == reads + 1
        # Another event is refused as well while the first is unknown.
        with pytest.raises(ServiceValidationError):
            await _create(hass, "Ostern", "2027-04-01", "2027-04-03")
        assert cloud.calls["POST", "/events"] == 1
        # The seventh read shows the first event: the guard opens, and the
        # same event again overlaps it.
        with pytest.raises(ServiceValidationError) as overlap:
            await _create(hass, "Skiurlaub", "2027-02-01", "2027-02-03")
        assert overlap.value.translation_key == "rrc_event_overlap"
        assert cloud.calls["POST", "/events"] == 1
        del cloud.timeout_after_write["/events"]
        cloud.adopt_after["/events/list1"] = 0
        await _create(hass, "Ostern", "2027-04-01", "2027-04-03")
        assert cloud.calls["POST", "/events"] == 2
        names = [
            item["name"] for item in coordinator.data["/events/list1"].resource.value
        ]
        assert names.count(SKI) == 1
        stats = coordinator.rrc_write_stats.snapshot("absence")
        assert stats["writes_sent"] == 2
        assert stats["writes_unconfirmed"] == 1
        assert stats["rejection_reasons"]["pending"] == 2
        assert stats["rejection_reasons"]["rrc_event_overlap"] == 1
        del clock
    finally:
        await teardown(hass, entry, stack)


async def test_the_guard_expires_and_a_fresh_read_decides(hass: HomeAssistant) -> None:
    cloud = EventCloud()
    cloud.timeout_after_write["/events"] = False  # lost before the controller
    entry, _coordinator, clock, _sleeps, stack = await setup(hass, cloud)
    try:
        with pytest.raises(HomeAssistantError):
            await _create(hass, "Skiurlaub", "2027-02-01", "2027-02-03")
        with pytest.raises(ServiceValidationError):
            await _create(hass, "Skiurlaub", "2027-02-01", "2027-02-03")
        clock.now += 601
        del cloud.timeout_after_write["/events"]
        await _create(hass, "Skiurlaub", "2027-02-01", "2027-02-03")
        assert cloud.calls["POST", "/events"] == 2
    finally:
        await teardown(hass, entry, stack)


@pytest.mark.parametrize(
    ("error", "key", "kind"),
    [
        (UnexpectedHttpStatus(400), "rrc_event_rejected", ServiceValidationError),
        (ResourceError("/events", 403), "rrc_event_rejected", ServiceValidationError),
        (RateLimited(retry_after=60), "write_rate_limited", HomeAssistantError),
    ],
)
async def test_a_refused_post_changes_nothing_and_is_not_repeated(
    hass: HomeAssistant, error: Exception, key: str, kind: type[Exception]
) -> None:
    cloud = EventCloud()
    entry, coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    try:
        cloud.post_error = error
        with pytest.raises(kind) as refused:
            await _create(hass, "Skiurlaub", "2027-02-01", "2027-02-03")
        cloud.post_error = None
        assert refused.value.translation_key == key
        assert len(cloud.written("POST")) == 1
        if isinstance(error, RateLimited):
            # The backoff holds every write until it ends.
            assert coordinator._cloud_backoff_until > _clock.now
            with pytest.raises(HomeAssistantError):
                await _create(hass, "Skiurlaub", "2027-02-01", "2027-02-03")
            assert len(cloud.written("POST")) == 1
            _clock.now += 3600
        # The guard was released: the next creation is sent.
        await _create(hass, "Skiurlaub", "2027-02-01", "2027-02-03")
        assert len(cloud.written("POST")) == 2
    finally:
        await teardown(hass, entry, stack)


@pytest.mark.parametrize(
    ("fields", "key"),
    [
        ({"summary": "x" * 33}, "rrc_event_invalid_name"),
        (
            {"summary": "Alt", "start_date": "2026-11-01", "end_date": "2026-11-03"},
            "rrc_event_invalid_dates",
        ),
        (
            {
                "summary": "Weihnachten",
                "start_date": "2026-12-26",
                "end_date": "2026-12-28",
            },
            "rrc_event_overlap",
        ),
        ({"summary": "Ski", "description": "Berge"}, "rrc_event_unsupported_fields"),
        ({"summary": "Ski", "rrule": "FREQ=WEEKLY"}, "rrc_event_unsupported_fields"),
    ],
)
async def test_invalid_events_are_refused_before_sending(
    hass: HomeAssistant, fields: dict, key: str
) -> None:
    cloud = EventCloud()
    entry, coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    calendar = _calendar(hass)
    request = {
        "dtstart": date.fromisoformat(fields.pop("start_date", "2027-02-01")),
        "dtend": date.fromisoformat(fields.pop("end_date", "2027-02-03")),
        **fields,
    }
    try:
        with pytest.raises(ServiceValidationError) as refused:
            await calendar.async_create_event(**request)
        assert refused.value.translation_key == key
        assert cloud.written() == []
        assert coordinator.rrc_write_stats.snapshot("absence")["rejection_reasons"] == {
            key: 1
        }
    finally:
        await teardown(hass, entry, stack)


async def test_times_only_at_midnight_of_the_controller(hass: HomeAssistant) -> None:
    cloud = EventCloud()
    entry, _coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    berlin = ZoneInfo("Europe/Berlin")
    calendar = _calendar(hass)
    try:
        with pytest.raises(ServiceValidationError) as refused:
            await calendar.async_create_event(
                summary="Ski",
                dtstart=datetime(2027, 2, 1, 8, tzinfo=berlin),
                dtend=datetime(2027, 2, 3, tzinfo=berlin),
            )
        assert refused.value.translation_key == "rrc_event_invalid_dates"
        await calendar.async_create_event(
            summary="Ski",
            dtstart=datetime(2027, 2, 1, tzinfo=berlin),
            dtend=datetime(2027, 2, 3, tzinfo=berlin),
        )
        assert cloud.written("POST")[-1][2][0]["end"] == "2027-02-02"
    finally:
        await teardown(hass, entry, stack)


async def test_change_and_delete_with_one_request_each(hass: HomeAssistant) -> None:
    cloud = EventCloud()
    cloud.adopt_after["/events/list1"] = 2
    entry, coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    calendar = _calendar(hass)
    try:
        away = next(item for item in calendar._state.events if item.identifier == 1)
        sunday = next(item for item in calendar._state.events if item.identifier == 2)
        from custom_components.bosch_buderus_heating.rrc_event_calendar import _uid

        # The name stays as it was when the summary is unchanged.
        await calendar.async_update_event(
            _uid(away),
            {
                "summary": MARKER,
                "dtstart": date(2027, 1, 11),
                "dtend": date(2027, 1, 18),
            },
        )
        assert cloud.written() == [
            (
                "PUT",
                "/events/ev1",
                [
                    {
                        "name": away.fields.name,
                        "start": "2027-01-11",
                        "end": "2027-01-17",
                        "type": "away",
                    }
                ],
            )
        ]
        assert (
            coordinator.data["/events/list1"].resource.value[0]["start"] == "2027-01-11"
        )
        # Unchanged: nothing is sent.
        moved = next(item for item in calendar._state.events if item.identifier == 1)
        await calendar.async_update_event(
            _uid(moved),
            {
                "summary": MARKER,
                "dtstart": date(2027, 1, 11),
                "dtend": date(2027, 1, 18),
            },
        )
        assert len(cloud.written()) == 1
        # An outdated UID (the event moved meanwhile) matches nothing.
        with pytest.raises(ServiceValidationError) as outdated:
            await calendar.async_delete_event(_uid(away))
        assert outdated.value.translation_key == "write_validation_failed"
        # A weekday event is only deleted.
        with pytest.raises(ServiceValidationError) as weekday:
            await calendar.async_update_event(
                _uid(sunday),
                {
                    "summary": "X",
                    "dtstart": date(2026, 12, 25),
                    "dtend": date(2026, 12, 27),
                },
            )
        assert weekday.value.translation_key == "rrc_event_type_read_only"
        await calendar.async_delete_event(_uid(sunday))
        assert cloud.written()[-1] == ("DELETE", "/events/ev2", None)
        assert coordinator.data["/events/list2"].resource.value == []
        assert len(cloud.written()) == 2
        # Changing into another event's days is an overlap.
        await _create(hass, "Ostern", "2027-04-01", "2027-04-03")
        moved = next(item for item in calendar._state.events if item.identifier == 1)
        with pytest.raises(ServiceValidationError) as overlap:
            await calendar.async_update_event(
                _uid(moved),
                {
                    "summary": "Ski",
                    "dtstart": date(2027, 3, 30),
                    "dtend": date(2027, 4, 2),
                },
            )
        assert overlap.value.translation_key == "rrc_event_overlap"
        assert len(cloud.written()) == 3
    finally:
        await teardown(hass, entry, stack)


async def test_a_write_without_effect_is_not_confirmed(hass: HomeAssistant) -> None:
    """P32: 204 without effect; the list shows the event unchanged."""
    cloud = EventCloud()
    cloud.ignore_writes.add("/events/ev2")
    entry, coordinator, _clock, sleeps, stack = await setup(hass, cloud)
    calendar = _calendar(hass)
    from custom_components.bosch_buderus_heating.rrc_event_calendar import _uid

    try:
        sunday = next(item for item in calendar._state.events if item.identifier == 2)
        with pytest.raises(HomeAssistantError) as unconfirmed:
            await calendar.async_delete_event(_uid(sunday))
        assert unconfirmed.value.translation_key == "rrc_write_pending_confirmation"
        assert cloud.calls["DELETE", "/events/ev2"] == 1
        assert sleeps[-4:] == [1.0, 2.0, 4.0, 8.0]
        assert (
            coordinator.rrc_write_stats.snapshot("absence")["writes_unconfirmed"] == 1
        )
    finally:
        await teardown(hass, entry, stack)


async def test_an_unreadable_list_stops_writing_but_not_reading(
    hass: HomeAssistant,
) -> None:
    """P31: a list answers 500 for a while."""
    cloud = EventCloud()
    entry, coordinator, clock, _sleeps, stack = await setup(hass, cloud)
    entity = entity_id(hass, "calendar", CALENDAR)
    try:
        cloud.down["/events/list2"] = 500
        clock.now += 3 * 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        state = hass.states.get(entity)
        assert state.state != "unavailable"
        assert state.attributes["supported_features"] == 0
        with pytest.raises(HomeAssistantError):
            await _calendar(hass).async_create_event(
                summary="Ski", dtstart=date(2027, 2, 1), dtend=date(2027, 2, 3)
            )
        assert cloud.written() == []
        del cloud.down["/events/list2"]
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(entity).attributes["supported_features"] == 7
    finally:
        await teardown(hass, entry, stack)


async def test_lists_are_read_without_a_readable_root(hass: HomeAssistant) -> None:
    """The lists are fixed paths: read also when ``/events`` itself is
    missing; the event entries and ``activeEvent`` are then never read."""
    tree = absence_tree()
    for path in ("/events", "/events/ev1", "/events/ev2", "/events/activeEvent"):
        del tree[path]
    cloud = EventCloud(tree)
    entry, coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    try:
        assert entity_id(hass, "calendar", CALENDAR) is not None
        assert {path for path in LISTS} <= set(coordinator.data)
        assert not {"/events/ev1", "/events/activeEvent"} & cloud.all_requested()
    finally:
        await teardown(hass, entry, stack)


async def test_polling_reads_the_lists_only(hass: HomeAssistant) -> None:
    """The lists poll with the controls (at most every 5 minutes); the
    entries and ``activeEvent`` are never polled."""
    cloud = EventCloud()
    entry, coordinator, clock, _sleeps, stack = await setup(hass, cloud)
    try:
        cloud.requests.clear()
        cloud.single.clear()
        for _ in range(10):
            clock.now += 60
            await coordinator.async_refresh()
        assert 1 <= cloud.requested("/events/list1") <= 2
        clock.now += 3600
        await coordinator.async_refresh()
        requested = cloud.all_requested()
        assert set(LISTS) <= requested
        assert not {"/events/ev1", "/events/ev2", "/events/activeEvent"} & requested
    finally:
        await teardown(hass, entry, stack)


async def test_names_stay_out_of_diagnostics_and_logs(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=PACKAGE)
    cloud = EventCloud()
    cloud.timeout_after_write["/events"] = False
    entry, _coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    try:
        with pytest.raises(HomeAssistantError):
            await _create(hass, "Geheimreise", "2027-02-01", "2027-02-03")
        report = await async_get_config_entry_diagnostics(hass, entry)
        text = json.dumps(report, default=str)
        encoded = base64.b64encode(b"Geheimreise").decode()
        for secret in (
            MARKER,
            base64.b64encode(MARKER.encode()).decode(),
            "Geheimreise",
            encoded,
            "2027-01-10",
        ):
            assert secret not in text
            assert secret not in caplog.text
        absence = report["gateways"][0]["rrc_features"]["contracts"]["absence"]
        assert absence["events"] == {
            "lists": 3,
            "lists_readable": 3,
            "events_away": 1,
            "events_weekday": 1,
            "entries_skipped": 0,
            "write_issue": None,
        }
        assert absence["writes"]["writes_unconfirmed"] == 1
    finally:
        await teardown(hass, entry, stack)


async def test_no_calendar_and_no_event_reads_on_other_gateways(
    hass: HomeAssistant,
) -> None:
    """S11: the same paths on a heat pump gateway stay unread."""
    tree = {
        path: resource
        for path, resource in absence_tree().items()
        if not path.startswith(("/zones", "/programs", "/devices"))
        and path != "/gateway/productType"
    }
    cloud = RrcCloud(tree, forbidden=set(), missing_as_500=False)
    entry, _coordinator, _clock, _sleeps, stack = await setup(hass, cloud, EMS_GATEWAY)
    try:
        assert entity_id(hass, "calendar", CALENDAR) is None
        assert not {
            path for path in cloud.all_requested() if path.startswith("/events")
        }
    finally:
        await teardown(hass, entry, stack)


# ----------------------------------------------------- review fixes (AWAY)
class _RenumberingCloud(EventCloud):
    """A controller that keeps list1 sorted by date and gives the IDs out
    again in that order: a new, earlier event takes an older one's ID."""

    def _create(self, value: Any) -> None:
        entries = [*self._listed("/events/list1"), {**value[0], "id": self.next_id}]
        identifiers = sorted(item["id"] for item in entries)
        self.next_id += 1
        ordered = sorted(entries, key=lambda item: item["start"])
        self._settle(
            "/events/list1",
            [
                {**item, "id": identifier}
                for item, identifier in zip(ordered, identifiers, strict=True)
            ],
        )


class _FlakyCloud(EventCloud):
    """A list answers 500 from the first read after a write on (P31)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.down_after_write: dict[str, int] = {}

    def _write(self, method: str, path: str, value: Any) -> None:
        super()._write(method, path, value)
        self.down.update(self.down_after_write)


async def test_a_creation_is_confirmed_by_its_fields(hass: HomeAssistant) -> None:
    """AWAY-05: the new event takes ID 1, the old one moves to ID 10. The
    creation is confirmed at once and the next write is not held."""
    cloud = _RenumberingCloud()
    entry, coordinator, _clock, sleeps, stack = await setup(hass, cloud)
    try:
        await _create(hass, "Neujahr", "2027-01-01", "2027-01-03")
        assert sleeps[-1:] == [1.0]
        listed = coordinator.data["/events/list1"].resource.value
        assert [(item["id"], item["start"]) for item in listed] == [
            (1, "2027-01-01"),
            (10, "2027-01-10"),
        ]
        await _create(hass, "Ostern", "2027-04-01", "2027-04-03")
        assert cloud.calls["POST", "/events"] == 2
        stats = coordinator.rrc_write_stats.snapshot("absence")
        assert stats["writes_confirmed"] == 2
        assert stats.get("writes_unconfirmed", 0) == 0
    finally:
        await teardown(hass, entry, stack)


async def test_one_failed_list_read_keeps_its_events_for_a_while(
    hass: HomeAssistant, today
) -> None:
    """AWAY-04: a running absence stays on through a 500 of its list; the
    attribute ``stale`` says so and writing stops. Once the reading is older
    than three poll intervals, the list's events are no longer shown."""
    tree = absence_tree()
    tree["/events/list1"] = _list(
        "/events/list1", [_event(1, "2026-11-29", "2026-12-06")]
    )
    cloud = EventCloud(tree)
    entry, coordinator, clock, _sleeps, stack = await setup(hass, cloud)
    entity = entity_id(hass, "calendar", CALENDAR)
    try:
        state = hass.states.get(entity)
        assert state.state == "on" and state.attributes["stale"] is False
        cloud.down["/events/list1"] = 500
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        state = hass.states.get(entity)
        assert state.state == "on"
        assert state.attributes["message"] == "Skiurlaub"
        assert state.attributes["stale"] is True
        assert state.attributes["supported_features"] == 0
        events = await _events(
            hass, "2026-12-01T00:00:00+01:00", "2026-12-02T00:00:00+01:00"
        )
        assert [item["summary"] for item in events] == ["Skiurlaub"]
        # Older than three poll intervals: no longer shown.
        today.tick(timedelta(minutes=31))
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        state = hass.states.get(entity)
        assert state.state == "off" and state.attributes["stale"] is True
        assert state.attributes["message"] == "Weihnachten"
        # A valid reading ends it.
        del cloud.down["/events/list1"]
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        state = hass.states.get(entity)
        assert state.state == "on" and state.attributes["stale"] is False
        assert state.attributes["supported_features"] == 7
        assert cloud.written() == []
    finally:
        await teardown(hass, entry, stack)


async def test_a_read_back_needs_only_the_lists_it_looks_at(
    hass: HomeAssistant,
) -> None:
    """AWAY-11: another list answering 500 after the request does not leave
    a creation or deletion unconfirmed; the list of a deleted event must
    read validly."""
    cloud = _FlakyCloud()
    entry, coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    calendar = _calendar(hass)
    from custom_components.bosch_buderus_heating.rrc_event_calendar import _uid

    try:
        cloud.down_after_write = {"/events/list2": 500}
        await _create(hass, "Skiurlaub", "2027-02-01", "2027-02-03")
        del cloud.down["/events/list2"]
        cloud.down_after_write = {"/events/list3": 500}
        away = next(item for item in calendar._state.events if item.identifier == 1)
        await calendar.async_delete_event(_uid(away))
        del cloud.down["/events/list3"]
        stats = coordinator.rrc_write_stats.snapshot("absence")
        assert stats["writes_confirmed"] == 2
        # The deleted event's own list fails: unknown, so unconfirmed.
        cloud.down_after_write = {"/events/list2": 500}
        sunday = next(item for item in calendar._state.events if item.identifier == 2)
        with pytest.raises(HomeAssistantError) as unconfirmed:
            await calendar.async_delete_event(_uid(sunday))
        assert unconfirmed.value.translation_key == "rrc_write_pending_confirmation"
        assert cloud.calls["DELETE", "/events/ev2"] == 1
    finally:
        await teardown(hass, entry, stack)


async def test_dates_after_2099_are_refused_before_sending(
    hass: HomeAssistant,
) -> None:
    """AWAY-10 in a real cycle: the last day 2099-12-31 is allowed."""
    cloud = EventCloud()
    entry, _coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    try:
        with pytest.raises(ServiceValidationError) as refused:
            await _create(hass, "Fern", "2099-12-30", "2100-01-02")
        assert refused.value.translation_key == "rrc_event_invalid_dates"
        assert cloud.written() == []
        await _create(hass, "Fern", "2099-12-30", "2100-01-01")
        assert cloud.written("POST")[-1][2][0]["end"] == "2099-12-31"
    finally:
        await teardown(hass, entry, stack)
