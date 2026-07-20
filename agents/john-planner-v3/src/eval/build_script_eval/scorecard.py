"""Execution-only scorecard analytics for the e2e build-script eval. Pure core
(this file's top half): ladder result type, pytest exit-code classifier, headline
gate, language/system gap split, and failure attribution from execution error
text. No oracle, no recall fraction. The docker orchestration (score_repo) lives
in the second half (Task 3) and reuses coverage.py primitives.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src"
for _p in (_REPO_ROOT, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from python_deps.depgraph.executor import TIMEOUT_RC  # noqa: E402
from src.eval.language_package_eval.coverage import canon_pip  # noqa: E402

_SYSTEM_TIERS: frozenset[str] = frozenset({"SYSTEM_LIB", "TOOL"})


@dataclass(frozen=True)
class LadderResult:
    """One repo's fresh-container replay outcome, rung by rung."""

    install_ok: bool
    env_works: bool
    tests_ran: bool
    tests_passed: bool
    highest_rung: str            # none|install|env_works|tests_ran|tests_passed
    reason: str | None           # why it stopped (timeout, no_tests_collected, ...)
    first_failure: dict | None   # {"command","stderr_tail"} at the failing rung
    gaps: tuple[dict, ...]        # classify_execution_failures dicts (typed)
    collect_ok: bool | None = None  # True=collected clean, False=collect failed, None=not attempted


def classify_pytest_result(returncode: int) -> tuple[bool, bool, str | None]:
    """(tests_ran, tests_passed, reason) from a `pytest -q` exit code.

    tests_ran is True only when pytest executed tests to a pass/fail verdict
    (rc 0 or 1) — a collection/usage error (2/3/4) or an empty collection (5)
    means tests did NOT run. tests_passed is rc 0 only. A timeout (TIMEOUT_RC)
    is a non-hanging recorded miss."""
    if returncode == TIMEOUT_RC:
        return (False, False, "timeout")
    if returncode == 0:
        return (True, True, None)
    if returncode == 1:
        return (True, False, "tests_failed")
    if returncode == 5:
        return (False, False, "no_tests_collected")
    return (False, False, "collection_or_usage_error")


def env_works_passed(ladder: LadderResult) -> bool:
    """The HEADLINE gate: setup.sh installed clean AND the repo imports. Test
    COLLECTION is a separate diagnostic (`collect_ok`), NOT part of this gate — a
    pytest-version/config incompatibility must not sink the headline."""
    return ladder.install_ok and ladder.env_works


def extract_gaps(gaps: tuple[dict, ...]) -> tuple[tuple[dict, ...], tuple[dict, ...]]:
    """Split typed execution gaps into (language, system). SERVICE dropped
    (out of scope). Language = PACKAGE; system = SYSTEM_LIB + TOOL."""
    language = tuple(g for g in gaps if g.get("tier") == "PACKAGE")
    system = tuple(g for g in gaps if g.get("tier") in _SYSTEM_TIERS)
    return language, system


def _attribute_install_failure(first_failure: dict | None) -> str:
    """apt/dpkg failure ⇒ system_gap; pip/module failure ⇒ language_gap; else
    render_bug (the setup.sh itself broke for a non-coverage reason)."""
    blob = ""
    if first_failure:
        blob = f"{first_failure.get('command', '')}\n{first_failure.get('stderr_tail', '')}".lower()
    if any(tok in blob for tok in ("apt-get", "apt ", "dpkg", "unable to locate package",
                                   "e: package", ".so", "shared object")):
        return "system_gap"
    if any(tok in blob for tok in ("pip ", "pip3", "could not find a version",
                                   "no matching distribution", "modulenotfounderror")):
        return "language_gap"
    return "render_bug"


