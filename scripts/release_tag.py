"""Check that a pushed tag may publish a release and derive its release type."""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass

_TAG = re.compile(
    r"^v(?P<version>\d+\.\d+\.\d+(?P<pre>-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?)$"
)
# Results of GitHub's comparison of ``main`` with the tagged commit in which
# the tagged commit is part of ``main``.
_ON_MAIN = frozenset({"identical", "behind"})


@dataclass(frozen=True)
class ReleaseTag:
    """A tag that passed the publication checks."""

    version: str
    prerelease: bool


def check_release_tag(tag: str, *, object_type: str, main_status: str) -> ReleaseTag:
    """Return the release described by ``tag`` or explain why it must not publish.

    ``object_type`` is the type of the object the tag reference points to:
    ``tag`` for an annotated tag, ``commit`` for a lightweight one.
    ``main_status`` is the status of comparing ``main`` with the tagged commit.
    Every release needs an annotated tag. A stable release must also come from
    a commit on ``main``; pre-releases may come from a feature branch.
    """
    match = _TAG.fullmatch(tag)
    if match is None:
        raise ValueError(f"tag {tag!r} is not a release tag such as v1.2.3")
    if object_type != "tag":
        raise ValueError(
            f"tag {tag!r} is not annotated (it points to a {object_type or 'unknown'}"
            " object); create it with git tag -a"
        )
    prerelease = match.group("pre") is not None
    if not prerelease and main_status not in _ON_MAIN:
        raise ValueError(
            f"stable tag {tag!r} does not point to a commit on main "
            f"(comparison status {main_status or 'unknown'!r})"
        )
    return ReleaseTag(match.group("version"), prerelease)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--object-type", required=True)
    parser.add_argument("--main-status", required=True)
    return parser


def main() -> int:
    """Print the release type as workflow outputs, or fail with the reason."""
    args = _parser().parse_args()
    try:
        release = check_release_tag(
            args.tag, object_type=args.object_type, main_status=args.main_status
        )
    except ValueError as error:
        raise SystemExit(f"error: {error}") from None
    print(f"version={release.version}")
    print(f"prerelease={'true' if release.prerelease else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
