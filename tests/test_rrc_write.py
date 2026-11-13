"""W0: structured RRC writes and the RRC write counters (S5 to S7, S12, C36).

``RrcStructWrite`` sends exactly one request, resolves an unknown outcome
only by reading, and blocks a second creation while the first is open.
"""

from __future__ import annotations

import aiohttp
import pytest

from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    RateLimited,
    RequestTimeout,
    ResourceError,
    ServiceUnavailable,
    UnexpectedHttpStatus,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.rrc_write import (
    PENDING_EXPIRY_SECONDS,
    RrcStructWrite,
    RrcWritePending,
    RrcWriteStats,
    never_sent,
)
from custom_components.bosch_buderus_heating.writes import RrcWriteNotConfirmed


class Controller:
    """A list the controller shows an added entry in after some reads."""

    def __init__(self, shows_after: int = 1) -> None:
        self.entries: list[str] = []
        self.sent: list[str] = []
        self.shows_after = shows_after
        self.reads = 0
        self.pending: str | None = None
        self.send_error: Exception | None = None
        self.applies_on_error = True
        self.read_errors: list[Exception] = []

    async def preflight(self) -> tuple[str, ...]:
        return tuple(self.entries)

    def sender(self, name: str):
        async def send(state: tuple[str, ...]) -> None:
            self.sent.append(name)
            if self.send_error is None or self.applies_on_error:
                self.pending = name
                self.reads = 0
            if self.send_error is not None:
                raise self.send_error

        return send

    def confirmer(self, name: str):
        async def confirm(state: tuple[str, ...]) -> tuple[str, ...] | None:
            if self.read_errors:
                raise self.read_errors.pop(0)
            if self.pending is not None:
                self.reads += 1
                if self.reads >= self.shows_after:
                    self.entries.append(self.pending)
                    self.pending = None
            return tuple(self.entries) if name in self.entries else None

        return confirm


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def writer(sleeps: list[float], clock: Clock | None = None) -> RrcStructWrite:
    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    return RrcStructWrite(sleep=sleep, clock=clock or Clock())


async def run(
    struct: RrcStructWrite, controller: Controller, name: str, *, guard="events"
):
    return await struct.async_write(
        preflight=controller.preflight,
        send=controller.sender(name),
        confirm=controller.confirmer(name),
        guard=guard,
        pending_reason="rrc_event_pending",
    )


async def test_confirmed_after_the_third_read_with_one_send() -> None:
    controller = Controller(shows_after=3)
    sleeps: list[float] = []
    sent: list[int] = []
    struct = writer(sleeps)
    result = await struct.async_write(
        preflight=controller.preflight,
        send=controller.sender("away"),
        confirm=controller.confirmer("away"),
        on_sent=lambda: sent.append(1),
    )
    assert result == ("away",)
    assert controller.sent == ["away"] and sent == [1]
    assert sleeps == [1.0, 2.0, 4.0]


async def test_not_confirmed_after_four_reads_and_the_guard_blocks() -> None:
    controller = Controller(shows_after=6)
    sleeps: list[float] = []
    clock = Clock()
    struct = writer(sleeps, clock)
    with pytest.raises(RrcWriteNotConfirmed):
        await run(struct, controller, "away")
    assert controller.sent == ["away"] and sleeps == [1.0, 2.0, 4.0, 8.0]
    assert struct.pending("events")
    # C36: a second creation is refused while the first is unresolved.
    with pytest.raises(RrcWritePending) as pending:
        await run(struct, controller, "other")
    assert pending.value.translation_key == "rrc_event_pending"
    assert controller.sent == ["away"]
    # The first one shows now (sixth read, of the first write's own entry):
    # the guard is released and the new one runs.
    controller.shows_after = 1
    result = await run(struct, controller, "other")
    assert result == ("away", "other")
    assert controller.sent == ["away", "other"]
    assert not struct.pending("events")


async def test_the_guard_expires() -> None:
    controller = Controller(shows_after=100)
    clock = Clock()
    struct = writer([], clock)
    with pytest.raises(RrcWriteNotConfirmed):
        await run(struct, controller, "away")
    clock.now += PENDING_EXPIRY_SECONDS
    controller.shows_after = 1
    assert not struct.pending("events")
    assert await run(struct, controller, "again") == ("again",)
    assert controller.sent == ["away", "again"]


async def test_timeout_after_sending_is_resolved_by_reading_only() -> None:
    controller = Controller(shows_after=2)
    controller.send_error = RequestTimeout("lost answer")
    sleeps: list[float] = []
    struct = writer(sleeps)
    assert await run(struct, controller, "away") == ("away",)
    assert controller.sent == ["away"] and sleeps == [1.0, 2.0]
    assert not struct.pending("events")


