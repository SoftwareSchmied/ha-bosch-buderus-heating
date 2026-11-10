"""Tests for deterministic release construction."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from scripts.build_release import ARCHIVE_NAME, ROOT, build_release
from scripts.release_notes import extract_release_notes

RELEASE_VERSION = "0.9.0-alpha.4"


@pytest.fixture
def git_worktree() -> None:
    """Skip archive tests locally when git cannot resolve this worktree.

    A Windows worktree seen from WSL points at a Windows path that the Linux
    git cannot open. CI must always build the archive, so there the failure
    stays a hard error instead of a skip.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 and not os.environ.get("CI"):
        pytest.skip(
            "git cannot resolve this worktree outside CI "
            f"({result.stderr.strip() or 'git rev-parse failed'})"
        )


@pytest.mark.usefixtures("git_worktree")
def test_release_archive_is_reproducible_and_component_rooted(tmp_path: Path) -> None:
    first, checksum = build_release(
        tmp_path / "first", expected_version=RELEASE_VERSION
    )
    second, _ = build_release(tmp_path / "second", expected_version=RELEASE_VERSION)

    assert first.name == ARCHIVE_NAME
    assert first.read_bytes() == second.read_bytes()
    digest = hashlib.sha256(first.read_bytes()).hexdigest()
    assert checksum.read_text(encoding="ascii") == f"{digest}  {ARCHIVE_NAME}\n"

    with zipfile.ZipFile(first) as release_zip:
        names = release_zip.namelist()
        assert "manifest.json" in names
        assert "pointt/py.typed" in names
        assert "holiday_timezones.py" in names
        assert "holiday_ui.py" in names
        assert "start_counters.py" in names
        assert "dhw_safety.py" in names
        assert "water_softener.py" in names
        assert "button.py" in names
        assert "www/holiday-dialog.js" in names
        assert "www/controls-dialog.js" in names
        assert "heating_climate.py" in names
        assert "controls_flow.py" in names
        assert "switch_programs.py" in names
        assert "plant_model.py" in names
        assert "gateway_identity.py" in names
        assert "configuration.py" in names
        assert "configuration_migration.py" in names
        assert "storage_migration.py" in names
        assert "configuration_access.py" in names
        assert "configuration_changes.py" in names
        assert "configuration_management.py" in names
        assert "connection_runtime.py" in names
        assert "connection_inventory.py" in names
        assert "connection_flow.py" in names
        assert "cloud_configuration.py" in names
        assert "cloud_options_flow.py" in names
        assert "managed_entry.py" in names
        assert "managed_platforms.py" in names
        assert "managed_services.py" in names
        assert "entry_sources.py" in names
        assert "repair_scope.py" in names
        assert "platform_scope.py" in names
        assert "managed_pointt.py" in names
        assert "managed_updates.py" in names
        assert "managed_wallbox.py" in names
        assert "managed_cloud.py" in names
        assert "managed_bacon.py" in names
        assert "bacon/access.py" in names
        assert not any(name.startswith("custom_components/") for name in names)
        assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)


def test_release_rejects_a_version_mismatch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not match"):
        build_release(tmp_path, expected_version="9.9.9")


@pytest.mark.usefixtures("git_worktree")
def test_release_archive_imports_from_an_isolated_install(tmp_path: Path) -> None:
    archive, _ = build_release(tmp_path / "dist", expected_version=RELEASE_VERSION)
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
                "import importlib; "
                "import custom_components.bosch_buderus_heating as integration; "
                "from custom_components.bosch_buderus_heating.gateway_identity "
                "import parse_local_gateway_identity; "
                "from custom_components.bosch_buderus_heating.configuration_migration "
                "import convert_legacy_configuration; "
                "from custom_components.bosch_buderus_heating.storage_migration "
                "import migrate_entry_storage; "
                "from custom_components.bosch_buderus_heating.configuration_access "
                "import EntryConfiguration; "
                "from custom_components.bosch_buderus_heating.configuration_changes "
                "import ConfigurationRuns; "
                "from custom_components.bosch_buderus_heating.managed_entry "
                "import async_setup_managed_entry; "
                "from custom_components.bosch_buderus_heating.managed_platforms "
                "import ManagedPlatforms; "
                "from custom_components.bosch_buderus_heating.managed_services "
                "import ManagedServices; "
                "from custom_components.bosch_buderus_heating.entry_sources "
                "import pointt_coordinators; "
                "from custom_components.bosch_buderus_heating.platform_scope "
                "import PlatformScope; "
                "from custom_components.bosch_buderus_heating.managed_cloud "
                "import CloudDriverFactory; "
                "from custom_components.bosch_buderus_heating.managed_wallbox "
                "import ManagedWallboxCoordinator; "
                "from custom_components.bosch_buderus_heating.connection_flow "
                "import ConnectionOptionsFlow; "
                "from custom_components.bosch_buderus_heating.cloud_options_flow "
                "import CloudOptionsFlow; "
                "from custom_components.bosch_buderus_heating.const import PLATFORMS; "
                "[importlib.import_module(integration.__name__ + '.' + platform) "
                "for platform in PLATFORMS]; "
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


def test_release_notes_extract_only_requested_version() -> None:
    """Release notes come from the matching non-empty changelog section."""
    changelog = """# Changelog

## [Unreleased]

## [2.0.0] - 2030-02-01

Second release.

### Added

- A feature.

## [1.0.0] - 2030-01-01

First release.
"""

    assert extract_release_notes(changelog, "2.0.0") == (
        "Second release.\n\n### Added\n\n- A feature.\n"
    )


def test_release_notes_extract_a_pre_release_section() -> None:
    """Alpha and beta sections are selected by their exact version."""
    changelog = """# Changelog

## [Unreleased]

## [2.0.0-alpha.1] - 2030-02-01

Alpha release.

## [1.0.0] - 2030-01-01

First release.
"""

    assert extract_release_notes(changelog, "2.0.0-alpha.1") == "Alpha release.\n"
    with pytest.raises(ValueError, match="no section"):
        extract_release_notes(changelog, "2.0.0")


@pytest.mark.parametrize(
    ("changelog", "message"),
    [
        ("# Changelog\n", "no section"),
        ("# Changelog\n\n## [2.0.0] - 2030-02-01\n", "is empty"),
    ],
)
def test_release_notes_reject_missing_or_empty_sections(
    changelog: str, message: str
) -> None:
    """A tag cannot silently publish generic or empty release notes."""
    with pytest.raises(ValueError, match=message):
        extract_release_notes(changelog, "2.0.0")
