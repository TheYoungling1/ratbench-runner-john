"""Track B — fault injection: manufacture under-declaration, measure recovery.

Track B of docs/superpowers/plans/2026-07-03-root-selection-ab-eval.md. Track A showed the two
architectures produce identical RUNTIME closures on well-declared repos (the generator only
ever ADDS optional-extras). Track B tests the OTHER cell — the one where imports-as-GENERATOR
could win: when a repo genuinely UNDER-declares a runtime dep it imports, does the generator's
scan-gap-fill auto-add rescue it?

Method: delete one declared dep from the evidence (scoped ``patch.object`` on the
``collect_python_dependency_evidence`` bindings — no disk mutation), re-run the declared-only
``select_roots`` (the verifier) and the RECONSTRUCTED generator arm
(``root_selection_ab.reconstruct_generator_roots`` = verifier UNION ``naming.package_roots``
gap-fill; post-P0.1 ``select_roots`` alone is declared-only), and check whether the deleted dist
reappears in the generator roots vs the verifier roots.

Key post-Phase-2 fact this measures: the generator can only re-derive a deleted dep when its
import maps via the CURATED table (there is no identity fallback anymore). So it recovers
curated aliases (``cv2``->opencv-python) but NOT identity-named deps (``requests``). The
verifier recovers neither at construction — it defers to the post-install honest flag / repair.

Level 1 (this module, pure/no-Docker): does root selection re-add the deleted dep?
Level 2 (spec, deferred): does the rendered setup.sh actually build a working env? — arbitrate
with ``coverage.run_execution_probe`` (fresh -slim replay). Container + build phase, out of the
construction-only scope here.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Mapping, Sequence
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[3]
for _p in (_REPO_ROOT, _REPO_ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from src.eval.graph_fidelity.root_selection_ab import (  # noqa: E402
    _canon,
    partition_roots,
    reconstruct_generator_roots,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def classify_naming(dep_name: str, import_name: str) -> str:
    """``identity`` (canon dep == canon import), ``curated_alias`` (import is a key in the real
    curated table), else ``other`` (e.g. a namespace-truncated import). Determines whether the
    generator's post-Phase-2 gap-fill can re-derive a deleted dep from its import at all."""
    from python_deps.import_mapping import CURATED_IMPORT_TO_PACKAGE

    if _canon(dep_name) == _canon(import_name):
        return "identity"
    if import_name.lower() in CURATED_IMPORT_TO_PACKAGE:
        return "curated_alias"
    return "other"


def recovery_flags(partition, deleted_canon: str) -> tuple[bool, bool]:
    """(generator_recovered, verifier_recovered) — did the deleted dist reappear in each root
    set after the declaration was removed?"""
    return (deleted_canon in partition.generator, deleted_canon in partition.verifier)


def write_synthetic_fixture(root: "Path | str", dist: str, import_name: str) -> str:
    """A minimal installable repo: pyproject declares ``dist`` as a runtime dep; the package
    imports ``import_name``. Used to prove recovery deterministically without cloning."""
    root = Path(root)
    (root / "fx").mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "fx"\nversion = "0"\n'
        f'dependencies = ["{dist}"]\n'
    )
    (root / "fx" / "__init__.py").write_text(f"import {import_name}\n")
    return str(root)


# ---------------------------------------------------------------------------
# Injection (scoped patch — no disk mutation)
# ---------------------------------------------------------------------------

def inject_and_score(repo_dir: str, full_name: str, deleted_dep: str, import_name: str) -> dict:
    """Drop ``deleted_dep`` from the collected evidence, re-run the declared-only
    ``select_roots`` (verifier) and the reconstructed generator arm, and record
    whether each architecture recovered it.

    The deletion is applied via a scoped patch on BOTH evidence bindings: the one
    ``roots.select_roots`` reads (``roots_mod``) AND the one
    ``reconstruct_generator_roots`` reads afresh (``python_deps.evidence``). Both
    must see the removal, or the reconstructed generator's ``declared_names`` would
    still carry the deleted dep and falsely "recover" an identity-named dep through
    the declared-precedence path — inverting the post-Phase-2 result this measures.
    """
    import python_deps.depgraph.roots as roots_mod
    import python_deps.evidence as evidence_mod
    from python_deps.depgraph.scan import scan_to_nodes
    from python_deps.depgraph.schema import NodeType

    target = _canon(deleted_dep)
    real_collect = evidence_mod.collect_python_dependency_evidence

    def _patched(repo_path):
        ev = real_collect(repo_path)
        return replace(
            ev,
            declared_dependencies=[
                d for d in ev.declared_dependencies if _canon(d.name) != target
            ],
        )

    with patch.object(roots_mod, "collect_python_dependency_evidence", _patched), patch.object(
        evidence_mod, "collect_python_dependency_evidence", _patched
    ):
        graph = scan_to_nodes(repo_dir)
        id_to_name = {n.id: n.name for n in graph.nodes if n.type is NodeType.IMPORT}
        verifier_roots = roots_mod.select_roots(repo_dir, graph)
        roots = reconstruct_generator_roots(repo_dir, graph, verifier_roots, id_to_name)

    part = partition_roots(roots, id_to_name)
    gen_rec, ver_rec = recovery_flags(part, target)
    return {
        "repo": full_name,
        "deleted_dep": deleted_dep,
        "import_name": import_name,
        "naming": classify_naming(deleted_dep, import_name),
        "generator_recovered": gen_rec,
        "verifier_recovered": ver_rec,
    }


