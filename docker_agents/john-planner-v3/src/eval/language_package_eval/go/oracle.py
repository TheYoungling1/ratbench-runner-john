#!/usr/bin/env python3
"""Gold closures for the Go package-layer eval, inside a golang container.
Deterministic — NO agent (the toolchain emits the build list directly). Docker-gated
(spec §5). Two oracles:
  * oracle_closure  — BUILD LIST via `go list -m -json all` (manifest-only); `-mod=mod`
    normally, no `-mod` flag in a `go.work` workspace (`-mod=vendor` can't compute 'all').
  * oracle_loadset  — PACKAGE-LOADING set via `go list -deps -json ./...` (needs SOURCE).

Usage:
    GO_ORACLE_DOCKER=1 python3 -m src.eval.language_package_eval.go.oracle <repo,...> <out_dir>
Emits per-repo ``<repo>.json`` = {"installed": {module: version}, "repo": name} (build list).
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[4]))  # repo root

SMOKE = pathlib.Path(
    os.environ.get("GO_SMOKE_ROOT", "outputs/graph_fidelity/_smoke_go")
)
GO_IMAGE = os.environ.get("GO_IMAGE", "golang:1.22")


def parse_go_list_json(output: str) -> dict[str, str]:
    """Parse the ``go list -m -json all`` object STREAM -> {module: version}.
    Skips ``Main: true`` objects (main / workspace-member modules, which have no
    Version); a ``Replace`` keys the ORIGINAL Path with the replacement Version."""
    decoder = json.JSONDecoder()
    result: dict[str, str] = {}
    idx, n = 0, len(output)
    while idx < n:
        while idx < n and output[idx].isspace():
            idx += 1
        if idx >= n:
            break
        obj, idx = decoder.raw_decode(output, idx)
        if obj.get("Main"):
            continue
        path, ver = obj.get("Path"), obj.get("Version")
        repl = obj.get("Replace")
        if repl:
            ver = repl.get("Version", ver)
        if path and ver:
            result[path] = ver
    return result


def _docker_go(repo: pathlib.Path, go_image: str, *args: str) -> str:
    cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{repo}:/src",
        "-w",
        "/src",
        go_image,
        "go",
        *args,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def oracle_closure(
    repo_dir, *, go_image: str = GO_IMAGE, vendored: bool = False
) -> dict[str, str]:
    """BUILD LIST via ``go list -m -json all``. ``-mod=vendor`` cannot compute 'all'
    (go: can't compute 'all' using the vendor directory), so it is never used here
    even when vendored — ``-mod=mod`` gives the authoritative full build list instead.
    In a ``go.work`` workspace, ``-mod`` may only be ``readonly`` or ``vendor``, so we
    pass no ``-mod`` flag at all and rely on Go's default (readonly). The ``vendored``
    parameter is kept for signature compatibility but no longer selects the mode here.
    """
    repo = pathlib.Path(repo_dir).resolve()
    args = ["list", "-m", "-json", "all"]
    if not (repo / "go.work").is_file():
        args.insert(1, "-mod=mod")
    return parse_go_list_json(_docker_go(repo, go_image, *args))


def oracle_loadset(repo_dir, *, go_image: str = GO_IMAGE) -> dict[str, str]:
    """PACKAGE-LOADING set: modules that provide packages the main module's packages
    import, via ``go list -deps -json ./...``. Needs the repo SOURCE (NOT manifest-
    only) — run on a full clone of the anchor (spec §2). Std-lib packages have no
    ``Module`` and are skipped."""
    repo = pathlib.Path(repo_dir).resolve()
    out = _docker_go(repo, go_image, "list", "-deps", "-json", "./...")
    decoder = json.JSONDecoder()
    result: dict[str, str] = {}
    idx, n = 0, len(out)
    while idx < n:
        while idx < n and out[idx].isspace():
            idx += 1
        if idx >= n:
            break
        obj, idx = decoder.raw_decode(out, idx)
        mod = obj.get("Module")
        if mod and not mod.get("Main"):
            path, ver = mod.get("Path"), mod.get("Version")
            if path and ver:
                result[path] = ver
    return result


def main() -> int:
    repos = sys.argv[1].split(",") if len(sys.argv) > 1 else []
    out = (
        pathlib.Path(sys.argv[2])
        if len(sys.argv) > 2
        else pathlib.Path("/tmp/oracle_go")
    )
    out.mkdir(parents=True, exist_ok=True)
    for name in repos:
        repo_dir = SMOKE / name
        vendored = (repo_dir / "vendor" / "modules.txt").is_file()
        rec = {"repo": name}
        try:
            rec["installed"] = oracle_closure(repo_dir, vendored=vendored)
            print(f"OK {name}: {len(rec['installed'])} modules (vendored={vendored})")
        except subprocess.CalledProcessError as exc:
            rec["error"] = (exc.stderr or "").strip()[:500]
            print(f"ERR {name}: {rec['error']}")
        (out / f"{name}.json").write_text(
            json.dumps(rec, indent=2, sort_keys=True) + "\n"
        )
    print(f"DONE oracle(go) -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
