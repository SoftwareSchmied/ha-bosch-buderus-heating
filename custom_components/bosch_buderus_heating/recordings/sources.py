"""One read of a recording, from the local data port or from the cloud.

Local: the ``sampleRate`` form (``all`` once, then ``PT1H`` for yesterday
and today); the ``interval`` form is not part of the local contract. Each
read goes through the running local source, its limits and identity
checks, one after another and within ``READ_SECONDS``.

Cloud: the ``interval`` form through the coordinator's client, with the
integration's pacing, sign-in and pause rules; a rate limit pauses the
whole cloud access (the transport does that).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any, Final, Protocol

from ..local_api.catalog import make_request
from ..local_api.errors import LocalResourceError
from ..local_api.models import Recording
from ..local_read import ReadProblem, ReadResult, ReadState
from ..pointt import (
    PointTError,
    RateLimited,
    ResourceForbidden,
    ResourceNotFound,
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
        request = make_request(series.path, query=query)
    except LocalResourceError:
        return Answer(Outcome.FAILED)
    try:
        async with asyncio.timeout(READ_SECONDS):
            result = await driver.read_explicit(request)
    except TimeoutError:
        return Answer(Outcome.UNREACHABLE)
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
    except (PointTError, ValueError) as err:
        _LOGGER.debug(
            "A recording could not be read from the cloud: %s", type(err).__name__
        )
        return Answer(Outcome.FAILED)
    return Answer(Outcome.OK, payload)
