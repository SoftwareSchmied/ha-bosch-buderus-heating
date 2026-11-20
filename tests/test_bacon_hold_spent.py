"""A used-up hold of a lost connection never opens again without a connection."""

import asyncio
from unittest.mock import AsyncMock, patch

import aiomqtt
import pytest

from custom_components.bosch_buderus_heating.bacon.session import BaconAccount
from custom_components.bosch_buderus_heating.cloud_hold import GRACE_MAX_SECONDS
from tests.test_bacon_session import MODULE, emit, environment


class Clock:
    """A simulated clock for the session loop and its hold timer."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def run_with_clock(account: BaconAccount, clock: Clock, attempts: int):
    """Run the real session loop; returns what each sleep after a failure saw."""
    seen: list[tuple[float, bool, bool]] = []
    start = clock.now

    async def sleep(delay: float) -> None:
        seen.append(
            (
                clock.now - start,
                account.available("serial-A"),
                account.diagnostics()["cloud_hold"]["holding"],
            )
        )
        if len(seen) == attempts:
            raise asyncio.CancelledError
        target = clock.now + delay
        timer = account._grace_timer
        since = account.cloud_hold.since
        if timer is not None and since is not None:
            deadline = since + GRACE_MAX_SECONDS
            if deadline <= target:
                # The real timer would fire in the middle of the pause.
                clock.now = deadline
                timer.cancel()
                account._grace_ended()
        clock.now = target

    return seen, sleep


async def test_failed_attempt_after_the_time_limit_opens_no_new_hold():
    """Attempts at 0, 60, 180 and 420 s (rate limit backoff): the hold ends at
    300 s and the attempt at 420 s must not show the old readings again."""
    account = environment()
    account.run_connection = AsyncMock(side_effect=aiomqtt.MqttCodeError(151))
    clock = Clock()
    seen, sleep = run_with_clock(account, clock, 5)
    with (
        patch(MODULE + ".monotonic", clock),
        patch(MODULE + ".asyncio.sleep", side_effect=sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await account.run()
    assert [round(at) for at, _, _ in seen] == [0, 60, 180, 420, 720]
    assert [shown for _, shown, _ in seen] == [True, True, True, False, False]
    assert [holding for _, _, holding in seen] == [True, True, True, False, False]
    report = account.diagnostics()["cloud_hold"]
    assert report["holds_started"] == 1 and report["holds_expired"] == 1
    assert account.grace_expired == 1


async def test_a_confirmed_connection_makes_the_next_outage_a_new_hold():
    account = environment()
    account.run_connection = AsyncMock(side_effect=aiomqtt.MqttCodeError(151))
    clock = Clock()
    seen, sleep = run_with_clock(account, clock, 4)
    with (
        patch(MODULE + ".monotonic", clock),
        patch(MODULE + ".asyncio.sleep", side_effect=sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await account.run()
    assert [shown for _, shown, _ in seen] == [True, True, True, False]
    # The connection comes back: a message reaches the device.
    account.connected = True
    account.generation += 1
    emit(account, "topics/info", {"online": True, "timestamp": 2})
    assert account.available("serial-A")
    account._disconnected(failure="connection")
    assert account.available("serial-A")
    assert account.diagnostics()["cloud_hold"]["holds_started"] == 2
    account._cancel_grace_timer()
