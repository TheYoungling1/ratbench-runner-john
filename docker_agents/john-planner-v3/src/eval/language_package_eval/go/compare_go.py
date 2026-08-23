"""Recall/precision + divergence buckets for the Go package-layer eval. OURS
(offline require-block closure) is a SUBSET of the build-list oracle by Go pruning
semantics (spec §0.1), so recall is the story. A second, optional package-loading
oracle splits build-list misses into expected `pruned_superset` vs real
`recall_defect`. Mirrors ``compare_node.py``."""

from __future__ import annotations


def score_repo(ours: dict, oracle: dict, oracle_loadset: dict | None = None) -> dict:
    replace_local = set(ours.get("replace_local", []))
    o_pkgs = {k: v for k, v in ours["packages"].items() if k not in replace_local}
    g_pkgs = {k: v for k, v in oracle["installed"].items() if k not in replace_local}
    ours_keys, build_keys = set(o_pkgs), set(g_pkgs)
    load_keys = (
        {k for k in oracle_loadset["installed"] if k not in replace_local}
        if oracle_loadset is not None
        else None
    )

    if ours.get("resolve_required"):
        # Whole oracle is a KNOWN offline limitation, not a recall defect (spec §6).
        return {
            "recall_buildlist": None,
            "recall_loadset": None,
            "precision": None,
            "resolve_required": True,
            "missing": [],
            "pruned_superset": None,
            "recall_defect": None,
            "extra": [],
            "replace_local": sorted(replace_local),
            "resolve_required_missing": sorted(build_keys),
            "vexact": 0,
        }

    inter = ours_keys & build_keys
    missing = build_keys - ours_keys
    result = {
        "recall_buildlist": len(inter) / len(build_keys) if build_keys else 1.0,
        "recall_loadset": None,
        "precision": len(inter) / len(ours_keys) if ours_keys else 1.0,
        "resolve_required": False,
        "missing": sorted(missing),
        "pruned_superset": None,
        "recall_defect": None,
        "extra": sorted(ours_keys - build_keys),
        "replace_local": sorted(replace_local),
        "vexact": sum(1 for k in inter if o_pkgs[k] == g_pkgs[k]),
    }
    if load_keys is not None:
        load_inter = ours_keys & load_keys
        result["recall_loadset"] = (
            len(load_inter) / len(load_keys) if load_keys else 1.0
        )
        # A build-list miss that IS in the load-set = a real recall defect (our parser
        # should have found it). One NOT in the load-set = expected pruned superset.
        result["recall_defect"] = sorted(m for m in missing if m in load_keys)
        result["pruned_superset"] = sorted(m for m in missing if m not in load_keys)
    return result
