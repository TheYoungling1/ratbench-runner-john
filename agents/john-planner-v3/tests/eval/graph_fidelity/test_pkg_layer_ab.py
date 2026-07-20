"""Unit tests for the pure pkg_layer root-selection A/B scorer (Task 7).

NEW (pkg_layer.contract, contract-only) vs CURRENT (depgraph.roots, manifest
+ scan-gap-fill) — the divergence is "packages CURRENT has that NEW omits",
adjudicated against gold labels exactly like Track A's generator/verifier
split (``root_selection_ab.py``). All pure here — no repo checkout, no
container, no ``python_deps`` import; the tests build both root sets and the
gold map by hand.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.eval.graph_fidelity.pkg_layer_ab import (  # noqa: E402
    _bare_name,
    build_divergence,
    render_report_md,
)
from src.eval.graph_fidelity.root_selection_ab import (  # noqa: E402
    DivergentAdd,
    adjudicate_divergence,
    aggregate,
)


# --- _bare_name --------------------------------------------------------------

def test_bare_name_strips_version_specifier():
    assert _bare_name("numpy<2") == "numpy"
    assert _bare_name("annotated-types>=0.6.0") == "annotated-types"


def test_bare_name_strips_extras_bracket_and_specifier():
    assert _bare_name("uvicorn[standard]>=0.30") == "uvicorn"
    assert _bare_name("cachecontrol[filecache]<0.15.0,>=0.14.0") == "cachecontrol"


def test_bare_name_idempotent_on_plain_name():
    assert _bare_name("requests") == "requests"


def test_bare_name_strips_leading_whitespace_before_matching():
    # A leading space (e.g. from a loosely-formatted requirements line) must
    # not defeat the anchored name-prefix regex -- otherwise the token comes
    # back unchanged (" numpy<2"), canonizes to a different string than the
    # stripped "numpy", and is misreported as a divergence.
    assert _bare_name(" numpy<2") == "numpy"
    assert _bare_name("  uvicorn[standard]>=0.30") == "uvicorn"
    assert _bare_name(" requests ") == "requests"


# --- build_divergence -------------------------------------------------------

def test_build_divergence_specifier_carrying_declared_root_not_divergent():
    # depgraph.roots deliberately carries the version specifier on a
    # manifest-declared root (e.g. "numpy<2"); pkg_layer.contract always
    # returns bare names ("numpy"). A declared dep must NOT be misreported
    # as a root-selection divergence just because CURRENT's token shape
    # differs from NEW's.
    new_roots = ["numpy", "uvicorn"]
    current_roots = [(None, "numpy<2"), (None, "uvicorn[standard]>=0.30")]

    divergence = build_divergence(new_roots, current_roots, {})

    assert divergence == ()



def test_build_divergence_returns_packages_current_has_that_new_omits():
    new_roots = ["requests"]
    current_roots = [(None, "requests"), ("import:brotli", "brotli")]
    id_to_name = {"import:brotli": "brotli"}

    divergence = build_divergence(new_roots, current_roots, id_to_name)

    assert divergence == (
        DivergentAdd(import_id="import:brotli", import_name="brotli", dist="brotli"),
    )


def test_build_divergence_empty_when_new_equals_current():
    new_roots = ["requests", "click"]
    current_roots = [(None, "requests"), (None, "click")]

    divergence = build_divergence(new_roots, current_roots, {})

    assert divergence == ()


def test_build_divergence_canon_dedup_and_sorted():
    new_roots = ["Requests"]
    current_roots = [
        (None, "requests"),
        ("import:b", "Brotli"),
        ("import:z", "zeta_lib"),
    ]
    id_to_name = {"import:b": "brotli", "import:z": "Zeta-Lib"}

    divergence = build_divergence(new_roots, current_roots, id_to_name)

    assert [d.dist for d in divergence] == ["brotli", "zeta-lib"]


def test_build_divergence_manifest_only_divergence_falls_back_to_dist_name():
    # A pair with import_id None that STILL diverges (no import provenance at
    # all) -- a surprising manifest-level discrepancy between the two
    # designs; falls back to the dist's own canon as import_name rather than
    # dropping the row or crashing on a missing id_to_name entry.
    current_roots = [(None, "ghost-pkg")]

    divergence = build_divergence([], current_roots, {})

    assert divergence == (
        DivergentAdd(import_id=None, import_name="ghost-pkg", dist="ghost-pkg"),
    )


def test_build_divergence_ignores_duplicate_canon_pairs():
    current_roots = [("import:a", "brotli"), ("import:b", "Brotli")]

    divergence = build_divergence([], current_roots, {"import:a": "brotli", "import:b": "Brotli"})

    assert len(divergence) == 1
    assert divergence[0].dist == "brotli"


# --- adjudicate_divergence (reused from root_selection_ab) ------------------

def test_adjudicate_divergence_classifies_good_bad_unlabeled():
    divergence = (
        DivergentAdd(import_id="i1", import_name="needed_thing", dist="needed-thing"),
        DivergentAdd(import_id="i2", import_name="optional_thing", dist="optional-thing"),
        DivergentAdd(import_id="i3", import_name="mystery_thing", dist="mystery-thing"),
    )
    gold = {"needed_thing": "needed", "optional_thing": "optional"}

    adj = adjudicate_divergence(divergence, gold)

    assert [d.dist for d in adj["good"]] == ["needed-thing"]
    assert [d.dist for d in adj["bad"]] == ["optional-thing"]
    assert [d.dist for d in adj["unlabeled"]] == ["mystery-thing"]


# --- end-to-end pure scoring: NEW roots + CURRENT roots + gold -> verdict ---

def _scorecard(repo, new_roots, current_roots, id_to_name, gold, under_declared_count=0):
    divergence = build_divergence(new_roots, current_roots, id_to_name)
    adj = adjudicate_divergence(divergence, gold)

    def _dump(adds):
        return [{"import": a.import_name, "dist": a.dist} for a in adds]

    return {
        "repo": repo,
        "new_root_count": len(set(new_roots)),
        "current_root_count": len({dist for _iid, dist in current_roots}),
        "divergence": _dump(divergence),
        "good_adds": _dump(adj["good"]),
        "bad_adds": _dump(adj["bad"]),
        "unlabeled_adds": _dump(adj["unlabeled"]),
        "under_declared_count": under_declared_count,
    }


def test_hand_built_corpus_new_roots_subset_of_current_verdict_verifier():
    # NEW omits one optional-extra add (bad) that CURRENT's scan gap-fill
    # re-adds -- the exact shape the DoD calls out: NEW ⊆ CURRENT, divergence
    # is the current design's over-add.
    sc = _scorecard(
        repo="acme/widgets",
        new_roots=["requests"],
        current_roots=[(None, "requests"), ("import:brotli", "brotli")],
        id_to_name={"import:brotli": "brotli"},
        gold={"brotli": "optional"},
    )

    agg = aggregate([sc])

    assert sc["divergence"] == [{"import": "brotli", "dist": "brotli"}]
    assert agg["total_divergence"] == 1
    assert agg["bad_adds"] == 1
    assert agg["good_adds"] == 0
    assert agg["verdict"] == "verifier"


def test_hand_built_corpus_identical_when_no_divergence():
    sc = _scorecard(
        repo="acme/widgets",
        new_roots=["requests", "click"],
        current_roots=[(None, "requests"), (None, "click")],
        id_to_name={},
        gold={},
    )

    agg = aggregate([sc])

    assert agg["verdict"] == "identical"


def test_hand_built_corpus_needs_labels_when_divergence_unlabeled():
    sc = _scorecard(
        repo="acme/widgets",
        new_roots=[],
        current_roots=[("import:x", "mystery")],
        id_to_name={"import:x": "mystery"},
        gold={},
    )

    agg = aggregate([sc])

    assert agg["verdict"] == "needs-labels"


# --- render_report_md (smoke) -----------------------------------------------

def test_render_report_md_includes_verdict_and_under_declared_column():
    sc = _scorecard(
        repo="acme/widgets",
        new_roots=["requests"],
        current_roots=[(None, "requests"), ("import:brotli", "brotli")],
        id_to_name={"import:brotli": "brotli"},
        gold={"brotli": "optional"},
        under_declared_count=2,
    )
    agg = aggregate([sc])

    report = render_report_md([sc], agg)

    assert "Verdict: verifier" in report
    assert "acme/widgets" in report
    assert "under_declared" in report.lower()
