"""Isolated HA storage process used by the abrupt-termination regression tests."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.components import persistent_notification as pn
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import file as file_util

from custom_components.bosch_buderus_heating.const import CONF_GATEWAY_IDS
from custom_components.bosch_buderus_heating.faults import FaultTracker
from custom_components.bosch_buderus_heating.notifications import FaultNotifications
from custom_components.bosch_buderus_heating.pointt import Resource


async def run(directory: Path, stage: str) -> None:
    """Exercise real Store files without any cloud connection or HA server."""
    hass = HomeAssistant(str(directory))
    await hass.config.async_set_time_zone("UTC")
    if hasattr(dr, "async_setup"):  # Home Assistant 2026.9 and newer
        dr.async_setup(hass)
    await dr.async_load(hass)
    await ir.async_load(hass)
    entry = SimpleNamespace(
        entry_id="storage-test",
        version=3,
        minor_version=1,
        data={CONF_GATEWAY_IDS: ["fixture-gateway"]},
        options={},
    )
    tracker = FaultTracker(hass, entry.entry_id, "fixture-gateway")
    coordinator = SimpleNamespace(
        faults=tracker, gateway=SimpleNamespace(gateway_id="fixture-gateway")
    )
    manager = FaultNotifications(hass, entry, coordinator)
    current = {}

    @callback
    def record(kind, notifications):
        for key, item in notifications.items():
            if key != manager._id:
                continue
            if kind is pn.UpdateType.REMOVED:
                current.pop(key, None)
            else:
                current[key] = item

    remove = pn.async_register_callback(hass, record)
    await tracker.async_load()
    if stage in {"seed", "inspect-fresh"}:
        resource = Resource(
            path="/notifications",
            values=({"ccd": "6249", "fc": "12", "occurrenceId": "fixture"},),
            has_values=True,
        )
        tracker.process_resources({resource.path: resource})
    await manager.async_start()
    if stage == "seed":
        await tracker.async_flush()
        await manager._store.async_save(manager._serialize())
    elif stage.startswith("inspect"):
        print(
            json.dumps(
                {
                    "active": len(tracker.active_faults),
                    "notifications": [
                        {"title": item["title"], "message": item["message"]}
                        for item in current.values()
                    ],
                }
            ),
            flush=True,
        )
    else:
        pn.async_dismiss(hass, manager._id)

        def barrier() -> None:
            (directory / "ready").write_text(stage, encoding="utf-8")
            threading.Event().wait()

        if stage == "before-save":
            # Stop the event loop at the persistence boundary. The parent kills
            # this process before the normal one-second delayed save can run.
            barrier()
        original_replace = file_util.os.replace

        def replace(source, destination):
            target = str(destination) == manager._store.path
            if target and stage == "write-error":
                raise OSError(errno.ENOSPC, "Injected full test disk")
            if target and stage == "before-replace":
                barrier()
            original_replace(source, destination)
            if target and stage == "after-replace":
                barrier()

        original_temporary = file_util.tempfile.NamedTemporaryFile

        def temporary(*args, **kwargs):
            temporary_file = original_temporary(*args, **kwargs)
            if stage != "during-temp-write":
                return temporary_file
            original_write = temporary_file.write

            def partial_write(data):
                original_write(data[: len(data) // 2])
                temporary_file.flush()
                barrier()

            temporary_file.write = partial_write
            return temporary_file

        with (
            patch.object(file_util.os, "replace", replace),
            patch.object(file_util.tempfile, "NamedTemporaryFile", temporary),
        ):
            await manager._store.async_save(manager._serialize())
        if stage == "write-error":
            print(json.dumps({"visible": bool(current)}), flush=True)
        else:
            raise AssertionError("The writer passed the expected termination boundary")
    manager.stop()
    remove()


def _child(directory: str, stage: str) -> int:
    """One stage: what it prints goes to ``<stage>.out``, a failure to
    ``<stage>.err``."""
    folder = Path(directory)
    with (folder / f"{stage}.out").open("w", encoding="utf-8") as output:
        try:
            with contextlib.redirect_stdout(output):
                asyncio.run(run(folder, stage))
        except BaseException:
            (folder / f"{stage}.err").write_text(
                traceback.format_exc(), encoding="utf-8"
            )
            return 1
    return 0


def _status(pid: int, timeout: float) -> int | None:
    """The exit status (negative: the signal), or None while it runs."""
    deadline = time.monotonic() + timeout
    while True:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            return os.waitstatus_to_exitcode(status)
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.005)


def serve() -> None:
    """Fork one process per stage from this interpreter, which has imported
    Home Assistant once. Requests and answers are JSON lines on stdin and
    stdout: ``{"run": [directory, stage]}`` answers the process ID,
    ``{"wait": pid, "timeout": seconds}`` its exit status or None."""
    for line in sys.stdin:
        request = json.loads(line)
        if "run" in request:
            pid = os.fork()
            if pid == 0:  # the stage process
                code = 1
                try:
                    devnull = os.open(os.devnull, os.O_RDWR)
                    os.dup2(devnull, 0)
                    os.dup2(devnull, 1)
                    code = _child(*request["run"])
                finally:
                    os._exit(code)
            answer = pid
        else:
            answer = _status(request["wait"], request["timeout"])
        print(json.dumps(answer), flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--serve"]:
        serve()
    else:
        asyncio.run(run(Path(sys.argv[1]), sys.argv[2]))
