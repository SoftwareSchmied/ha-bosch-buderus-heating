"""One read of a recording, from the local data port or from the cloud.

Local: the ``sampleRate`` form (``all`` once, then ``PT1H`` for yesterday
and today); the ``interval`` form is not part of the local contract. Each
read goes through the running local source, its limits and identity
checks, one after another and within ``READ_SECONDS``.

Cloud: the ``interval`` form through the coordinator's client, with the
integration's pacing, sign-in and pause rules; a rate limit pauses the
whole cloud access (the transport does that). The bulk form
(``query=all&sampleRate=all``) was refused with 403 for
a real account, as a resource read and inside a bulk read, so it is not
used.

Outcomes: an answer about the path (404, 403, a failed or unusable answer)
concerns that path only; ``UNREACHABLE`` (no connection, sign-in, a rate
limit of the local access), ``TIMEOUT`` and ``SERVER_ERROR`` (5xx of the
local data port, which pauses the local access) end the run.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any, Final, Protocol

from ..local_api.catalog import make_request, request_for_path
from ..local_api.errors import LocalResourceError
from ..local_api.models import Recording
from ..local_read import ReadProblem, ReadResult, ReadState
from ..pointt import (
    PointTError,
    RateLimited,
    ResourceForbidden,
    ResourceNotFound,
)
from ..pointt.exceptions import (
    AuthenticationError,
    RefreshRefused,
    ResourceError,
    ServiceUnavailable,
    SignInUnavailable,
    SourceStopped,
    TransportError,
    UnexpectedHttpStatus,
)
from .series import Series

_LOGGER = logging.getLogger(__name__)
READ_SECONDS: Final = 30.0


class Outcome(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    BUSY = "busy"
    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"
    SERVER_ERROR = "server_error"
    RATE_LIMITED = "rate_limited"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Answer:
    outcome: Outcome
    data: Any = None


class LocalDriver(Protocol):
    async def read_explicit(self, request: Any) -> ReadResult | None: ...


def local_query(full: bool, today: date | None = None) -> dict[str, str]:
    """``all`` for the whole history, else the hours since the day before
    yesterday: every hour of the last 48 hours is read again."""
    if full or today is None:
        return {"sampleRate": "all"}
    first = date.fromordinal(today.toordinal() - 2)
    return {
        "sampleRate": "PT1H",
        "startDate": first.isoformat(),
        "endDate": today.isoformat(),
    }


async def read_local(
    driver: LocalDriver, series: Series, query: dict[str, str]
) -> Answer:
    try:
        request = (
            # A heat source's path fills the placeholder of its template.
            request_for_path(series.path, query=query)
            if series.heat_source is not None
            else make_request(series.path, query=query)
        )
    except LocalResourceError:
        return Answer(Outcome.FAILED)
    try:
        async with asyncio.timeout(READ_SECONDS):
            result = await driver.read_explicit(request)
    except TimeoutError:
        return Answer(Outcome.TIMEOUT)
    if result is None:
        return Answer(Outcome.UNREACHABLE)
    if result.state is ReadState.SUCCESS:
        observation = result.observation
        data = observation.data if observation is not None else None
        if isinstance(data, Recording):
            return Answer(Outcome.OK, data)
        return Answer(Outcome.FAILED)
    if result.state is ReadState.DEFERRED:
        return Answer(Outcome.BUSY)
    if result.state is ReadState.PATH_ERROR:
        if result.problem == ReadProblem.NOT_FOUND:
            return Answer(Outcome.NOT_FOUND)
        if result.problem == ReadProblem.FORBIDDEN:
            return Answer(Outcome.FORBIDDEN)
        return Answer(Outcome.FAILED)
    if result.problem == ReadProblem.SERVICE_UNAVAILABLE:
        return Answer(Outcome.SERVER_ERROR)
    return Answer(Outcome.UNREACHABLE)


async def read_cloud(
    client: Any, gateway_id: str, series: Series, interval: str
) -> Answer:
    try:
        payload = await client.get_recording(gateway_id, series.path, interval)
    except ResourceNotFound:
        return Answer(Outcome.NOT_FOUND)
    except ResourceForbidden:
        return Answer(Outcome.FORBIDDEN)
    except RateLimited:
        return Answer(Outcome.RATE_LIMITED)
    except (
        SignInUnavailable,
        RefreshRefused,
        AuthenticationError,
        SourceStopped,
    ) as err:
        _log_cloud(err)
        return Answer(Outcome.UNREACHABLE)
    except (ServiceUnavailable, UnexpectedHttpStatus, ResourceError) as err:
        # A server error or an unexpected status may concern this path only.
        _log_cloud(err)
        return Answer(Outcome.FAILED)
    except TransportError as err:  # no connection, a timeout
        _log_cloud(err)
        return Answer(Outcome.UNREACHABLE)
    except (PointTError, ValueError) as err:  # an unreadable answer
        _log_cloud(err)
        return Answer(Outcome.FAILED)
    return Answer(Outcome.OK, payload)


def _log_cloud(err: BaseException) -> None:
    _LOGGER.debug(
        "A recording could not be read from the cloud: %s", type(err).__name__
    )