async def test_a_temporary_read_failure_keeps_reading() -> None:
    controller = Controller(shows_after=1)
    controller.read_errors = [ServiceUnavailable(503)]
    sleeps: list[float] = []
    assert await run(writer(sleeps), controller, "away") == ("away",)
    assert sleeps == [1.0, 2.0]
    controller = Controller(shows_after=10)
    controller.read_errors = [RequestTimeout("x")] * 4
    with pytest.raises(RrcWriteNotConfirmed) as unconfirmed:
        await run(writer([]), controller, "away")
    assert isinstance(unconfirmed.value.__cause__, RequestTimeout)


async def test_a_failed_read_back_leaves_the_write_unconfirmed() -> None:
    controller = Controller()
    controller.read_errors = [RateLimited(retry_after=60)]
    struct = writer([])
    with pytest.raises(RrcWriteNotConfirmed) as unconfirmed:
        await run(struct, controller, "away")
    assert isinstance(unconfirmed.value.__cause__, RateLimited)
    assert struct.pending("events")  # the outcome is still unknown


@pytest.mark.parametrize(
    "error",
    [
        RateLimited(retry_after=60),
        UnexpectedHttpStatus(400),
        AuthenticationError("signed out"),
        ResourceError("/events", 403),
    ],
)
async def test_a_refused_send_releases_the_guard(error: Exception) -> None:
    controller = Controller()
    controller.send_error = error
    controller.applies_on_error = False
    struct = writer([])
    with pytest.raises(type(error)):
        await run(struct, controller, "away")
    assert controller.sent == ["away"]
    assert not struct.pending("events")


async def test_a_connection_that_never_opened_sent_nothing() -> None:
    error = ServiceUnavailable()
    error.__cause__ = aiohttp.ClientConnectorError(
        connection_key=None,  # type: ignore[arg-type]
        os_error=OSError("refused"),
    )
    assert never_sent(error)
    controller = Controller()
    controller.send_error = error
    controller.applies_on_error = False
    struct = writer([])
    with pytest.raises(ServiceUnavailable):
        await run(struct, controller, "away")
    assert not struct.pending("events")
    assert not never_sent(ServiceUnavailable(503))


async def test_a_refused_preflight_sends_nothing() -> None:
    controller = Controller()

    async def refuse() -> tuple[str, ...]:
        raise WriteValidationError("overlap")

    struct = writer([])
    with pytest.raises(WriteValidationError):
        await struct.async_write(
            preflight=refuse,
            send=controller.sender("away"),
            confirm=controller.confirmer("away"),
            guard="events",
        )
    assert controller.sent == [] and not struct.pending("events")


def test_invalid_schedules_are_refused() -> None:
    with pytest.raises(ValueError):
        RrcStructWrite(delays=())
    with pytest.raises(ValueError):
        RrcStructWrite(delays=(1.0, -1.0))


def _reading(value: float):
    return parse_resource(
        {
            "id": "/zones/zn1/manualTemperatureHeating",
            "type": "floatValue",
            "value": value,
        }
    )


def test_counters_and_late_confirmations() -> None:
    stats = RrcWriteStats(window=900)
    stats.sent("zone")
    stats.unconfirmed(
        "zone",
        "/zones/zn1/manualTemperatureHeating",
        lambda resource: resource.value == 21.5,
        100.0,
    )
    assert stats.awaiting() == 1
    # A reading that does not show it yet keeps waiting.
    stats.observe(_reading(20.0), 200.0)
    assert stats.snapshot("zone")["late_confirmed"] == 0
    stats.observe(_reading(21.5), 300.0)
    stats.observe(_reading(21.5), 400.0)
    snapshot = stats.snapshot("zone")
    assert snapshot["late_confirmed"] == 1
    assert snapshot["writes_sent"] == 1 and snapshot["writes_unconfirmed"] == 1
    assert snapshot["awaiting_late_confirmation"] == 0
    # Too late: the window ended.
    stats.unconfirmed(
        "zone",
        "/zones/zn1/manualTemperatureHeating",
        lambda resource: True,
        1000.0,
    )
    stats.observe(_reading(21.5), 1901.0)
    assert stats.snapshot("zone")["late_confirmed"] == 1
    assert stats.awaiting() == 0
    # A confirmed write ends a wait; rejections count with their reason.
    stats.unconfirmed("core", "/zones/zn1/userMode", lambda resource: True, 0.0)
    stats.confirmed("core", "/zones/zn1/userMode")
    stats.rejected("core", "locked")
    stats.rejected("core", "locked")
    stats.unconfirmed("core", "/x", None, 0.0)
    core = stats.snapshot("core")
    assert core["writes_confirmed"] == 1 and core["writes_rejected"] == 2
    assert core["rejection_reasons"] == {"locked": 2}
    assert core["awaiting_late_confirmation"] == 0
    assert stats.snapshot("unused") == {
        "writes_sent": 0,
        "writes_confirmed": 0,
        "writes_unconfirmed": 0,
        "late_confirmed": 0,
        "writes_rejected": 0,
        "awaiting_late_confirmation": 0,
        "rejection_reasons": {},
    }
