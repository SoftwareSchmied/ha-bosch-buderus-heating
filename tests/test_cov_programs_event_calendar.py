"""RRC event calendar: error translation, refusals and edge cases."""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    BatchItemResult,
    PointTError,
    RateLimited,
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.rrc_event_calendar import (
    TIMEZONE_PATH,
    _uid,
    async_read_lists,
    async_run_rrc_write,
)
from custom_components.bosch_buderus_heating.rrc_write import RrcWritePending
from custom_components.bosch_buderus_heating.writes import (
    ContractRejected,
    ControlLocked,
    RrcWriteNotConfirmed,
    WriteBusy,
)
from tests.helpers.rrc_absence import (
    EventCloud,
    set_value,
    setup,
    teardown,
    wire,
)
from tests.test_rrc_events import (  # noqa: F401 - fixtures and helpers
    _calendar,
    _event,
    legacy_setup_path,
    today,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


# ------------------------------------------------- error translation
def failing(error: Exception) -> SimpleNamespace:
    return SimpleNamespace(async_run_rrc_operation=AsyncMock(side_effect=error))


@pytest.mark.parametrize(
    ("error", "kind", "key"),
    [
        (
            ContractRejected("rrc_event_overlap"),
            ServiceValidationError,
            "rrc_event_overlap",
        ),
        (RrcWritePending(), ServiceValidationError, "rrc_write_pending_confirmation"),
        (ControlLocked("locked"), ServiceValidationError, "rrc_boost_locked"),
        (WriteValidationError("v"), ServiceValidationError, "write_validation_failed"),
        (
            RrcWriteNotConfirmed("n"),
            HomeAssistantError,
            "rrc_write_pending_confirmation",
        ),
        (WriteNotConfirmed("n"), HomeAssistantError, "write_not_confirmed"),
        (WriteBusy("b"), HomeAssistantError, "write_busy"),
        (AuthenticationError("a"), HomeAssistantError, "write_authentication_failed"),
        (RateLimited(retry_after=60), HomeAssistantError, "write_rate_limited"),
        (PointTError("p"), HomeAssistantError, "write_failed"),
    ],
)
async def test_failures_reach_the_user_as_translated_errors(error, kind, key):
    async def operation():
        return ()

    with pytest.raises(kind) as raised:
        await async_run_rrc_write(failing(error), "/events", operation)  # type: ignore[arg-type]
    assert raised.value.translation_key == key
    assert raised.value.translation_domain == "bosch_buderus_heating"


def lists_coordinator(*results: BatchItemResult) -> SimpleNamespace:
    return SimpleNamespace(
        client=SimpleNamespace(get_resources_bulk=AsyncMock(return_value=results)),
        gateway=SimpleNamespace(gateway_id="gw"),
    )


async def test_a_rate_limited_bulk_read_of_the_lists_is_an_error_not_a_gap():
    coordinator = lists_coordinator(
        BatchItemResult("gw", "/events/list1", 200, resource=wire("/events/list1")),
        BatchItemResult("gw", "/events/list2", 429),
    )
    with pytest.raises(RateLimited):
        await async_read_lists(coordinator, ("/events/list1", "/events/list2"))  # type: ignore[arg-type]


async def test_lists_without_a_matching_answer_stay_unread():
    coordinator = lists_coordinator(
        # Answered with another path, a failure, or an unknown path.
        BatchItemResult("gw", "/events/list1", 200, resource=wire("/events/list3")),
        BatchItemResult("gw", "/events/list2", 500),
        BatchItemResult("gw", "/events/other", 200, resource=wire("/events/other")),
    )
    readings = await async_read_lists(coordinator, ("/events/list1", "/events/list2"))  # type: ignore[arg-type]
    assert readings == {"/events/list1": None, "/events/list2": None}


# ------------------------------------------------------- calendar writes
async def test_unknown_or_ranged_events_are_refused_without_a_request(
    hass: HomeAssistant,
) -> None:
    cloud = EventCloud()
    entry, _coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    calendar = _calendar(hass)
    try:
        away = next(item for item in calendar._state.events if item.identifier == 1)
        fields = {
            "summary": "Ski",
            "dtstart": date(2027, 1, 11),
            "dtend": date(2027, 1, 18),
        }
        with pytest.raises(ServiceValidationError) as ranged:
            await calendar.async_update_event(
                _uid(away), fields, recurrence_range="THISANDFUTURE"
            )
        assert ranged.value.translation_key == "rrc_event_unsupported_fields"
        with pytest.raises(ServiceValidationError) as ranged:
            await calendar.async_delete_event(
                _uid(away), recurrence_range="THISANDFUTURE"
            )
        assert ranged.value.translation_key == "rrc_event_unsupported_fields"
        with pytest.raises(ServiceValidationError) as unknown:
            await calendar.async_update_event("not-listed", fields)
        assert unknown.value.translation_key == "write_validation_failed"
        assert cloud.written() == []
    finally:
        await teardown(hass, entry, stack)


async def test_an_event_changed_in_the_app_is_not_overwritten(
    hass: HomeAssistant,
) -> None:
    cloud = EventCloud()
    entry, _coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    calendar = _calendar(hass)
    try:
        away = next(item for item in calendar._state.events if item.identifier == 1)
        # The app moves the event after the calendar last read it.
        set_value(cloud, "/events/list1", [_event(1, "2027-06-01", "2027-06-05")])
        with pytest.raises(ServiceValidationError) as changed:
            await calendar.async_delete_event(_uid(away))
        assert changed.value.translation_key == "write_validation_failed"
        with pytest.raises(ServiceValidationError) as changed:
            await calendar.async_update_event(
                _uid(away),
                {
                    "summary": "Ski",
                    "dtstart": date(2027, 1, 11),
                    "dtend": date(2027, 1, 18),
                },
            )
        assert changed.value.translation_key == "write_validation_failed"
        assert cloud.written() == []
    finally:
        await teardown(hass, entry, stack)


async def test_a_list_with_an_invalid_entry_stops_creating_events(
    hass: HomeAssistant,
) -> None:
    """A hidden entry could overlap the new one: the fresh read refuses."""
    cloud = EventCloud()
    entry, _coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    calendar = _calendar(hass)
    try:
        # Not yet polled: the calendar still believes the lists are fine.
        set_value(
            cloud,
            "/events/list1",
            [
                _event(1, "2027-01-10", "2027-01-17"),
                _event(4, "2027-02-05", "2027-02-01"),
            ],
        )
        with pytest.raises(ServiceValidationError) as refused:
            await calendar.async_create_event(
                summary="Ski", dtstart=date(2027, 8, 1), dtend=date(2027, 8, 3)
            )
        assert refused.value.translation_key == "write_validation_failed"
        assert cloud.written() == []
    finally:
        await teardown(hass, entry, stack)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2027, 2, 3), date(2027, 2, 3)),  # no day at all
        (date(2027, 2, 3), date(2027, 2, 1)),  # ends before it starts
        (date(2027, 2, 1), datetime(2027, 2, 3)),  # a date and a time
    ],
)
async def test_unusable_dates_are_refused_before_sending(
    hass: HomeAssistant, start, end
) -> None:
    cloud = EventCloud()
    entry, _coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    calendar = _calendar(hass)
    try:
        with pytest.raises(ServiceValidationError) as refused:
            await calendar.async_create_event(summary="Ski", dtstart=start, dtend=end)
        assert refused.value.translation_key == "rrc_event_invalid_dates"
        assert cloud.written() == []
    finally:
        await teardown(hass, entry, stack)


