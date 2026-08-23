"""A/B eval — imports-as-GENERATOR vs imports-as-VERIFIER root selection.

Track A of docs/superpowers/plans/2026-07-03-root-selection-ab-eval.md.

Post-P0.1 the in-tree ``select_roots`` is declared-only (imports never generate install roots),
so a single call can no longer yield the generator's gap-fill adds — its output IS the verifier.
The generator arm is therefore RECONSTRUCTED (``reconstruct_generator_roots``) from the retained
reference helper ``naming.package_roots``, reproducing the exact scan-gap-fill block P0.1 deleted
from ``select_roots`` so the eval still measures the same divergence against the declared-only
selector. Both arms then carry the ``select_roots`` tagging convention — declared roots keep
``import_id=None`` and reconstructed gap-fill roots carry the import node id:

  * GENERATOR (reconstructed) roots = declared verifier UNION package_roots gap-fill
  * VERIFIER  (in-tree) roots       = declared only (``import_id is None``)
  * DIVERGENCE = the gap-fill adds   = exactly where the two differ.

This module partitions that output and adjudicates the divergence set against gold labels: a
divergent add is a GOOD add (a genuinely-needed runtime dep the repo under-declared) or a BAD
add (benign: optional / typing / local / tooling / transitive / alias-of-declared / stdlib —
i.e. over-install). Verdict: `bad >= good` ⇒ verifier wins with no loss; `good > bad` ⇒ the
generator's auto-add earns its keep.

The pure functions (partition/adjudicate/aggregate) import nothing from ``python_deps`` so the
unit tests stay fast and container-free; ``score_repo`` (the runner) lazy-imports the pipeline.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src"
for _p in (_REPO_ROOT, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pure adjudication (no python_deps import — fast unit tests)
# ---------------------------------------------------------------------------

_CANON_RUN = re.compile(r"[-_.]+")

# PEP 503 distribution-name character class: letters, digits, ``.``/``_``/``-``.
_NAME_PREFIX_RE = re.compile(r"^[A-Za-z0-9._-]+")


def _canon(name: str) -> str:
    """PEP 503 comparison key (``Flask_Login`` == ``flask-login``)."""
    return _CANON_RUN.sub("-", name.strip()).lower()


def _bare_name(token: str) -> str:
    """Strip a ``[extras]`` bracket and/or PEP 440 version specifier suffix.

    ``depgraph.roots.select_roots`` carries the declared constraint on a
    manifest-declared root -- e.g. ``numpy<2`` or ``uvicorn[standard]>=0.30`` --
    so the resolver can see version conflicts (see ``_manifest_root_token``).
    Comparing such a token against a bare mapped-import name (``numpy``) requires
    stripping the suffix first, or a declared dep would canon-differ from its own
    gap-fill counterpart and be double-counted. Idempotent on an already-bare
    name. ``token`` is stripped of leading/trailing whitespace first so the
    start-anchored ``_NAME_PREFIX_RE`` still matches a loosely-formatted token
    (e.g. ``" numpy<2"``). Shared with ``pkg_layer_ab`` (imported from here)."""
    token = token.strip()
    match = _NAME_PREFIX_RE.match(token)
    return match.group(0) if match else token


#: Gold label ⇒ a divergent add is a mistake (the generator over-installs).
BENIGN_LABELS = frozenset(
    {"optional", "typing", "local", "tooling", "transitive", "alias_of_declared", "stdlib"}
)
#: Gold label ⇒ a divergent add is correct (the generator rescues a real under-declaration).
NEEDED_LABEL = "needed"


@dataclass(frozen=True)
class DivergentAdd:
    """A package the GENERATOR adds and the VERIFIER omits."""

    import_id: str
    import_name: str
    dist: str


@dataclass(frozen=True)
class RootPartition:
    generator: tuple[str, ...]  # canon dist names, sorted (all roots)
    verifier: tuple[str, ...]  # canon dist names, sorted (declared only)
    divergence: tuple[DivergentAdd, ...]


def partition_roots(
    roots: Sequence[tuple[str | None, str]], id_to_name: Mapping[str, str]
) -> RootPartition:
    """Split ``select_roots`` output into the generator/verifier root sets and the
    divergence (gap-fill) adds. ``id_to_name`` maps an import node id to its module name;
    a missing id falls back to the id string itself (never fabricated)."""
    generator = sorted({_canon(dist) for _iid, dist in roots})
    verifier = sorted({_canon(dist) for iid, dist in roots if iid is None})
    divergence = tuple(
        DivergentAdd(import_id=iid, import_name=id_to_name.get(iid, iid), dist=dist)
        for iid, dist in roots
        if iid is not None
    )
    return RootPartition(tuple(generator), tuple(verifier), divergence)


def adjudicate_divergence(
    divergence: Sequence[DivergentAdd], gold_imports: Mapping[str, str]
) -> dict[str, list[DivergentAdd]]:
    """Bucket each divergent add by its gold label: ``good`` (needed), ``bad`` (any benign
    label), or ``unlabeled`` (no gold entry — flags a corpus label gap, never assumed either
    way)."""
    good: list[DivergentAdd] = []
    bad: list[DivergentAdd] = []
    unlabeled: list[DivergentAdd] = []
    for add in divergence:
        label = gold_imports.get(add.import_name)
        if label == NEEDED_LABEL:
            good.append(add)
        elif label in BENIGN_LABELS:
            bad.append(add)
        else:
            unlabeled.append(add)
    return {"good": good, "bad": bad, "unlabeled": unlabeled}


def aggregate(scorecards: Sequence[Mapping]) -> dict:
    """Pool the per-repo scorecards into totals + a verdict.

    ``identical`` — no repo diverged (the verifier is the current design's output, only
    cleaner + honest). ``needs-labels`` — some divergent add is unlabeled, so the good/bad
    tally is not yet decisive. Otherwise ``verifier`` when ``bad >= good`` (auto-add is a net
    cost) else ``generator`` (auto-add earns its keep)."""
    total_div = sum(len(sc["divergence"]) for sc in scorecards)
    good = sum(len(sc["good_adds"]) for sc in scorecards)
    bad = sum(len(sc["bad_adds"]) for sc in scorecards)
    unlabeled = sum(len(sc["unlabeled_adds"]) for sc in scorecards)

    if total_div == 0:
        verdict = "identical"
    elif unlabeled > 0:
        verdict = "needs-labels"
    elif bad >= good:
        verdict = "verifier"
    else:
        verdict = "generator"

    return {
        "repos": len(scorecards),
        "total_divergence": total_div,
        "good_adds": good,
        "bad_adds": bad,
        "unlabeled_adds": unlabeled,
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# Runner (lazy python_deps import — host-only, no container)
# ---------------------------------------------------------------------------

def reconstruct_generator_roots(
    repo_dir: str,
    graph,
    verifier_roots: Sequence[tuple[str | None, str]],
    id_to_name: Mapping[str, str],
) -> list[tuple[str | None, str]]:
    """Reconstruct the OLD (pre-P0.1) generator root set = declared-only verifier
    roots UNION the mapped-import gap-fill ``select_roots`` no longer performs.

    Reproduces, byte-for-byte in behavior, the scan-gap-fill block deleted from
    ``depgraph.roots.select_roots`` in P0.1, using the retained reference helper
    ``naming.package_roots``: for every mapped Import node whose distribution is
    not already covered by a declared root (deduped by PEP 503 name), append it
    as a ``(import_id, dist)`` gap-fill root — after dropping non-distribution
    module names, exactly as the deleted block did. Declared names for
    ``package_roots`` are the FULL manifest set (all declared deps, incl. the
    optional-extra ones ``select_roots`` filters out) so an imported-but-
    extra-gated dep is still recovered as the generator's over-add — the very
    behavior the verifier drops. Host-only (lazy ``python_deps`` import); NOT one
    of the pure adjudication functions."""
    from python_deps.depgraph.naming import package_roots
    from python_deps.depgraph.roots import _is_non_distribution
    from python_deps.evidence import collect_python_dependency_evidence
    from python_deps.import_mapping import normalize_package_name

    declared_names = {
        req.name
        for req in collect_python_dependency_evidence(repo_dir).declared_dependencies
    }
    # Dedup gap-fill against the declared roots that actually survived selection
    # (the verifier set), keyed by the same PEP 503 bare-name the deleted block used.
    seen = {normalize_package_name(_bare_name(dist)) for _iid, dist in verifier_roots}
    gapfill: list[tuple[str | None, str]] = []
    for import_node_id, dist_name in package_roots(graph, declared_names):
        module_name = id_to_name.get(import_node_id, import_node_id)
        if _is_non_distribution(module_name):
            continue
        normalized = normalize_package_name(dist_name)
        if normalized in seen:
            continue
        seen.add(normalized)
        gapfill.append((import_node_id, dist_name))
    return list(verifier_roots) + gapfill


def score_repo(repo_dir: str, full_name: str, gold_imports: Mapping[str, str]) -> dict:
    """Run the real host-side root selection on ``repo_dir`` and adjudicate its divergence.

    Host-only: ``scan_to_nodes`` (AST scan) + the declared-only ``select_roots``
    (the verifier arm) + the reconstructed generator arm (``select_roots`` UNION
    ``naming.package_roots`` gap-fill). No container, no network, no build-agent phase."""
    from python_deps.depgraph.roots import select_roots
    from python_deps.depgraph.scan import scan_to_nodes
    from python_deps.depgraph.schema import NodeType

    graph = scan_to_nodes(repo_dir)
    id_to_name = {n.id: n.name for n in graph.nodes if n.type is NodeType.IMPORT}
    verifier_roots = select_roots(repo_dir, graph)  # declared-only (in-tree, P0.1)
    roots = reconstruct_generator_roots(repo_dir, graph, verifier_roots, id_to_name)
    part = partition_roots(roots, id_to_name)
    adj = adjudicate_divergence(part.divergence, gold_imports)

    def _dump(adds: Sequence[DivergentAdd]) -> list[dict]:
        return [{"import": a.import_name, "dist": a.dist} for a in adds]

    return {
        "repo": full_name,
        "generator_root_count": len(part.generator),
        "verifier_root_count": len(part.verifier),
        "divergence": _dump(part.divergence),
        "good_adds": _dump(adj["good"]),
        "bad_adds": _dump(adj["bad"]),
        "unlabeled_adds": _dump(adj["unlabeled"]),
    }


def render_report_md(scorecards: Sequence[Mapping], agg: Mapping) -> str:
    lines = [
        "# Root-selection A/B — Track A (divergence adjudication)",
        "",
        f"Corpus: {agg['repos']} repos. **Verdict: {agg['verdict']}.**",
        "",
        f"- total divergence (generator-only adds): {agg['total_divergence']}",
        f"- good adds (rescued a real under-declaration): {agg['good_adds']}",
        f"- bad adds (over-install / benign): {agg['bad_adds']}",
        f"- unlabeled adds (need a gold label): {agg['unlabeled_adds']}",
        "",
        "| Repo | gen roots | ver roots | divergence | good | bad | unlabeled |",
        "|---|---|---|---|---|---|---|",
    ]
    for sc in scorecards:
        lines.append(
            f"| {sc['repo']} | {sc['generator_root_count']} | {sc['verifier_root_count']} "
            f"| {len(sc['divergence'])} | {len(sc['good_adds'])} | {len(sc['bad_adds'])} "
            f"| {len(sc['unlabeled_adds'])} |"
        )
    detail = [
        sc for sc in scorecards if sc["divergence"]
    ]
    if detail:
        lines += ["", "## Divergent adds (generator adds these; verifier omits)", ""]
        for sc in detail:
            for kind in ("good_adds", "bad_adds", "unlabeled_adds"):
                for a in sc[kind]:
                    lines.append(f"- {sc['repo']}: `{a['import']}` → `{a['dist']}` ({kind[:-5]})")
    lines.append("")
    return "\n".join(lines)


def run_corpus(clones: Mapping[str, str], gold: Mapping[str, Mapping[str, str]]) -> tuple[list[dict], dict]:
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
        default=_REPO_ROOT / "outputs" / "graph_fidelity" / "root_selection_ab.md",
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
