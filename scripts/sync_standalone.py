#!/usr/bin/env python3
"""
Keep standalone/mangadex_sync.py in sync with the canonical source,
pypi pkg/mangadex_sync/app.py.

The two files used to be maintained as separate copies and drifted apart
easily (e.g. a bug fix landing in one but not the other). This script makes
`pypi pkg/mangadex_sync/app.py` the single source of truth: the standalone
script stays a single, dependency-light file (no `mangadex_sync` package
import needed, so "just run this one file" keeps working), but its content
is generated instead of hand-edited. app.py is already directly runnable
(it has its own `if __name__ == "__main__":` block), so this is a copy
with a small generated-file notice inserted after the shebang.

Usage:
    python scripts/sync_standalone.py          # copy app.py -> standalone script
    python scripts/sync_standalone.py --check  # exit 1 if they've diverged, no write
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "pypi pkg" / "mangadex_sync" / "app.py"
TARGET = ROOT / "standalone" / "mangadex_sync.py"

NOTICE = (
    "# This file is generated from `pypi pkg/mangadex_sync/app.py` by\n"
    "# scripts/sync_standalone.py — do not edit it directly.\n"
    "# Edit the source file and re-run that script instead.\n"
)


def build_target_content() -> str:
    source = SOURCE.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    if lines and lines[0].startswith("#!"):
        shebang, rest = lines[0], "".join(lines[1:])
    else:
        shebang, rest = "", "".join(lines)
    return shebang + NOTICE + rest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                         help="Don't write; exit 1 if the standalone file is out of date.")
    args = parser.parse_args()

    if not SOURCE.exists():
        print(f"Source file not found: {SOURCE}", file=sys.stderr)
        return 2

    new_content = build_target_content()

    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else None
        if current != new_content:
            print(f"OUT OF DATE: {TARGET} does not match {SOURCE}.\n"
                  f"Run `python scripts/sync_standalone.py` to regenerate it.",
                  file=sys.stderr)
            return 1
        print("OK: standalone script is in sync.")
        return 0

    TARGET.write_text(new_content, encoding="utf-8")
    print(f"Wrote {TARGET} from {SOURCE}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
