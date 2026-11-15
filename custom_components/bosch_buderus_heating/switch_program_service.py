"""Z2: one write transaction for a PointT weekly switch program.

Fresh read of the program and, on a heating circuit that reports it, of its
program type, comparison with the program shown, exactly one PUT of the whole
week (a raw JSON array; for the WDDW2 hot water program the object
``{"switchPoints": [...]}``), read-back of the whole week. The PUT is never
repeated: a lost answer or a server error after sending is resolved only by
reading (``rrc_write.RrcStructWrite``, the same rules as the structured room
controller writes). The coordinator runs it under its polling/write lock and
its overall deadline (D25).

Pure: no Home Assistant import.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from time import monotonic

from .pointt import (
    AuthenticationError,
    PointTClient,
    PointTError,
    ProtocolError,
    RateLimited,
    Resource,
)
from .pointt.exceptions import SourceStopped
from .rrc_write import RrcStructWrite
from .switch_program_edit import (
    EditableProgram,
    editable_program,
    encode_body,
    fingerprint,
    program_mode_issue,
    program_mode_path,
    same_points,
    week_issue,
)
from .switch_programs import SwitchPoint, assess_switch_program
from .write_policies import ContractRejected
from .writes import ControlUnused

DEFAULT_DELAYS = (0.5, 1.0, 2.0)

# TP-06: builds the new week from a fresh reading (the program, the gateway's
# readings, the fresh program type); ``None`` sends nothing.
type SwitchProgramPlan = Callable[
    [Resource, Mapping[str, Resource], Resource | None],
    tuple[SwitchPoint, ...] | None,
]


class SwitchProgramChanged(ContractRejected):
    """The program on the gateway is no longer the one shown; nothing sent."""

    def __init__(self) -> None:
        super().__init__("program_changed")


def program_shows(
    path: str, points: Sequence[SwitchPoint]
) -> Callable[[Resource], bool]:
    """Whether a reading of ``path`` holds exactly these points (any order)."""
    desired = tuple(points)

    def shows(resource: Resource) -> bool:
        if resource.path != path:
            return False
        assessment = assess_switch_program(resource)
        return assessment.supported and same_points(assessment.points, desired)

    return shows


class SwitchProgramService:
    """Fresh preflight, one PUT, bounded read-back; no guessed limits."""

    def __init__(
        self,
        client: PointTClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.client = client
        self.sleep = sleep
        self._clock = clock
        # Diagnostics: how long the last confirmed read-back took (seconds).
        self.last_read_back_seconds: float | None = None

    async def async_write(
        self,
        gateway_id: str,
        expected: Resource,
        points: Sequence[SwitchPoint],
        resources: Mapping[str, Resource],
        *,
        program_mode: Resource | None = None,
        delays: tuple[float, ...] = DEFAULT_DELAYS,
        on_sent: Callable[[], None] | None = None,
    ) -> Resource:
        """Write the week ``points`` over the program ``expected`` was read as.

        ``resources`` are the gateway's readings for the level rules;
        ``program_mode`` the current ``switchProgramMode`` of a heating
        circuit, if reported. Returns the confirmed reading; a refusal raises
        a ``WriteValidationError`` before anything is sent.
        """
        if expected.metadata.used is False:
            raise ControlUnused("The gateway does not use this program now")
        shown = editable_program(expected, resources, program_mode=program_mode)
        if shown is None:
            raise ContractRejected("program_not_editable")
        if (reason := week_issue(points, shown)) is not None:
            raise ContractRejected(f"program_{reason}")
        desired = tuple(points)
        path = expected.path
        shows = program_shows(path, desired)
        mode_path = program_mode_path(path)
        reads_mode = mode_path is not None and (
            shown.value_type == "absolute"
            or program_mode is not None
            or mode_path in resources
        )

        async def preflight() -> Resource:
            current = await self.client.get_resource(gateway_id, path)
            if (
                current.path != path
                or fingerprint(current) is None
                or fingerprint(current) != fingerprint(expected)
            ):
                raise SwitchProgramChanged
            if current.metadata.used is False:
                raise ControlUnused("The gateway does not use this program now")
            mode = program_mode
            if reads_mode and mode_path is not None:
                # The program type is read again with the program; an
                # absolute program or one whose type cannot be read is never
                # written as a level program.
                mode = await self.read_mode(gateway_id, mode_path)
                issue = program_mode_issue(path, mode, shown.value_type)
                if issue is not None:
                    raise ContractRejected(
                        "program_mode_unknown"
                        if issue == "program_mode_unknown"
                        else "program_mode_not_absolute"
                        if shown.value_type == "absolute"
                        else "program_mode_not_level"
                    )
            live: EditableProgram | None = editable_program(
                current, resources, program_mode=mode
            )
            if (
                live is None
                or live.body != shown.body
                or live.value_type != shown.value_type
                or week_issue(desired, live) is not None
            ):
                raise SwitchProgramChanged
            return current

        current = await preflight()
        if shows(current):
            # Nothing to change: no request.
            return current
        started: list[float] = []

        async def checked() -> Resource:
            return current

        async def send(_current: Resource) -> None:
            started.append(self._clock())
            body = encode_body(desired, shown)
            try:
                if isinstance(body, dict):
                    # Only the WDDW2 program is written as an object.
                    await self.client.put_resource_object(gateway_id, path, body)
                else:
                    await self.client.put_resource_raw_json(gateway_id, path, body)
            except ProtocolError:
                # The gateway accepted the request (2xx) but its answer could
                # not be read (not JSON, too large, an unexpected envelope):
                # only reading tells whether the week was stored.
                return

        async def confirm(_current: Resource) -> Resource | None:
            reading = await self.client.get_resource(gateway_id, path)
            return reading if shows(reading) else None

        writer = RrcStructWrite(sleep=self.sleep, delays=delays, clock=self._clock)
        confirmed = await writer.async_write(
            preflight=checked, send=send, confirm=confirm, on_sent=on_sent
        )
        if started:
            self.last_read_back_seconds = round(self._clock() - started[0], 1)
        return confirmed

    async def read_mode(self, gateway_id: str, mode_path: str) -> Resource | None:
        """The current program type; ``None`` if it cannot be read now."""
        try:
            return await self.client.get_resource(gateway_id, mode_path)
        except AuthenticationError, RateLimited, SourceStopped:
            raise
        except PointTError:
            return None
