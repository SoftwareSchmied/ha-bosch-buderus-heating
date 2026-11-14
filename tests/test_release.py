"""Tests for deterministic release construction."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from scripts.build_release import (
    ARCHIVE_NAME,
    COMPONENT_PATH,
    ROOT,
    build_release,
    component_files,
)
from scripts.release_notes import CHANGELOG, extract_release_notes
from scripts.release_tag import ReleaseTag, check_release_tag

RELEASE_VERSION = "0.9.1"
# The last section written before release notes had to stand on their own.
LAST_SECTION_WITH_CROSS_REFERENCES = "0.9.0-alpha.6"


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
        tmp_path / "first", expected_version=RELEASE_VERSION, allow_dirty=True
    )
    second, _ = build_release(
        tmp_path / "second", expected_version=RELEASE_VERSION, allow_dirty=True
    )

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
    archive, _ = build_release(
        tmp_path / "dist", expected_version=RELEASE_VERSION, allow_dirty=True
    )
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


_STANDALONE_CHANGELOG = """# Changelog

## [Unreleased]

## [2.0.0-alpha.2] - 2030-02-02

> [!IMPORTANT]
> Make a backup first. From alpha.1 no new setup is needed; from 1.0.x, the
> notes of alpha.1 below apply as well.

## [2.0.0-alpha.1] - 2030-02-01

Alpha release.

## [1.0.0] - 2030-01-01

First release.

## [1.0.0-alpha.2] - 2029-12-01

