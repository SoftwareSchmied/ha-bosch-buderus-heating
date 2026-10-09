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
python -m venv .audit-venv
.audit-venv/bin/python -m pip install -e ".[security]"
.audit-venv/bin/python -m pip freeze --exclude-editable > .audit-venv/requirements.txt
.audit-venv/bin/python -m pip_audit --strict --requirement .audit-venv/requirements.txt
python scripts/build_release.py --expected-version <version>
```

Use `.audit-venv\Scripts\python.exe` instead on Windows. Keeping the audit in a
clean environment prevents Home Assistant's test-only dependency set from
being mistaken for the integration's runtime dependencies.

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

## Field validation

I prepared version 0.8.0-beta.3 for local validation of the solar entity
cleanup and reauthentication fixes, together with the maintenance updates
from PRs #45, #46 and #47. Version 0.7.2 remains Stable/Latest.

After the HACS update and a Home Assistant restart, I need to confirm that
expected solar entities, where present, remain after another restart and an
integration reload. They belong to the gateway device; a separate solar
device is not introduced. For any requested reauthentication, I need to
confirm that the current sign-in link works, existing devices and entities
remain, and the connection stays available for at least 48 hours. The
original cause of the connection loss in #49 has not been established.
Issues #48 and #49 remain open pending field confirmation. I will assess
the local results and remaining beta issues before publishing 0.8.0 as stable.

The fault-notification checks from beta.2 also remain relevant. I tested
beta.1 on my installation: notifications appeared when faults occurred and
changed to resolved when they cleared. The retained details and timestamps
still need confirmation in Home Assistant. Resolved details are kept only
for the visible message and are cleared on dismissal, disabling notifications,
unloading or restart. No appliance write or deliberately triggered fault is
part of these checks. Issue #36 remains open for the remaining validation.

Existing field feedback includes a Buderus installation
with a K40 gateway and an installation with two heating circuits whose reporter
confirmed the expected controls and changes appearing in MyBuderus. That report
does not enumerate every tested control or confirm restoration of every value.
Bosch systems, additional gateway and multi-source configurations, and long-term
operation still need broader field evidence. A stable release does not imply
that every supported function has been tested on every installation.
The holiday-input corrections in 0.7.2 still need confirmation on a physical
installation after an update and restart.
