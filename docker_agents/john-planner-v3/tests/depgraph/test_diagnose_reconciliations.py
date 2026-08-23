"""Reconciliation tests amending the diagnosis-router companion plan
(.superpowers/sdd/task-3-brief.md):

  2. PEP-503-normalize ``RepoContext.invalid_names`` comparisons.
  4. Conservative ``import_name_error`` routing (weaker evidence than
     ``module_not_found`` — only trust the package guess when the failed
     "from" target is itself the bare top-level name; a dotted/nested
     "from" stays AMBIGUOUS rather than minting a bogus package node).
  5. ``diagnose_all`` — pure batch helper for Phase 6's orchestrator routing.

Pure, no Docker.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from python_deps.depgraph.diagnose import (
    Diagnosis, Mode, RepoContext, _norm, diagnose, diagnose_all,
    make_diagnostic_classifier,
)
from python_deps.depgraph.schema import NodeType


# ---------------------------------------------------------------------------
# Reconciliation 2: normalized invalid_names comparison (PEP 503)
# ---------------------------------------------------------------------------

def test_norm_helper_lowercases_strips_and_dashes_underscores():
    assert _norm("Frobnicate_9000") == "frobnicate-9000"
    assert _norm("  Already-Dashed  ") == "already-dashed"
    assert _norm(None) == ""  # type: ignore[arg-type]
    assert _norm("") == ""


def test_previously_invalid_name_matches_normalized_form():
    # ctx stores the pre-normalized form; the curated mapping's mixed-case
    # distribution name ("fitz" -> "PyMuPDF") must still match via _norm on
    # both sides. (An unmapped import now resolves to name=None and can never
    # match ctx.invalid_names, so this must use a curated import.)
    ctx = RepoContext(invalid_names=frozenset({"pymupdf"}))
    d = diagnose("python app.py",
                 "ModuleNotFoundError: No module named 'fitz'",
                 ctx)
    assert d.mode is Mode.INVALID_ATTEMPT
    assert d.discovery is None


# ---------------------------------------------------------------------------
# Reconciliation 4: conservative import_name_error
# ---------------------------------------------------------------------------

def test_import_name_error_dotted_from_routes_ambiguous():
    # "cannot import name 'helper' from 'mypkg.utils'" — the failed reference is
    # a nested submodule, not a bare top-level name. classify_observation would
    # guess a package named after the top-level segment ("mypkg") with no real
    # evidence it's the missing distribution (Y may be installed-but-outdated,
    # mid circular-import, etc). Stay AMBIGUOUS rather than mint that guess.
    d = diagnose("python app.py",
                 "ImportError: cannot import name 'helper' from 'mypkg.utils'",
                 RepoContext())
    assert d.mode is Mode.AMBIGUOUS
    assert d.discovery is None


def test_import_name_error_flat_from_routes_environment():
    # A bare top-level "from" target is exactly what classify_observation
    # mapped -> trust it (module_not_found keeps its existing handling; this
    # confirms the flat import_name_error case is not regressed to AMBIGUOUS).
    d = diagnose("python app.py",
                 "ImportError: cannot import name 'get' from 'requests'",
                 RepoContext())
    assert d.mode is Mode.ENVIRONMENT
    assert d.discovery is not None
    assert d.discovery.node_type is NodeType.PACKAGE


def test_import_name_error_local_dotted_from_routes_repo_internal_ref():
    ctx = RepoContext(local_names=frozenset({"mypkg"}))
    d = diagnose("python app.py",
                 "ImportError: cannot import name 'helper' from 'mypkg.utils'",
                 ctx)
    assert d.mode is Mode.REPO_INTERNAL_REF
    assert d.discovery is None


# ---------------------------------------------------------------------------
# Reconciliation 5: diagnose_all
# ---------------------------------------------------------------------------

def test_diagnose_all_maps_each_observation_in_order():
    obs = (
        ("python app.py", "ModuleNotFoundError: No module named 'requests'"),
        ("python -m pytest -q", "E       assert 1 == 2\nAssertionError"),
    )
    results = diagnose_all(obs, RepoContext())
    assert isinstance(results, tuple)
    assert len(results) == 2
    assert all(isinstance(r, Diagnosis) for r in results)
    assert results[0].mode is Mode.ENVIRONMENT
    assert results[1].mode is Mode.RESIDUAL


def test_diagnose_all_empty_observations_returns_empty_tuple():
    assert diagnose_all((), RepoContext()) == ()


# ---------------------------------------------------------------------------
# Part C (residual-giveup-fix.md): a WEAK tool/config signal that co-occurs
# with an AssertionError is RESIDUAL, not a mintable ENVIRONMENT obligation.
# Kills the click-repo phantom (`tool:less`/`config:PAGER` minted from an
# incidental token in a pytest blob whose real failure is a stable
# AssertionError) at the diagnosis root, before it ever reaches the graph.
# ---------------------------------------------------------------------------

def test_tool_signal_with_assertion_error_is_residual_not_environment():
    # The click-repo phantom: an incidental 'less' token inside a pytest blob
    # whose REAL failure is a stable AssertionError. Before Part C this routed
    # ENVIRONMENT (tool) and minted an unfixable tool:less obligation that the
    # scheduler re-handed every cycle.
    d = diagnose(
        "python -m pytest -q",
        "...FileNotFoundError: [Errno 2] No such file or directory: 'less'...\n"
        "E   AssertionError\n",
        RepoContext(),
    )
    assert d.mode is Mode.RESIDUAL
    assert d.discovery is None


def test_config_signal_with_assertion_error_is_residual_not_environment():
    # Same reconciliation for the CONFIG tier (e.g. a missing PAGER/LESS/TERM
    # env var scraped from the same traceback as the real AssertionError).
    d = diagnose(
        "python -m pytest -q",
        "KeyError: 'PAGER'\nE   AssertionError\n",
        RepoContext(),
    )
    assert d.mode is Mode.RESIDUAL
    assert d.discovery is None


def test_tool_signal_without_assertion_error_still_routes_environment():
    # Positive control: the identical weak tool signal with NO co-occurring
    # AssertionError is a genuine environment requirement — unaffected by the
    # guard (it only fires when the two signals are co-reported).
    d = diagnose(
        "python -m pytest -q",
        "FileNotFoundError: [Errno 2] No such file or directory: 'less'\n",
        RepoContext(),
    )
    assert d.mode is Mode.ENVIRONMENT
    assert d.discovery is not None
    assert d.discovery.node_type is NodeType.TOOL


def test_strong_tier_systemlib_not_downgraded_by_assertion_co_occurrence():
    # STRONG, anchored tiers (SystemLib soname, matched from a specific
    # failure signature rather than an arbitrary token) must NOT be
    # downgraded even when an AssertionError co-occurs — the guard is scoped
    # to the WEAK TOOL/CONFIG tiers only.
    d = diagnose(
        "python app.py",
        "ImportError: libGL.so.1: cannot open shared object file\n"
        "E   AssertionError\n",
        RepoContext(),
    )
    assert d.mode is Mode.ENVIRONMENT
    assert d.discovery is not None
    assert d.discovery.node_type is NodeType.SYSTEM_LIB


def test_make_diagnostic_classifier_mints_no_node_for_phantom_tool_signal():
    # Ingest-time seam proof: RESIDUAL -> discovery=None -> no node minted by
    # ingest_runtime_failures's classifier plumbing.
    classifier = make_diagnostic_classifier(RepoContext())
    result = classifier(
        "python -m pytest -q",
        "...FileNotFoundError: [Errno 2] No such file or directory: 'less'...\n"
        "E   AssertionError\n",
    )
    assert result is None
