"""Verify cold recovery after SIGKILL at real HA storage write boundaries."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tests" / "helpers" / "fault_storage_process.py"
pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX SIGKILL semantics"
)


class Stages:
    """Stage processes forked from one interpreter that imported Home
    Assistant once (about a second per process otherwise). Every stage
    still runs in a process of its own that can be killed."""

    def __init__(self) -> None:
        self.server = subprocess.Popen(
            [sys.executable, str(HELPER), "--serve"],
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )

    def _ask(self, request: dict) -> object:
        assert self.server.stdin is not None and self.server.stdout is not None
        self.server.stdin.write(json.dumps(request) + "\n")
        self.server.stdin.flush()
        answer = self.server.stdout.readline()
        assert answer, "The stage server ended"
        return json.loads(answer)

    def start(self, directory: Path, stage: str) -> int:
        return self._ask({"run": [str(directory), stage]})

    def status(self, pid: int, timeout: float = 0) -> int | None:
        return self._ask({"wait": pid, "timeout": timeout})

    def close(self) -> None:
        assert self.server.stdin is not None
        self.server.stdin.close()
        try:
            self.server.wait(10)
        finally:
            if self.server.poll() is None:
                self.server.kill()
                self.server.wait()
            if self.server.stdout is not None:
                self.server.stdout.close()


@pytest.fixture(scope="module")
def stages():
    server = Stages()
    try:
        yield server
    finally:
        server.close()


def _error(directory: Path, stage: str) -> str:
    error = directory / f"{stage}.err"
    return error.read_text("utf-8") if error.exists() else ""


def _run(stages: Stages, directory: Path, stage: str) -> str:
    pid = stages.start(directory, stage)
    status = stages.status(pid, timeout=20)
    if status is None:
        os.kill(pid, signal.SIGKILL)
        stages.status(pid, timeout=10)
        pytest.fail(f"The storage stage {stage} did not finish")
    assert status == 0, _error(directory, stage)
    return (directory / f"{stage}.out").read_text("utf-8")


@pytest.mark.parametrize(
    "stage,dismissal_survives",
    [
        ("before-save", False),
        ("during-temp-write", False),
        ("before-replace", False),
        ("after-replace", True),
    ],
)
def test_sigkill_preserves_complete_store_and_recovers_conservatively(
    stages, tmp_path, stage, dismissal_survives
):
    _run(stages, tmp_path, "seed")
    pid = stages.start(tmp_path, stage)
    status = None
    try:
        deadline = time.monotonic() + 20
        while not (tmp_path / "ready").exists():
            status = stages.status(pid)
            if status is not None:
                break
            if time.monotonic() > deadline:
                pytest.fail("Storage writer did not reach the termination boundary")
            time.sleep(0.01)
        assert (tmp_path / "ready").exists(), _error(tmp_path, stage) or status
    finally:
        if status is None:
            os.kill(pid, signal.SIGKILL)
            status = stages.status(pid, timeout=10)
    assert status == -signal.SIGKILL
    # The destination files must remain valid even if a partial temporary file
    # was left behind by the killed process.
    for path in (tmp_path / ".storage").glob("bosch_buderus_heating*"):
        assert "data" in json.loads(path.read_bytes())
    result = json.loads(_run(stages, tmp_path, "inspect"))
    assert result["active"] == 1
    assert bool(result["notifications"]) is not dismissal_survives
    for item in result["notifications"]:
        assert "6249" in item["message"]
        assert "not yet been confirmed" in item["message"]
        assert "faults resolved" not in item["title"]


@pytest.mark.parametrize("store", ["preferences", "baseline"])
def test_corrupt_json_does_not_prevent_fresh_fault_detection(stages, tmp_path, store):
    _run(stages, tmp_path, "seed")
    pattern = (
        "bosch_buderus_heating_faults_*"
        if store == "preferences"
        else "bosch_buderus_heating.faults.*"
    )
    (target,) = (tmp_path / ".storage").glob(pattern)
    target.write_text('{"version": 1, "data":', encoding="utf-8")
    result = json.loads(_run(stages, tmp_path, "inspect-fresh"))
    assert result["active"] == 1
    assert "6249" in result["notifications"][0]["message"]
    assert "faults resolved" not in result["notifications"][0]["title"]
    assert list((tmp_path / ".storage").glob(target.name + ".corrupt.*"))


def test_failed_disk_replace_preserves_old_file_and_in_memory_dismissal(
    stages, tmp_path
):
    _run(stages, tmp_path, "seed")
    assert json.loads(_run(stages, tmp_path, "write-error"))["visible"] is False
    result = json.loads(_run(stages, tmp_path, "inspect"))
    assert result["active"] == 1
    assert "6249" in result["notifications"][0]["message"]
    assert "faults resolved" not in result["notifications"][0]["title"]
    assert not list((tmp_path / ".storage").glob("*.corrupt.*"))
