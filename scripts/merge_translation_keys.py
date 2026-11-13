"""Merge two versions of a translation file key by key (``strings.json``,
``translations/en.json``, ``translations/de.json``).

Work packages add keys to the same files in parallel; a text merge of the
JSON then conflicts. This deep merge keeps the order of the base file and
adds the keys of the other file in their own order, each after the key it
follows there. A key with different texts in both files is a conflict and
stops the merge, unless ``--prefer`` names the side that wins.

    python scripts/merge_translation_keys.py BASE OTHER [-o OUTPUT]
    python scripts/merge_translation_keys.py --git-conflict FILE

With ``--git-conflict`` the two sides come from the index stages of a file in
a merge conflict (``:2:`` ours, ``:3:`` theirs) and the result replaces the
file. Output is JSON with two-space indentation, as Home Assistant writes it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class MergeConflict(ValueError):
    """Both sides changed the text of one key differently."""


def merge(
    base: Mapping[str, Any],
    other: Mapping[str, Any],
    *,
    prefer: str | None = None,
    path: str = "",
) -> dict[str, Any]:
    """A deep merge in stable order: base order, new keys in other's order.

    A new key follows the key it follows in ``other`` (after keys only the
    base has there), or comes first when it comes first there.
    """
    result: dict[str, Any] = {}
    for key, value in base.items():
        where = f"{path}{key}"
        if key not in other:
            result[key] = value
            continue
        theirs = other[key]
        if isinstance(value, Mapping) and isinstance(theirs, Mapping):
            result[key] = merge(value, theirs, prefer=prefer, path=f"{where}.")
        elif value == theirs or prefer == "base":
            result[key] = value
        elif prefer == "other":
            result[key] = theirs
        else:
            raise MergeConflict(where)
    order = list(result)
    previous: str | None = None
    for key, value in other.items():
        if key not in result:
            position = 0 if previous is None else order.index(previous) + 1
            # After the base's own keys that follow the same neighbour, so a
            # third merge keeps the order of the first two.
            while position < len(order) and order[position] not in other:
                position += 1
            order.insert(position, key)
            result[key] = value
        previous = key
    return {key: result[key] for key in order}


def _load(text: str) -> dict[str, Any]:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("A translation file is a JSON object")
    return data


def dumps(data: Mapping[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _git_stage(stage: int, file: Path) -> str:
    return subprocess.run(
        ["git", "show", f":{stage}:{file.as_posix()}"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--prefer", choices=("base", "other"))
    parser.add_argument("--git-conflict", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.git_conflict:
            (file,) = args.files
            merged = merge(
                _load(_git_stage(2, file)),
                _load(_git_stage(3, file)),
                prefer=args.prefer,
            )
            file.write_text(dumps(merged), encoding="utf-8", newline="\n")
            return 0
        base_file, other_file = args.files
        merged = merge(
            _load(base_file.read_text(encoding="utf-8")),
            _load(other_file.read_text(encoding="utf-8")),
            prefer=args.prefer,
        )
    except MergeConflict as err:
        print(f"Conflicting texts for {err}", file=sys.stderr)
        return 1
    except ValueError as err:
        print(f"Cannot merge: {err}", file=sys.stderr)
        return 2
    if args.output is None:
        sys.stdout.write(dumps(merged))
    else:
        args.output.write_text(dumps(merged), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