# ---------------------------------------------------------------------------
# Aggregate + report
# ---------------------------------------------------------------------------

def aggregate_faults(results: Sequence[Mapping]) -> dict:
    """Bucket recovery counts by naming category — the split that matters (the generator's
    advantage, if any, is confined to whichever categories it recovers)."""
    buckets: dict[str, dict[str, int]] = {}
    for r in results:
        b = buckets.setdefault(
            r["naming"], {"total": 0, "generator_recovered": 0, "verifier_recovered": 0}
        )
        b["total"] += 1
        b["generator_recovered"] += int(r["generator_recovered"])
        b["verifier_recovered"] += int(r["verifier_recovered"])
    return buckets


def render_report_md(results: Sequence[Mapping], agg: Mapping) -> str:
    lines = [
        "# Root-selection A/B — Track B (fault injection: under-declaration recovery)",
        "",
        "Delete one declared dep; does each architecture re-derive it at construction?",
        "",
        "| naming | faults | generator recovered | verifier recovered |",
        "|---|---|---|---|",
    ]
    for naming in sorted(agg):
        b = agg[naming]
        lines.append(
            f"| {naming} | {b['total']} | {b['generator_recovered']} | {b['verifier_recovered']} |"
        )
    lines += ["", "## Per-fault detail", ""]
    lines += ["| repo | deleted dep | import | naming | gen | ver |", "|---|---|---|---|---|---|"]
    for r in results:
        lines.append(
            f"| {r['repo']} | {r['deleted_dep']} | {r['import_name']} | {r['naming']} "
            f"| {'✓' if r['generator_recovered'] else '·'} "
            f"| {'✓' if r['verifier_recovered'] else '·'} |"
        )
    lines.append("")
    return "\n".join(lines)


# Synthetic curated-alias faults — the one cell where the generator recovers.
SYNTHETIC_ALIAS_TARGETS: tuple[tuple[str, str], ...] = (
    ("PyYAML", "yaml"),
    ("beautifulsoup4", "bs4"),
    ("opencv-python", "cv2"),
    ("Pillow", "PIL"),
    ("PyGithub", "github"),
)

# Real identity-named runtime deps (declared AND imported) from the live-probe clones — the
# realistic case. (repo full_name, dep, import_name); resolved to a clone dir at run time.
REAL_IDENTITY_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("pallets/flask", "click", "click"),
    ("pallets/flask", "jinja2", "jinja2"),
    ("pallets/flask", "werkzeug", "werkzeug"),
    ("pallets/flask", "blinker", "blinker"),
    ("pallets/flask", "itsdangerous", "itsdangerous"),
    ("psf/requests", "urllib3", "urllib3"),
    ("psf/requests", "certifi", "certifi"),
    ("psf/requests", "idna", "idna"),
    ("python-poetry/poetry", "shellingham", "shellingham"),
    ("python-poetry/poetry", "virtualenv", "virtualenv"),
    ("scrapy/scrapy", "parsel", "parsel"),
    ("scrapy/scrapy", "w3lib", "w3lib"),
)


def run(clones: Mapping[str, str]) -> tuple[list[dict], dict]:
    results: list[dict] = []

    # 1) synthetic curated-alias fixtures (deterministic, no clone needed)
    with tempfile.TemporaryDirectory() as tmp:
        for i, (dist, import_name) in enumerate(SYNTHETIC_ALIAS_TARGETS):
            repo = write_synthetic_fixture(Path(tmp) / f"fx{i}", dist, import_name)
            results.append(inject_and_score(repo, f"synthetic/{import_name}", dist, import_name))

    # 2) real identity-named runtime deps from the corpus clones
    for full_name, dep, import_name in REAL_IDENTITY_TARGETS:
        repo_dir = clones.get(full_name)
        if not repo_dir or not Path(repo_dir).is_dir():
            logger.warning("skip %s (%s): clone not found", full_name, dep)
            continue
        try:
            results.append(inject_and_score(repo_dir, full_name, dep, import_name))
        except Exception as exc:  # noqa: BLE001
            logger.warning("skip %s (%s): %s: %s", full_name, dep, type(exc).__name__, exc)

    return results, aggregate_faults(results)


def main(argv: list[str] | None = None) -> int:
    from src.eval.graph_fidelity.ab_gold_labels import default_clone_map

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clones-root", type=Path, default=None)
    parser.add_argument(
        "--out", type=Path,
        default=_REPO_ROOT / "outputs" / "graph_fidelity" / "fault_injection.md",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    results, agg = run(default_clone_map(args.clones_root))
    report = render_report_md(results, agg)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report + "\n")
    (args.out.with_suffix(".json")).write_text(
        json.dumps({"aggregate": agg, "results": results}, indent=2, sort_keys=True) + "\n"
    )
    print(report)
    print(f"\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
