"""Private bounded checkpoints without measurement values or credentials."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Callable
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.json import save_json

from ..local_api.catalog import (
    ReadRequest,
    request_for_path,
    response_schema,
    validate_path,
)
from ..local_api.errors import LocalResourceError, Problem
from ..local_api.json import JsonLimits, load_json
from .inventory import ResourceRecord
from .planner import WHEEL, PollPlanner
from .policy import MAX_RECORDS, MAX_SIGNALS, REVISION, candidates

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

STORE_LIMIT = 1024 * 1024


class ScheduleStorageError(ValueError):
    def __init__(self) -> None:
        super().__init__("local_schedule_storage_failed")


def schema_key(request: ReadRequest) -> str:
    return hashlib.sha256(
        repr((request.definition, response_schema(request))).encode()
    ).hexdigest()


def checkpoint(planner: PollPlanner, now: float) -> dict[str, Any]:
    return {
        "version": 1,
        "revision": REVISION,
        "cursor": planner.cursor,
        "wheel": planner.wheel,
        "exhausted": planner.exhausted,
        "rescan_remaining": max(0.0, planner.rescan_at - now),
        "slice_remaining": max(0.0, 600 - (now - planner.slice_started))
        if planner.slice_started is not None
        else None,
        "slice_attempts": planner.slice_attempts,
        "discovery_pause_remaining": max(0.0, planner.discovery_pause_until - now),
        "signals": sorted(planner.inventory.signal_ids),
        "access_key": planner.access_key,
        "firmware": planner.firmware,
        "records": [
            {
                "path": r.request.path,
                "schema": schema_key(r.request),
                "confirmed": r.confirmed,
                "remaining": max(0.0, r.due - now),
                "age": min(86400.0, max(0.0, now - r.created)),
                "failures": r.failures,
                "signal_misses": r.signal_misses,
            }
            for r in planner.inventory.records.values()
        ],
    }


def _number(value: object, maximum: float, *, integer: bool = False) -> float:
    if type(value) not in (int, float) or (integer and type(value) is not int):
        raise ScheduleStorageError()
    number = float(value)  # type: ignore[arg-type]
    if not math.isfinite(number) or not 0 <= number <= maximum:
        raise ScheduleStorageError()
    return number


def restore(planner: PollPlanner, saved: object, now: float) -> None:
    """Validate completely before applying; monotonic observations never restore."""
    fields = {
        "version",
        "revision",
        "cursor",
        "wheel",
        "exhausted",
        "rescan_remaining",
        "slice_remaining",
        "slice_attempts",
        "discovery_pause_remaining",
        "signals",
        "records",
    }
    # P41: optional, so schedules saved before stay valid.
    optional = {"access_key", "firmware"}
    if (
        not isinstance(saved, dict)
        or not fields <= set(saved) <= fields | optional
        or any(
            saved.get(key) is not None
            and (not isinstance(saved[key], str) or len(saved[key]) > 64)
            for key in optional
        )
        or type(saved["version"]) is not int
        or saved["version"] != 1
        or not isinstance(saved["revision"], str)
        or len(saved["revision"]) != 64
        or type(saved["exhausted"]) is not bool
    ):
        raise ScheduleStorageError()
    cursor = int(_number(saved["cursor"], MAX_RECORDS, integer=True))
    wheel = int(_number(saved["wheel"], len(WHEEL) - 1, integer=True))
    rescan = _number(saved["rescan_remaining"], 86400)
    attempts = int(_number(saved["slice_attempts"], 64, integer=True))
    pause = _number(saved["discovery_pause_remaining"], 300)
    remaining = saved["slice_remaining"]
    if remaining is not None:
        remaining = _number(remaining, 600)
    if (
        not isinstance(saved["signals"], list)
        or len(saved["signals"]) > MAX_SIGNALS
        or any(not isinstance(s, str) for s in saved["signals"])
        or len(set(saved["signals"])) != len(saved["signals"])
    ):
        raise ScheduleStorageError()
    if not isinstance(saved["records"], list) or len(saved["records"]) > MAX_RECORDS:
        raise ScheduleStorageError()
    changed = saved["revision"] != REVISION
    records: dict[str, ResourceRecord] = {}
    for index, raw in enumerate(saved["records"]):
        if (
            not isinstance(raw, dict)
            or set(raw)
            != {
                "path",
                "schema",
                "confirmed",
                "remaining",
                "age",
                "failures",
                "signal_misses",
            }
            or not isinstance(raw["path"], str)
            or not isinstance(raw["schema"], str)
            or len(raw["schema"]) != 64
            or type(raw["confirmed"]) is not bool
        ):
            raise ScheduleStorageError()
        delay = _number(raw["remaining"], 86400)
        age = _number(raw["age"], 86400)
        failures = int(_number(raw["failures"], 4, integer=True))
        misses = int(_number(raw["signal_misses"], 2, integer=True))
        path = raw["path"]
        try:
            request = request_for_path(
                path, signal_ids=(path.removeprefix("/signals/"),)
            )
            from .policy import PERIODS, category

            if category(request) not in PERIODS or request.query:
                raise ScheduleStorageError()
        except LocalResourceError as error:
            if changed and error.problem is Problem.INVALID_PATH:
                try:
                    validate_path(path)
                except ValueError:
                    raise ScheduleStorageError() from None
                continue
            raise ScheduleStorageError() from None
        except ValueError:
            raise ScheduleStorageError() from None
        if request.target in records:
            raise ScheduleStorageError()
        if raw["schema"] != schema_key(request):
            delay = min(delay, index * 0.25)
        record = ResourceRecord(
            request,
            now if raw["confirmed"] else now + delay,
            now - age,
            confirmed=raw["confirmed"],
            invalid=True,
            failures=failures,
            signal_misses=misses,
        )
        # A persisted signal ID is not fresh directory evidence in this process.
        record.active = request.definition.template != "/signals/{signalId}"
        records[request.target] = record
    iterator = candidates()
    if not changed and sum(1 for _ in islice(iterator, cursor)) != cursor:
        raise ScheduleStorageError()
    if (
        not changed
        and saved["exhausted"]
        and len(records) < MAX_RECORDS
        and next(iterator, None) is not None
    ):
        # At the record limit the search ends early on purpose (P41).
        raise ScheduleStorageError()
    planner.inventory.records = records
    planner.inventory.signal_ids = frozenset()
    planner.inventory.retained_bytes = planner.inventory.retained_nodes = 0
    planner._candidates = iterator
    planner.cursor, planner.wheel = (0 if changed else cursor), wheel
    planner.exhausted = saved["exhausted"] and not changed
    planner.rescan_at = now + rescan
    planner.slice_started = now - (600 - remaining) if remaining is not None else None
    planner.slice_attempts = attempts
    planner.discovery_pause_until = now + pause
    saved_key = saved.get("access_key")
    if saved_key is not None and saved_key != planner.access_key:
        # New token or connection settings: forget learned "not found/denied"
        # pauses of paths never read successfully (P41).
        for record in records.values():
            if not record.confirmed:
                record.failures, record.due = 0, now
    planner.restored_firmware = saved.get("firmware")
    planner.firmware = planner.restored_firmware


class LocalScheduleStore:
    def __init__(self, hass: HomeAssistant, scope: str) -> None:
        self._hass = hass
        name = hashlib.sha256(scope.encode()).hexdigest()
        self._path = Path(
            hass.config.path(".storage", f"bosch_buderus_heating.local_schedule.{name}")
        )
        self._lock = asyncio.Lock()

    async def _io[T](self, operation: Callable[[], T]) -> T:
        task = self._hass.async_add_executor_job(operation)
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        try:
            result = task.result()
        except Exception:
            raise ScheduleStorageError() from None
        if cancelled:
            raise asyncio.CancelledError
        return result

    def _load(self) -> object:
        try:
            with self._path.open("rb") as file:
                body = file.read(STORE_LIMIT + 1)
        except FileNotFoundError:
            return None
        return load_json(
            body, exact_numbers=False, limits=JsonLimits(STORE_LIMIT, 16, 65536)
        )

    async def load(self) -> object:
        async with self._lock:
            return await self._io(self._load)

    async def save(self, value: dict[str, Any]) -> None:
        # Detach from mutable state before entering the executor or waiting.
        data = json.loads(json.dumps(value, allow_nan=False))
        if len(json.dumps(data).encode()) > STORE_LIMIT:
            raise ScheduleStorageError()

        def write() -> None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            save_json(str(self._path), data, private=True, atomic_writes=True)
            if self._load() != data:
                raise ScheduleStorageError()

        async with self._lock:
            await self._io(write)
