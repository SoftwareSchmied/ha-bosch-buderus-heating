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


def _command(directory: Path, stage: str) -> list[str]:
    return [sys.executable, str(HELPER), str(directory), stage]


def _environment() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(ROOT)}


def _run(directory: Path, stage: str) -> str:
    result = subprocess.run(
        _command(directory, stage),
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


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
    tmp_path, stage, dismissal_survives
):
    _run(tmp_path, "seed")
    process = subprocess.Popen(
        _command(tmp_path, stage),
        env=_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not (tmp_path / "ready").exists() and process.poll() is None:
            if time.monotonic() > deadline:
                pytest.fail("Storage writer did not reach the termination boundary")
            time.sleep(0.01)
        assert (tmp_path / "ready").exists(), process.communicate(timeout=5)[1]
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
    assert process.returncode == -signal.SIGKILL
    # The destination files must remain valid even if a partial temporary file
    # was left behind by the killed process.
    for path in (tmp_path / ".storage").glob("bosch_buderus_heating*"):
        assert "data" in json.loads(path.read_bytes())
    result = json.loads(_run(tmp_path, "inspect"))
    assert result["active"] == 1
    assert bool(result["notifications"]) is not dismissal_survives
    for item in result["notifications"]:
        assert "6249" in item["message"]
        assert "not yet been confirmed" in item["message"]
        assert "faults resolved" not in item["title"]


@pytest.mark.parametrize("store", ["preferences", "baseline"])
def test_corrupt_json_does_not_prevent_fresh_fault_detection(tmp_path, store):
    _run(tmp_path, "seed")
    pattern = (
        "bosch_buderus_heating_faults_*"
        if store == "preferences"
        else "bosch_buderus_heating.faults.*"
    )
    (target,) = (tmp_path / ".storage").glob(pattern)
    target.write_text('{"version": 1, "data":', encoding="utf-8")
    result = json.loads(_run(tmp_path, "inspect-fresh"))
    assert result["active"] == 1
    assert "6249" in result["notifications"][0]["message"]
    assert "faults resolved" not in result["notifications"][0]["title"]
    assert list((tmp_path / ".storage").glob(target.name + ".corrupt.*"))


def test_failed_disk_replace_preserves_old_file_and_in_memory_dismissal(tmp_path):
    _run(tmp_path, "seed")
    assert json.loads(_run(tmp_path, "write-error"))["visible"] is False
    result = json.loads(_run(tmp_path, "inspect"))
    assert result["active"] == 1
    assert "6249" in result["notifications"][0]["message"]
    assert "faults resolved" not in result["notifications"][0]["title"]
    assert not list((tmp_path / ".storage").glob("*.corrupt.*"))
