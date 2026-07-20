"""Track-B fault injection: delete one declared dep, does each architecture recover it?

Controlled synthetic fixtures prove the mechanism deterministically (no network, no container):
imports-as-GENERATOR recovers a deleted dep ONLY when the import maps to it via the curated
table (post-Phase-2 there is no identity fallback); imports-as-VERIFIER recovers neither at
construction (it relies on the post-install honest flag / repair phase).
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
for _p in (_REPO_ROOT, _REPO_ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from src.eval.graph_fidelity.fault_injection import (  # noqa: E402
    aggregate_faults,
    classify_naming,
    inject_and_score,
    write_synthetic_fixture,
)


# --- pure classification --------------------------------------------------

def test_classify_identity():
    assert classify_naming("requests", "requests") == "identity"
    assert classify_naming("Flask-Login", "flask_login") == "identity"  # canon-equal


def test_classify_curated_alias():
    assert classify_naming("PyYAML", "yaml") == "curated_alias"
    assert classify_naming("opencv-python", "cv2") == "curated_alias"
    assert classify_naming("beautifulsoup4", "bs4") == "curated_alias"


def test_classify_other():
    assert classify_naming("zope.interface", "zope") == "other"  # namespace, not curated


# --- end-to-end injection on synthetic fixtures (deterministic) ------------

def test_identity_dep_not_recovered_by_generator(tmp_path):
    repo = write_synthetic_fixture(tmp_path / "id", "requests", "requests")
    res = inject_and_score(repo, "fx/identity", "requests", "requests")
    assert res["naming"] == "identity"
    assert res["generator_recovered"] is False
    assert res["verifier_recovered"] is False


def test_curated_alias_recovered_by_generator(tmp_path):
    repo = write_synthetic_fixture(tmp_path / "alias", "PyYAML", "yaml")
    res = inject_and_score(repo, "fx/alias", "PyYAML", "yaml")
    assert res["naming"] == "curated_alias"
    assert res["generator_recovered"] is True   # yaml -> PyYAML via curated table
    assert res["verifier_recovered"] is False   # declared-only, never re-adds


# --- aggregate ------------------------------------------------------------

def test_aggregate_buckets_by_naming():
    results = [
        {"naming": "identity", "generator_recovered": False, "verifier_recovered": False},
        {"naming": "identity", "generator_recovered": False, "verifier_recovered": False},
        {"naming": "curated_alias", "generator_recovered": True, "verifier_recovered": False},
    ]
    agg = aggregate_faults(results)
    assert agg["identity"] == {"total": 2, "generator_recovered": 0, "verifier_recovered": 0}
    assert agg["curated_alias"] == {"total": 1, "generator_recovered": 1, "verifier_recovered": 0}
