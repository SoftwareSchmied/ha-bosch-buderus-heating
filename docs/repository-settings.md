# Repository settings

The GitHub settings are part of the project baseline and should match this
document.

## Main branch

- Changes go through pull requests.
- Required checks: `quality`, `dependency-audit`, `hassfest`, `hacs`, and
  `analyze`.
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