async def test_times_without_a_zone_count_in_the_controllers_zone(
    hass: HomeAssistant,
) -> None:
    cloud = EventCloud()
    entry, _coordinator, _clock, _sleeps, stack = await setup(hass, cloud)
    calendar = _calendar(hass)
    try:
        await calendar.async_create_event(
            summary="Ski", dtstart=datetime(2027, 2, 1), dtend=datetime(2027, 2, 3)
        )
        (post,) = cloud.written("POST")
        assert post[2][0]["start"] == "2027-02-01"
        assert post[2][0]["end"] == "2027-02-02"
        # Asking for events with times without a zone reads them as UTC.
        events = await calendar.async_get_events(
            hass, datetime(2027, 1, 11), datetime(2027, 1, 12)
        )
        assert [item.start for item in events] == [date(2027, 1, 10)]
    finally:
        await teardown(hass, entry, stack)


async def test_a_changed_time_zone_of_the_controller_is_picked_up(
    hass: HomeAssistant,
) -> None:
    cloud = EventCloud()
    entry, coordinator, clock, _sleeps, stack = await setup(hass, cloud)
    calendar = _calendar(hass)
    try:
        assert calendar._timezone.key == "Europe/Berlin"  # type: ignore[attr-defined]
        set_value(cloud, TIMEZONE_PATH, "Atlantic/Reykjavik")
        clock.now += 3 * 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert calendar._timezone.key == "Atlantic/Reykjavik"  # type: ignore[attr-defined]
    finally:
        await teardown(hass, entry, stack)


async def test_an_unusable_time_zone_falls_back_to_the_system_zone(
    hass: HomeAssistant,
) -> None:
    from homeassistant.util import dt as dt_util

    cloud = EventCloud()
    entry, coordinator, clock, _sleeps, stack = await setup(hass, cloud)
    calendar = _calendar(hass)
    try:
        set_value(cloud, TIMEZONE_PATH, 42)
        clock.now += 3 * 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert calendar._timezone_name() is None
        assert calendar._timezone == dt_util.get_default_time_zone()
        # The same zone twice is resolved once; an unchanged name changes nothing.
        set_value(cloud, TIMEZONE_PATH, "Europe/Berlin")
        clock.now += 3 * 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert await calendar._async_resolve_timezone() is False
    finally:
        await teardown(hass, entry, stack)
