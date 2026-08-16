# Contributing

Keep changes small, reviewable, and backed by fixtures or test systems. Broad
endpoint coverage is not a goal on its own.

## Before opening a pull request

1. Open or reference an issue for user-visible behavior.
2. Keep one risk class per pull request: authentication, device support,
   writing, energy semantics, or architecture/migration.
3. Add tests for success, missing data, malformed data, and relevant HTTP
   failures.
4. Add German and English user-facing text.
5. Update documentation and the changelog when users are affected.
6. Run `ruff format --check .`, `ruff check .`, `mypy`, and `pytest`.

All changes to `main` go through a pull request. Required checks must pass and
all review conversations must be resolved before squash-merging.

## Definition of done

A capability must have documented semantics, dynamic presence checks, strict
known-field validation, stable identity, correct Home Assistant metadata,
redacted diagnostics, a measured cloud-load impact, and fixture-backed tests.
Writable capabilities additionally require value validation and read-back.

## Real-device fixtures

Only anonymized structural fixtures belong in Git. Follow
[the privacy and fixture policy](docs/privacy-and-fixtures.md). If uncertain,
do not upload the data; contact the maintainer privately first.

## Source and license provenance

This project is an independent implementation. A pull request that adapts code
from another project must identify the source, exact scope, commit, and license
and preserve every required notice. Do not submit proprietary application
files, certificates, logos, secrets, or decompiled code.