def attribute_failure(ladder: LadderResult, *, static_ok: bool,
                      top_import: str | None, feasible: bool) -> str:
    """One label for a repo. `pass` when env_works; otherwise the blocking layer.
    Priority: infeasible ▸ static render_bug ▸ system_gap ▸ own-package render_bug
    ▸ language_gap ▸ install-failure classification ▸ unknown."""
    if not feasible:
        return "infeasible"
    if env_works_passed(ladder):
        return "pass"
    if not static_ok:
        return "render_bug"

    language, system = extract_gaps(ladder.gaps)
    if system:
        return "system_gap"
    own = canon_pip(top_import) if top_import else None
    lang_ids = {canon_pip(g["id"]) for g in language}
    if own and own in lang_ids:
        return "render_bug"   # the repo's OWN package — the PROJECT-node install gap
    if language:
        return "language_gap"
    if not ladder.install_ok:
        return _attribute_install_failure(ladder.first_failure)
    return "unknown"


from python_deps.depgraph.build_script import render_build_script  # noqa: E402
from src.eval.graph_fidelity.render_fidelity import check_render  # noqa: E402
from src.eval.language_package_eval.coverage import (  # noqa: E402
    apt_names_in_graph, base_image_for_repo, build_graph_construction_only,
    package_versions_in_graph, top_level_import_name,
)


def _static_ok(fidelity) -> bool:
    """The render pre-gate: valid bash (None = no bash on host ⇒ don't penalize),
    single emit, topo order, all reciped nodes emitted."""
    return (
        fidelity.valid_bash is not False
        and fidelity.single_emit
        and fidelity.topo_order_ok
        and fidelity.all_reciped_emitted
    )


def _assemble_scorecard(full_name, stratum, feasible, image, minor, graph,
                        static_ok, top_import, ladder) -> dict:
    """Pure per-repo scorecard row. `execution_missing` is the exact key
    `coverage.missing_node_clusters` consumes (gaps that broke the env)."""
    language_gaps, system_gaps = extract_gaps(ladder.gaps)
    attribution = attribute_failure(
        ladder, static_ok=static_ok, top_import=top_import, feasible=feasible
    )
    return {
        "repo": full_name,
        "stratum": stratum,
        "feasible": feasible,
        "base_image": image,
        "target_python": minor,
        "predicted_apt": sorted(apt_names_in_graph(graph)),
        "predicted_packages": sorted(package_versions_in_graph(graph)),
        "static_render_ok": static_ok,
        "first_pass_env_works": env_works_passed(ladder),
        "install_ok": ladder.install_ok,
        "env_works": ladder.env_works,
        "collect_ok": ladder.collect_ok,
        "tests_ran": ladder.tests_ran,
        "tests_passed": ladder.tests_passed,
        "highest_rung": ladder.highest_rung,
        "ladder_reason": ladder.reason,
        "attribution": attribution,
        "language_gaps": list(language_gaps),
        "system_gaps": list(system_gaps),
        # SERVICE gaps are out of scope, so execution_missing (what
        # coverage.missing_node_clusters reads) is the SERVICE-free union:
        "execution_missing": [*language_gaps, *system_gaps],
        "first_failure": ladder.first_failure,
    }


def score_repo(repo_dir: str, spec) -> dict:
    """Full per-repo pipeline (docker). `spec` is a corpus.RepoSpec."""
    # Local import: replay.py imports LadderResult/classify_pytest_result back
    # from this module at its own module scope (Task 2). Importing
    # run_replay_ladder at THIS module's top level would race that — whichever
    # of the two modules loads first wins, and the other sees a partially
    # initialized module (breaks whenever something imports replay before
    # scorecard, e.g. test_replay_ladder.py, or the full test-suite collection
    # order). Deferring to call time breaks the cycle without touching replay.py.
    from src.eval.build_script_eval.replay import run_replay_ladder

    image, minor, _reason = base_image_for_repo(repo_dir)
    graph = build_graph_construction_only(repo_dir, image, minor)
    script = render_build_script(graph, ())
    static_ok = _static_ok(check_render(graph, script))
    top_import = spec.top_import or top_level_import_name(repo_dir)
    ladder = run_replay_ladder(
        repo_dir, image, script, top_import, isolate_network=not spec.network_in_tests,
    )
    return _assemble_scorecard(
        spec.full_name, spec.stratum, spec.feasible, image, minor, graph,
        static_ok, top_import, ladder,
    )
