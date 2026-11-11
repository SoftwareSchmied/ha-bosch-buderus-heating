"""F1: the read-only firmware update list and its update entity.

All answers are synthetic; they follow the shape of the cloud's firmware
update list (gateway, installed version, offered update, automatic update
setting).
"""

from __future__ import annotations

import asyncio
import gc
import json
import time
import weakref
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.bosch_buderus_heating import firmware_updates as module
from custom_components.bosch_buderus_heating.const import DOMAIN, PLATFORMS
from custom_components.bosch_buderus_heating.diagnostics import (
    _firmware_update_diagnostics,
)
from custom_components.bosch_buderus_heating.firmware_updates import (
    FIRST_READ_DELAY,
    FIRST_READ_SPREAD_SECONDS,
    HELD_RECHECK,
    READ_INTERVAL,
    REFUSED_INTERVAL,
    VALIDITY,
    FirmwareUpdateTracker,
    Outcome,
    classify,
)
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    InvalidPayload,
    PointTClient,
    RateLimited,
    RequestTimeout,
    ResourceForbidden,
    ResourceNotAcceptable,
    ResourceNotFound,
    ServiceUnavailable,
    UnexpectedHttpStatus,
)
from custom_components.bosch_buderus_heating.pointt.exceptions import (
    RefreshTokenRejected,
    SourceStopped,
)
from custom_components.bosch_buderus_heating.pointt.firmware_updates import (
    FirmwareUpdates,
    FirmwareUpdatesSchemaError,
    FirmwareVersion,
    ReleaseNote,
    parse_firmware_updates,
    release_url,
    restored_updates,
    stored_entry,
)
from custom_components.bosch_buderus_heating.pointt.metrics import request_category
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from tests.ha_test_helpers import (
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

GATEWAY_ID = "gateway-one"
NOTES = [
    {"language": "de", "url": "https://notes.example.test/de/2.0"},
    {"language": "en", "url": "https://notes.example.test/en/2.0"},
    {"language": "fr", "url": "https://notes.example.test/fr/2.0"},
]


def entry(**fields: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "gatewayId": GATEWAY_ID,
        "currentVersion": {"id": "c-1", "version": "1.5.2", "state": "NONE"},
        "autoUpdateInformation": {"enabled": True, "changeable": False},
    }
    item.update(fields)
    return item


def offered(state: str = "WAITING", **fields: Any) -> dict[str, Any]:
    return entry(
        updateInformation={
            "id": "u-1",
            "version": "2.0.0",
            "state": state,
            "releaseNotes": NOTES,
            **fields,
        }
    )


# Contract -----------------------------------------------------------------


def test_no_update_shows_the_installed_version_as_current() -> None:
    parsed = parse_firmware_updates([entry()], GATEWAY_ID)
    assert parsed == FirmwareUpdates(
        current=FirmwareVersion("1.5.2", "NONE"),
        update=None,
        auto_update_enabled=True,
        auto_update_changeable=False,
    )
    assert parsed.offered_version is None
    assert parsed.update_state is None


@pytest.mark.parametrize(
    ("state", "offered_version", "token"),
    [
        ("WAITING", "2.0.0", "waiting"),
        ("DENIED", "2.0.0", "denied"),
        ("APPROVED", "2.0.0", "approved"),
        # NONE is neutral: no offered update, nothing being installed; it is
        # only shown as the reported state.
        ("NONE", None, "none"),
        ("none", None, "none"),
        ("waiting", "2.0.0", "waiting"),
        # UNKNOWN keeps the gateway current; an unknown value is shown, never
        # interpreted.
        ("UNKNOWN", None, "unknown"),
        ("SCHEDULED_LATER", None, "scheduled_later"),
        ("not a token!", None, "other"),
    ],
)
def test_update_states(state: str, offered_version: str | None, token: str) -> None:
    parsed = parse_firmware_updates([offered(state)], GATEWAY_ID)
    assert parsed is not None
    assert parsed.offered_version == offered_version
    assert parsed.update_state == token


@pytest.mark.parametrize("state", ["WAITING", "DENIED", "APPROVED", "NONE"])
def test_an_update_to_the_installed_version_offers_nothing(state: str) -> None:
    parsed = parse_firmware_updates(
        [entry(updateInformation={"version": "1.5.2", "state": state})], GATEWAY_ID
    )
    assert parsed is not None
    assert parsed.offered_version is None
    assert parsed.update_state == state.lower()


def test_none_without_a_version_is_no_shape_problem() -> None:
    parsed = parse_firmware_updates(
        [entry(updateInformation={"state": "NONE"})], GATEWAY_ID
    )
    assert parsed is not None
    assert parsed.offered_version is None
    assert parsed.update_state == "none"


def test_missing_fields_stay_unknown_without_a_shape_problem() -> None:
    parsed = parse_firmware_updates(
        [{"gatewayId": GATEWAY_ID, "autoUpdateInformation": {"enabled": "yes"}}],
        GATEWAY_ID,
    )
    assert parsed == FirmwareUpdates(None, None, None, None)
    parsed = parse_firmware_updates(
        [entry(currentVersion={}, updateInformation=None)], GATEWAY_ID
    )
    assert parsed is not None
    assert parsed.current == FirmwareVersion(None, None)
    assert parsed.update is None
    # An update entry without a state offers nothing.
    parsed = parse_firmware_updates(
        [entry(updateInformation={"version": "2.0.0"})], GATEWAY_ID
    )
    assert parsed is not None and parsed.offered_version is None


@pytest.mark.parametrize("payload", [[], None, [entry(gatewayId="another-gateway")]])
def test_a_list_without_the_gateway_names_nothing(payload: Any) -> None:
    assert parse_firmware_updates(payload, GATEWAY_ID) is None


def test_the_own_entry_is_found_among_others() -> None:
    parsed = parse_firmware_updates(
        [entry(gatewayId="another-gateway"), offered()], GATEWAY_ID
    )
    assert parsed is not None and parsed.offered_version == "2.0.0"


@pytest.mark.parametrize(
    "payload",
    [
        {"gatewayId": GATEWAY_ID},
        "text",
        [None],
        [{"currentVersion": {"version": "1.0"}}],
        [entry(), entry()],
        [entry(currentVersion="1.5.2")],
        [entry(currentVersion={"version": 152})],
        [entry(currentVersion={"version": ""})],
        [entry(currentVersion={"version": "1.5\n2"})],
        [entry(currentVersion={"version": "1" * 65})],
        [entry(updateInformation={"version": "2.0", "state": 3})],
        [entry(updateInformation={"state": "WAITING"})],
        [entry(updateInformation={"state": "APPROVED"})],
        [entry(autoUpdateInformation=[True])],
        [entry()] * 33,
    ],
)
def test_an_unexpected_shape_is_a_schema_issue(payload: Any) -> None:
    with pytest.raises(FirmwareUpdatesSchemaError):
        parse_firmware_updates(payload, GATEWAY_ID)


def test_release_notes_keep_only_plain_https_links() -> None:
    parsed = parse_firmware_updates(
        [
            offered(
                releaseNotes=[
                    {"language": "de", "url": "http://notes.example.test/de"},
                    {"language": "de", "url": "javascript:alert(1)"},
                    {"language": "de", "url": "https://notes.example.test/a b"},
                    {"language": "d", "url": "https://notes.example.test/x"},
                    {"language": "en_GB", "url": "https://notes.example.test/en"},
                    {"language": "en", "url": 5},
                    "https://notes.example.test/plain",
                ]
            )
        ],
        GATEWAY_ID,
    )
    assert parsed is not None and parsed.update is not None
    assert parsed.update.release_notes == (
        ReleaseNote("en-gb", "https://notes.example.test/en"),
    )


@pytest.mark.parametrize(
    ("language", "url"),
    [
        ("de", "https://notes.example.test/de/2.0"),
        ("de-AT", "https://notes.example.test/de/2.0"),
        ("fr", "https://notes.example.test/fr/2.0"),
        ("cs", "https://notes.example.test/en/2.0"),
        (None, "https://notes.example.test/en/2.0"),
    ],
)
def test_release_notes_follow_the_language_then_english(
    language: str | None, url: str
) -> None:
    parsed = parse_firmware_updates([offered()], GATEWAY_ID)
    assert parsed is not None
    assert release_url(parsed, language) == url


def test_release_notes_fall_back_to_the_first_link_and_the_installed_version() -> None:
    only_sv = [{"language": "sv", "url": "https://notes.example.test/sv"}]
    parsed = parse_firmware_updates([offered(releaseNotes=only_sv)], GATEWAY_ID)
    assert parsed is not None
    assert release_url(parsed, "de") == "https://notes.example.test/sv"
    # Without an offered update, the installed version's notes are linked;
    # the notes of an update that is not offered never are.
    installed = entry(
        currentVersion={"version": "1.5.2", "releaseNotes": only_sv},
        updateInformation={"version": "9.9", "state": "UNKNOWN", "releaseNotes": NOTES},
    )
    parsed = parse_firmware_updates([installed], GATEWAY_ID)
    assert parsed is not None
    assert release_url(parsed, "de") == "https://notes.example.test/sv"
    parsed = parse_firmware_updates([entry()], GATEWAY_ID)
    assert parsed is not None and release_url(parsed, "de") is None


# Client -------------------------------------------------------------------


async def test_the_client_reads_the_list_once_with_the_gateway_as_query() -> None:
    client = PointTClient(AsyncMock(), "token", gateway_id=GATEWAY_ID)
    with patch.object(
        client, "_request", AsyncMock(return_value=[offered()])
    ) as request:
        parsed = await client.get_firmware_updates(GATEWAY_ID)
    assert parsed is not None and parsed.offered_version == "2.0.0"
    request.assert_awaited_once_with(
        "GET", "firmwareUpdates?gatewayId=gateway-one", retryable=False
    )
    # A source bound to another gateway never reads it.
    with pytest.raises(ValueError):
        await client.get_firmware_updates("another-gateway")


async def test_the_client_encodes_the_gateway_in_the_query() -> None:
    client = PointTClient(AsyncMock(), "token")
    with patch.object(client, "_request", AsyncMock(return_value=[])) as request:
        assert await client.get_firmware_updates(" a&b=c ") is None
    assert request.await_args.args[1] == "firmwareUpdates?gatewayId=a%26b%3Dc"


def test_the_request_metrics_count_the_list_apart_without_identifiers() -> None:
    assert (
        request_category("firmwareUpdates?gatewayId=private-id") == "firmware_updates"
    )
    assert request_category("firmwareUpdates/confirmation") == "other"


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (ResourceForbidden("/", 403), Outcome.ENDPOINT_REFUSED),
        (ResourceNotFound("/", 404), Outcome.ENDPOINT_REFUSED),
        (UnexpectedHttpStatus(405), Outcome.ENDPOINT_REFUSED),
        (UnexpectedHttpStatus(418), Outcome.UNEXPECTED_STATUS),
        (ResourceNotAcceptable("/", 406), Outcome.UNEXPECTED_STATUS),
        (RateLimited(60.0), Outcome.RATE_LIMITED),
        (RequestTimeout("timeout"), Outcome.NO_ANSWER),
        (ServiceUnavailable(503), Outcome.NO_ANSWER),
        (InvalidPayload("not JSON"), Outcome.SCHEMA_ISSUE),
        (FirmwareUpdatesSchemaError("shape"), Outcome.SCHEMA_ISSUE),
        (RefreshTokenRejected(), Outcome.SIGN_IN_REQUIRED),
        (RuntimeError("bug"), Outcome.ERROR),
    ],
)
def test_each_failure_has_its_result_class(error: Exception, outcome: Outcome) -> None:
    assert classify(error) is outcome


