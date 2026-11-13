"""F1: read the firmware update list of a cloud gateway, read-only.

One tracker per gateway coordinator. It runs while the update platform shows
the gateway (``attach``) and reads ``firmwareUpdates`` once about ten minutes
after the start (staggered per gateway), then every six hours. A refusing
endpoint (403/404/405) or a list without the gateway falls back to one read a
day. There is no retry of its own: a failed read waits for the next round,
and the account pause (429/Retry-After) holds the read like any other
request. Nothing is ever sent to confirm an update or to change the gateway's
automatic update setting.

The update entity keeps the last answer across a restart and hands it back
with ``restore``; it is shown until the first new read, within the same
validity as in operation, and never made newer than it was.
"""

from __future__ import annotations

import asyncio
import logging
import math
import zlib
from collections import Counter
from collections.abc import Callable
from datetime import timedelta
from enum import StrEnum
from time import monotonic, time
from typing import TYPE_CHECKING, Final
from weakref import WeakKeyDictionary, ref

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, callback
from homeassistant.helpers.event import async_call_later

from .pointt.exceptions import (
    AuthenticationError,
    InvalidPayload,
    RateLimited,
    ResourceForbidden,
    ResourceNotAcceptable,
    ResourceNotFound,
    SourceStopped,
    TransportError,
    UnexpectedHttpStatus,
)
from .pointt.firmware_updates import KNOWN_STATES, FirmwareUpdates

if TYPE_CHECKING:
    from .coordinator import BoschBuderusDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

FIRST_READ_DELAY: Final = timedelta(minutes=10)
# The first reads of several gateways are spread over these seconds.
FIRST_READ_SPREAD_SECONDS: Final = 180
READ_INTERVAL: Final = timedelta(hours=6)
REFUSED_INTERVAL: Final = timedelta(hours=24)
# A round the cloud state holds back sends nothing; it looks again then.
HELD_RECHECK: Final = timedelta(hours=1)
# The last answer is shown for two missed rounds and a margin.
VALIDITY: Final = 2 * READ_INTERVAL + timedelta(hours=1)
_VERSION_TOKEN_LENGTH = 32


class Outcome(StrEnum):
    """The result class of one round (diagnostics, entity availability)."""

    OK = "ok"
    ENDPOINT_REFUSED = "endpoint_refused"
    NOT_LISTED = "not_listed"
    SCHEMA_ISSUE = "schema_issue"
    RATE_LIMITED = "rate_limited"
    NO_ANSWER = "no_answer"
    UNEXPECTED_STATUS = "unexpected_status"
    SIGN_IN_REQUIRED = "sign_in_required"
    ERROR = "error"


# Results that end the shown answer at once (F1): the entity is not created
# or becomes unavailable.
_ENDING: Final = frozenset(
    {Outcome.ENDPOINT_REFUSED, Outcome.NOT_LISTED, Outcome.SCHEMA_ISSUE}
)
# The tracker holds its coordinator only weakly, so an unloaded coordinator
# and its entry in this mapping are freed.
_TRACKERS: WeakKeyDictionary[BoschBuderusDataUpdateCoordinator, FirmwareUpdateTracker]
_TRACKERS = WeakKeyDictionary()


def firmware_update_tracker(
    coordinator: BoschBuderusDataUpdateCoordinator,
) -> FirmwareUpdateTracker:
    """The coordinator's tracker, created on first use."""
    tracker = _TRACKERS.get(coordinator)
    if tracker is None:
        tracker = _TRACKERS[coordinator] = FirmwareUpdateTracker(coordinator)
    return tracker


def existing_tracker(
    coordinator: BoschBuderusDataUpdateCoordinator,
) -> FirmwareUpdateTracker | None:
    """The tracker if the update platform ever attached one (diagnostics)."""
    return _TRACKERS.get(coordinator)


def classify(error: Exception) -> Outcome:
    """The result class of a failed read."""
    if isinstance(error, ResourceForbidden | ResourceNotFound) or (
        isinstance(error, UnexpectedHttpStatus) and error.status == 405
    ):
        return Outcome.ENDPOINT_REFUSED
    if isinstance(error, RateLimited):
        return Outcome.RATE_LIMITED
    if isinstance(error, InvalidPayload):
        return Outcome.SCHEMA_ISSUE
    if isinstance(error, UnexpectedHttpStatus | ResourceNotAcceptable):
        return Outcome.UNEXPECTED_STATUS
    if isinstance(error, TransportError):
        return Outcome.NO_ANSWER
    if isinstance(error, AuthenticationError):
        return Outcome.SIGN_IN_REQUIRED
    return Outcome.ERROR


