"""Build the deterministic HACS release archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT_PATH = "custom_components/bosch_buderus_heating"
COMPONENT = ROOT / COMPONENT_PATH
ARCHIVE_NAME = "bosch_buderus_heating.zip"
CHECKSUM_NAME = f"{ARCHIVE_NAME}.sha256"
_ZIP_TIMESTAMP = (2020, 1, 1, 0, 0, 0)


def manifest_version(root: Path = ROOT) -> str:
    """Return the integration version from the Home Assistant manifest."""
    manifest_file = root / COMPONENT_PATH / "manifest.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    version = manifest.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("manifest.json does not contain a valid version")
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project_version = project.get("project", {}).get("version")
    if project_version != version:
        raise ValueError(
            "pyproject.toml version does not match the integration manifest"
        )
    return version


def _git(root: Path, *arguments: str) -> str:
    """Return the output of one git command run in ``root``."""
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def ensure_clean_component(root: Path = ROOT) -> None:
    """Refuse to build while the component differs from the checked-out commit.

    The archive must contain exactly what the tag contains. Changed, staged,
    deleted and untracked (not ignored) files below the component directory
    would make a local archive differ from the one built from the tag.
    """
    status = _git(root, "status", "--porcelain", "--", COMPONENT_PATH)
    if status.strip():
        changed = ", ".join(line[3:] for line in status.splitlines()[:5])
        raise ValueError(
            "the integration directory has uncommitted changes "
            f"({changed}); commit or stash them, or pass --allow-dirty "
            "for a local trial build"
        )


def component_files(root: Path = ROOT) -> tuple[str, ...]:
    """Return tracked component files as POSIX paths in stable archive order.

    The order is the plain string order of the paths ``git ls-files`` reports,
    so it is the same on every platform and file system.
    """
    prefix = f"{COMPONENT_PATH}/"
    files = tuple(
        sorted(
            line
            for line in _git(root, "ls-files", "-z", "--", prefix).split("\0")
            if line.startswith(prefix) and (root / line).is_file()
        )
    )
    if f"{prefix}manifest.json" not in files:
        raise ValueError("tracked integration manifest is missing")
    return files


def build_release(
    output_dir: Path,
    *,
    expected_version: str | None = None,
    allow_dirty: bool = False,
    root: Path = ROOT,
) -> tuple[Path, Path]:
    """Create a reproducible component-root ZIP and its SHA-256 checksum."""
    version = manifest_version(root)
    if expected_version is not None and version != expected_version:
        raise ValueError(
            f"manifest version {version!r} does not match {expected_version!r}"
        )
    if not allow_dirty:
        ensure_clean_component(root)
    files = component_files(root)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / ARCHIVE_NAME
    with zipfile.ZipFile(
        archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as release_zip:
        for tracked in files:
            relative = tracked.removeprefix(f"{COMPONENT_PATH}/")
            info = zipfile.ZipInfo(relative, date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            release_zip.writestr(info, (root / tracked).read_bytes(), compresslevel=9)

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = output_dir / CHECKSUM_NAME
    checksum.write_text(f"{digest}  {ARCHIVE_NAME}\n", encoding="ascii")
    return archive, checksum


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--expected-version")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="build from uncommitted files (local trial builds only)",
    )
    return parser


def main() -> int:
    """Build the release files from command-line arguments."""
    args = _parser().parse_args()
    archive, checksum = build_release(
        args.output_dir,
        expected_version=args.expected_version,
        allow_dirty=args.allow_dirty,
    )
    print(archive)
    print(checksum)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
