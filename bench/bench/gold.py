# bench/gold.py
#
# ============================ DEPRECATED / DORMANT (2026-07-14) ============================
# The gold-anchored metric is NO LONGER USED by the runner. compute_metrics() does not call
# gold_coverage() (the import + call are commented out in metrics.py) and unified_bench forces
# gold=None. The active headline metrics are EBSR (Repo2Run-exact collect-only gate) and ESSR
# (RAT-exact passed/(total-skipped) ÷exec). This module is kept for reference only; do NOT
# re-enable without reinstating a pinned gold JSON and resolving the node-id-form contract.
# ==========================================================================================
#
# Gold-anchored EBSR/ESSR: re-anchor the denominator from each agent's own (floating)
# pytest collection to a FIXED, independently-certified set of pytest node-ids, so a
# broken environment that collects fewer tests can no longer inflate its score.
#
# Consumes `rat_python50_gold.json` (schema in
# docs/superpowers/handoffs/2026-07-12-gold-set-schema-for-essr-ebsr.md). The gold set
# supplies, per CERTIFIED repo: the fixed denominator D_r = |node_ids| and the reference
# set G_r to intersect against. Two coverage numerators, both over D_r:
#   C (collection coverage, EBSR-style) = |collected(A,r) ∩ G_r| / D_r
#   P (pass coverage,       ESSR-style) = |passed(A,r)    ∩ G_r| / D_r
#
# NODE-ID FORM: gold ids and pytest `--co` collected ids are PATH-based
# (tests/x.py::Cls::t). JUnit ids are classname-based (tests.x.Cls::t). These never
# intersect, so `junit_ids_to_paths` translates JUnit outcomes back to path form using
# the collected path list as the authority (see bench/measure.py).
from __future__ import annotations

import json
from dataclasses import dataclass


def _r(x: float) -> float:
    return round(x, 4)


# --- node-id normalization / form translation ---------------------------------------------

def norm(nid: str) -> str:
    """Strip leading src-layout prefixes so `src/pkg/test.py::t` matches `pkg/test.py::t`.

    Mirrors tier1b.norm exactly; apply identically to gold AND measured node-ids.
    """
    nid = nid.strip()
    for p in ("/src/", "./", "src/"):
        if nid.startswith(p):
            nid = nid[len(p):]
    return nid


def path_to_junit_key(path_nodeid: str) -> str:
    """Deterministic path-form node-id -> JUnit `classname::name` key.

    `a/b/c.py::Cls::t` -> `a.b.c.Cls::t`;  `a/b.py::t` -> `a.b::t`.
    The only information JUnit erases is the `::`-vs-`.` boundary between file, class,
    and test — which the path form preserves, so the forward direction is lossless.
    """
    parts = path_nodeid.split("::")
    if len(parts) < 2:
        return path_nodeid
    filep = parts[0]
    mod = (filep[:-3] if filep.endswith(".py") else filep).replace("/", ".")
    middle = parts[1:-1]          # class chain (usually 0 or 1 segments)
    name = parts[-1]
    classname = ".".join([mod, *middle]) if middle else mod
    return f"{classname}::{name}"


def junit_ids_to_paths(junit_ids, collected_paths) -> tuple:
    """Translate JUnit `classname::name` ids back to path form via the collected list.

    The collected path list is authoritative: we key it by `path_to_junit_key` and look
    each JUnit id up. Ids with no collected match (should not happen for a real run) are
    kept verbatim — they simply won't intersect gold.
    """
    key_to_path: dict = {}
    for p in collected_paths:
        key_to_path.setdefault(path_to_junit_key(p), p)
    return tuple(key_to_path.get(j, j) for j in junit_ids)


# --- gold set loading ----------------------------------------------------------------------

@dataclass(frozen=True)
class GoldRepo:
    full_name: str
    sha: str                  # lowercased 40-hex pinned commit (SHA-alignment key)
    status: str               # "CERTIFIED" | "REJECTED" | "ERROR"
    node_ids: frozenset       # NORMED, path-based; empty unless CERTIFIED
    manifest_size: int


def load_gold(source) -> dict:
    """Parse `rat_python50_gold.json` (path or already-parsed dict) into
    {repo_lower: GoldRepo}. Node-ids are normed; keys and shas are lowercased.
    """
    if isinstance(source, str):
        with open(source) as f:
            data = json.load(f)
    else:
        data = source
    repos_raw = data.get("repos", data)   # tolerate a bare repos map
    out: dict = {}
    for key, v in repos_raw.items():
        full = v.get("full_name", key)
        ids = v.get("node_ids") or []
        out[full.lower()] = GoldRepo(
            full_name=full,
            sha=(v.get("sha") or "").lower(),
            status=v.get("status", ""),
            node_ids=frozenset(norm(x) for x in ids),
            manifest_size=v.get("manifest_size", len(ids)),
        )
    return out


# --- the metric ----------------------------------------------------------------------------

def gold_coverage(rows: list, gold_map: dict) -> dict:
    """Gold-anchored collection (C) and pass (P) coverage for one agent's rows.

    Iterates the repos the agent actually ran. A repo is scored (included) only when its
    gold entry is CERTIFIED and the row's head_sha matches the pinned gold sha; a broken
    env that built nothing still scores 0 (that is the point) rather than being dropped.
    Every non-scored repo is reported in `gold_excluded` with a reason — no silent caps.
    """
    included: list = []
    excluded: list = []
    for r in rows:
        g = gold_map.get(r.repo.lower())
        if g is None:
            excluded.append((r.repo, "gold_missing"))
            continue
        if g.status != "CERTIFIED" or not g.node_ids:
            # PENDING = gold still building (partial file), will certify on regen; distinct from
            # terminally REJECTED/ERROR so the excluded log makes the difference visible.
            excluded.append((r.repo, "gold_pending" if g.status == "PENDING" else "gold_not_certified"))
            continue
        row_sha = (r.meta.get("head_sha") or "").lower()
        if not row_sha or row_sha != g.sha:
            excluded.append((r.repo, "sha_misaligned"))
            continue
        D = len(g.node_ids)
        coll_hit = len({norm(x) for x in r.collected_node_ids} & g.node_ids)
        pass_hit = len({norm(x) for x in r.passed_node_ids} & g.node_ids)
        included.append({
            "repo": r.repo, "sha": g.sha, "D": D,
            "collected_hit": coll_hit, "passed_hit": pass_hit,
            "C": _r(coll_hit / D), "P": _r(pass_hit / D),
        })

    cs = [d["C"] for d in included]
    ps = [d["P"] for d in included]
    return {
        # HEADLINE (gold-anchored, fixed denominator). EBSR_improved = collection coverage C;
        # ESSR_improved = pass coverage P. compute_metrics still emits the legacy floating
        # EBSR/ESSR_all/ESSR_exec alongside these for comparison — these are the ones to report.
        "EBSR_improved": _r(sum(cs) / len(cs)) if cs else 0.0,
        "ESSR_improved": _r(sum(ps) / len(ps)) if ps else 0.0,
        "n_gold_included": len(included),
        "n_gold_excluded": len(excluded),
        "gold_breakdown": included,
        "gold_excluded": [{"repo": rp, "reason": rs} for rp, rs in excluded],
    }
