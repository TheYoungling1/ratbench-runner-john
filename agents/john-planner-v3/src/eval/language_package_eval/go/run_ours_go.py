#!/usr/bin/env python3
"""OUR side of the Go package-layer eval: parse go.mod(>=1.17)/vendor/go.work into
the offline module build-list closure. Pure text parse: no toolchain, no network
(spec §4). Mirrors ``run_ours_node.py``.

Usage:
    python3 -m src.eval.language_package_eval.go.run_ours_go <repo,repo,...> <out_dir>
where each repo is a subdir of GO_SMOKE_ROOT holding a committed go.mod (+ go.sum,
optionally vendor/modules.txt or go.work). Emits per-repo ``<repo>.json``.

Env knobs: GO_SMOKE_ROOT (corpus dir), GO_TARGET="goos,goarch" (default "linux,amd64").
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[4]))  # repo root

from src.eval.language_package_eval.go.gomod import (
    module_closure,
    parse_go_mod,
)  # noqa: E402

SMOKE = pathlib.Path(
    os.environ.get("GO_SMOKE_ROOT", "outputs/graph_fidelity/_smoke_go")
)
DEFAULT_TARGET = {"goos": "linux", "goarch": "amd64"}


def _target() -> dict[str, str]:
    raw = os.environ.get("GO_TARGET", "")
    if raw and len(raw.split(",")) == 2:
        goos, goarch = raw.split(",")
        return {"goos": goos, "goarch": goarch}
    return dict(DEFAULT_TARGET)


def _project_name(repo_dir: pathlib.Path) -> str | None:
    gomod = repo_dir / "go.mod"
    if gomod.is_file():
        return parse_go_mod(gomod).module_path or None
    work = repo_dir / "go.work"
    return "<workspace>" if work.is_file() else None


def ours_for_repo(repo_dir: str | pathlib.Path, target: dict | None = None) -> dict:
    """Construction-only OURS closure for one Go repo: {module: version}, offline."""
    repo_dir = pathlib.Path(repo_dir)
    c = module_closure(repo_dir)
    return {
        "packages": dict(c.packages),
        "package_count": len(c.packages),
        "closure_source": c.source,
        "go_version": c.go_version,
        "toolchain": c.toolchain,
        "direct_count": c.direct,
        "indirect_count": c.indirect,
        "replace_local": list(c.replace_local),
        "resolve_required": c.resolve_required,
        "target": target or _target(),
        "project": _project_name(repo_dir),
    }


def main() -> int:
    repos = sys.argv[1].split(",") if len(sys.argv) > 1 else []
    out = (
        pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else pathlib.Path("/tmp/ours_go")
    )
    out.mkdir(parents=True, exist_ok=True)
    target = _target()
    for name in repos:
        repo_dir = SMOKE / name
        rec = {"repo_dir": name}
        try:
            rec.update(ours_for_repo(repo_dir, target))
            print(
                f"OK {name}: {rec['package_count']} modules ({rec['closure_source']})"
            )
        except Exception as exc:  # noqa: BLE001
            rec["error"] = f"{type(exc).__name__}: {exc}"
            print(f"ERR {name}: {rec['error']}")
        (out / f"{name}.json").write_text(
            json.dumps(rec, indent=2, sort_keys=True) + "\n"
        )
    print(f"DONE ours(go) -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
