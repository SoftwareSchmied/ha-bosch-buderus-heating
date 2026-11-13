"""Private bounded checkpoints without measurement values or credentials."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Callable
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.util.file import write_utf8_file_atomic

from ..local_api.catalog import (
    ReadRequest,
    request_for_path,
    response_schema,
    validate_path,
)
from ..local_api.errors import LocalResourceError, Problem
from ..local_api.json import JsonLimits, load_json
from .inventory import NOT_FOUND_RETRY, ResourceRecord
from .planner import WHEEL, PollPlanner
from .policy import MAX_RECORDS, MAX_SIGNALS, REVISION, candidates

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

STORE_LIMIT = 1024 * 1024


def compact_json(value: object) -> bytes:
    """Compact UTF-8 JSON; NaN and infinite numbers raise ``ValueError``.

    Files written before with indentation load the same way.
    """
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":")
    ).encode()


class ScheduleStorageError(ValueError):
    def __init__(self) -> None:
        super().__init__("local_schedule_storage_failed")


class ScheduleCorrupt(ScheduleStorageError):
    """The file was read, but its content is not a valid schedule."""


# Problems of paths never read that "not found/denied" answers set; restored
# with their pause also when the paths' schema changed (O42).
_NOT_FOUND_PROBLEMS = frozenset({"forbidden", "not_found", "not_acceptable"})
_PROBLEM = re.compile(r"[a-z0-9_]{1,64}")
# The longest pause a stored record may carry: the weekly "not found" retry.
_MAX_REMAINING = float(NOT_FOUND_RETRY)
# Schema key per catalog template: it depends on the definition only.
_SCHEMA_KEYS: dict[str, str] = {}


def schema_key(request: ReadRequest) -> str:
    response_schema(request)  # only for a valid request
    return _definition_key(request)


def _definition_key(request: ReadRequest) -> str:
    """The schema key of a valid request, computed once per template."""
    definition = request.definition
    key = _SCHEMA_KEYS.get(definition.template)
    if key is None:
        key = _SCHEMA_KEYS[definition.template] = hashlib.sha256(
            repr((definition, response_schema(request))).encode()
        ).hexdigest()
    return key


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
        "records": [_record(r, now) for r in planner.inventory.records.values()],
    }


def _record(record: ResourceRecord, now: float) -> dict[str, Any]:
    saved: dict[str, Any] = {
        "path": record.request.path,
        "schema": schema_key(record.request),
        "confirmed": record.confirmed,
        "remaining": min(_MAX_REMAINING, max(0.0, record.due - now)),
        "age": min(86400.0, max(0.0, now - record.created)),
        "failures": record.failures,
        "signal_misses": record.signal_misses,
    }
    if (problem := _stored_problem(record)) is not None:
        # Why a path was never read (for example "not_found"); a fixed code.
        saved["problem"] = problem
    return saved


def _stored_problem(record: ResourceRecord) -> str | None:
    problem = record.problem
    if record.confirmed or not isinstance(problem, str):
        return None
    return problem if _PROBLEM.fullmatch(problem) else None


def schedule_state(planner: PollPlanner) -> tuple[object, object]:
    """What a checkpoint records, as absolute times instead of remaining ones.

    Unlike ``checkpoint`` it does not change while only time passes, so an
    unchanged schedule is not written again. The first part is the search and
    each record's result; the second the timing (the lane wheel, the search
    slice, pauses, due times of records not confirmed so far). A confirmed
    record is read at once after a restart, so its due time is left out.
    """
    records = tuple(planner.inventory.records.values())
    return (
        (
            planner.cursor,
            planner.exhausted,
            tuple(sorted(planner.inventory.signal_ids)),
            planner.access_key,
            planner.firmware,
            tuple(
                (
                    r.request.target,
                    r.confirmed,
                    r.failures,
                    r.signal_misses,
                    _stored_problem(r),
                )
                for r in records
            ),
        ),
        (
            planner.wheel,
            planner.slice_attempts,
            planner.slice_started,
            planner.discovery_pause_until,
            planner.rescan_at,
            tuple(
                (r.request.target, r.created, None if r.confirmed else r.due)
                for r in records
            ),
        ),
    )


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
    record_fields = {
        "path",
        "schema",
        "confirmed",
        "remaining",
        "age",
        "failures",
        "signal_misses",
    }
    for index, raw in enumerate(saved["records"]):
        if (
            not isinstance(raw, dict)
            # "problem" is optional: schedules saved before stay valid.
            or not record_fields <= set(raw) <= record_fields | {"problem"}
            or not isinstance(raw["path"], str)
            or not isinstance(raw["schema"], str)
            or len(raw["schema"]) != 64
            or type(raw["confirmed"]) is not bool
        ):
            raise ScheduleStorageError()
        problem = raw.get("problem")
        if problem is not None and (
            not isinstance(problem, str) or _PROBLEM.fullmatch(problem) is None
        ):
            raise ScheduleStorageError()
        delay = _number(raw["remaining"], _MAX_REMAINING)
        age = _number(raw["age"], 86400)
        failures = int(_number(raw["failures"], 4, integer=True))
        misses = int(_number(raw["signal_misses"], 2, integer=True))
        path = raw["path"]
        try:
            request = request_for_path(
                path, signal_ids=(path.removeprefix("/signals/"),)
            )
            from .policy import PERIODS, category

            if request.query:
                raise ScheduleStorageError()
            if category(request) not in PERIODS:
                if changed:
                    # No longer read regularly (for example device serial
                    # numbers, O47): the record is dropped.
                    continue
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
        # The request is already checked: its key without a second parse.
        if raw["schema"] != _definition_key(request) and not (
            not raw["confirmed"] and failures and problem in _NOT_FOUND_PROBLEMS
        ):
            # A changed answer schema asks again soon; a path the gateway
            # did not find or denied keeps its pause (O42).
            delay = min(delay, index * 0.25)
        record = ResourceRecord(
            request,
            now if raw["confirmed"] else now + delay,
            now - age,
            confirmed=raw["confirmed"],
            invalid=True,
            failures=failures,
            signal_misses=misses,
            problem=None if raw["confirmed"] else problem,
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
        except ScheduleCorrupt:
            raise ScheduleCorrupt() from None
        except Exception:
            raise ScheduleStorageError() from None
        if cancelled:
            raise asyncio.CancelledError
        return result

    def _read(self) -> bytes | None:
        try:
            with self._path.open("rb") as file:
                return file.read(STORE_LIMIT + 1)
        except FileNotFoundError:
            return None

    def _load(self) -> object:
        body = self._read()
        if body is None:
            return None
        # A read error above is passing; an unusable content is not.
        try:
            return load_json(
                body, exact_numbers=False, limits=JsonLimits(STORE_LIMIT, 16, 65536)
            )
        except Exception:
            raise ScheduleCorrupt() from None

    async def load(self) -> object:
        """The saved schedule or None; ``ScheduleCorrupt`` for bad content."""
        async with self._lock:
            return await self._io(self._load)

    async def set_aside(self) -> None:
        """Keep an unusable schedule as the only backup and remove it.

        A later backup replaces an earlier one, so at most one is kept.
        """

        def move() -> None:
            self._path.replace(self._path.with_name(self._path.name + ".corrupt"))

        async with self._lock:
            await self._io(move)

    async def save(self, value: dict[str, Any]) -> None:
        # Serialized once, compact, before entering the executor or waiting:
        # detached from mutable state; non-finite numbers are refused.
        body = compact_json(value)
        if len(body) > STORE_LIMIT:
            raise ScheduleStorageError()

        def write() -> None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            write_utf8_file_atomic(str(self._path), body, True, mode="wb")
            # The file must hold exactly the bytes produced above.
            if self._read() != body:
                raise ScheduleStorageError()

        async with self._lock:
            await self._io(write)
