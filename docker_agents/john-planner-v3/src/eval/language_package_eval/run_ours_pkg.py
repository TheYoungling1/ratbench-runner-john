#!/usr/bin/env python3
"""OUR side of the package-layer eval: run construction-only build_dep_graph on
each repo and emit the PACKAGE-tier closure {name: version} + honest flags.
No agent, no repair loop (the TEST goal's pytest run is intercepted).
"""
import json
import os
import pathlib
import sys
import traceback

sys.path.insert(0, "src")
sys.path.insert(0, ".")

from src.eval.language_package_eval.coverage import (  # noqa: E402
    base_image_for_repo,
    build_graph_construction_only,
)
from python_deps.depgraph.schema import NodeType  # noqa: E402

SMOKE = pathlib.Path(os.environ.get("OURS_SMOKE_ROOT", "outputs/graph_fidelity/_smoke"))
OUT = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else pathlib.Path("/tmp/ours")
OUT.mkdir(parents=True, exist_ok=True)

# dir-name -> full label
REPOS = sys.argv[1].split(",")

# optional per-repo target-python override: OURS_PY_OVERRIDE="repo:minor,repo:minor"
# (for apps that declare no requires-python but pin a py-floored dep, e.g. Django 6 -> 3.12)
_PY_OVERRIDE = dict(
    kv.split(":") for kv in os.environ.get("OURS_PY_OVERRIDE", "").split(",") if ":" in kv
)


def extract(graph) -> dict:
    packages, audit = {}, []
    for n in graph.nodes:
        if n.type is NodeType.PACKAGE:
            packages[n.name] = n.version
            if getattr(n.discovered_by, "value", "") == "audit":
                audit.append(n.name)
    runtime = sorted(n.version for n in graph.nodes if n.type is NodeType.RUNTIME and n.version)
    unresolved, unresolved_runtime = [], []
    for n in graph.nodes:
        if n.type is NodeType.IMPORT:
            if n.data.get("unresolved") is True:
                unresolved.append(n.name)
            if n.data.get("unresolved_runtime") is True:
                unresolved_runtime.append(n.name)
    return {
        "packages": packages,  # {dist: version}
        "package_count": len(packages),
        "audit_repaired": sorted(audit),
        "runtime_python": runtime,
        "unresolved_imports": sorted(unresolved),
        "unresolved_runtime_imports": sorted(unresolved_runtime),
    }


def main() -> int:
    for name in REPOS:
        repo_dir = SMOKE / name
        rec = {"repo_dir": name}
        try:
            image, minor, reason = base_image_for_repo(str(repo_dir))
            if name in _PY_OVERRIDE:
                minor = _PY_OVERRIDE[name]
                image = f"python:{minor}-slim"
            rec["base_image"] = image
            rec["target_python"] = minor
            graph = build_graph_construction_only(str(repo_dir), image, minor)
            rec.update(extract(graph))
            print(f"OK {name}: {rec['package_count']} pkgs, py{minor}, "
                  f"audit={len(rec['audit_repaired'])}, unresolved={len(rec['unresolved_imports'])}")
        except Exception as exc:  # noqa: BLE001
            rec["error"] = f"{type(exc).__name__}: {exc}"
            rec["traceback"] = traceback.format_exc()[-1500:]
            print(f"ERR {name}: {rec['error']}")
        (OUT / f"{name}.json").write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n")
    print(f"DONE ours -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
