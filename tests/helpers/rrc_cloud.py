"""A fake PointT cloud for RRC 2.0 room controllers (W0, shared by W1 to W6).

It answers single and bulk reads from a resource tree, takes PUT, structured
PUT, POST and DELETE requests and query reads, and counts every request per
method and path. Failure modes are set per path:

- ``adopt_after[path] = n``: a write shows only from the n-th read after it
  (the controller applies it late; 0 shows it at once).
- ``ignore_writes``: the write answers 204 and changes nothing.
- ``timeout_after_write[path] = applied``: the write times out after it was
  (or was not) applied.
- ``rate_limit[path] = retry_after``: every request touching the path
  answers 429 with that ``Retry-After`` (``None`` without one);
  ``rate_limit_writes`` the same for its writes only.
- ``forbidden``: the path answers 403, for reads and writes.
- a missing path answers 404 for a root and 500 otherwise, as these
  controllers do (``missing_as_500``).
- ``down[path]``: an HTTP status (a path error) or an error instance (such
  as ``ServiceUnavailable(503)`` for a transport failure) for its reads.

``cloud_patches`` routes ``PointTClient`` to the fake for a real config-entry
setup. Values and names in the fixtures are synthetic.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from custom_components.bosch_buderus_heating.discovery import ROOT_RESOURCE_PATHS
from custom_components.bosch_buderus_heating.discovery_inventory import (
    resource_from_data,
)
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Gateway,
    PointTClient,
    PointTError,
    RateLimited,
    RequestTimeout,
    Resource,
    ResourceError,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from tests.helpers.open_patches import TrackedStack

FIXTURES = Path(__file__).parents[1] / "fixtures" / "contracts"
COORDINATOR = "custom_components.bosch_buderus_heating.coordinator"
PACKAGE = "custom_components.bosch_buderus_heating"
GATEWAY = Gateway("gateway-one")


class Clock:
    """A monotonic clock the test moves on."""

    def __init__(self, now: float = 10_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def load_tree(relative: str) -> dict[str, Resource]:
    """A fixture below ``tests/fixtures/contracts``: a list of wire resources
    (``{"resources": [...]}``) or a stored inventory (``{"resources": {...}}``)."""
    data = json.loads((FIXTURES / relative).read_text(encoding="utf-8"))
    items = data["resources"]
    if isinstance(items, dict):
        return {path: resource_from_data(value) for path, value in items.items()}
    return {item["id"]: parse_resource(item) for item in items}


def with_containers(tree: Mapping[str, Resource]) -> dict[str, Resource]:
    """Add a reference container for every missing ancestor, so that the
    discovery reaches the leaves of a flat fixture from the roots."""
    result = dict(tree)
    children: dict[str, set[str]] = {}
    for path in tree:
        parts = path.strip("/").split("/")
        for depth in range(1, len(parts)):
            parent = "/" + "/".join(parts[:depth])
            child = "/" + "/".join(parts[: depth + 1])
            children.setdefault(parent, set()).add(child)
    for parent, kids in children.items():
        if parent not in result:
            result[parent] = parse_resource(
                {
                    "id": parent,
                    "type": "refEnum",
                    "references": [{"id": kid} for kid in sorted(kids)],
                }
            )
    return result


@dataclass(slots=True)
class _Pending:
    value: Any
    reads_left: int


class RrcCloud:
    """Answers like an RRC controller behind the PointT cloud."""

    def __init__(
        self,
        resources: Mapping[str, Resource],
        *,
        forbidden: set[str] | None = None,
        missing_as_500: bool = True,
    ) -> None:
        self.tree: dict[str, Resource] = dict(resources)
        self.down: dict[str, int | PointTError] = {}
        self.bulk_error: PointTError | None = None
        self.request_errors: dict[str, PointTError] = {}
        self.gateway_errors: dict[str, PointTError] = {}
        self.get_errors: dict[str, PointTError] = {}
        self.forbidden: set[str] = (
            {"/devices/inclusionWhitelist"} if forbidden is None else forbidden
        )
        self.missing_as_500 = missing_as_500
        # Bulk path tuples and single reads, in order.
        self.requests: list[tuple[str, ...]] = []
        self.single: list[str] = []
        # Every request: (method, path) -> count; GET? marks a query read.
        self.calls: Counter[tuple[str, str]] = Counter()
        # Every write: (method, path, value).
        self.writes: list[tuple[str, str, Any]] = []
        self.adopt_after: dict[str, int] = {}
        self.ignore_writes: set[str] = set()
        self.timeout_after_write: dict[str, bool] = {}
        self.rate_limit: dict[str, float | None] = {}
        self.rate_limit_writes: dict[str, float | None] = {}
        # Query reads: (path, sorted query items as text) -> resource.
        self.queries: dict[tuple[str, tuple[tuple[str, str], ...]], Resource] = {}
        # POST and DELETE behaviour per path; by default a POST is recorded
        # and a DELETE removes the path.
        self.post_handlers: dict[str, Callable[[Any], None]] = {}
        self.delete_handlers: dict[str, Callable[[], None]] = {}
        self._pending: dict[str, _Pending] = {}

    # ------------------------------------------------------------- helpers
    def requested(self, path: str) -> int:
        """How often a path was read (bulk and single)."""
        return sum(path in paths for paths in self.requests) + self.single.count(path)

    def all_requested(self) -> set[str]:
        return {path for paths in self.requests for path in paths} | set(self.single)

    def written(self, method: str | None = None) -> list[tuple[str, str, Any]]:
        return [item for item in self.writes if method in (None, item[0])]

    def _missing(self, path: str) -> int:
        if path in self.forbidden:
            return 403
        if path in ROOT_RESOURCE_PATHS or not self.missing_as_500:
            return 404
        return 500

    def _check_rate_limit(self, path: str) -> None:
        if path in self.rate_limit:
            raise RateLimited(retry_after=self.rate_limit[path])

    def _read(self, path: str) -> Resource | None:
        """The current resource; a pending write may show with this read."""
        pending = self._pending.get(path)
        if pending is not None:
            pending.reads_left -= 1
            if pending.reads_left <= 0:
                self._apply(path, pending.value)
                del self._pending[path]
        return self.tree.get(path)

    def _apply(self, path: str, value: Any) -> None:
        current = self.tree.get(path)
        if current is not None:
            self.tree[path] = replace(current, value=value, has_value=True)

    # --------------------------------------------------------------- reads
    async def get_resources_bulk(self, gateway_id, paths, **_kwargs):
        self.requests.append(tuple(paths))
        for path in paths:
            self.calls["BULK", path] += 1
        if self.bulk_error is not None:
            raise self.bulk_error
        if gateway_id in self.gateway_errors:
            raise self.gateway_errors[gateway_id]
        for path in paths:
            if path in self.request_errors:
                raise self.request_errors[path]
        results = []
        for path in paths:
            failure = self.down.get(path)
            if path in self.rate_limit:
                error: PointTError = RateLimited(retry_after=self.rate_limit[path])
                results.append(BatchItemResult(gateway_id, path, 429, error=error))
            elif isinstance(failure, int):
                error = (
                    RateLimited(retry_after=None)
                    if failure == 429
                    else ResourceError(path, failure)
                )
                results.append(BatchItemResult(gateway_id, path, failure, error=error))
            elif failure is not None:
                results.append(BatchItemResult(gateway_id, path, None, error=failure))
            elif (resource := self._read(path)) is not None:
                results.append(
                    BatchItemResult(gateway_id, path, 200, resource=resource)
                )
            else:
                status = self._missing(path)
                results.append(
                    BatchItemResult(
                        gateway_id, path, status, error=ResourceError(path, status)
                    )
                )
        return tuple(results)

    async def get_resource(self, gateway_id, path, **_kwargs):
        self.single.append(path)
        self.calls["GET", path] += 1
        if self.bulk_error is not None:
            raise self.bulk_error
        if gateway_id in self.gateway_errors:
            raise self.gateway_errors[gateway_id]
        if path in self.get_errors:
            raise self.get_errors[path]
        self._check_rate_limit(path)
        failure = self.down.get(path)
        if isinstance(failure, int):
            # As the shared discovery fake: a status of the path itself; put
            # a ServiceUnavailable into ``down`` for a transport failure.
            raise ResourceError(path, failure)
        if failure is not None:
            raise failure
        resource = self._read(path)
        if resource is None:
            status = self._missing(path)
            if status >= 500:
                raise ServiceUnavailable(status)
            raise ResourceError(path, status)
        return resource

    async def get_resource_query(self, gateway_id, path, query, **_kwargs):
        self.calls["GET?", path] += 1
        self._check_rate_limit(path)
        key = (path, tuple(sorted((name, str(value)) for name, value in query.items())))
        if key not in self.queries:
            raise ResourceError(path, 404)
        return self.queries[key]

    # -------------------------------------------------------------- writes
    def _write(self, method: str, path: str, value: Any) -> None:
        self.calls[method, path] += 1
        self.writes.append((method, path, value))
        self._check_rate_limit(path)
        if path in self.rate_limit_writes:
            raise RateLimited(retry_after=self.rate_limit_writes[path])
        if path in self.forbidden:
            raise ResourceError(path, 403)

    def _settle(self, path: str, value: Any) -> None:
        """Apply a write now or after ``adopt_after`` reads."""
        if path in self.ignore_writes:
            return
        reads = self.adopt_after.get(path, 0)
        if reads <= 0:
            self._apply(path, value)
        else:
            self._pending[path] = _Pending(value, reads)

    async def put_resource_value(self, gateway_id, path, value, **_kwargs):
        self._write("PUT", path, value)
        if path in self.timeout_after_write:
            if self.timeout_after_write[path]:
                self._settle(path, value)
            raise RequestTimeout("PointT request timed out")
        self._settle(path, value)
        return None

    async def put_resource_json(self, gateway_id, path, value, **_kwargs):
        if not isinstance(value, list | dict):
            raise ValueError("A structured PointT value is a list or an object")
        return await self.put_resource_value(gateway_id, path, value)

    async def post_resource(self, gateway_id, path, value, **_kwargs):
        self._write("POST", path, value)
        applied = self.timeout_after_write.get(path, True)
        handler = self.post_handlers.get(path)
        if applied and path not in self.ignore_writes and handler is not None:
            handler(value)
        if path in self.timeout_after_write:
            raise RequestTimeout("PointT request timed out")
        return None

    async def delete_resource(self, gateway_id, path, **_kwargs):
        self._write("DELETE", path, None)
        applied = self.timeout_after_write.get(path, True)
        if applied and path not in self.ignore_writes:
            if (handler := self.delete_handlers.get(path)) is not None:
                handler()
            else:
                self.tree.pop(path, None)
        if path in self.timeout_after_write:
            raise RequestTimeout("PointT request timed out")


def cloud_patches(
    cloud: RrcCloud, clock: Clock, gateway: Gateway = GATEWAY
) -> ExitStack:
    """Route the PointT client of a real setup to ``cloud``.

    The patches are active from here on, also without ``with``. A test that
    fails before its own teardown leaves them to the teardown check in
    ``tests/conftest.py``, which closes them.
    """
    stack = TrackedStack()
    for item in (
        patch(f"{COORDINATOR}.monotonic", clock),
        patch.object(PointTClient, "get_gateways", AsyncMock(return_value=(gateway,))),
        patch.object(
            PointTClient,
            "get_resources_bulk",
            AsyncMock(side_effect=cloud.get_resources_bulk),
        ),
        patch.object(
            PointTClient, "get_resource", AsyncMock(side_effect=cloud.get_resource)
        ),
        patch.object(
            PointTClient,
            "put_resource_value",
            AsyncMock(side_effect=cloud.put_resource_value),
        ),
        # create=True: also usable against a tree without these methods.
        patch.object(
            PointTClient,
            "put_resource_json",
            AsyncMock(side_effect=cloud.put_resource_json),
            create=True,
        ),
        patch.object(
            PointTClient,
            "post_resource",
            AsyncMock(side_effect=cloud.post_resource),
            create=True,
        ),
        patch.object(
            PointTClient,
            "delete_resource",
            AsyncMock(side_effect=cloud.delete_resource),
            create=True,
        ),
        patch.object(
            PointTClient,
            "get_resource_query",
            AsyncMock(side_effect=cloud.get_resource_query),
            create=True,
        ),
        # F1: the firmware update list names no gateway unless a test says so.
        patch.object(
            PointTClient,
            "get_firmware_updates",
            AsyncMock(return_value=None),
            create=True,
        ),
        patch(f"{PACKAGE}.async_setup_notifications", AsyncMock(return_value=())),
    ):
        stack.enter_context(item)
    return stack