An older alpha.2 of another version.
"""


def test_release_notes_reject_a_pointer_to_another_section() -> None:
    """A published release cannot send its readers to notes it lacks."""
    with pytest.raises(ValueError, match=r"points to other sections.*alpha\.1 below"):
        extract_release_notes(_STANDALONE_CHANGELOG, "2.0.0-alpha.2")


@pytest.mark.parametrize(
    "sentence",
    [
        "The changes of 1.0.0 above still apply.",
        "Coming from 1.0.x, read the notes below first.",
        "Coming from alpha.1, read the notes below first.",
        "See the 2.0.0-alpha.1 section below.",
    ],
)
def test_release_notes_reject_every_named_section(sentence: str) -> None:
    changelog = _STANDALONE_CHANGELOG.replace(
        "> Make a backup first. From alpha.1 no new setup is needed; from 1.0.x, the\n"
        "> notes of alpha.1 below apply as well.",
        sentence,
    )
    with pytest.raises(ValueError, match="points to other sections"):
        extract_release_notes(changelog, "2.0.0-alpha.2")


@pytest.mark.parametrize(
    "sentence",
    [
        # Own subsections, comparisons and plain mentions stay allowed, also
        # when an older version had a pre-release with the same name.
        "What alpha.2 confirmed is marked as confirmed below.",
        "The sections below say where.",
        "Gateways below 1.0.0 are paused; firmware 05.04.00 or above works.",
        "A flow rate below the measuring range shows unknown.",
        "Updating from alpha.1 needs no new setup.",
    ],
)
def test_release_notes_allow_text_within_the_section(sentence: str) -> None:
    changelog = _STANDALONE_CHANGELOG.replace(
        "> Make a backup first. From alpha.1 no new setup is needed; from 1.0.x, the\n"
        "> notes of alpha.1 below apply as well.",
        sentence,
    )
    assert extract_release_notes(changelog, "2.0.0-alpha.2") == (
        f"> [!IMPORTANT]\n{sentence}\n"
    )


def test_changelog_sections_since_the_rule_stand_on_their_own() -> None:
    """Every version section newer than the rule is complete in its release."""
    changelog = CHANGELOG.read_text(encoding="utf-8")
    versions = re.findall(r"^## \[([^]]+)]", changelog, re.MULTILINE)
    assert LAST_SECTION_WITH_CROSS_REFERENCES in versions
    newer = versions[1 : versions.index(LAST_SECTION_WITH_CROSS_REFERENCES)]
    assert versions[0] == "Unreleased"
    for version in newer:
        extract_release_notes(changelog, version)
    # The rule recognises the pointer that the earlier sections still carry.
    with pytest.raises(ValueError, match="points to other sections"):
        extract_release_notes(changelog, LAST_SECTION_WITH_CROSS_REFERENCES)


def test_stable_release_notes_open_with_what_to_do_before_the_update() -> None:
    """Who updates from an older stable version reads only this section."""
    if "-" in RELEASE_VERSION:
        pytest.skip("only the notes of a stable release need the box")
    notes = extract_release_notes(
        CHANGELOG.read_text(encoding="utf-8"), RELEASE_VERSION
    )
    box = notes.split("\n\n", 1)[0]
    assert box.startswith("> [!IMPORTANT]\n> **Before you update**")
    text = " ".join(line.removeprefix(">").strip() for line in box.splitlines())
    assert "backup" in text
    assert "storage format 4" in text
    assert "Home Assistant 2026.5.0 or newer" in text


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Release test",
            "-c",
            "user.email=release-test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.autocrlf=false",
            *arguments,
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def release_repository(tmp_path: Path) -> Path:
    """Return a committed repository with a small integration directory."""
    root = tmp_path / "repository"
    component = root / COMPONENT_PATH
    for relative, content in {
        "manifest.json": '{"domain": "demo", "version": "1.2.3"}\n',
        # Path objects and strings sort these names differently.
        "a.py": "a\n",
        "a-b.py": "a-b\n",
        "a/b.py": "a/b\n",
        "B.py": "B\n",
        "www/dialog.js": "dialog\n",
    }.items():
        (component / relative).parent.mkdir(parents=True, exist_ok=True)
        (component / relative).write_text(content, encoding="utf-8", newline="\n")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "1.2.3"\n', encoding="utf-8"
    )
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    _git(root, "init", "--quiet")
    _git(root, "add", ".")
    _git(root, "commit", "--quiet", "-m", "initial")
    return root


def test_release_archive_order_is_the_plain_path_order(
    release_repository: Path, tmp_path: Path
) -> None:
    """The entries follow the POSIX path strings, independent of the platform."""
    archive, _ = build_release(
        tmp_path / "dist", expected_version="1.2.3", root=release_repository
    )
    expected = [
        "B.py",
        "a-b.py",
        "a.py",
        "a/b.py",
        "manifest.json",
        "www/dialog.js",
    ]
    assert sorted(expected) == expected
    with zipfile.ZipFile(archive) as release_zip:
        assert release_zip.namelist() == expected
        assert release_zip.read("a/b.py") == b"a/b\n"
    assert component_files(release_repository) == tuple(
        f"{COMPONENT_PATH}/{name}" for name in expected
    )


@pytest.mark.parametrize("change", ["modified", "staged", "deleted", "untracked"])
def test_release_refuses_an_integration_directory_with_changes(
    release_repository: Path, tmp_path: Path, change: str
) -> None:
    """A local archive is built only from what the commit contains."""
    component = release_repository / COMPONENT_PATH
    if change in {"modified", "staged"}:
        (component / "a.py").write_text("changed\n", encoding="utf-8")
        if change == "staged":
            _git(release_repository, "add", ".")
    elif change == "deleted":
        (component / "a.py").unlink()
    else:
        (component / "new_module.py").write_text("new\n", encoding="utf-8")

    with pytest.raises(ValueError, match="uncommitted changes"):
        build_release(tmp_path / "dist", root=release_repository)
    assert not (tmp_path / "dist").exists()

    # A deliberate local trial build stays possible.
    archive, _ = build_release(
        tmp_path / "trial", root=release_repository, allow_dirty=True
    )
    with zipfile.ZipFile(archive) as release_zip:
        assert "new_module.py" not in release_zip.namelist()


def test_release_ignores_changes_outside_the_integration(
    release_repository: Path, tmp_path: Path
) -> None:
    (release_repository / "README.md").write_text("changed\n", encoding="utf-8")
    (release_repository / "notes.txt").write_text("draft\n", encoding="utf-8")
    cache = release_repository / COMPONENT_PATH / "__pycache__"
    cache.mkdir()
    (cache / "a.cpython-314.pyc").write_bytes(b"\0")

    archive, _ = build_release(tmp_path / "dist", root=release_repository)

    with zipfile.ZipFile(archive) as release_zip:
        assert not any("__pycache__" in name for name in release_zip.namelist())


@pytest.mark.parametrize(
    ("tag", "main_status", "expected"),
    [
        ("v0.9.0", "identical", ReleaseTag("0.9.0", prerelease=False)),
        ("v0.9.0", "behind", ReleaseTag("0.9.0", prerelease=False)),
        ("v0.9.1-beta.1", "diverged", ReleaseTag("0.9.1-beta.1", prerelease=True)),
        ("v0.9.0-alpha.6", "ahead", ReleaseTag("0.9.0-alpha.6", prerelease=True)),
    ],
)
def test_release_tag_decides_the_release_type(
    tag: str, main_status: str, expected: ReleaseTag
) -> None:
    """Stable tags come from main; pre-releases may come from a feature branch."""
    assert check_release_tag(tag, object_type="tag", main_status=main_status) == (
        expected
    )


@pytest.mark.parametrize(
    ("tag", "object_type", "main_status", "message"),
    [
        ("v0.9.0", "commit", "identical", "not annotated"),
        ("v0.9.0-beta.1", "commit", "identical", "not annotated"),
        ("v0.9.0", "tag", "ahead", "not point to a commit on main"),
        ("v0.9.0", "tag", "diverged", "not point to a commit on main"),
        ("v0.9.0", "tag", "", "not point to a commit on main"),
        ("0.9.0", "tag", "identical", "not a release tag"),
        ("v0.9", "tag", "identical", "not a release tag"),
        ("v0.9.0-", "tag", "identical", "not a release tag"),
    ],
)
def test_release_tag_refuses_to_publish(
    tag: str, object_type: str, main_status: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        check_release_tag(tag, object_type=object_type, main_status=main_status)


def test_release_tag_prints_the_workflow_outputs() -> None:
    script = ROOT / "scripts" / "release_tag.py"
    published = subprocess.run(
        [
            sys.executable,
            str(script),
            "--tag",
            "v0.9.0",
            "--object-type",
            "tag",
            "--main-status",
            "behind",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert published.stdout == "version=0.9.0\nprerelease=false\n"

    refused = subprocess.run(
        [
            sys.executable,
            str(script),
            "--tag",
            "v0.9.0",
            "--object-type",
            "commit",
            "--main-status",
            "behind",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert refused.returncode != 0
    assert refused.stdout == ""
    assert "not annotated" in refused.stderr
