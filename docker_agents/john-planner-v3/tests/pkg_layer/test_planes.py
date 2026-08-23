"""TDD tests for the pkg_layer plane data model (Task 1).

Pure data structures: frozen dataclasses + enums for the four planes
(Contract / Closure / Usage / Environment-to-come). No I/O, no Docker, no
LLM — construction + the small set of pure query helpers on PackageLayer.
"""
from __future__ import annotations

import dataclasses

import pytest

from python_deps.pkg_layer.planes import (
    CandidateEdge,
    ClosurePkg,
    DeclaredDep,
    ImportContext,
    ImportNode,
    PackageLayer,
    ProvidedEdge,
    Tier,
    Trust,
)


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #

def test_import_context_members():
    assert {c.name for c in ImportContext} == {
        "RUNTIME", "TEST", "OPTIONAL", "TYPING", "DYNAMIC",
    }


def test_tier_members():
    assert {t.name for t in Tier} == {"DIRECT", "TRANSITIVE"}


def test_trust_members():
    assert {t.name for t in Trust} == {
        "DECLARED", "CLOSURE", "CERTIFIED", "CANDIDATE",
    }


# --------------------------------------------------------------------------- #
# Construction + frozen-ness
# --------------------------------------------------------------------------- #

def test_declared_dep_construction_and_frozen():
    dep = DeclaredDep(name="Flask", kind="install_requires", specifier=">=2.0",
                       marker=None, group=None)
    assert dep.name == "Flask"
    assert dep.kind == "install_requires"
    assert dep.specifier == ">=2.0"
    assert dep.marker is None
    assert dep.group is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        dep.name = "flask"  # type: ignore[misc]


def test_declared_dep_optional_extra_group():
    dep = DeclaredDep(name="requests", kind="extras_require",
                       specifier=None, marker=None, group="http")
    assert dep.group == "http"


def test_closure_pkg_construction_and_frozen():
    pkg = ClosurePkg(name="flask", version="2.3.0", tier=Tier.DIRECT)
    assert pkg.name == "flask"
    assert pkg.version == "2.3.0"
    assert pkg.tier is Tier.DIRECT
    with pytest.raises(dataclasses.FrozenInstanceError):
        pkg.version = "9.9.9"  # type: ignore[misc]


def test_import_node_construction_and_frozen():
    node = ImportNode(name="flask", context=ImportContext.RUNTIME,
                       provenance=("app.py",))
    assert node.name == "flask"
    assert node.context is ImportContext.RUNTIME
    assert node.provenance == ("app.py",)
    with pytest.raises(dataclasses.FrozenInstanceError):
        node.name = "other"  # type: ignore[misc]


def test_provided_edge_construction_and_frozen():
    edge = ProvidedEdge(import_name="flask", dist="Flask", origin="certified")
    assert edge.import_name == "flask"
    assert edge.dist == "Flask"
    assert edge.origin == "certified"
    with pytest.raises(dataclasses.FrozenInstanceError):
        edge.origin = "guessed"  # type: ignore[misc]


def test_candidate_edge_construction_and_frozen():
    edge = CandidateEdge(import_name="yaml", dist="PyYAML",
                          trust=Trust.CANDIDATE, verify_required=True)
    assert edge.import_name == "yaml"
    assert edge.dist == "PyYAML"
    assert edge.trust is Trust.CANDIDATE
    assert edge.verify_required is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        edge.verify_required = False  # type: ignore[misc]


def test_package_layer_construction_and_frozen():
    layer = PackageLayer(contract=(), closure=(), imports=(), provided=())
    assert layer.contract == ()
    assert layer.closure == ()
    assert layer.imports == ()
    assert layer.provided == ()
    with pytest.raises(dataclasses.FrozenInstanceError):
        layer.contract = ("bogus",)  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Fixture: a small hand-built layer exercising every query helper
# --------------------------------------------------------------------------- #

