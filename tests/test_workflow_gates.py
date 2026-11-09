"""The release gate runs every check of CI, including the browser tests."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
FRONTEND = ROOT / "tests" / "frontend"


def _jobs(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))["jobs"]


def _browser_pytest(job: dict) -> set[str]:
    runs = [step["run"] for step in job["steps"] if "pytest" in step.get("run", "")]
    assert len(runs) == 1
    return set(runs[0].split()) - {"pytest", "--no-cov"}


def test_ci_runs_every_home_assistant_browser_test() -> None:
    files = {path.relative_to(ROOT).as_posix() for path in FRONTEND.glob("ha_*.py")}
    assert files >= {
        "tests/frontend/ha_local_first.py",
        "tests/frontend/ha_rescan.py",
    }
    assert _browser_pytest(_jobs("ci.yml")["frontend"]) == files


def test_every_browser_script_has_a_runner() -> None:
    npm = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    run = set(re.findall(r"tests/frontend/([\w.-]+\.cjs)", npm["scripts"]["test"]))
    driven = {
        name
        for path in FRONTEND.glob("ha_*.py")
        for name in re.findall(r'"([\w.-]+\.cjs)"', path.read_text(encoding="utf-8"))
    }
    assert "controls-focus.test.cjs" in run
    assert {path.name for path in FRONTEND.glob("*.cjs")} == run | driven
    assert not run & driven


def test_release_publishes_only_after_the_ci_checks() -> None:
    ci = _jobs("ci.yml")
    release = _jobs("release.yml")
    assert release["release"]["needs"] == ["verify"]
    assert set(release["verify"]["needs"]) == {
        "frontend",
        "compatibility",
        "minimum-version",
    }
    for name in release["verify"]["needs"]:
        # The same steps as in CI, so a tag alone cannot skip a CI check.
        assert release[name]["steps"] == ci[name]["steps"], name
        assert release[name]["permissions"] == {"contents": "read"}
    verify = "\n".join(step.get("run", "") for step in release["verify"]["steps"])
    for command in ci["quality"]["steps"][3:]:
        assert command["run"] in verify
    assert any(
        step["run"] == "npm test"
        for step in release["frontend"]["steps"]
        if "run" in step
    )


def test_documented_required_checks_cover_every_ci_job() -> None:
    text = (ROOT / "docs" / "repository-settings.md").read_text(encoding="utf-8")
    line = re.search(r"- Required checks: (.*?)\.\s", text, re.DOTALL)
    assert line is not None
    documented = set(re.findall(r"`([\w-]+)`", line.group(1)))
    assert documented == set(_jobs("ci.yml")) | {"analyze"}
    assert "analyze" in _jobs("codeql.yml")
