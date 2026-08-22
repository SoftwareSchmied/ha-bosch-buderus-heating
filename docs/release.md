# Creating a release

Releases are built from a verified Git tag. The HACS archive contains the
integration directly at its root and is published together with a SHA-256
checksum file.

## Local release checks

Run the following commands from the repository root:

```bash
ruff format --check .
ruff check .
mypy
pytest
python -m pip_audit --strict
python scripts/build_release.py --expected-version <version>
```

The official HACS and hassfest validation requires Docker. If Docker is not
available locally, publication remains gated: the tag-triggered GitHub
workflow runs both checks before creating a release.

After the build, `dist/bosch_buderus_heating.zip` and
`dist/bosch_buderus_heating.zip.sha256` must exist. For an additional check,
extract the ZIP into an empty directory. `manifest.json` must be located at the
archive root.

## Publication

1. Ensure that the version in `manifest.json`, `pyproject.toml`, and the
   heading in `CHANGELOG.md` match.
2. Require a clean working tree and successful local checks.
3. Push the verified state to GitHub only after explicit release approval.
4. Create and push the signed or annotated version tag.

The tag starts the release workflow. It repeats tests, type checks, formatting,
linting, dependency auditing, hassfest, and HACS validation. Only then does it
create the GitHub release with the ZIP and checksum.

## Preview limitations

The preview has been tested on a real Buderus installation with a K40 gateway.
Bosch systems, other gateway models, installations with multiple circuits, and
long-term operation do not yet have sufficient field evidence. These limits
remain visible in the README and roadmap; preview releases are not a stable
`1.0`.