class FirmwareUpdateTracker:
    """Schedule, result and diagnostics of the firmware update reads."""

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        self._coordinator = ref(coordinator)
        self.updates: FirmwareUpdates | None = None
        self.outcome: Outcome | None = None
        self.restored = False
        self._answered_at: float | None = None
        # The wall clock time of the shown answer, kept across a restart.
        self.answered_at_time: float | None = None
        self._attempted_at: float | None = None
        self._due: float | None = None
        self.attempts = 0
        self.held_rounds = 0
        self.outcomes: Counter[str] = Counter()
        self._users = 0
        self._timer: CALLBACK_TYPE | None = None
        self._stop_listener: CALLBACK_TYPE | None = None
        self._task: asyncio.Task[None] | None = None
        self._listeners: list[Callable[[], None]] = []
        self._error_logged = False

    @property
    def coordinator(self) -> BoschBuderusDataUpdateCoordinator:
        coordinator = self._coordinator()
        if coordinator is None:
            raise RuntimeError("The coordinator of the firmware tracker is gone")
        return coordinator

    # Lifecycle -------------------------------------------------------------

    @callback
    def attach(self) -> Callable[[], None]:
        """Start the reads for one user; the returned callback ends its use.

        The reads stop when the last user is gone or Home Assistant stops.
        """
        self._users += 1
        if self._users == 1:
            self._start()
        released = False

        @callback
        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            self._users -= 1
            if self._users == 0:
                self._stop()

        return release

    @callback
    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        @callback
        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    @callback
    def _start(self) -> None:
        hass = self.coordinator.hass
        self._stop_listener = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._hass_stopped
        )
        if self._attempted_at is None:
            spread = zlib.crc32(self.coordinator.gateway.gateway_id.encode()) % (
                FIRST_READ_SPREAD_SECONDS
            )
            delay = FIRST_READ_DELAY.total_seconds() + spread
        else:
            # Attached again (a platform reload): keep the running cadence.
            delay = max(0.0, (self._due or monotonic()) - monotonic())
        self._schedule(delay)

    @callback
    def _stop(self) -> None:
        self._cancel_timer()
        if self._stop_listener is not None:
            self._stop_listener()
            self._stop_listener = None
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    @callback
    def _hass_stopped(self, _event: Event) -> None:
        self._stop_listener = None
        self._cancel_timer()

    @callback
    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer()
            self._timer = None

    @callback
    def _schedule(self, delay: float) -> None:
        self._cancel_timer()
        self._due = monotonic() + delay
        self._timer = async_call_later(self.coordinator.hass, delay, self._fire)

    @callback
    def _fire(self, _now: object) -> None:
        self._timer = None
        if self._users == 0 or self._coordinator() is None:
            return
        if self._task is not None and not self._task.done():
            return
        self._task = self.coordinator.hass.async_create_background_task(
            self.async_read(), "home_comfort_firmware_updates", eager_start=False
        )

    # Reading ---------------------------------------------------------------

    def _held(self) -> bool:
        """The cloud is not usable now; this round sends nothing."""
        coordinator = self.coordinator
        return bool(
            getattr(coordinator, "read_only_view", False)
            or getattr(coordinator, "inventory_view", False)
            or coordinator.update_interval is None
            or coordinator.cloud_unavailable()
        )

    async def async_read(self) -> None:
        """One round: read once unless held, then plan the next round."""
        if self._held():
            self.held_rounds += 1
            self._next(HELD_RECHECK)
            return
        coordinator = self.coordinator
        self.attempts += 1
        self._attempted_at = monotonic()
        try:
            updates = await coordinator.client.get_firmware_updates(
                coordinator.gateway.gateway_id
            )
        except SourceStopped:
            return  # the source ends; its tracker goes with it
        except Exception as error:  # one failed read never breaks anything else
            outcome = classify(error)
            if outcome is Outcome.ERROR:
                if not self._error_logged:
                    self._error_logged = True
                    _LOGGER.warning(
                        "The firmware update list could not be read (%s); "
                        "further failures are logged at debug level",
                        type(error).__name__,
                    )
                else:
                    _LOGGER.debug(
                        "The firmware update list could not be read (%s)",
                        type(error).__name__,
                    )
            else:
                _LOGGER.debug("Firmware update list not read: %s", outcome.value)
            self._finish(outcome, None)
            return
        if updates is None:
            self._finish(Outcome.NOT_LISTED, None)
        elif isinstance(updates, FirmwareUpdates):
            self._finish(Outcome.OK, updates)
        else:  # never shown without the parsed contract
            self._finish(Outcome.SCHEMA_ISSUE, None)

    def _finish(self, outcome: Outcome, updates: FirmwareUpdates | None) -> None:
        self.outcome = outcome
        self.outcomes[outcome.value] += 1
        if outcome is Outcome.OK:
            self.updates = updates
            self.restored = False
            self._answered_at = monotonic()
            self.answered_at_time = time()
        elif outcome in _ENDING:
            self.updates = None
            self.restored = False
            self._answered_at = None
            self.answered_at_time = None
        refused = outcome in (Outcome.ENDPOINT_REFUSED, Outcome.NOT_LISTED)
        self._next(REFUSED_INTERVAL if refused else READ_INTERVAL)
        for listener in tuple(self._listeners):
            listener()

    def _next(self, interval: timedelta) -> None:
        if self._users:
            self._schedule(interval.total_seconds())

    @callback
    def restore(self, updates: FirmwareUpdates, answered_at_time: float) -> bool:
        """Show the answer kept from before a restart until the first read.

        Only before this run read anything, and only within the validity of
        the answer at the time it was read; its age is kept.
        """
        if self.outcome is not None or self._answered_at is not None:
            return False
        age = time() - answered_at_time
        if not 0 <= age <= VALIDITY.total_seconds():
            return False
        self.updates = updates
        self._answered_at = monotonic() - age
        self.answered_at_time = answered_at_time
        self.restored = True
        return True

    # State -----------------------------------------------------------------

    def shown(self) -> FirmwareUpdates | None:
        """The answer to show: the last one, within its validity."""
        if (
            self.updates is None
            or self._answered_at is None
            or monotonic() - self._answered_at > VALIDITY.total_seconds()
        ):
            return None
        return self.updates

    def diagnostics(self) -> dict[str, object]:
        """Times, result class, state and versions; no gateway ID, no links."""
        now = monotonic()
        updates = self.updates
        current = updates.current if updates is not None else None
        update = updates.update if updates is not None else None
        return {
            "active": self._users > 0,
            "result": self.outcome.value if self.outcome is not None else None,
            "results": dict(sorted(self.outcomes.items())),
            "attempts": self.attempts,
            "held_rounds": self.held_rounds,
            "last_attempt_age_seconds": _age(now, self._attempted_at),
            "last_answer_age_seconds": _age(now, self._answered_at),
            "next_read_in_seconds": (
                max(0, math.ceil(self._due - now))
                if self._due is not None and self._timer is not None
                else None
            ),
            "shown": self.shown() is not None,
            "restored": self.restored,
            "installed_version": _version_token(current.version if current else None),
            "update_version": _version_token(update.version if update else None),
            "update_offered": (
                updates.offered_version is not None if updates is not None else None
            ),
            "update_state": _state_token(updates.update_state if updates else None),
            "release_notes_links": (
                len(update.release_notes) if update is not None else 0
            ),
            "auto_update_enabled": (
                updates.auto_update_enabled if updates is not None else None
            ),
            "auto_update_changeable": (
                updates.auto_update_changeable if updates is not None else None
            ),
        }


def _age(now: float, then: float | None) -> int | None:
    return max(0, int(now - then)) if then is not None else None


def _version_token(value: str | None) -> str | None:
    """A version as reported if it looks like one, else ``other``."""
    if value is None:
        return None
    return (
        value
        if len(value) <= _VERSION_TOKEN_LENGTH
        and all(char.isalnum() or char in "._-" for char in value)
        and value.isascii()
        else "other"
    )


def _state_token(value: str | None) -> str | None:
    if value is None:
        return None
    return value if value.upper() in KNOWN_STATES else "other"
