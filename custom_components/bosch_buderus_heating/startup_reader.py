"""Read supplementary PointT data without depending on Home Assistant."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic

from .pointt import (
    AuthenticationError,
    BatchItemResult,
    PointTClient,
    PointTError,
    RateLimited,
    RequestTimeout,
    Resource,
    ServiceUnavailable,
)
from .pointt.redaction import resource_path_template
from .startup_data import LICENSE_PATH, STARTUP_BUDGET_SECONDS, StartupGatewayData

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StartupBatchResult:
    """Completed resource outcomes and any account-wide rate limit."""

    outcomes: tuple[tuple[str, str], ...] = ()
    rate_limit: RateLimited | None = None

    @property
    def attempted(self) -> bool:
        """Whether a batch was sent, rather than skipped before the request."""
        return bool(self.outcomes)


class StartupReader:
    """Own bounded I/O and reply handling; leave HA lifecycle to the caller."""

    def __init__(
        self,
        client: PointTClient,
        gateway_id: str,
        state: StartupGatewayData,
        *,
        roots: tuple[str, ...],
    ) -> None:
        self._client = client
        self._gateway_id = gateway_id
        self._state = state
        self._roots = roots

    async def async_read_batch(
        self,
        known_paths: Collection[str],
        *,
        budget: float = STARTUP_BUDGET_SECONDS,
    ) -> StartupBatchResult:
        """Read one due batch with no immediate retry or fallback GET."""
        paths = self._state.due(monotonic())
        if not paths or budget <= 0:
            return StartupBatchResult()
        try:
            self._client.backoff.raise_if_active()
        except RateLimited:
            return StartupBatchResult()
        self._state.start(paths, datetime.now(UTC))
        try:
            async with asyncio.timeout(budget):
                results = await self._client.get_resources_bulk(
                    self._gateway_id,
                    paths,
                    retryable=False,
                    request_type="startup_metadata",
                )
        except asyncio.CancelledError:
            self._state.fail(paths, "cancelled", monotonic())
            raise
        except AuthenticationError:
            self._state.fail(paths, "authentication_error", monotonic())
            raise
        except (PointTError, TimeoutError) as err:
            if isinstance(err, TimeoutError):
                self._state.budget_expired(monotonic(), in_flight=True)
            category = _failure_category(err, getattr(err, "status", None))
            self._state.fail(paths, category, monotonic())
            _LOGGER.debug("PointT startup batch: %s for %d paths", category, len(paths))
            return StartupBatchResult(
                tuple((path, category) for path in paths),
                err if isinstance(err, RateLimited) else None,
            )
        return self._receive_batch(paths, results, known_paths)

    def _receive_batch(
        self,
        paths: tuple[str, ...],
        results: tuple[BatchItemResult, ...],
        known_paths: Collection[str],
    ) -> StartupBatchResult:
        """Apply only this gateway's requested replies to its pending reads."""
        by_path = {
            item.path: item
            for item in results
            if item.path in paths and item.gateway_id == self._gateway_id
        }
        # A 401 of one item inside an accepted request concerns that path,
        # not the sign-in: the token was just accepted. As in the regular
        # poll it is retried later; only a refused request asks for sign-in.
        outcomes = []
        for path in paths:
            result = by_path.get(path)
            category = self._receive_item(path, result, known_paths)
            outcomes.append((path, category))
            _LOGGER.debug(
                "PointT startup read %s: %s", resource_path_template(path), category
            )
        return StartupBatchResult(
            tuple(outcomes),
            RateLimited(None)
            if any(item.status == 429 for item in by_path.values())
            else None,
        )

    def _receive_item(
        self,
        path: str,
        result: BatchItemResult | None,
        known_paths: Collection[str],
    ) -> str:
        """Validate a reply before caching it or following its references."""
        if result is None:
            category = "missing_result"
        elif result.resource is not None and result.resource.path == path:
            if self._state.receive(result.resource, monotonic(), datetime.now(UTC)):
                self._queue_references(result.resource, known_paths)
                return "success"
            return self._state.reads[path].result
        elif result.resource is not None or (
            result.status is not None and 200 <= result.status < 300
        ):
            category = "invalid_value"
        else:
            category = _failure_category(result.error, result.status)
        self._state.fail((path,), category, monotonic())
        return category

    def _queue_references(
        self, resource: Resource, known_paths: Collection[str]
    ) -> None:
        """Follow unexpected containers within the shared discovery bounds."""
        depth = self._state.reads[resource.path].depth
        for reference in resource.references:
            path = reference.path
            if path == LICENSE_PATH:
                self._state.omit_license()
            elif path not in known_paths and any(
                path == root or path.startswith(f"{root}/") for root in self._roots
            ):
                self._state.schedule(
                    path, depth + 1, advertised=True, referenced_child=True
                )


def _failure_category(
    error: PointTError | TimeoutError | None, status: int | None
) -> str:
    """Keep local metadata errors distinct from temporary service failures."""
    if status in (401, 403, 404, 406):
        return f"http_{status}"
    if status == 429 or isinstance(error, RateLimited):
        return "rate_limited"
    if isinstance(error, TimeoutError | RequestTimeout):
        return "timeout"
    if status in (502, 503, 504) or isinstance(error, ServiceUnavailable):
        return "service_unavailable"
    return "unknown" if error is None else "request_failed"
