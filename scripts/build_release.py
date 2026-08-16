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
COMPONENT = ROOT / "custom_components" / "bosch_buderus_heating"
ARCHIVE_NAME = "bosch_buderus_heating.zip"
CHECKSUM_NAME = f"{ARCHIVE_NAME}.sha256"
_ZIP_TIMESTAMP = (2020, 1, 1, 0, 0, 0)


def manifest_version() -> str:
    """Return the integration version from the Home Assistant manifest."""
    manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
    version = manifest.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("manifest.json does not contain a valid version")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project_version = project.get("project", {}).get("version")
    if project_version != version:
        raise ValueError(
            "pyproject.toml version does not match the integration manifest"
        )
    return version


def component_files() -> tuple[Path, ...]:
    """Return tracked component files in stable archive order."""
    result = subprocess.run(
        ["git", "ls-files", "--", COMPONENT.relative_to(ROOT).as_posix()],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    files = tuple(
        sorted(
            ROOT / line
            for line in result.stdout.splitlines()
            if line and (ROOT / line).is_file()
        )
    )
    if COMPONENT / "manifest.json" not in files:
        raise ValueError("tracked integration manifest is missing")
    return files


def build_release(
    output_dir: Path, *, expected_version: str | None = None
) -> tuple[Path, Path]:
    """Create a reproducible component-root ZIP and its SHA-256 checksum."""
    version = manifest_version()
    if expected_version is not None and version != expected_version:
        raise ValueError(
            f"manifest version {version!r} does not match {expected_version!r}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / ARCHIVE_NAME
    with zipfile.ZipFile(
        archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as release_zip:
        for source in component_files():
            relative = source.relative_to(COMPONENT).as_posix()
            info = zipfile.ZipInfo(relative, date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            release_zip.writestr(info, source.read_bytes(), compresslevel=9)

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = output_dir / CHECKSUM_NAME
    checksum.write_text(f"{digest}  {ARCHIVE_NAME}\n", encoding="ascii")
    return archive, checksum


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--expected-version")
    return parser


def main() -> int:
    """Build the release files from command-line arguments."""
    args = _parser().parse_args()
    archive, checksum = build_release(
        args.output_dir, expected_version=args.expected_version
    )
    print(archive)
    print(checksum)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
