"""Tests for diagnose.diagnose routing (pure, no Docker)."""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from python_deps.depgraph.diagnose import Mode, RepoContext, diagnose
from python_deps.depgraph.schema import NodeType


def test_external_import_routes_environment_with_discovery():
    d = diagnose("python app.py",
                 "ModuleNotFoundError: No module named 'requests'",
                 RepoContext())
    assert d.mode is Mode.ENVIRONMENT
    assert d.discovery is not None
    assert d.discovery.node_type is NodeType.PACKAGE


def test_local_import_routes_repo_internal_ref_no_discovery():
    ctx = RepoContext(local_names=frozenset({"docs_src"}))
    d = diagnose("python -m docs_src.build",
                 "ModuleNotFoundError: No module named 'docs_src'",
                 ctx)
    assert d.mode is Mode.REPO_INTERNAL_REF
    assert d.discovery is None


def test_no_matching_distribution_routes_invalid_attempt():
    d = diagnose("pip install frobnicate9000",
                 "ERROR: No matching distribution found for frobnicate9000",
                 RepoContext())
    assert d.mode is Mode.INVALID_ATTEMPT
    assert d.discovery is None


def test_previously_invalid_name_routes_invalid_attempt():
    # An external import whose mapped package was already disproven. Use a
    # curated import ("django_filters" -> "django-filter") since an unmapped
    # import now resolves to name=None and can never match ctx.invalid_names.
    ctx = RepoContext(invalid_names=frozenset({"django-filter"}))
    d = diagnose("python app.py",
                 "ModuleNotFoundError: No module named 'django_filters'",
                 ctx)
    assert d.mode is Mode.INVALID_ATTEMPT
    assert d.discovery is None


def test_native_lib_routes_environment_systemlib():
    d = diagnose("python app.py",
                 "ImportError: libGL.so.1: cannot open shared object file",
                 RepoContext())
    assert d.mode is Mode.ENVIRONMENT
    assert d.discovery is not None
    assert d.discovery.node_type is NodeType.SYSTEM_LIB


def test_assertion_routes_residual():
    d = diagnose("python -m pytest -q",
                 "E       assert 1 == 2\nAssertionError",
                 RepoContext())
    assert d.mode is Mode.RESIDUAL
    assert d.discovery is None


def test_unclassified_routes_ambiguous():
    d = diagnose("python app.py", "Segmentation fault (core dumped)", RepoContext())
    assert d.mode is Mode.AMBIGUOUS
    assert d.discovery is None


def test_none_named_discovery_does_not_route_invalid_attempt(monkeypatch):
    # Task 4 (runtime_classify) can now emit Discovery(name=None, ...) for an
    # import that failed to map to any distribution. _norm(None) coerces to
    # "" (see test_diagnose_reconciliations.py), so if "" were ever recorded
    # as a disproven name, a None-named discovery would wrongly and
    # permanently match it as a previously-invalid attempt. A discovery with
    # no name is unnameable -- it cannot be "previously disproven" -- and
    # must route exactly as any other present-but-not-invalid name does.
    import python_deps.depgraph.diagnose as diagnose_module
    from python_deps.depgraph.runtime_classify import Discovery
    from python_deps.depgraph.schema import Layer

    unresolved = Discovery(
        node_type=NodeType.PACKAGE,
        name=None,
        layer=Layer.PIP,
        evidence="ModuleNotFoundError: No module named 'mystery'",
        check_command='python3 -c "import mystery"',
        data={"import_name": "mystery"},
    )
    monkeypatch.setattr(diagnose_module, "classify_observation", lambda cmd, out: unresolved)

    ctx = RepoContext(invalid_names=frozenset({""}))
    d = diagnose("python app.py", "ModuleNotFoundError: No module named 'mystery'", ctx)

    assert d.mode is not Mode.INVALID_ATTEMPT
    assert d.mode is Mode.ENVIRONMENT
    assert d.discovery is unresolved
