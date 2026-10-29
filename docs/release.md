# Creating a release

Releases are built from a verified Git tag. The HACS archive contains the
integration directly at its root and is published together with a SHA-256
checksum file.

## 0.8.3-beta.1 verification

I prepared **0.8.3-beta.1 as a pre-release** for the read-only heating and
hot-water time-program contribution in #96. **0.8.2 remains Stable/Latest.**
Thank you to ReuDa for the implementation and the initial WLW186i/MX400
device evidence. I corrected clock-change scheduling, gateway time-zone
selection, preservation of existing entities and updates to program levels.

The local checks passed **3,277 automated cases**, including parametrized
variants, with **96.82% combined statement and branch coverage**. Ruff,
formatting and strict Mypy passed. The tests cover both clock changes using
Home Assistant's timer helpers, actual HA enum-state validation, recovery
after invalid or unavailable data, preserved registry preferences and cleanup.
The new program module has full statement and branch coverage.

Two local Linux builds produced the same **98-file**, **337,379-byte** archive
with manifest version **0.8.3-beta.1**. SHA-256:
`b843bfcbc9331388fd6628a9923f3231d8135f8c4b84a941b9e691555a466182`.
The package tests verify an import from an isolated ZIP installation. An
isolated runtime dependency audit found no known vulnerabilities.

The release must pass all GitHub checks on the final PR revision, including
the browser tests, followed by the tag-triggered release workflow. Publication
uses an annotated tag from the verified merged result. I compare the downloaded
ZIP and checksum with the local Linux build and the English release notes with
the exact CHANGELOG section before considering publication verified.

The new sensors are disabled by default and make no appliance writes. They
show the level scheduled for the current local time, which can differ from
actual operation under a manual setting, holiday or other override. I have
not installed this beta on a physical system or confirmed the controller's
clock-change behavior. Issue #95 remains open for the updated display checks.

## 0.8.2 verification

I prepared **0.8.2 as Stable/Latest** on 2026-10-27 from the 0.8.2 beta series,
the sign-in documentation from #93 and all three remaining maintenance PRs:
#90, #91 and #92. I retained pytest 9.0.3 because the updated Home Assistant
test package requires that exact version. The runtime dependencies remain
unchanged.

The combined local release checks passed **3,190 automated cases** on Home
Assistant **2026.10.2** and Python **3.14.7**, with **96.78% combined statement
and branch coverage**. This includes parametrized variants. Ruff 0.17.1,
formatting, strict Mypy, reproducible packaging and isolated ZIP imports passed.
I also checked 114 local file and heading links in the changed documentation.
Publication requires the GitHub checks, including the browser tests, on the
final PR, followed by the tag-triggered release checks.
The downloaded ZIP, checksum and release notes must match the local files.

The local Linux archive contains **97 files**, **333,306 bytes**, and version
**0.8.2** in its root manifest. I compared it with the published Beta.7 ZIP:
the file paths are identical and only the manifest version differs. SHA-256:
`48d281b005ecdcdbe08b4e5459b5f5dd26fb929d8a3ceb47acd65fa2207c6d3f`.
The isolated runtime dependency audit found no known vulnerabilities.

I updated the README, setup and operating guides, roadmap and project status
to describe stable 0.8.2. Older release records below remain historical.
I updated the HACS download advice to reflect the maintainer's confirmation
of the GitHub change and Home Assistant 2026.10.4 correction.

I performed no new Home Assistant installation or physical appliance writes
for this publication. The earlier Beta.7 appliance tests remain the available
evidence for those particular controls; broader family and outage validation
remain open. Issue #49 is not closed by the documentation or token corrections.

### Deutsch

Ich veröffentliche **0.8.2 als Stable/Latest** mit den Änderungen aller sieben
Betas und den verbleibenden Wartungs-PRs. Die Beschreibungen unterscheiden den
veröffentlichten Funktionsumfang von noch ausstehenden Praxistests. Bestehende
Eintragsnamen, Geräte- und Entitätskennungen sowie eigene Namen bleiben erhalten.
Für dieses Release habe ich keine neuen Schreibtests an der Anlage durchgeführt.
Die Bestätigung weiterer Gerätefamilien und echter Verbindungsabbrüche steht aus.

## 0.8.2-beta.7 verification

