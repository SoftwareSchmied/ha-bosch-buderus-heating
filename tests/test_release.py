"""Tests for deterministic release construction."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from scripts.build_release import ARCHIVE_NAME, build_release


def test_release_archive_is_reproducible_and_component_rooted(tmp_path: Path) -> None:
    first, checksum = build_release(tmp_path / "first", expected_version="0.2.2")
    second, _ = build_release(tmp_path / "second", expected_version="0.2.2")

    assert first.name == ARCHIVE_NAME
    assert first.read_bytes() == second.read_bytes()
    digest = hashlib.sha256(first.read_bytes()).hexdigest()
    assert checksum.read_text(encoding="ascii") == f"{digest}  {ARCHIVE_NAME}\n"

    with zipfile.ZipFile(first) as release_zip:
        names = release_zip.namelist()
        assert "manifest.json" in names
        assert "pointt/py.typed" in names
        assert not any(name.startswith("custom_components/") for name in names)
        assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)


def test_release_rejects_a_version_mismatch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not match"):
        build_release(tmp_path, expected_version="9.9.9")


def test_release_archive_imports_from_an_isolated_install(tmp_path: Path) -> None:
    archive, _ = build_release(tmp_path / "dist", expected_version="0.2.2")
    install_root = tmp_path / "install"
    component = install_root / "custom_components" / "bosch_buderus_heating"
    component.mkdir(parents=True)
    with zipfile.ZipFile(archive) as release_zip:
        release_zip.extractall(component)

    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(install_root)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import custom_components.bosch_buderus_heating as integration; "
                "print(integration.__file__)"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert str(component) in result.stdout
