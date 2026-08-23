#!/usr/bin/env python3
"""Backfill `test_count` into a Python dataset, matching the field rat_rust50/java50 carry.

The Rust and Java counts are STATIC declaration counts (`#[test]` / `@Test` occurrences) taken at
curation time, not pytest collections — deliberately, so the number is a property of the repo at
its pinned commit rather than of some environment that happened to build. The Python equivalent is
`def test_*` / `async def test_*` in files pytest would collect by default (`test_*.py`,
`*_test.py`). Class-based unittest methods are the same `def test_*` line, so one pattern covers
both.

Like the Rust/Java numbers this is a LOWER BOUND: `@pytest.mark.parametrize`, generated tests, and
`load_tests` expand at runtime and are invisible here. It only ever under-counts.

USAGE
    python3 tools/count_python_tests.py [datasets/rat_python50.json] [--apply] [--jobs N]
    Dry-run by default: prints the counts and exits without writing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

_TEST_DEF = re.compile(rb"^\s*(?:async\s+)?def\s+test", re.M)
_TEST_FILE = re.compile(r"^test_.*\.py$|^.*_test\.py$")
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", ".tox", "build", "dist", "__pycache__"}


def _clone(url: str, commit: str | None, dest: str) -> None:
    """Shallow clone at the row's pinned commit — same pin protocol as producers/repo2run.py."""
    subprocess.run(["git", "clone", "--quiet", "--depth=1", url, dest], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if commit:
        subprocess.run(["git", "fetch", "--quiet", "--depth", "1", "origin", commit], cwd=dest,
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "checkout", "--quiet", "--detach", commit], cwd=dest, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def count_tree(root: str) -> int:
    """`def test_*` declarations across every pytest-collectable file under `root`."""
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if not _TEST_FILE.match(fn):
                continue
            try:
                with open(os.path.join(dirpath, fn), "rb") as fh:
                    n += len(_TEST_DEF.findall(fh.read()))
            except OSError:
                pass                          # unreadable file counts as zero, never as a crash
    return n


def count_repo(row: dict) -> int | None:
    """Clone-count-delete one row. None means the clone/checkout failed — never a silent 0."""
    with tempfile.TemporaryDirectory(prefix="tc-") as tmp:
        dest = os.path.join(tmp, "repo")
        try:
            _clone(row["clone_url"], row.get("commit"), dest)
        except subprocess.CalledProcessError:
            return None
        return count_tree(dest)


def main(argv: list) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset", nargs="?", default="datasets/rat_python50.json")
    ap.add_argument("--apply", action="store_true", help="write test_count back into the dataset")
    ap.add_argument("--jobs", type=int, default=4)
    args = ap.parse_args(argv)

    with open(args.dataset, encoding="utf-8") as fh:
        rows = json.load(fh)

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        counts = list(pool.map(count_repo, rows))

    failed = 0
    for row, n in zip(rows, counts):
        print(f"  {row['full_name']:<40} {'FAILED' if n is None else n}")
        if n is None:
            failed += 1
        else:
            row["test_count"] = n

    ok = [n for n in counts if n is not None]
    print(f"\n{len(ok)}/{len(rows)} counted, {failed} failed, "
          f"min={min(ok, default=0)} median={sorted(ok)[len(ok) // 2] if ok else 0} "
          f"max={max(ok, default=0)}")
    if not args.apply:
        print("dry run — pass --apply to write")
        return 0
    if failed:
        print("refusing to write a partial backfill: re-run and fix the failures first")
        return 1
    with open(args.dataset, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2)
        fh.write("\n")
    print(f"wrote test_count into {args.dataset}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
