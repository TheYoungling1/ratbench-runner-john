"""A/B eval — pkg_layer contract-only roots (NEW) vs depgraph roots (CURRENT).

Task 7 of the pkg_layer four-plane build. Reuses the Track A
(``root_selection_ab.py``) scoring shape (``adjudicate_divergence`` /
``aggregate``) and the hand-labeled ``ab_gold_labels.AB_GOLD`` corpus, so the
two A/B evals share one adjudication vocabulary and one verdict grammar.

  * NEW roots     = ``pkg_layer.contract.select_roots(read_contract(repo), frozenset())``
                    -- contract-only, no import ever consulted (Task 3); this
                    is the "verifier" design.
  * CURRENT roots = the RECONSTRUCTED generator: the in-tree declared-only
                    ``depgraph.roots.select_roots`` UNION the ``naming.package_roots``
                    scan-gap-fill P0.1 deleted from it (see
                    ``root_selection_ab.reconstruct_generator_roots``). Post-P0.1
                    ``select_roots`` alone is declared-only, so without this
                    re-anchor CURRENT would collapse onto NEW and hide the win.
  * DIVERGENCE    = packages CURRENT has that NEW omits, adjudicated against
                    AB_GOLD exactly like Track A's generator/verifier split.

Also reports the NEW builder's own ``Alignment.under_declared`` count per
repo (``pkg_layer.construct.build_package_layer``) -- the honest completeness
signal the contract-only design relies on INSTEAD OF import-driven root
gap-fill.

The pure function here (``build_divergence``; ``adjudicate_divergence`` /
``aggregate`` reused from ``root_selection_ab``) imports nothing from
``python_deps``, so its unit tests stay fast and container-free;
``score_repo`` (the runner) lazy-imports the pipeline.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src"
for _p in (_REPO_ROOT, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from src.eval.graph_fidelity.root_selection_ab import (  # noqa: E402
    DivergentAdd,
    _bare_name,
    _canon,
    adjudicate_divergence,
    aggregate,
    reconstruct_generator_roots,
)

logger = logging.getLogger(__name__)

# ``_bare_name`` (strip ``[extras]``/PEP 440 specifier suffix) is shared with
# ``root_selection_ab`` — imported above so both A/B evals compare CURRENT's
# specifier-carrying declared tokens (``numpy<2``, ``uvicorn[standard]>=0.30``)
# against NEW's bare names on one PEP 503 key.

# ---------------------------------------------------------------------------
# Pure divergence (no python_deps import — fast unit tests)
# ---------------------------------------------------------------------------


def build_divergence(
    new_roots: Sequence[str],
    current_roots: Sequence[tuple[str | None, str]],
    id_to_name: Mapping[str, str],
) -> tuple[DivergentAdd, ...]:
    """Packages CURRENT (full ``depgraph.roots`` output) has that NEW (the
    pkg_layer contract-only selector) omits, canon-deduped and sorted.

    ``current_roots`` is the raw ``depgraph.roots.select_roots`` output --
    ``(import_id | None, distribution_name)`` pairs, where a manifest-
    declared token may carry a ``[extras]``/version-specifier suffix (see
    ``_bare_name``) that is stripped before comparison. A pair's
    ``import_name`` is resolved via ``id_to_name`` when it carries a
    scan-gap-fill import id (the expected divergence shape: NEW omits
    exactly what CURRENT's import-driven gap-fill re-adds). A pair with
    ``import_id is None`` that STILL diverges has no import provenance at
    all -- a manifest-level discrepancy between the two designs, not
    expected when both read the same evidence -- and falls back to the
    dist's own canon name rather than dropping the row or raising on a
    missing ``id_to_name`` entry.
    """
    new_canon = frozenset(_canon(_bare_name(name)) for name in new_roots)
    seen: dict[str, DivergentAdd] = {}
    for import_id, dist in current_roots:
        canon = _canon(_bare_name(dist))
        if canon in new_canon or canon in seen:
            continue
        import_name = (
            id_to_name.get(import_id, import_id) if import_id is not None else canon
        )
        seen[canon] = DivergentAdd(import_id=import_id, import_name=import_name, dist=canon)
    return tuple(seen[canon] for canon in sorted(seen))


# ---------------------------------------------------------------------------
# Runner (lazy python_deps import — host-only, no container)
# ---------------------------------------------------------------------------


def score_repo(repo_dir: str, full_name: str, gold_imports: Mapping[str, str]) -> dict:
    """Run the NEW contract-only selector, the CURRENT depgraph selector, and
    the NEW builder's alignment on ``repo_dir``; adjudicate the divergence.

    Host-only: AST scan + manifest reads. No container, no network, no
    build-agent phase.
    """
    from python_deps.depgraph.roots import select_roots as current_select_roots
    from python_deps.depgraph.scan import scan_to_nodes
    from python_deps.depgraph.schema import NodeType
    from python_deps.pkg_layer.construct import build_package_layer
    from python_deps.pkg_layer.contract import read_contract
    from python_deps.pkg_layer.contract import select_roots as new_select_roots

    graph = scan_to_nodes(repo_dir)
    id_to_name = {n.id: n.name for n in graph.nodes if n.type is NodeType.IMPORT}
    # CURRENT arm = the reconstructed generator (declared-only ``select_roots``
    # UNION ``naming.package_roots`` gap-fill), reproducing the scan-gap-fill the
    # in-tree selector no longer performs (P0.1). Without this re-anchor CURRENT
    # would be declared-only too, collapsing the divergence against NEW to ~0 and
    # HIDING the measured win.
    verifier_roots = current_select_roots(repo_dir, graph)
    current_roots = reconstruct_generator_roots(repo_dir, graph, verifier_roots, id_to_name)
    new_roots = new_select_roots(read_contract(repo_dir), frozenset())

    divergence = build_divergence(new_roots, current_roots, id_to_name)
    adj = adjudicate_divergence(divergence, gold_imports)

    _layer, alignment = build_package_layer(repo_dir)

    def _dump(adds: Sequence[DivergentAdd]) -> list[dict]:
        return [{"import": a.import_name, "dist": a.dist} for a in adds]

    return {
        "repo": full_name,
        "new_root_count": len(frozenset(_canon(_bare_name(n)) for n in new_roots)),
        "current_root_count": len(
            frozenset(_canon(_bare_name(dist)) for _iid, dist in current_roots)
        ),
        "divergence": _dump(divergence),
        "good_adds": _dump(adj["good"]),
        "bad_adds": _dump(adj["bad"]),
        "unlabeled_adds": _dump(adj["unlabeled"]),
        "under_declared_count": len(alignment.under_declared),
    }


def render_report_md(scorecards: Sequence[Mapping], agg: Mapping) -> str:
    lines = [
        "# pkg_layer root-selection A/B — NEW (contract-only) vs CURRENT (depgraph.roots)",
        "",
        f"Corpus: {agg['repos']} repos. **Verdict: {agg['verdict']}.**",
        "",
        f"- total divergence (CURRENT has, NEW omits): {agg['total_divergence']}",
        f"- good adds (CURRENT rescues a real under-declaration NEW misses): {agg['good_adds']}",
        f"- bad adds (CURRENT over-installs; NEW correctly omits): {agg['bad_adds']}",
        f"- unlabeled adds (need a gold label): {agg['unlabeled_adds']}",
        "",
        "| Repo | NEW roots | CURRENT roots | divergence | good | bad | unlabeled | NEW under_declared |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for sc in scorecards:
        lines.append(
            f"| {sc['repo']} | {sc['new_root_count']} | {sc['current_root_count']} "
            f"| {len(sc['divergence'])} | {len(sc['good_adds'])} | {len(sc['bad_adds'])} "
            f"| {len(sc['unlabeled_adds'])} | {sc['under_declared_count']} |"
        )
    detail = [sc for sc in scorecards if sc["divergence"]]
    if detail:
        lines += ["", "## Divergent packages (CURRENT has these; NEW omits)", ""]
        for sc in detail:
            for kind in ("good_adds", "bad_adds", "unlabeled_adds"):
                for a in sc[kind]:
                    lines.append(f"- {sc['repo']}: `{a['import']}` → `{a['dist']}` ({kind[:-5]})")
    lines.append("")
    return "\n".join(lines)


def run_corpus(
    clones: Mapping[str, str], gold: Mapping[str, Mapping[str, str]]
) -> tuple[list[dict], dict]:
    """Score every repo whose clone dir exists; skip (with a warning) those that don't."""
    scorecards: list[dict] = []
    for full_name, repo_dir in clones.items():
        if not Path(repo_dir).is_dir():
            logger.warning("skip %s: clone not found at %s", full_name, repo_dir)
            continue
        gold_imports = gold.get(full_name, {})
        try:
            scorecards.append(score_repo(repo_dir, full_name, gold_imports))
        except Exception as exc:  # noqa: BLE001 — one repo's crash must not abort the corpus
            logger.warning("skip %s: %s: %s", full_name, type(exc).__name__, exc)
    return scorecards, aggregate(scorecards)


def main(argv: list[str] | None = None) -> int:
    from src.eval.graph_fidelity.ab_gold_labels import AB_GOLD, default_clone_map

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--clones-root", type=Path, default=None,
        help="Directory containing the live-probe clones (probe_A/... layout).",
    )
    parser.add_argument(
        "--out", type=Path,
        default=_REPO_ROOT / "outputs" / "graph_fidelity" / "pkg_layer_ab.md",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    clones = default_clone_map(args.clones_root)
    scorecards, agg = run_corpus(clones, AB_GOLD)
    report = render_report_md(scorecards, agg)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report + "\n")
    (args.out.with_suffix(".json")).write_text(
        json.dumps({"aggregate": agg, "scorecards": scorecards}, indent=2, sort_keys=True) + "\n"
    )
    print(report)
    print(f"\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
