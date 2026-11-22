"""Serial reads of a local iCom gateway (HTTP port 80, encrypted answers).

One client belongs to one gateway. Its requests never overlap and keep a gap
between them: these gateways are slow, and a gateway that hangs only recovers
after a power interruption. The client reads only (GET); a failure is reported
as one fixed category and never with the text of an answer, a path or a
password. It decides nothing about pauses: the source does.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from time import monotonic
from typing import Any, Final

import aiohttp
from yarl import URL

from .icom_crypto import IcomCryptoError, decrypt, derive_key, reset_key

ICOM_PORT: Final = 80
# The client name some firmware versions expect.
USER_AGENT: Final = "TeleHeater/2.2.3"
REQUEST_TIMEOUT: Final = 10.0
CONNECT_TIMEOUT: Final = 5.0
# The gap after every request, answered or not.
REQUEST_GAP: Final = 1.0
MAX_BODY_BYTES: Final = 256 * 1024
MAX_RETRY_AFTER: Final = 3600.0
_CHUNK: Final = 16 * 1024


class IcomProblem(StrEnum):
    """Why a read gave no usable document."""

    CONNECT = "connection_failed"
    TIMEOUT = "request_timeout"
    # The answer is no encrypted JSON text of this gateway: a wrong password.
    UNDECRYPTABLE = "undecryptable"
    # No Base64 text, a redirect or an oversized body: not an iCom answer.
    INVALID_RESPONSE = "invalid_response"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    BUSY = "service_unavailable"
    HTTP_ERROR = "http_error"


# Only these end a path without a failure of the gateway as a whole.
PATH_PROBLEMS: Final = frozenset({IcomProblem.NOT_FOUND, IcomProblem.FORBIDDEN})


class IcomError(Exception):
    """A failed read with a fixed category (no answer text)."""

    def __init__(
        self,
        problem: IcomProblem,
        status: int | None = None,
        retry_after: float | None = None,
        *,
        answer: bytes | None = None,
    ) -> None:
        self.problem = problem
        self.status = status
        self.retry_after = retry_after
        # The encrypted answer of an undecryptable read, so that other forms
        # of the private password can be checked offline with it (no further
        # request). Never part of the text of the error.
        self.answer = answer
        super().__init__(problem.value)


@dataclass(frozen=True, slots=True)
class IcomReply:
    """One decrypted answer: its JSON text and the parsed document."""

    body: bytes = field(repr=False)
    document: dict[str, Any] = field(repr=False)


def decode_answer(key: bytes, body: bytes) -> IcomReply:
    """Decrypt one answer of the gateway into its document.

    Raises IcomError: INVALID_RESPONSE for no encrypted text at all,
    UNDECRYPTABLE (with the answer kept) when the key gives no JSON object.
    """
    try:
        text = decrypt(key, body)
    except IcomCryptoError:
        raise IcomError(IcomProblem.INVALID_RESPONSE, 200) from None
    try:
        # A control character or a byte of another encoding inside a text
        # is the gateway's own content, not a sign of a wrong key.
        document = json.loads(text.decode("utf-8", errors="replace"), strict=False)
    except ValueError, RecursionError:
        # A whole number of blocks that is no JSON: the key is wrong.
        raise IcomError(IcomProblem.UNDECRYPTABLE, 200, answer=body) from None
    if not isinstance(document, dict):
        raise IcomError(IcomProblem.UNDECRYPTABLE, 200, answer=body)
    return IcomReply(text, document)


# How the private password fits the gateway (diagnostics; never the password).
EXACT: Final = "exact"
TRIMMED: Final = "trimmed"
RESET: Final = "reset"


@dataclass(frozen=True, slots=True)
class OtherPrivatePassword:
    """A form of the private password that decrypted an answer offline."""

    variant: str
    private_password: str = field(repr=False)
    key: bytes = field(repr=False)
    reply: IcomReply = field(repr=False)


def other_private_password(
    gateway_password: str, private_password: str, answer: bytes
) -> OtherPrivatePassword | None:
    """Try other forms of the private password on an undecryptable answer.

    Offline, with the answer already received: first the password without
    spaces at its start and end (when that differs), then an empty one, the
    state of a gateway whose private password was reset. None when neither
    decrypts the answer.
    """
    trimmed = private_password.strip()
    candidates: list[tuple[str, str, bytes]] = []
    if trimmed and trimmed != private_password:
        candidates.append((TRIMMED, trimmed, derive_key(gateway_password, trimmed)))
    candidates.append((RESET, "", reset_key(gateway_password)))
    for variant, candidate, key in candidates:
        try:
            reply = decode_answer(key, answer)
        except IcomError:
            continue
        return OtherPrivatePassword(variant, candidate, key, reply)
    return None


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    if seconds != seconds or seconds < 0:
        return None
    return min(seconds, MAX_RETRY_AFTER)


@dataclass(slots=True)
class IcomMetrics:
    """Counts only, for the diagnostics."""

    requests: int = 0
    answered: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    last_duration_ms: int | None = None

    def failed(self, problem: IcomProblem) -> None:
        self.failures[problem.value] = self.failures.get(problem.value, 0) + 1


class IcomClient:
    """GET requests of one gateway: serial, spaced, bounded."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        key: bytes,
        *,
        port: int = ICOM_PORT,
        gap: float | None = None,
        timeout: float | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._session = session
        self._host, self._port, self._key = host, port, key
        self._gap = REQUEST_GAP if gap is None else gap
        self._timeout = REQUEST_TIMEOUT if timeout is None else timeout
        self._sleep, self._clock = sleep, clock
        self._lock = asyncio.Lock()
        self._earliest = 0.0
        self.metrics = IcomMetrics()

    def use_key(self, key: bytes) -> None:
        """Read on with another key (one proved on an answer of this gateway)."""
        self._key = key

    def _url(self, path: str) -> URL:
        return URL.build(
            scheme="http",
            host=self._host.strip("[]"),
            port=self._port,
            path=path,
            encoded=False,
        )

    async def get(self, path: str) -> IcomReply:
        """Read one resource. Raises IcomError; never retries."""
        if not path.startswith("/") or "?" in path or "#" in path:
            raise ValueError("invalid_icom_path")
        async with self._lock:
            wait = self._earliest - self._clock()
            if wait > 0:
                await self._sleep(wait)
            started = self._clock()
            self.metrics.requests += 1
            try:
                return await self._read(path)
            except IcomError as error:
                self.metrics.failed(error.problem)
                raise
            finally:
                self._earliest = self._clock() + self._gap
                self.metrics.last_duration_ms = round((self._clock() - started) * 1000)

    async def _read(self, path: str) -> IcomReply:
        try:
            url = self._url(path)
        except ValueError:
            raise IcomError(IcomProblem.CONNECT) from None
        try:
            async with self._session.get(
                url,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                allow_redirects=False,
                # The gateway's small web server is not asked for compression.
                skip_auto_headers=("Accept-Encoding",),
                timeout=aiohttp.ClientTimeout(
                    total=self._timeout, sock_connect=CONNECT_TIMEOUT
                ),
            ) as response:
                status = response.status
                if status != 200:
                    delay = (
                        _retry_after(response.headers.get("Retry-After"))
                        if status in (429, 503)
                        else None
                    )
                    response.close()
                    raise IcomError(_status_problem(status), status, delay)
                body = await _bounded(response)
        except IcomError:
            raise
        except TimeoutError:
            raise IcomError(IcomProblem.TIMEOUT) from None
        except aiohttp.ClientError, OSError:
            raise IcomError(IcomProblem.CONNECT) from None
        return self._decoded(body)

    def _decoded(self, body: bytes) -> IcomReply:
        reply = decode_answer(self._key, body)
        self.metrics.answered += 1
        return reply


def _status_problem(status: int) -> IcomProblem:
    if status in (404, 405, 406):
        return IcomProblem.NOT_FOUND
    if status in (401, 403):
        return IcomProblem.FORBIDDEN
    if status in (429, 503):
        return IcomProblem.BUSY
    if 300 <= status < 400:
        return IcomProblem.INVALID_RESPONSE
    return IcomProblem.HTTP_ERROR


async def _bounded(response: aiohttp.ClientResponse) -> bytes:
    """The body, read in parts; one over the limit is no answer."""
    declared = response.content_length
    if declared is not None and declared > MAX_BODY_BYTES:
        response.close()
        raise IcomError(IcomProblem.INVALID_RESPONSE, 200)
    parts: list[bytes] = []
    size = 0
    async for chunk in response.content.iter_chunked(_CHUNK):
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            response.close()
            raise IcomError(IcomProblem.INVALID_RESPONSE, 200)
        parts.append(chunk)
    return b"".join(parts)