I prepared this beta on 2026-10-25 with the controls dialog, PointT heating-circuit
climates, native PointT hot-water entity and token-lifecycle corrections from
PRs #85 to #87. It also introduces the Home Comfort display name and the HACS
README logo correction from #89. The repository, domain and ZIP filename stay
the same. Existing entry titles, device/entity identities and custom names remain.

**Beta.7 includes the major changes introduced in Beta.6.** I retained the
backup and careful-testing warning in the English release notes and README.
The HACS oversized-response-header download issue remains the first release-note
notice. This integration release does not patch HACS. **0.8.1 remains Stable/Latest.**

- The combined Python suite passed **3,190 automated cases with 96.78% combined
  statement and branch coverage**, including separately counted parameter variants.
- Ruff, formatting and strict Mypy passed. The JavaScript checks and both complete
  Home Assistant browser tests cover the dialog and native hot-water entity.
- Reproducible package construction and import from an isolated ZIP passed.
  The package contains **97 files**, **333,311 bytes**, and manifest version
  **0.8.2-beta.7**. Its SHA-256 is
  `15b1dc3d484da3fc4ffbb7acfaa04552662e1a35944b36e6dc854509ba041b6b`.

The [project status](specification-status.md#added-in-beta7) records the actual
appliance tests. On my Buderus system with Home Assistant 2026.10.3, I confirmed
manual heating 19.0 to 18.5 to 19.0 °C and hot water Auto to Off to Auto through
the new controls. I checked both directions in MyBuderus and restored all changed
settings. This does not confirm other devices, modes or real outage recovery.
The automatic sign-in helper is deferred to a later beta and is not included.

Publication uses the protected main branch after the dependent PRs and final
combined checks pass. The tag workflow repeats quality, dependency, Hassfest,
HACS and package checks. The published ZIP, checksum and English release notes
must match the locally verified files and this version's CHANGELOG section.
Historical package records below remain unchanged.

Select Beta.7 explicitly in HACS, restart Home Assistant and reload the browser.
No new deployment or appliance writes are performed as part of this publication.

## 0.8.2-beta.6 verification

I prepared this substantial beta on 2026-10-24 with the completed P03 and
P05 through P14 packages, P16 and the subsequent review corrections. I added
water-heater, climate, fan and dehumidifier entities, read-only wallbox
sensors, zone and schedule controls, separate cloud connections and corrected
state freshness and conversions. P15 remains excluded. I updated aiomqtt to 2.5.1 to satisfy the current
Home Assistant dependency constraints and verified the MQTT transport with it.

**This beta needs careful field testing.** I placed the warning at the start
of both language sections in the release notes and in the README. I recommend
a full Home Assistant backup before updating. **0.8.1 remains Stable/Latest.**

- Home Assistant 2026.10.1 and Python 3.14.2: **3,036 automated cases passed**
  with **96.83% combined statement and branch coverage**. The count includes
  separately parameterized variants.
- Ruff, formatting and strict Mypy passed. The suite covers malformed and
  partial messages, individual field freshness, reconnects, optional-service
  outages, write confirmation, cancellation, current metadata and conversions.
- Reproducible package construction and import from an isolated ZIP passed.
  German and English use the same 585 translation keys with matching
  placeholders. I checked 75 local documentation file and heading links.
- The separate validation of filter thresholds and salt warnings matched all
  707 filter cases and 13 selected salt cases.

The local Linux archive contains **90 files** and **313,016 bytes**, with
version **0.8.2-beta.6** in its root manifest. Every archived file matches
the prepared component source and the tracked file list. ZIP SHA-256:
**f9e7ab347b81a1f3aa7eae01b6796cd45ae9990c777062572a07293c8d3a36e0**.

Publication requires successful quality, dependency-audit, Hassfest, HACS and
CodeQL checks on the final PR commit. The tag workflow repeats the release
checks. The published archive, checksum and notes must match this local
package and the Beta.6 CHANGELOG section.

I performed no new physical-device tests or appliance writes for this release.
The new device families still need confirmation of readings, reconnects and
write effects. WE800i controls remain disabled by default pending the response
in issue #60. Wallbox controls and consumption history are not included.
Existing PointT heating gateways retain one system device.

Select Beta.6 explicitly in HACS, restart Home Assistant and reload the
browser after updating. Check readings and test controls individually before
relying on them in automations.

## 0.8.2-beta.5 verification

I prepared this beta on 2026-10-23 from the combined device extensions in
PR #76, #77 and #78, with P01, P02 and P04. The release includes initial
WE800i discovery and readings, controls disabled by default, additional
heating/cooling zone readings, WDDW2 safety-state and start-counter corrections,
German resource names and diagnostics schema 17.

The release is a pre-release. **0.8.1 remains Stable/Latest.**

- Home Assistant 2026.10.1 and Python 3.14.2: **1,767 tests passed**, with
  **96.70% combined statement and branch coverage**. Counts include separately
  parameterized test variants.
- Ruff, formatting and strict Mypy passed. The tests include malformed
  readings, shared discovery limits, regular polling recovery, stable
  identities, metadata validation, guarded writes and gateway isolation.
- The suite checks reproducible package construction and an isolated import
  from the ZIP. Publication also requires the dependency audit, HACS,
  Hassfest and CodeQL on the combined PR.
- P02 additionally checked both system/profile language combinations in an
  isolated actual HA frontend with synthetic data. These browser results do
  not establish device behavior.

The isolated runtime dependency audit found no known vulnerabilities. I also
checked 62 local documentation links and the public release text. The Linux
archive contains **59 files** and **240,642 bytes**, with version
`0.8.2-beta.5` in the root manifest. Each file matches the Git index and source.
ZIP SHA-256:
`574e09af5b7dc59a4ad4c424a1fcb1055c12daf107622ebe3ef51fdb94e6919e`.

The published archive, checksum and notes must match this local package and
the beta.5 CHANGELOG section. This comparison follows the release workflow.

The device-support work is included through one combined release PR so the
final checks cover all changes together. Previous package commits and draft
branches remain available for comparison. The release workflow repeats its
verification before publishing the archive and checksum.

I performed no new physical installation or appliance writes for this beta.
WE800i and WDDW2 readings, the additional zone fields and the new softener
controls still need device confirmation. Available water is the remaining
volume, not consumption; `/recordings` remains a deferred package. Issue #60
stays open. The [WE800i validation guide](water-softener-validation.md) explains
the requested comparisons. Select beta.5 explicitly in HACS, update and
restart Home Assistant.

## 0.8.2-beta.3 verification

I prepared this beta on 2026-10-21 with the HACS documentation updates from
PR #73. The integration was accepted into the default HACS catalog through
[hacs/default#10040](https://github.com/hacs/default/pull/10040).
Thank you to the HACS maintainers for reviewing and accepting the submission.

I updated the installation guide, HACS search troubleshooting, project status
and README badge. The README now points to beta.3. The release is a pre-release;
**0.8.1 remains Stable/Latest.**

- Home Assistant 2026.10.1, Python 3.14.2: **1,460 tests passed**, with
  **96.64% combined statement and branch coverage**. Counts include
  parametrized variants.
- Ruff formatting and lint and strict Mypy passed.
- The tests include reproducible packaging and an isolated import from the
  generated archive. I also checked 45 local file and heading links.

I compared the archive with the published beta.2 package. Both contain the
same 56 paths, and only the manifest version differs. The integration code,
translations, frontend assets and behavior are unchanged. The updated guides
are available in the repository; the HACS archive contains the integration.

The Linux archive contains **56 files** and **230,306 bytes**, with
`manifest.json` at its root and version `0.8.2-beta.3`. Every archived file
matches the prepared component source. ZIP SHA-256:
`360bfbf2ca0c9965e47a7e24124c1eddbd2e59a179ffaa4b30b565aff71fcbd7`.

Publication requires successful checks on the final PR commit and a merge
through the protected main branch. The tag-triggered workflow repeats source
checks, tests, the isolated dependency audit, Hassfest and HACS validation.
The published ZIP, checksum and release notes must match the locally verified
package and the corresponding CHANGELOG section.

I performed no physical appliance writes or new field tests for this release.
The revised counter recovery and multi-source behavior from beta.2 still need
field confirmation. Select the beta explicitly in HACS and restart Home
Assistant after updating.

## 0.8.2-beta.2 verification

I prepared this beta on 2026-10-21 with the start-counter changes from PR #72.
Thank you, CHLINDE, for the initial implementation and for confirming that the
missing counter became visible. I keep general and per-source counters
independent, validate their readings and restore existing entities through
normal polling. The [implementation report](start-counters.md) and its
[German version](start-counters.de.md) explain the previous weaknesses and
the changes in detail.

The release is a pre-release. **0.8.1 remains Stable/Latest.**

- Home Assistant 2026.10.1, Python 3.14.2: **1,460 tests passed**, with
  **96.64% combined statement and branch coverage**. Counts include
  parametrized variants; the counter-assessment module has 100% coverage.
- Ruff formatting and lint and strict Mypy passed.
- The follow-up review found inconsistent source-ID handling during recovery.
  Five cases failed before the correction. I added twelve cases covering
  identity restoration, rejected paths and actual discovery/polling after
  403, 404 and 406 responses. Forbidden-resource pauses remain effective.
- Tests also exercise Home Assistant registration and reload with two
  synthetic gateways, recorder statistics, bounded request batches, malformed
  readings, cloud failures, deterministic packaging and isolated ZIP imports.

The Linux archive contains **56 files** and **230,306 bytes**, with
`manifest.json` at its root and version `0.8.2-beta.2`. Every archived file
matches the prepared component source. ZIP SHA-256:
`02919378b6036bf537ebef1899f1d943bf4c84aa676c99f194b765f0ca97fa50`.

Publication requires the protected main branch and successful checks on the
final PR commit. The tag-triggered workflow repeats source checks, tests, the
isolated dependency audit, Hassfest and HACS before publishing. The published
ZIP, checksum and release notes must match the locally verified package and
the matching CHANGELOG section.

CHLINDE's feedback confirms visibility with the initial implementation.
The revised recovery and multi-source behavior still need field confirmation.
I performed no physical appliance writes for this beta. Select the beta
explicitly in HACS and restart Home Assistant after updating.

## 0.8.2-beta.1 verification

I prepared this beta on 2026-10-20 with the pool-heating start counter from
PR #70 and the manual-setpoint recovery from PR #71. I extended CHLINDE's
original recovery implementation and tests with bounded outage handling,
normal polling for late values, error pauses and cancellation checks.
Thank you, CHLINDE, for the initial contribution.

The release is a pre-release. **0.8.1 remains Stable/Latest.**

- Home Assistant 2026.10.1, Python 3.14.7: **1,350 tests passed**, with
  **96.55% combined statement and branch coverage**. Counts include parametrized
  variants.
- Ruff 0.17.0 formatting and lint and strict Mypy 2.4.0 passed.
- An isolated runtime dependency audit found no known vulnerabilities.
- Tests cover at most one 3/10/20-second recovery sequence per outage,
  continued regular polling, independent circuits and gateways, authentication
  and rate-limit errors, forbidden-resource pauses, mode changes, unloading,
  stable entity identities and late pool-counter values.
- The release tests built two identical component-rooted archives and imported
  the integration from an isolated extracted installation.

I independently built the Linux archive twice. It contains **55 files** and
**226,828 bytes**, with `manifest.json` at its root and version
`0.8.2-beta.1`. Every archived file matches the prepared component source.
ZIP SHA-256:
`6234bb5ea0f87166a18b699f7d3c5bf17889bc7f0133b20182516b922b34db4b`.

The protected main branch requires successful combined checks before merging.
The tag-triggered workflow repeats verification, including the dependency
audit, Hassfest and HACS, before publication. Published notes, archive and
checksum must match the verified local artifacts.

I performed no physical appliance writes for this beta. The static APK
comparison does not establish the recovery delay on a real installation.
Field confirmation remains open. Select the beta explicitly in HACS and
restart Home Assistant after updating.

## 0.8.1 verification

I prepared the full stable release on 2026-10-20 from main, including the HACS
timezone corrections, holiday dialog and write changes, the earlier startup
recovery and season-switching betas, and the compatible dependency updates.

- Home Assistant 2026.10.1, Python 3.14.2: **1,287 tests passed**, with
  **96.45% combined statement and branch coverage**.
- Home Assistant 2026.9.4: **366 targeted compatibility cases passed** for
  configuration, holiday dialogs, timezone preparation, writing and coordination.
- Ruff 0.17.0 formatting and lint, strict Mypy 2.4.0 and the isolated runtime
  dependency audit passed. The audit found no known vulnerabilities.
- Local browser component checks passed at 390 and 1280 pixels: adjacent date
  and time, minute-only clocks, empty required dates, editable 08:00 defaults,
  saved times, keyboard deletion, repeated-click protection, invalid drafts,
  failed-request recovery and confirmation routing. The test used simulated
  flow responses and performed no appliance writes.
- Regression tests cover asynchronous timezone preparation before publishing
  successful, adjusted and duplicate write results. The existing cold-cache
  tests check that named-zone disk access occurs outside the event loop.

The earlier installation evidence in [Holiday periods](holiday-periods.md)
covers the dialog layout and a midnight edit of one existing test holiday.
Creation and deletion were not repeated on the appliance for this release.
The broader recovery and installation checks below remain open.

The locally built Linux archive contains **55 files**, **224,569 bytes**, with
`manifest.json` at the root and both the timezone module and new dialog assets.
Two independent test builds produced identical archives, and import from an
isolated extracted installation passed. ZIP SHA-256:
`d39b236bb724a284b6a1fbf715f0c57666763bad977e338b0b2d0fd35e4bf13a`.

The release uses the protected main branch and its required checks. The
tag-triggered workflow repeats source checks, tests, the dependency audit,
Hassfest and HACS validation before publication. Published notes, the archive
and its checksum must match the locally verified release artifacts.

## 0.8.1-beta.2 verification

I prepared this beta on 2026-10-11 to expose central season switching in Home
Assistant. The new select uses the gateway's offered options and write
permission, with the existing validated write and read-back service.
[Season switching](season-switching.md) documents the setting, its separate
actual-status sensor and the available local appliance evidence.

- Python 3.14.2 with the pinned development environment (HA 2026.10.0b4):
  **1,125 tests passed**, **96.26% combined statement and branch coverage**.
  This includes **24 new parametrized season-control test cases**.
- With released HA 2026.10.0, **185 targeted control, sensor and diagnostic
  tests passed**.
- Ruff 0.16.10 formatting/lint, strict Mypy 2.4.0 and development dependency
  consistency passed. An isolated runtime dependency audit found no known
  vulnerabilities.
- The Linux package contains **52 files**, **213,834 bytes**, with
  `manifest.json` at its root. Tests verify reproducible construction and
  import from an isolated extracted installation. Every archived file matches
  the prepared component source.
- ZIP SHA-256:
  `eef26ab8ccf75096aa4efc6455d639b339d53fcffd68c6f082bade7881b3a30a`.

The package was built and tested from an isolated Linux copy of the staged
source to avoid Windows worktree metadata being interpreted as Linux paths.
No application credentials or raw installation exports are included.
The new HA select still needs a physical test after installation. Existing
local appliance records confirm the underlying setting, not this new UI.

Publication uses the protected main branch, required PR checks and the existing
tag-triggered release workflow. The release is a pre-release;
**0.8.0 remains Stable/Latest**. Published assets and notes must be compared with
this package and the matching CHANGELOG section after the workflow finishes.

## 0.8.1-beta.1 verification

I prepared this beta on 2026-10-11 with PointT robustness and request use as
its main focus. It separates nonessential reads from functional discovery,
recovers pending metadata without a restart, preserves entity identities and
stops repeating successful reviewed metadata reads. The full policy, including
paths, deadlines, exception cases and measured savings, is in
[Request behavior and recovery](request-behavior.md).

This version is a pre-release; **0.8.0 remains Stable/Latest**. The combined
release branch is
`release/v0.8.1-beta.1`. Other working checkouts are unaffected.

### Included changes

| Source | Included in the beta |
| --- | --- |
| Startup recovery work | Separate reader service and encapsulated state; license omission; reviewed successful-once metadata; bounded automatic recovery; stable entities; diagnostics schema 14; documentation and tests. |
| PR #55, `5070ff21ba915a970940fe8fd1f02f9b4a176818` | Ruff pre-commit 0.16.10. |
| PR #56, `a4f650cad750d50f4ebe73593ede36b3b690ed36` | Hypothesis 6.168.5, Mypy 2.4.0, HA test package 0.13.370 and Ruff 0.16.10. Pytest stays at 9.0.3, the exact version required by that HA test package. The proposed 9.1.1 pin is incompatible. |
| PR #57, `9b95fc107ab24cf0cc5ab106ec9a7c3abf14bc02` | Both pinned CodeQL actions at 4.38.3. |
| Compatibility found during combined validation | Form schemas use the backend selected by the installed HA. Suggested values and required markers remain consistent across Voluptuous and Probatio. Binary-sensor typing uses the newer defining module while preserving the older runtime import. |
| Current main through PR #54 | Existing 0.8.0 features and the already documented local notification testing. This is prior evidence, not a new field test of the startup changes. |

The maintenance commits were incorporated into the combined release change
with their provenance, superseding the separate changes in #55, #56 and #57.
PR #56's original quality check failed during dependency
installation because its two Pytest requirements conflict. The combined beta
corrects that conflict without relaxing validation or suppressing type errors.

### Verification scope

Python 3.14.2 is used throughout. The new pinned test package supplies HA
2026.10.0b4, so I also check a released HA version separately. The additional
environment deliberately overrides only the test package's HA pin to 2026.10.0;
it is a compatibility check, not the default dependency resolution used by CI.
The default development environment passes `pip check`.

- Full test suite with the pinned development environment: **1,101 passed**,
  **96.23% combined statement and branch coverage**.
- Full test suite with released **HA 2026.10.0**: **1,101 passed**, with the
  same **96.23% combined coverage**. Strict Mypy passed in this environment too.
- On HA 2026.9.4, **167 targeted form, notification-options and holiday-write
  tests passed**, including choosing the correct schema backend when both
  validator packages are installed.
- Ruff 0.16.10 lint and formatting checks and strict Mypy 2.4.0 passed.
- A fresh audit environment containing the integration runtime dependencies
  and audit tools found **no known vulnerabilities**. Test-only HA dependencies
  are not presented as integration runtime requirements.
- The tests include deterministic component-rooted package construction and
  an import from an isolated extracted installation.

These counts include parametrized test variants. They do not represent 1,101
different physical installations. No appliance writes or new field tests were
performed for this beta. Long-term cloud stability and recovery on affected
installations still require observation.

### Prepared package

- Version: `0.8.1-beta.1` in both manifest and project metadata.
- Code/package verification commit: `30f307d79cd086a53e193eaec5f100a6112c4073`.
- Archive: `dist/bosch_buderus_heating.zip`, **213,574 bytes**, **52 files**,
  with `manifest.json` at the root and all three new runtime modules included.
- SHA-256: `f30fb2da203987fcc256474e62ddb4a536244c5f7d2c455b0475dd4b0200b389`.
- Two Linux builds produced byte-identical archives. Every archived file was
  compared with its committed Git blob; archive integrity and the matching
  checksum file were verified. Generated notes match the exact 0.8.1-beta.1
  CHANGELOG section. No untracked files, caches or credentials are packaged.

The checksum identifies the local prepared artifact. Verification of a future
published download remains a separate publication step. Later changes to
release documentation alone do not change the component archive.

### Publication gates

Before publication, the combined change must go through a PR with all required
checks and resolved review conversations. HACS, hassfest and CodeQL must pass
on that combined change. Successful checks on individual maintenance PRs do
not substitute for those combined checks. The tag-triggered release workflow
repeats its validation before publishing the archive.

After explicit release approval, publish an annotated beta tag only from the
verified merged result. The release must be marked as a pre-release and must
not replace 0.8.0 as Latest. Compare the published ZIP and checksum with the
locally verified Linux build and compare the release body with the matching
CHANGELOG section. Overlapping maintenance PRs must be reconciled with the
combined change rather than applying their changes twice.

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

## Field validation before 0.8.1

I released 0.8.0 as stable after an initial local check in which beta.3
appeared to work. Apart from the version number, the integration code and
packaged assets match beta.3. The release includes all changes from the
three 0.8.0 betas, including the maintenance updates.

I have tested the notification feature extensively on my own installation
and closed #36 as implemented and tested locally.

A 48-hour connection test and preservation of the expected solar entities
after restart and reload on the affected installation still need specific
confirmation.
The original cause of the connection loss in #49 has not been established.
Issues #48 and #49 remain open for that feedback.

I tested beta.1 on my installation: notifications appeared when faults
occurred and changed to resolved when they cleared. Retained details are
kept only for the visible message and are cleared on dismissal, disabling
notifications, unloading or restart. Solar entities belong to the gateway
device. These checks do not require appliance writes or deliberately
triggering a fault.

Existing field feedback includes a Buderus installation
with a K40 gateway and an installation with two heating circuits whose reporter
confirmed the expected controls and changes appearing in MyBuderus. That report
does not enumerate every tested control or confirm restoration of every value.
Bosch systems, additional gateway and multi-source configurations, and long-term
operation still need broader field evidence. A stable release does not imply
that every supported function has been tested on every installation.
The holiday-input corrections in 0.7.2 still need confirmation on a physical
installation after an update and restart.
