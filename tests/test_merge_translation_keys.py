"""W0: the JSON deep merge of the translation files of two work packages."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import merge_translation_keys as merger  # noqa: E402

BASE = {
    "title": "Home Comfort",
    "exceptions": {
        "write_failed": {"message": "Failed"},
        "write_busy": {"message": "Busy"},
    },
    "entity": {"switch": {"away_mode": {"name": "Away"}}},
}


def test_new_keys_follow_their_neighbours_and_base_order_stays() -> None:
    other = {
        "title": "Home Comfort",
        "exceptions": {
            "write_failed": {"message": "Failed"},
            "rrc_curve_order": {"message": "Order"},
            "write_busy": {"message": "Busy"},
        },
        "entity": {
            "climate": {"rrc_zone": {"name": "Zone"}},
            "switch": {
                "away_mode": {"name": "Away"},
                "rrc_boost": {"name": "Boost"},
            },
        },
    }
    merged = merger.merge(BASE, other)
    assert list(merged["exceptions"]) == [
        "write_failed",
        "rrc_curve_order",
        "write_busy",
    ]
    assert list(merged["entity"]) == ["climate", "switch"]
    assert list(merged["entity"]["switch"]) == ["away_mode", "rrc_boost"]
    # Merging both ways gives the same keys; base order wins each way.
    assert merger.merge(other, BASE) == other
    # Idempotent.
    assert merger.merge(merged, other) == merged


def test_two_packages_merge_into_one_file() -> None:
    first = json.loads(json.dumps(BASE))
    first["exceptions"]["rrc_event_pending"] = {"message": "Pending"}
    second = json.loads(json.dumps(BASE))
    second["exceptions"]["rrc_curve_order"] = {"message": "Order"}
    merged = merger.merge(first, second)
    assert list(merged["exceptions"]) == [
        "write_failed",
        "write_busy",
        "rrc_event_pending",
        "rrc_curve_order",
    ]


def test_conflicting_texts_stop_unless_a_side_is_preferred() -> None:
    other = json.loads(json.dumps(BASE))
    other["exceptions"]["write_busy"]["message"] = "Still busy"
    with pytest.raises(merger.MergeConflict, match=r"exceptions\.write_busy\.message"):
        merger.merge(BASE, other)
    assert merger.merge(BASE, other, prefer="base") == BASE
    assert merger.merge(BASE, other, prefer="other") == other


def test_command_line(tmp_path: Path) -> None:
    base, other, out = (tmp_path / name for name in ("a.json", "b.json", "c.json"))
    base.write_text(json.dumps(BASE), encoding="utf-8")
    changed = json.loads(json.dumps(BASE))
    changed["entity"]["switch"]["rrc_fireplace"] = {"name": "Kaminöfen"}
    other.write_text(json.dumps(changed), encoding="utf-8")
    assert merger.main([str(base), str(other), "-o", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert json.loads(text) == changed
    assert text.endswith("}\n") and "Kaminöfen" in text and '\n  "' in text
    conflict = json.loads(json.dumps(BASE))
    conflict["title"] = "Other"
    other.write_text(json.dumps(conflict), encoding="utf-8")
    assert merger.main([str(base), str(other)]) == 1
    other.write_text("[]", encoding="utf-8")
    assert merger.main([str(base), str(other)]) == 2


def test_git_conflict_mode(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    file = tmp_path / "strings.json"
    git("init", "-q", "-b", "main")
    file.write_text(merger.dumps(BASE), encoding="utf-8")
    git("add", "strings.json")
    git("commit", "-q", "-m", "base")
    git("checkout", "-q", "-b", "one")
    first = json.loads(json.dumps(BASE))
    first["exceptions"] = {
        "write_failed": {"message": "Failed"},
        "rrc_event_pending": {"message": "Pending"},
        "write_busy": {"message": "Busy"},
    }
    file.write_text(merger.dumps(first), encoding="utf-8")
    git("commit", "-q", "-am", "one")
    git("checkout", "-q", "main")
    second = json.loads(json.dumps(BASE))
    second["exceptions"] = {
        "write_failed": {"message": "Failed"},
        "rrc_curve_order": {"message": "Order"},
        "write_busy": {"message": "Busy"},
    }
    file.write_text(merger.dumps(second), encoding="utf-8")
    git("commit", "-q", "-am", "two")
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "merge",
            "-q",
            "one",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    # The text merge conflicts.
    assert result.returncode != 0 and "CONFLICT" in result.stdout, result
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert merger.main(["--git-conflict", "strings.json"]) == 0
    finally:
        os.chdir(cwd)
    merged = json.loads(file.read_text(encoding="utf-8"))
    assert list(merged["exceptions"]) == [
        "write_failed",
        "rrc_curve_order",
        "rrc_event_pending",
        "write_busy",
    ]