# Tracker ------------------------------------------------------------------


class Clock:
    """The tracker's monotonic clock."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class FakeCoordinator:
    """What the tracker uses of a gateway coordinator."""

    def __init__(self, hass: HomeAssistant, answer: Any) -> None:
        self.hass = hass
        self.client = SimpleNamespace(
            get_firmware_updates=AsyncMock(
                side_effect=answer if isinstance(answer, Exception) else None,
                return_value=None if isinstance(answer, Exception) else answer,
            )
        )
        self.gateway = Gateway(GATEWAY_ID)
        self.update_interval: timedelta | None = timedelta(minutes=1)
        self.read_only_view = False
        self.down = False

    def cloud_unavailable(self) -> bool:
        return self.down


def fake_coordinator(hass: HomeAssistant, answer: Any) -> FakeCoordinator:
    return FakeCoordinator(hass, answer)


def first_read() -> timedelta:
    return FIRST_READ_DELAY + timedelta(seconds=FIRST_READ_SPREAD_SECONDS + 1)


async def advance(hass: HomeAssistant, clock: Clock, delta: timedelta) -> None:
    """Fire the timers due ``delta`` after the latest one was planned.

    A timer is planned from the loop's real time when its round ran, so
    each step counts from the last planned round, not from the test start.
    """
    clock.now += delta.total_seconds()
    async_fire_time_changed(hass, dt_util.utcnow() + delta)
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.fixture
def clock():
    value = Clock()
    with patch.object(module, "monotonic", value):
        yield value


async def test_the_first_read_waits_ten_minutes_then_every_six_hours(
    hass: HomeAssistant, clock: Clock
) -> None:
    coordinator = fake_coordinator(hass, parse_firmware_updates([entry()], GATEWAY_ID))
    tracker = FirmwareUpdateTracker(coordinator)  # type: ignore[arg-type]
    release = tracker.attach()
    calls = coordinator.client.get_firmware_updates
    # Staggered per gateway within the spread.
    assert (
        FIRST_READ_DELAY.total_seconds()
        <= tracker.diagnostics()["next_read_in_seconds"]
        <= FIRST_READ_DELAY.total_seconds() + FIRST_READ_SPREAD_SECONDS
    )
    await advance(hass, clock, timedelta(minutes=9))
    calls.assert_not_awaited()
    await advance(hass, clock, first_read())
    calls.assert_awaited_once_with(GATEWAY_ID)
    assert tracker.outcome is Outcome.OK and tracker.shown() is not None
    assert (
        tracker.diagnostics()["next_read_in_seconds"] == READ_INTERVAL.total_seconds()
    )
    await advance(hass, clock, READ_INTERVAL - timedelta(minutes=1))
    assert calls.await_count == 1
    await advance(hass, clock, READ_INTERVAL + timedelta(minutes=1))
    assert calls.await_count == 2
    release()
    await advance(hass, clock, READ_INTERVAL * 2)
    assert calls.await_count == 2
    assert tracker.diagnostics()["active"] is False


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        (ResourceForbidden("/", 403), Outcome.ENDPOINT_REFUSED),
        (ResourceNotFound("/", 404), Outcome.ENDPOINT_REFUSED),
        (UnexpectedHttpStatus(405), Outcome.ENDPOINT_REFUSED),
        (None, Outcome.NOT_LISTED),
    ],
)
async def test_a_refusing_endpoint_is_read_once_a_day_and_never_retried(
    hass: HomeAssistant, clock: Clock, answer: Any, outcome: Outcome
) -> None:
    coordinator = fake_coordinator(hass, answer)
    tracker = FirmwareUpdateTracker(coordinator)  # type: ignore[arg-type]
    release = tracker.attach()
    calls = coordinator.client.get_firmware_updates
    await advance(hass, clock, first_read())
    assert calls.await_count == 1
    assert tracker.outcome is outcome and tracker.shown() is None
    await advance(hass, clock, READ_INTERVAL * 3)
    assert calls.await_count == 1
    await advance(hass, clock, REFUSED_INTERVAL + timedelta(minutes=1))
    assert calls.await_count == 2
    assert tracker.diagnostics()["results"] == {outcome.value: 2}
    release()


async def test_a_failed_read_keeps_the_last_answer_until_its_validity_ends(
    hass: HomeAssistant, clock: Clock
) -> None:
    coordinator = fake_coordinator(
        hass, parse_firmware_updates([offered()], GATEWAY_ID)
    )
    tracker = FirmwareUpdateTracker(coordinator)  # type: ignore[arg-type]
    release = tracker.attach()
    calls = coordinator.client.get_firmware_updates
    await advance(hass, clock, first_read())
    shown = tracker.shown()
    assert shown is not None
    calls.side_effect = RequestTimeout("timeout")
    await advance(hass, clock, READ_INTERVAL + timedelta(minutes=1))
    # No retry of its own: one request per round.
    assert calls.await_count == 2
    assert tracker.outcome is Outcome.NO_ANSWER
    assert tracker.shown() is shown
    await advance(hass, clock, READ_INTERVAL + timedelta(minutes=1))
    assert calls.await_count == 3
    assert tracker.shown() is shown
    clock.now += VALIDITY.total_seconds()
    assert tracker.shown() is None
    release()


@pytest.mark.parametrize(
    "answer", [FirmwareUpdatesSchemaError("shape"), "an unparsed answer"]
)
async def test_an_unexpected_shape_ends_the_shown_answer(
    hass: HomeAssistant, clock: Clock, answer: Any
) -> None:
    coordinator = fake_coordinator(
        hass, parse_firmware_updates([offered()], GATEWAY_ID)
    )
    tracker = FirmwareUpdateTracker(coordinator)  # type: ignore[arg-type]
    release = tracker.attach()
    await advance(hass, clock, first_read())
    assert tracker.shown() is not None
    calls = coordinator.client.get_firmware_updates
    if isinstance(answer, Exception):
        calls.side_effect = answer
    else:
        calls.return_value = answer
    await advance(hass, clock, READ_INTERVAL + timedelta(minutes=1))
    assert tracker.outcome is Outcome.SCHEMA_ISSUE
    assert tracker.shown() is None
    assert tracker.diagnostics()["results"] == {"ok": 1, "schema_issue": 1}
    release()


@pytest.mark.parametrize("hold", ["down", "read_only_view", "no_polling"])
async def test_a_round_without_a_usable_cloud_sends_nothing(
    hass: HomeAssistant, clock: Clock, hold: str
) -> None:
    coordinator = fake_coordinator(hass, parse_firmware_updates([entry()], GATEWAY_ID))
    if hold == "down":
        coordinator.down = True
    elif hold == "read_only_view":
        coordinator.read_only_view = True
    else:
        coordinator.update_interval = None
    tracker = FirmwareUpdateTracker(coordinator)  # type: ignore[arg-type]
    release = tracker.attach()
    calls = coordinator.client.get_firmware_updates
    await advance(hass, clock, first_read())
    calls.assert_not_awaited()
    assert tracker.held_rounds == 1 and tracker.attempts == 0
    assert tracker.diagnostics()["next_read_in_seconds"] == HELD_RECHECK.total_seconds()
    coordinator.down = coordinator.read_only_view = False
    coordinator.update_interval = timedelta(minutes=1)
    await advance(hass, clock, HELD_RECHECK + timedelta(seconds=1))
    calls.assert_awaited_once()
    release()


async def test_an_unexpected_error_is_logged_once(
    hass: HomeAssistant, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = fake_coordinator(hass, RuntimeError("private detail"))
    tracker = FirmwareUpdateTracker(coordinator)  # type: ignore[arg-type]
    release = tracker.attach()
    await advance(hass, clock, first_read())
    await advance(hass, clock, READ_INTERVAL + timedelta(minutes=1))
    warnings = [item for item in caplog.records if item.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "RuntimeError" in warnings[0].getMessage()
    assert "private detail" not in caplog.text
    assert tracker.outcome is Outcome.ERROR
    release()


async def test_a_stopping_source_ends_the_round_quietly(
    hass: HomeAssistant, clock: Clock
) -> None:
    coordinator = fake_coordinator(hass, SourceStopped())
    tracker = FirmwareUpdateTracker(coordinator)  # type: ignore[arg-type]
    release = tracker.attach()
    await advance(hass, clock, first_read())
    assert tracker.outcome is None
    assert tracker.diagnostics()["next_read_in_seconds"] is None
    release()


async def test_users_share_one_schedule_and_a_new_attach_keeps_the_cadence(
    hass: HomeAssistant, clock: Clock
) -> None:
    coordinator = fake_coordinator(hass, parse_firmware_updates([entry()], GATEWAY_ID))
    tracker = FirmwareUpdateTracker(coordinator)  # type: ignore[arg-type]
    first, second = tracker.attach(), tracker.attach()
    first()
    first()  # a second release of the same user counts once
    assert tracker._timer is not None
    await advance(hass, clock, first_read())
    assert coordinator.client.get_firmware_updates.await_count == 1
    second()
    assert tracker._timer is None
    clock.now += 3600
    third = tracker.attach()
    assert tracker.diagnostics()["next_read_in_seconds"] == (
        READ_INTERVAL.total_seconds() - 3600
    )
    third()


async def test_the_last_release_cancels_a_running_read(
    hass: HomeAssistant, clock: Clock
) -> None:
    coordinator = fake_coordinator(hass, None)
    never = asyncio.Event()

    async def hanging(_gateway_id: str) -> None:
        await never.wait()

    coordinator.client.get_firmware_updates.side_effect = hanging
    tracker = FirmwareUpdateTracker(coordinator)  # type: ignore[arg-type]
    release = tracker.attach()
    async_fire_time_changed(hass, dt_util.utcnow() + first_read())
    await hass.async_block_till_done()
    task = tracker._task
    assert task is not None and not task.done()
    # A timer that fires while the read runs starts no second one.
    tracker._fire(None)
    assert tracker._task is task
    release()
    await hass.async_block_till_done()
    assert task.cancelled()
    assert tracker.outcome is None and tracker._timer is None
    # A timer left from before the release starts nothing.
    tracker._fire(None)
    assert tracker._task is None


async def test_home_assistant_stop_cancels_the_timer(
    hass: HomeAssistant, clock: Clock
) -> None:
    coordinator = fake_coordinator(hass, None)
    tracker = FirmwareUpdateTracker(coordinator)  # type: ignore[arg-type]
    tracker.attach()
    assert tracker._timer is not None
    hass.bus.async_fire("homeassistant_stop")
    await hass.async_block_till_done()
    assert tracker._timer is None


async def test_diagnostics_show_versions_and_classes_without_identifiers(
    hass: HomeAssistant, clock: Clock
) -> None:
    coordinator = fake_coordinator(
        hass, parse_firmware_updates([offered()], GATEWAY_ID)
    )
    assert _firmware_update_diagnostics(coordinator) == {  # type: ignore[arg-type]
        "active": False,
        "result": None,
    }
    tracker = module.firmware_update_tracker(coordinator)  # type: ignore[arg-type]
    release = tracker.attach()
    await advance(hass, clock, first_read())
    report = _firmware_update_diagnostics(coordinator)  # type: ignore[arg-type]
    assert report == {
        "active": True,
        "result": "ok",
        "results": {"ok": 1},
        "attempts": 1,
        "held_rounds": 0,
        "last_attempt_age_seconds": 0,
        "last_answer_age_seconds": 0,
        "next_read_in_seconds": int(READ_INTERVAL.total_seconds()),
        "shown": True,
        "restored": False,
        "installed_version": "1.5.2",
        "update_version": "2.0.0",
        "update_offered": True,
        "update_state": "waiting",
        "release_notes_links": 3,
        "auto_update_enabled": True,
        "auto_update_changeable": False,
    }
    dump = json.dumps(report)
    assert GATEWAY_ID not in dump and "example.test" not in dump
    release()


# Entity -------------------------------------------------------------------


def test_the_update_platform_is_set_up() -> None:
    from homeassistant.const import Platform

    assert Platform.UPDATE in PLATFORMS


async def _setup(
    hass: HomeAssistant,
    answer: Any,
    *,
    firmware: str | None = None,
    prepare: Any = None,
):
    """A real entry (migrated to the managed format) against the fake cloud."""
    from custom_components.bosch_buderus_heating.coordinator import (
        BoschBuderusDataUpdateCoordinator,
    )
    from tests.helpers.managed_runtime import wait_for_sources
    from tests.helpers.rrc_cloud import Clock as CloudClock
    from tests.helpers.rrc_cloud import RrcCloud, cloud_patches, load_tree
    from tests.test_entry import _entry

    tree = load_tree("rrc/ct200-shapes.json")
    if firmware == "":
        del tree["/gateway/versionFirmware"]
    elif firmware is not None:
        tree["/gateway/versionFirmware"] = parse_resource(
            {
                "id": "/gateway/versionFirmware",
                "type": "stringValue",
                "value": firmware,
                "writeable": 0,
                "recordable": 0,
            }
        )
    cloud = RrcCloud(tree)
    reads = AsyncMock(
        side_effect=answer if isinstance(answer, Exception) else None,
        return_value=None if isinstance(answer, Exception) else answer,
    )
    gateway = Gateway(GATEWAY_ID, device_type="rrc2", firmware_version="1.4.0")
    stack = cloud_patches(cloud, CloudClock(), gateway)
    stack.__enter__()
    stack.enter_context(patch.object(PointTClient, "get_firmware_updates", reads))
    entry = _entry(hass)
    if prepare is not None:
        prepare(entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    await wait_for_sources(hass, entry, 1)
    owner = entry.runtime_data
    coordinator = next(
        value
        for key in owner.connections.snapshot.bindings
        if isinstance(
            value := owner.connections.source_data(key),
            BoschBuderusDataUpdateCoordinator,
        )
    )
    return entry, coordinator, stack, reads


async def settle(hass: HomeAssistant) -> None:
    """Let the managed platforms add what a round found (background tasks;
    the running sources never finish, so they are not awaited as a whole)."""
    for _ in range(10):
        await hass.async_block_till_done()
        await asyncio.sleep(0.01)


def _entity_id(hass: HomeAssistant) -> str | None:
    return er.async_get(hass).async_get_entity_id(
        "update", DOMAIN, f"{GATEWAY_ID}:firmware_update"
    )


async def test_the_entity_shows_an_offered_update_read_only(
    hass: HomeAssistant,
) -> None:
    from tests.helpers.rrc_absence import teardown

    hass.config.language = "de"
    answer = parse_firmware_updates([offered("DENIED")], GATEWAY_ID)
    entry, _coordinator, stack, reads = await _setup(hass, answer)
    try:
        assert _entity_id(hass) is None
        async_fire_time_changed(hass, dt_util.utcnow() + first_read())
        await settle(hass)
        reads.assert_awaited_once_with(GATEWAY_ID)
        entity_id = _entity_id(hass)
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "on"
        assert state.attributes["installed_version"] == "1.5.2"
        assert state.attributes["latest_version"] == "2.0.0"
        assert state.attributes["release_url"] == "https://notes.example.test/de/2.0"
        assert state.attributes["device_class"] == "firmware"
        assert state.attributes["supported_features"] == 0
        assert state.attributes["auto_update"] is True
        assert state.attributes["auto_update_enabled"] is True
        assert state.attributes["auto_update_changeable"] is False
        assert state.attributes["update_state"] == "denied"
        # Never installs: the install action is refused by Home Assistant.
        from homeassistant.exceptions import HomeAssistantError

        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "update", "install", {"entity_id": entity_id}, blocking=True
            )
        assert reads.await_count == 1
    finally:
        await teardown(hass, entry, stack)


@pytest.mark.parametrize(
    ("firmware", "installed"),
    [("04.08.02", "04.08.02"), (None, "05.04.00"), ("", "1.4.0")],
)
async def test_the_entity_is_current_without_an_update_and_falls_back_to_the_gateway(
    hass: HomeAssistant, firmware: str | None, installed: str
) -> None:
    from tests.helpers.rrc_absence import teardown

    answer = FirmwareUpdates(None, None, None, None)
    entry, _coordinator, stack, _reads = await _setup(hass, answer, firmware=firmware)
    try:
        async_fire_time_changed(hass, dt_util.utcnow() + first_read())
        await settle(hass)
        entity_id = _entity_id(hass)
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "off"
        assert state.attributes["installed_version"] == installed
        assert state.attributes["latest_version"] == installed
        assert state.attributes["release_url"] is None
        assert state.attributes["auto_update"] is False
        assert state.attributes["update_state"] is None
    finally:
        await teardown(hass, entry, stack)


@pytest.mark.parametrize(
    "answer", [ResourceForbidden("/", 403), ResourceNotFound("/", 404), None]
)
async def test_a_refused_or_unlisted_gateway_gets_no_entity(
    hass: HomeAssistant, answer: Any
) -> None:
    from tests.helpers.rrc_absence import teardown

    entry, _coordinator, stack, reads = await _setup(hass, answer)
    try:
        async_fire_time_changed(hass, dt_util.utcnow() + first_read())
        await settle(hass)
        reads.assert_awaited_once()
        assert _entity_id(hass) is None
    finally:
        await teardown(hass, entry, stack)


async def test_an_existing_entity_becomes_unavailable_after_a_refusal(
    hass: HomeAssistant,
) -> None:
    from tests.helpers.rrc_absence import teardown

    answer = parse_firmware_updates([entry()], GATEWAY_ID)
    entry_, _coordinator, stack, reads = await _setup(hass, answer)
    try:
        async_fire_time_changed(hass, dt_util.utcnow() + first_read())
        await settle(hass)
        entity_id = _entity_id(hass)
        assert entity_id is not None
        assert hass.states.get(entity_id).state == "off"
        reads.side_effect = ResourceForbidden("/", 403)
        async_fire_time_changed(
            hass, dt_util.utcnow() + first_read() + READ_INTERVAL + timedelta(hours=1)
        )
        await settle(hass)
        assert reads.await_count == 2
        assert hass.states.get(entity_id).state == "unavailable"
    finally:
        await teardown(hass, entry_, stack)


async def test_unloading_the_entry_stops_the_reads(hass: HomeAssistant) -> None:
    from tests.helpers.rrc_absence import teardown

    entry, coordinator, stack, reads = await _setup(hass, None)
    tracker = module.existing_tracker(coordinator)
    assert tracker is not None and tracker._timer is not None
    await teardown(hass, entry, stack)
    assert tracker._timer is None
    async_fire_time_changed(hass, dt_util.utcnow() + first_read())
    await settle(hass)
    reads.assert_not_awaited()


# Restart ------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        [offered()],
        [offered("APPROVED")],
        [offered("NONE")],
        [offered("SCHEDULED_LATER")],
        [offered("not a token!")],
        [entry()],
        [entry(autoUpdateInformation=None)],
        [{"gatewayId": GATEWAY_ID}],
        [
            entry(
                currentVersion={
                    "version": "1.5.2",
                    "releaseNotes": [
                        {"language": "en_GB", "url": "https://notes.example.test/en"}
                    ],
                }
            )
        ],
    ],
)
def test_a_stored_answer_reads_back_the_same(answer: Any) -> None:
    parsed = parse_firmware_updates(answer, GATEWAY_ID)
    assert parsed is not None
    stored = stored_entry(parsed)
    assert GATEWAY_ID not in json.dumps(stored)
    assert restored_updates(json.loads(json.dumps(stored))) == parsed


@pytest.mark.parametrize(
    "stored",
    [
        None,
        "text",
        [offered()],
        {"gatewayId": GATEWAY_ID},
        {"currentVersion": "1.5.2"},
        {"updateInformation": {"state": "WAITING"}},
    ],
)
def test_an_invalid_stored_answer_is_not_restored(stored: Any) -> None:
    assert restored_updates(stored) is None


async def test_a_restored_answer_is_shown_until_the_first_read(
    hass: HomeAssistant, clock: Clock
) -> None:
    coordinator = fake_coordinator(hass, parse_firmware_updates([entry()], GATEWAY_ID))
    tracker = FirmwareUpdateTracker(coordinator)  # type: ignore[arg-type]
    release = tracker.attach()
    kept = parse_firmware_updates([offered()], GATEWAY_ID)
    assert kept is not None
    age = 3600.0
    read_at = time.time() - age
    assert tracker.restore(kept, read_at) is True
    assert tracker.shown() is kept
    assert tracker.answered_at_time == read_at
    report = tracker.diagnostics()
    assert report["restored"] is True and report["shown"] is True
    assert report["last_answer_age_seconds"] in (int(age), int(age) + 1)
    # The kept age counts: the answer ends 13 hours after it was read.
    clock.now += VALIDITY.total_seconds() - age - 10
    assert tracker.shown() is kept
    clock.now -= VALIDITY.total_seconds() - age - 10
    # The first read replaces it; a second restore changes nothing.
    await advance(hass, clock, first_read())
    assert tracker.shown() is not kept
    assert tracker.shown() == parse_firmware_updates([entry()], GATEWAY_ID)
    assert tracker.diagnostics()["restored"] is False
    assert tracker.restore(kept, time.time()) is False
    assert tracker.shown() is not kept
    release()


async def test_a_restored_answer_ends_with_its_validity(
    hass: HomeAssistant, clock: Clock
) -> None:
    coordinator = fake_coordinator(hass, None)
    tracker = FirmwareUpdateTracker(coordinator)  # type: ignore[arg-type]
    kept = parse_firmware_updates([offered()], GATEWAY_ID)
    assert kept is not None
    age = 3600.0
    assert tracker.restore(kept, time.time() - age) is True
    clock.now += VALIDITY.total_seconds() - age + 10
    assert tracker.shown() is None


@pytest.mark.parametrize(
    "age", [VALIDITY.total_seconds() + 60, -600.0], ids=["too_old", "future"]
)
async def test_a_stored_answer_outside_its_validity_is_not_restored(
    hass: HomeAssistant, clock: Clock, age: float
) -> None:
    coordinator = fake_coordinator(hass, None)
    tracker = FirmwareUpdateTracker(coordinator)  # type: ignore[arg-type]
    kept = parse_firmware_updates([offered()], GATEWAY_ID)
    assert kept is not None
    assert tracker.restore(kept, time.time() - age) is False
    assert tracker.shown() is None
    assert tracker.diagnostics()["restored"] is False


async def test_a_restored_answer_survives_a_round_without_an_answer(
    hass: HomeAssistant, clock: Clock
) -> None:
    coordinator = fake_coordinator(hass, RequestTimeout("timeout"))
    tracker = FirmwareUpdateTracker(coordinator)  # type: ignore[arg-type]
    release = tracker.attach()
    kept = parse_firmware_updates([offered()], GATEWAY_ID)
    assert kept is not None
    assert tracker.restore(kept, time.time() - 60) is True
    await advance(hass, clock, first_read())
    assert tracker.outcome is Outcome.NO_ANSWER
    assert tracker.shown() is kept
    assert tracker.diagnostics()["restored"] is True
    # A refusal ends it at once, as in operation.
    coordinator.client.get_firmware_updates.side_effect = ResourceForbidden("/", 403)
    await advance(hass, clock, READ_INTERVAL + timedelta(minutes=1))
    assert tracker.shown() is None
    assert tracker.diagnostics()["restored"] is False
    release()


async def test_an_unloaded_coordinator_and_its_tracker_are_freed(
    hass: HomeAssistant, clock: Clock
) -> None:
    coordinator = fake_coordinator(hass, parse_firmware_updates([entry()], GATEWAY_ID))
    tracker = module.firmware_update_tracker(coordinator)  # type: ignore[arg-type]
    release = tracker.attach()
    await advance(hass, clock, first_read())
    assert module.existing_tracker(coordinator) is tracker  # type: ignore[arg-type]
    release()
    coordinator_ref = weakref.ref(coordinator)
    tracker_ref = weakref.ref(tracker)
    count = len(module._TRACKERS)
    del coordinator, tracker, release
    gc.collect()
    assert coordinator_ref() is None
    assert tracker_ref() is None
    assert len(module._TRACKERS) == count - 1


async def test_a_tracker_without_its_coordinator_starts_no_read(
    hass: HomeAssistant, clock: Clock
) -> None:
    coordinator = fake_coordinator(hass, None)
    tracker = FirmwareUpdateTracker(coordinator)  # type: ignore[arg-type]
    tracker._users = 1
    del coordinator
    gc.collect()
    tracker._fire(None)
    assert tracker._task is None
    with pytest.raises(RuntimeError):
        _ = tracker.coordinator


def _register_entity(hass: HomeAssistant, extra: Any) -> Any:
    """Register the entity of an earlier run for the entry and keep ``extra``
    as its stored data."""
    from homeassistant.core import State

    def prepare(config_entry: Any) -> None:
        entity_id = (
            er.async_get(hass)
            .async_get_or_create(
                "update",
                DOMAIN,
                f"{GATEWAY_ID}:firmware_update",
                config_entry=config_entry,
            )
            .entity_id
        )
        if extra is not None:
            mock_restore_cache_with_extra_data(hass, [(State(entity_id, "on"), extra)])

    return prepare


def _kept(answer: Any, age: float) -> dict[str, Any]:
    parsed = parse_firmware_updates(answer, GATEWAY_ID)
    assert parsed is not None
    return {"answer": stored_entry(parsed), "answered_at": time.time() - age}


async def test_the_entity_shows_the_kept_answer_after_a_restart(
    hass: HomeAssistant,
) -> None:
    from tests.helpers.rrc_absence import teardown

    entry_, _coordinator, stack, reads = await _setup(
        hass,
        parse_firmware_updates([entry()], GATEWAY_ID),
        prepare=_register_entity(hass, _kept([offered()], 3600)),
    )
    entity_id = _entity_id(hass)
    assert entity_id is not None
    try:
        # Shown at once, before the first read of this run.
        reads.assert_not_awaited()
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "on"
        assert state.attributes["installed_version"] == "1.5.2"
        assert state.attributes["latest_version"] == "2.0.0"
        assert state.attributes["update_state"] == "waiting"
        # The first read replaces it.
        async_fire_time_changed(hass, dt_util.utcnow() + first_read())
        await settle(hass)
        reads.assert_awaited_once()
        state = hass.states.get(entity_id)
        assert state is not None and state.state == "off"
        assert state.attributes["latest_version"] == "1.5.2"
    finally:
        await teardown(hass, entry_, stack)


@pytest.mark.parametrize(
    "extra",
    [
        None,
        "too_old",
        {"answer": "1.5.2", "answered_at": 0},
        {"answer": {}, "answered_at": True},
        {"answered_at": 5},
    ],
)
async def test_the_entity_waits_without_a_usable_kept_answer(
    hass: HomeAssistant, extra: Any
) -> None:
    from tests.helpers.rrc_absence import teardown

    if extra == "too_old":
        extra = _kept([offered()], VALIDITY.total_seconds() + 60)
    entry_, _coordinator, stack, reads = await _setup(
        hass,
        parse_firmware_updates([offered()], GATEWAY_ID),
        prepare=_register_entity(hass, extra),
    )
    entity_id = _entity_id(hass)
    assert entity_id is not None
    try:
        state = hass.states.get(entity_id)
        assert state is not None and state.state == "unavailable"
        async_fire_time_changed(hass, dt_util.utcnow() + first_read())
        await settle(hass)
        reads.assert_awaited_once()
        state = hass.states.get(entity_id)
        assert state is not None and state.state == "on"
    finally:
        await teardown(hass, entry_, stack)


async def test_the_entity_keeps_the_answer_for_the_next_restart(
    hass: HomeAssistant,
) -> None:
    from homeassistant.helpers.restore_state import async_get as restore_data

    from tests.helpers.rrc_absence import teardown

    answer = parse_firmware_updates([offered()], GATEWAY_ID)
    assert answer is not None
    entry_, _coordinator, stack, _reads = await _setup(hass, answer)
    try:
        async_fire_time_changed(hass, dt_util.utcnow() + first_read())
        await settle(hass)
        entity_id = _entity_id(hass)
        assert entity_id is not None
        before = time.time()
        stored = {
            state.state.entity_id: state
            for state in restore_data(hass).async_get_stored_states()
        }[entity_id]
        assert stored.extra_data is not None
        kept = stored.extra_data.as_dict()
        assert restored_updates(kept["answer"]) == answer
        assert before - 60 <= kept["answered_at"] <= before
        assert GATEWAY_ID not in json.dumps(kept)
    finally:
        await teardown(hass, entry_, stack)
