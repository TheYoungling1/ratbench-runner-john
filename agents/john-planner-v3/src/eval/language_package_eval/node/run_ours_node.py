#!/usr/bin/env python3
"""OUR side of the Node package-layer eval: parse ``package-lock.json`` and
platform-filter it to the target triple's install subset — the LOCK-mode mirror
of ``run_ours_pkg.py``. Pure lockfile parse: no container, no network (§3.3).

Usage:
    python3 -m src.eval.language_package_eval.node.run_ours_node <repo,repo,...> <out_dir>
where each repo is a subdir of NODE_SMOKE_ROOT holding a committed
``package-lock.json`` + ``package.json``. Emits per-repo ``<repo>.json``.

Env knobs (mirror run_ours_pkg): NODE_SMOKE_ROOT (corpus dir),
NODE_TARGET="os,cpu,libc" (default "linux,x64,glibc").
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

# make the package importable whether run as a module or a script
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[4]))  # repo root

from src.eval.language_package_eval.node.lockfile import parse_lockfile_graph  # noqa: E402
from src.eval.language_package_eval.node.platform_filter import (  # noqa: E402
    DEFAULT_TARGET,
    installed_closure,
    keep,
)

SMOKE = pathlib.Path(os.environ.get("NODE_SMOKE_ROOT", "outputs/graph_fidelity/_smoke_node"))


def _target() -> dict[str, str]:
    raw = os.environ.get("NODE_TARGET", "")
    if raw and len(raw.split(",")) == 3:
        o, c, lc = raw.split(",")
        return {"os": o, "cpu": c, "libc": lc}
    return dict(DEFAULT_TARGET)


def _project_name(repo_dir: pathlib.Path) -> str | None:
    pj = repo_dir / "package.json"
    if pj.exists():
        try:
            return json.loads(pj.read_text()).get("name")
        except Exception:  # noqa: BLE001
            return None
    return None


def ours_for_repo(repo_dir: str | pathlib.Path, target: dict[str, str] | None = None) -> dict:
    """Construction-only OURS closure for one Node repo: {name: version}, filtered."""
    repo_dir = pathlib.Path(repo_dir)
    target = target or _target()
    own = _project_name(repo_dir)
    all_pkgs, root_names = parse_lockfile_graph(repo_dir / "package-lock.json")
    unfiltered = {p.name: p.version for p in all_pkgs if p.name != own}
    kept = installed_closure(all_pkgs, root_names, target)          # graph reachability, not per-node
    packages = {p.name: p.version for p in kept if p.name != own}
    # PLATFORM drops only: a package excluded because its OWN os/cpu/libc rejects the
    # target. A workspace member (or a member's dep) omitted for unreachability is NOT
    # platform-dropped — with correct workspace seeding it is now reached, and anything
    # still excluded here is a genuine platform-optional (the other-arch native binaries).
    dropped = sorted({p.name for p in all_pkgs if p.name != own and not keep(p, target)})
    return {
        "packages": packages,                          # honest OURS: target-install subset
        "packages_unfiltered": unfiltered,             # diagnostic: whole lockfile
        "package_count": len(packages),
        "unfiltered_count": len(unfiltered),
        "platform_dropped": dropped,                   # excluded by their own os/cpu/libc
        "platform_dropped_count": len(dropped),
        "target": target,
        "project": own,
        # per-package platform constraints (feeds the comparer's platform_optional_extra bucket)
        "constraints": {
            p.name: {"os": list(p.os), "cpu": list(p.cpu), "libc": list(p.libc)}
            for p in all_pkgs
            if (p.os or p.cpu or p.libc) and p.name != own
        },
    }


def main() -> int:
    repos = sys.argv[1].split(",") if len(sys.argv) > 1 else []
    out = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else pathlib.Path("/tmp/ours_node")
    out.mkdir(parents=True, exist_ok=True)
    target = _target()
    for name in repos:
        repo_dir = SMOKE / name
        rec = {"repo_dir": name}
        try:
            rec.update(ours_for_repo(repo_dir, target))
            print(f"OK {name}: {rec['package_count']} kept / {rec['unfiltered_count']} lock "
                  f"(dropped {rec['platform_dropped_count']} platform-optional)")
        except Exception as exc:  # noqa: BLE001
            rec["error"] = f"{type(exc).__name__}: {exc}"
            print(f"ERR {name}: {rec['error']}")
        (out / f"{name}.json").write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n")
    print(f"DONE ours(node) -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