@pytest.fixture
def layer() -> PackageLayer:
    contract = (
        DeclaredDep(name="Flask", kind="install_requires", specifier=">=2.0",
                    marker=None, group=None),
        # Same distribution under a different capitalization/separator ->
        # must canon-dedup with "Flask" in declared_names().
        DeclaredDep(name="flask", kind="install_requires", specifier=None,
                    marker=None, group=None),
        DeclaredDep(name="requests", kind="extras_require", specifier=None,
                    marker=None, group="http"),
    )
    closure = (
        ClosurePkg(name="flask", version="2.3.0", tier=Tier.DIRECT),
        ClosurePkg(name="requests", version="2.31.0", tier=Tier.DIRECT),
        ClosurePkg(name="werkzeug", version="2.3.0", tier=Tier.TRANSITIVE),
        ClosurePkg(name="urllib3", version="2.0.0", tier=Tier.TRANSITIVE),
    )
    imports = (
        ImportNode(name="flask", context=ImportContext.RUNTIME,
                   provenance=("app.py",)),
        ImportNode(name="requests", context=ImportContext.RUNTIME,
                   provenance=("app.py",)),
        ImportNode(name="pytest", context=ImportContext.TEST,
                   provenance=("tests/test_app.py",)),
        ImportNode(name="typing_extensions", context=ImportContext.TYPING,
                   provenance=("app.py",)),
        ImportNode(name="importlib", context=ImportContext.DYNAMIC,
                   provenance=("app.py",)),
    )
    provided = (
        ProvidedEdge(import_name="flask", dist="Flask", origin="certified"),
        # Same import under different case -> must canon-dedup in
        # provided_imports().
        ProvidedEdge(import_name="Flask", dist="Flask", origin="certified"),
        ProvidedEdge(import_name="requests", dist="requests", origin="certified"),
    )
    return PackageLayer(contract=contract, closure=closure, imports=imports,
                         provided=provided)


# --------------------------------------------------------------------------- #
# Query helpers
# --------------------------------------------------------------------------- #

def test_runtime_imports_filters_to_runtime_context(layer):
    out = layer.runtime_imports()
    assert isinstance(out, tuple)
    assert {n.name for n in out} == {"flask", "requests"}
    assert all(n.context is ImportContext.RUNTIME for n in out)


def test_runtime_imports_excludes_non_runtime(layer):
    out = layer.runtime_imports()
    names = {n.name for n in out}
    assert "pytest" not in names
    assert "typing_extensions" not in names
    assert "importlib" not in names


def test_direct_packages_filters_to_direct_tier(layer):
    out = layer.direct_packages()
    assert isinstance(out, tuple)
    assert {p.name for p in out} == {"flask", "requests"}
    assert all(p.tier is Tier.DIRECT for p in out)


def test_transitive_packages_filters_to_transitive_tier(layer):
    out = layer.transitive_packages()
    assert isinstance(out, tuple)
    assert {p.name for p in out} == {"werkzeug", "urllib3"}
    assert all(p.tier is Tier.TRANSITIVE for p in out)


def test_declared_names_canon_dedup(layer):
    out = layer.declared_names()
    assert isinstance(out, frozenset)
    # "Flask" and "flask" canon-dedup to one entry.
    assert out == {"flask", "requests"}


def test_provided_imports_canon_dedup(layer):
    out = layer.provided_imports()
    assert isinstance(out, frozenset)
    # "flask" and "Flask" canon-dedup to one entry.
    assert out == {"flask", "requests"}


def test_declared_names_pep503_separator_normalization():
    contract = (
        DeclaredDep(name="typing_extensions", kind="install_requires",
                    specifier=None, marker=None, group=None),
        DeclaredDep(name="typing.extensions", kind="install_requires",
                    specifier=None, marker=None, group=None),
        DeclaredDep(name="Typing-Extensions", kind="install_requires",
                    specifier=None, marker=None, group=None),
    )
    layer = PackageLayer(contract=contract, closure=(), imports=(), provided=())
    assert layer.declared_names() == {"typing-extensions"}


def test_query_helpers_are_pure(layer):
    # Calling twice yields equal results and never mutates the source layer.
    before = dataclasses.astuple(layer)
    first = layer.runtime_imports()
    second = layer.runtime_imports()
    assert first == second
    assert dataclasses.astuple(layer) == before


def test_empty_layer_query_helpers_return_empty():
    empty = PackageLayer(contract=(), closure=(), imports=(), provided=())
    assert empty.runtime_imports() == ()
    assert empty.direct_packages() == ()
    assert empty.transitive_packages() == ()
    assert empty.declared_names() == frozenset()
    assert empty.provided_imports() == frozenset()
