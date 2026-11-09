# Repository settings

The GitHub settings are part of the project baseline and should match this
document.

## Project name and description

- Display name: **Bosch/Buderus Home Comfort**, formerly Bosch/Buderus Heating.
- Repository: `SoftwareSchmied/ha-bosch-buderus-heating`.
- Home Assistant domain and component folder: `bosch_buderus_heating`.
- HACS archive: `bosch_buderus_heating.zip`.
- GitHub description:

  > Unofficial Home Assistant integration for compatible Bosch and Buderus heating, cooling, hot water, air treatment and water softeners, plus read-only wallbox monitoring.

The description is updated directly in GitHub. The display-name change
is included in stable 0.8.2, following its introduction in Beta.7. The repository,
URLs, domain and archive name remain unchanged. See
[Project identity](adr/0001-project-identity-and-license.md#display-name-amendment-2026-10-25).

## Main branch

- Changes go through pull requests.
- Required checks: `frontend`, `quality`, `compatibility`, `minimum-version`,
  `dependency-audit`, `hassfest`, `hacs`, and `analyze`. These are all CI and
  CodeQL jobs, including the browser tests and both older Home Assistant
  versions.
- Independently of these settings, the tag-triggered release workflow runs
  the browser tests, both older Home Assistant versions and the source checks
  again, and publishes only when all of them pass.
- Branches must be current before merging.
- Review conversations must be resolved.
- Force pushes and branch deletion are disabled.
- Linear history is required.

## Pull requests

- Squash merge is the only enabled merge method.
- Head branches are deleted after merge.
- CODEOWNERS assigns the repository to `@SoftwareSchmied`.

## Security

- Secret scanning and push protection are enabled.
- Dependabot alerts and security updates are enabled.
- CodeQL and `pip-audit` run in CI.
- Private vulnerability reporting is enabled.
- Third-party actions are pinned to full commit hashes and updated by
  Dependabot.
