"""P1.4 — Phase-A repair fixpoint: resolve -> install -> look -> repair loop.

Drives ``build_dep_graph`` over tiny fixture repos through the degraded
``uv pip compile`` fallback path (``uv lock`` returns non-ok, so no lock file is
produced), so resolution is fully controlled by a ``SequencedFakeExecutor`` whose
per-round output differs. The RECORD-union coverage oracle is driven by an
INJECTED fake ``record_provider`` (never the network, never a real container):
this is what proves the Corrections independently of any production reader.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

from conftest import SequencedFakeExecutor  # type: ignore

from python_deps.depgraph.build import (
    build_dep_graph,
    reconcile_packages,
)
from python_deps.depgraph.coverage import resolved_record_coverage
from python_deps.depgraph.executor import CommandResult
from python_deps.depgraph.ids import import_id, package_id
from python_deps.depgraph.resolve_errors import _offending_root_names
from python_deps.depgraph.resolve_lock import _package_node
from python_deps.depgraph.schema import (
    DepGraph,
    DiscoveredBy,
    Edge,
    EdgeType,
    Layer,
    Node,
    NodeType,
    State,
)
from python_deps.import_mapping import normalize_package_name


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _r(returncode=0, stdout="", stderr=""):
    return CommandResult(command="", returncode=returncode, stdout=stdout, stderr=stderr)


def _repo(tmp_path, app_src, pyproject=None):
    (tmp_path / "app.py").write_text(app_src)
    if pyproject is not None:
        (tmp_path / "pyproject.toml").write_text(pyproject)
    return str(tmp_path)


def _provider(mapping):
    """A fake RECORD provider: canon-dist -> set of top-level modules (else None)."""
    norm = {normalize_package_name(k): set(v) for k, v in mapping.items()}

    def provider(dist):
        return norm.get(normalize_package_name(dist))

    return provider


def _null_provider(_dist):
    return None


def _fallback_executor(compile_queue, *, install=None, packages_dist=None):
    """SequencedFakeExecutor that fails ``uv lock`` (forcing the pip-compile
    fallback) and returns queued ``uv pip compile`` closures per round."""
    responses = {
        "uv lock": [_r(1, stderr="lock unavailable")],
        "uv pip compile": [_r(0, stdout=text) for text in compile_queue],
    }
    if install is not None:
        responses["pip install"] = list(install)
    if packages_dist is not None:
        responses["packages_distributions"] = list(packages_dist)
    return SequencedFakeExecutor(responses=responses, default=_r(0))


def _build_counting(repo, ex, provider, **kwargs):
    """Run build_dep_graph, counting resolve_closure invocations (= rounds)."""
    import python_deps.depgraph.build as build_mod

    counter = {"resolve": 0}
    orig = build_mod.resolve_closure

    def spy(*args, **kw):
        counter["resolve"] += 1
        return orig(*args, **kw)

    with patch.object(build_mod, "resolve_closure", side_effect=spy):
        graph = build_dep_graph(
            repo, ex, host_executor=ex, record_provider=provider, **kwargs
        )
    return graph, counter


def _packages(graph):
    return [n for n in graph.nodes if n.type is NodeType.PACKAGE]


def _audit_packages(graph):
    return [
        n
        for n in graph.nodes
        if n.type is NodeType.PACKAGE and n.discovered_by is DiscoveredBy.AUDIT
    ]


# --------------------------------------------------------------------------- #
# coverage.py — resolved_record_coverage (Correction 3, pure)
# --------------------------------------------------------------------------- #
def _pkg(name, version="1.0", state=State.UNKNOWN):
    from dataclasses import replace

    return replace(_package_node(name, version), state=state)


def test_coverage_unions_provided_modules_over_resolved_dists():
    nodes = [_pkg("PyYAML"), _pkg("requests")]
    provider = _provider({"PyYAML": {"yaml"}, "requests": {"requests"}})
    assert resolved_record_coverage(nodes, provider) == {"yaml", "requests"}


def test_coverage_blind_dist_contributes_nothing():
    nodes = [_pkg("PyYAML"), _pkg("mystery")]
    provider = _provider({"PyYAML": {"yaml"}})  # mystery -> None
    assert resolved_record_coverage(nodes, provider) == {"yaml"}


def test_coverage_lowercases_module_names():
    nodes = [_pkg("Pillow")]
    provider = _provider({"Pillow": {"PIL"}})
    assert resolved_record_coverage(nodes, provider) == {"pil"}


def test_coverage_excludes_missing_placeholder_packages():
    nodes = [_pkg("PyYAML"), _pkg("ghost", version=None, state=State.MISSING)]
    provider = _provider({"PyYAML": {"yaml"}, "ghost": {"ghost"}})
    # The MISSING placeholder is never asked / never counted.
    assert resolved_record_coverage(nodes, provider) == {"yaml"}


def test_coverage_counts_resolved_but_failed_to_build_dist_as_provided():
    """Correction 3: a dist that resolved but would FAIL to build (absent from a
    post-install packages_distributions) is still PROVIDED here, because the
    oracle reads RECORD metadata via the injected provider, never install state.
    """
    nodes = [_pkg("psycopg2")]
    # The injected provider (RECORD/wheel-metadata) DOES provide it, even though
    # a post-install packages_distributions() would be empty (build failed).
    provider = _provider({"psycopg2": {"psycopg2"}})
    assert resolved_record_coverage(nodes, provider) == {"psycopg2"}


# --------------------------------------------------------------------------- #
# Fixpoint — convergence / no-op / bound / ambiguous / optional
# --------------------------------------------------------------------------- #
def test_fixpoint_converges_on_under_declaration(tmp_path):
    """Repo declares nothing but imports yaml. Round 1: empty closure -> missing
    {yaml} -> grounds PyYAML -> ACCEPT. Round 2: closure has PyYAML -> covered ->
    break. PyYAML enters as an AUDIT root; yaml is not flagged; loop == 2 rounds.
    """
    repo = _repo(tmp_path, "import yaml\n")
    # PyYAML installs (pip install -> default rc0), so the CONTAINER's post-install
    # packages_distributions() honestly reports yaml->PyYAML; Stage 4a (the sole
    # Import->Package source now) certifies that edge so yaml is not flagged.
    ex = _fallback_executor(
        ["PyYAML==6.0\n    # via -r -\n"],
        packages_dist=[_r(0, stdout='{"yaml": ["PyYAML"]}')],
    )
    provider = _provider({"PyYAML": {"yaml"}})

    graph, counter = _build_counting(repo, ex, provider)

    pyyaml = graph.get(package_id("PyYAML", "6.0"))
    assert pyyaml is not None
    assert pyyaml.discovered_by is DiscoveredBy.AUDIT
    assert graph.get(import_id("yaml")).data.get("unresolved") is not True
    assert counter["resolve"] == 2


def test_fixpoint_default_composite_provider_repairs_via_pypi_fetch(tmp_path):
    """P1.5 — the DEFAULT path repairs a real under-declaration (no injected
    record_provider). Repo imports ``yaml``, declares nothing. Build constructs
    its OWN composite default = ``composite_record_provider(default_record_provider
    (container), pypi_record_provider())``; the only thing stubbed is the network
    fetch seam (a fake wheel read). The post-install provider is blind for the
    not-yet-installed candidate, so the composite consults the fake PyPI fetch,
    grounds ``PyYAML``, and the fixpoint ACCEPTs it as an AUDIT root. This is the
    test P1.4 could not write — it proves production repair is no longer inert.
    """
    import python_deps.depgraph.build as build_mod
    from python_deps.depgraph.coverage import pypi_record_provider

    repo = _repo(tmp_path, "import yaml\n")
    # PyYAML installs, so the container honestly reports yaml->PyYAML; Stage 4a
    # certifies the edge (sole Import->Package source) so yaml is not flagged.
    ex = _fallback_executor(
        ["PyYAML==6.0\n    # via -r -\n"],
        packages_dist=[_r(0, stdout='{"yaml": ["PyYAML"]}')],
    )

    fetch_calls = {"n": 0}

    def fake_wheel_fetch(dist):
        fetch_calls["n"] += 1
        return {"yaml"} if normalize_package_name(dist) == "pyyaml" else None

    def fake_pypi_provider():
        # Same constructor build.py calls by default, but with the network seam
        # replaced by the fake — the composite wiring itself stays real.
        return pypi_record_provider(fetch=fake_wheel_fetch)

    with patch.object(build_mod, "pypi_record_provider", fake_pypi_provider):
        # NO record_provider injected -> build_dep_graph builds the composite default.
        graph = build_dep_graph(repo, ex, host_executor=ex)

    pyyaml = graph.get(package_id("PyYAML", "6.0"))
    assert pyyaml is not None
    assert pyyaml.discovered_by is DiscoveredBy.AUDIT
    assert graph.get(import_id("yaml")).data.get("unresolved") is not True
    assert fetch_calls["n"] >= 1  # the pre-install PyPI wheel read grounded the candidate


def test_fixpoint_well_declared_repo_does_zero_repair(tmp_path):
    """Declares + imports requests; coverage covers it round 1 -> break with one
    install and no AUDIT nodes."""
    repo = _repo(
        tmp_path,
        "import requests\n",
        '[project]\nname="fx"\nversion="0"\ndependencies=["requests"]\n',
    )
    ex = _fallback_executor(["requests==2.31.0\n    # via -r -\n"], install=[_r(0)])
    provider = _provider({"requests": {"requests"}})

    graph, counter = _build_counting(repo, ex, provider)

    assert counter["resolve"] == 1
    assert _audit_packages(graph) == []
    assert sum(1 for c in ex.calls if "pip install" in c) == 1


def test_fixpoint_bound_and_honest_residue(tmp_path, caplog):
    """Unresolvable import (provider None for every candidate) -> repair cannot
    progress -> loop stops, import flagged unresolved, no fabricated root, a
    warning is logged, no exception."""
    repo = _repo(tmp_path, "import zzznope\n")
    ex = _fallback_executor([""])  # closure stays empty (nothing to compile)
    provider = _null_provider

    with caplog.at_level(logging.WARNING):
        graph, _counter = _build_counting(repo, ex, provider)

    assert graph.get(import_id("zzznope")).data.get("unresolved") is True
    assert _packages(graph) == []
    assert any("phase-A" in rec.message for rec in caplog.records)


def test_fixpoint_ambiguous_does_not_pick(tmp_path):
    """Two canon-distinct confirming dists -> AMBIGUOUS -> no root added."""
    repo = _repo(tmp_path, "import attr\n")
    ex = _fallback_executor([""])
    # Both normalize variants confirm the module -> genuine ambiguity.
    provider = _provider({"attr": {"attr"}, "python-attr": {"attr"}})

    graph, _counter = _build_counting(repo, ex, provider)

    assert _audit_packages(graph) == []
    assert graph.get(package_id("attr", "1.0")) is None
    assert graph.get(package_id("attrs", "1.0")) is None
    assert not any(n.type is NodeType.PACKAGE for n in graph.nodes)


def test_fixpoint_optional_import_never_triggers_repair(tmp_path):
    """A guarded ``try: import ujson`` (tagged optional by the scan) is not in the
    missing set -> 0 repair rounds, not flagged."""
    repo = _repo(tmp_path, "try:\n    import ujson\nexcept ImportError:\n    pass\n")
    ex = _fallback_executor([""])
    provider = _null_provider  # no provider at all

    graph, counter = _build_counting(repo, ex, provider)

    assert counter["resolve"] == 1  # one look, no repair round
    assert _audit_packages(graph) == []
    uj = graph.get(import_id("ujson"))
    assert uj is not None
    assert uj.data.get("optional") is True
    assert uj.data.get("unresolved") is not True


def test_fixpoint_if_guarded_not_re_added_but_unconditional_still_rescued(tmp_path):
    """Precision fix AND its guard-rail in one repo. ``app.py`` imports ``yaml``
    unconditionally (a genuine under-declaration) and ``winloop`` ONLY under an
    ``if sys.platform == 'win32':`` guard. On the (linux) target the guarded
    import's declared-but-marker-excluded provider is correctly absent from the
    closure, so the audit must NOT re-add ``winloop`` -- while the unconditional
    ``yaml`` IS still rescued as an AUDIT root (the audit's legitimate job).
    ``winloop`` is even given a confirming provider entry to prove it is skipped
    because it is optional, not because grounding would have denied it."""
    repo = _repo(
        tmp_path,
        "import sys\n"
        "import yaml\n"
        "if sys.platform == 'win32':\n"
        "    import winloop\n",
    )
    ex = _fallback_executor(
        ["PyYAML==6.0\n    # via -r -\n"],
        packages_dist=[_r(0, stdout='{"yaml": ["PyYAML"]}')],
    )
    provider = _provider({"PyYAML": {"yaml"}, "winloop": {"winloop"}})

    graph, counter = _build_counting(repo, ex, provider)

    # yaml -> PyYAML rescued as an AUDIT root; winloop never re-added.
    assert sorted(n.name for n in _audit_packages(graph)) == ["PyYAML"]
    assert graph.get(package_id("PyYAML", "6.0")).discovered_by is DiscoveredBy.AUDIT
    assert not any(
        n.type is NodeType.PACKAGE and "winloop" in n.name.lower() for n in graph.nodes
    )
    # winloop import is optional, so it is neither repaired nor flagged unresolved.
    win = graph.get(import_id("winloop"))
    assert win is not None
    assert win.data.get("optional") is True
    assert win.data.get("unresolved") is not True
    assert counter["resolve"] == 2  # look -> repair yaml -> converge


# --------------------------------------------------------------------------- #
# Correction 2b — attempted-set termination (oscillation)
# --------------------------------------------------------------------------- #
def test_fixpoint_attempted_set_stops_oscillation(tmp_path, caplog):
    """A grounded candidate is ACCEPTED then evicted by resolution (re-appears
    missing). The attempted-set prevents re-adding the same pair, so the loop
    stops (bounded), residue flagged, oscillation warning logged, no exception."""
    repo = _repo(tmp_path, "import widget\n")
    # Round 2 closure is EMPTY (the added root failed to materialize == evicted).
    ex = _fallback_executor([""])
    provider = _provider({"widget": {"widget"}})

    with caplog.at_level(logging.WARNING):
        graph, counter = _build_counting(repo, ex, provider)

    # widget was accepted once (round 1) then the pair was not re-added (round 2).
    assert counter["resolve"] <= 2
    assert graph.get(import_id("widget")).data.get("unresolved") is True
    assert _packages(graph) == []
    assert any("phase-A" in rec.message for rec in caplog.records)


# --------------------------------------------------------------------------- #
# Correction 2c — per-round Package node/edge reconcile (version shift)
# --------------------------------------------------------------------------- #
def test_reconcile_packages_drops_stale_versioned_node():
    old = _package_node("foo", "1.0")
    other = _package_node("libr", "1.0")
    graph = DepGraph().with_node(other).with_node(old).with_edge(
        Edge(src=other.id, dst=old.id, relation=EdgeType.REQUIRES, origin="resolver")
    )
    prev_pkg_ids = {old.id, other.id}
    new_foo = _package_node("foo", "2.0")
    new_edges = [
        Edge(src=other.id, dst=new_foo.id, relation=EdgeType.REQUIRES, origin="resolver")
    ]

    out = reconcile_packages(graph, [other, new_foo], new_edges, prev_pkg_ids)

    assert out.get(package_id("foo", "1.0")) is None
    assert out.get(package_id("foo", "2.0")) is not None
    # No dangling edge to the removed v1 node survives.
    assert not any(package_id("foo", "1.0") in (e.src, e.dst) for e in out.edges)


def test_reconcile_packages_drops_stale_edge_between_survivors():
    a = _package_node("a", "1.0")
    b = _package_node("b", "1.0")
    graph = DepGraph().with_node(a).with_node(b).with_edge(
        Edge(src=a.id, dst=b.id, relation=EdgeType.REQUIRES, origin="resolver")
    )
    prev_pkg_ids = {a.id, b.id}
    # New resolve emits both nodes but NO a->b edge anymore.
    out = reconcile_packages(graph, [a, b], [], prev_pkg_ids)

    assert out.get(a.id) is not None and out.get(b.id) is not None
    assert not any(e.src == a.id and e.dst == b.id for e in out.edges)


def test_fixpoint_reconciles_stale_node_across_version_shift(tmp_path):
    """Two sequenced resolves where a transitive package's version changes ->
    the round-1 ``pkg:foo==1.0`` node is ABSENT after round 2 (only ==2.0)."""
    repo = _repo(
        tmp_path,
        "import yaml\n",
        '[project]\nname="fx"\nversion="0"\ndependencies=["libr"]\n',
    )
    ex = _fallback_executor(
        [
            "libr==1.0\n    # via -r -\nfoo==1.0\n    # via libr\n",
            "libr==1.0\n    # via -r -\nfoo==2.0\n    # via libr\nPyYAML==6.0\n    # via -r -\n",
        ]
    )
    provider = _provider({"PyYAML": {"yaml"}})

    graph, counter = _build_counting(repo, ex, provider)

    assert counter["resolve"] == 2
    assert graph.get(package_id("foo", "1.0")) is None
    assert graph.get(package_id("foo", "2.0")) is not None
    assert not any(package_id("foo", "1.0") in (e.src, e.dst) for e in graph.edges)


# --------------------------------------------------------------------------- #
# Correction 3 — RECORD-union oracle, build-failure not misrouted (fixpoint)
# --------------------------------------------------------------------------- #
def test_fixpoint_build_failure_not_misrouted_to_repair(tmp_path):
    """A resolved dist FAILS to install (empty post-install packages_distributions)
    but the injected record_provider DOES provide the import -> coverage marks it
    PROVIDED -> NOT missing -> repair fabricates no alternative."""
    repo = _repo(
        tmp_path,
        "import themod\n",
        '[project]\nname="fx"\nversion="0"\ndependencies=["somepkg"]\n',
    )
    ex = _fallback_executor(
        ["somepkg==1.0\n    # via -r -\n"],
        install=[_r(1, stderr="Failed building wheel for somepkg")],
        packages_dist=[_r(0, stdout="{}")],  # install failed -> empty
    )
    provider = _provider({"somepkg": {"themod"}})

    graph, counter = _build_counting(repo, ex, provider)

    # Coverage (RECORD-union) counts somepkg as providing themod despite the build
    # failure, so the fixpoint does NO repair round and fabricates NO alternative
    # package (the build failure is a Phase-B gap, not a Phase-A under-declaration).
    assert counter["resolve"] == 1  # covered on the first look -> no repair round
    assert _audit_packages(graph) == []
    assert graph.get(package_id("somepkg", "1.0")) is not None
    assert {n.name for n in _packages(graph)} == {"somepkg"}  # no fabricated alt


# --------------------------------------------------------------------------- #
# Correction 2a — _offending_root_names declared-drop-priority
# --------------------------------------------------------------------------- #
from types import SimpleNamespace  # noqa: E402


def _conflict(pkg, left_imp, right_imp):
    return SimpleNamespace(
        package=pkg,
        left=SimpleNamespace(imposed_by=left_imp),
        right=SimpleNamespace(imposed_by=right_imp),
    )


def test_offending_prefers_audit_over_declared_imposer():
    """A transitive conflict imposed by a DECLARED root and an AUDIT root drops
    the AUDIT root, never the declared one."""
    diag = SimpleNamespace(missing=[], conflicts=[_conflict("shared", "declared-d", "audit-a")])
    names = _offending_root_names(
        diag, {"declared-d", "audit-a"}, audit_root_names=frozenset({"audit-a"})
    )
    assert "audit-a" in names
    assert "declared-d" not in names


def test_offending_backward_compatible_without_audit_set():
    """With no audit set, behavior matches today (alphabetical drop of one root
    imposer)."""
    diag = SimpleNamespace(missing=[], conflicts=[_conflict("shared", "package-b", "package-c")])
    names = _offending_root_names(diag, {"package-b", "package-c"})
    assert len(names & {"package-b", "package-c"}) == 1


def test_offending_drops_shared_pin_when_no_audit_alternative():
    """The shared/conflicted pin is still dropped when it is a root and no AUDIT
    imposer is droppable."""
    diag = SimpleNamespace(missing=[], conflicts=[_conflict("a", "project", "package-b")])
    names = _offending_root_names(diag, {"a", "package-b"}, audit_root_names=frozenset())
    assert "a" in names
    assert "package-b" not in names


def test_resolve_closure_threads_audit_root_names(tmp_path):
    """resolve_closure forwards its audit_root_names down to _offending_root_names."""
    import python_deps.depgraph.resolve as resolve_mod
    from python_deps.depgraph.target_env import TargetEnv

    captured = {}
    orig = resolve_mod._offending_root_names

    def spy(diag, current_root_names, audit_root_names=frozenset()):
        captured["audit"] = audit_root_names
        return orig(diag, current_root_names, audit_root_names)

    env = TargetEnv(
        python_full="3.11.0",
        python_version="3.11",
        platform_machine="x86_64",
        sys_platform="linux",
        os_name="posix",
        platform_system="Linux",
        python_platform_tag="x86_64-manylinux_2_28",
    )
    ex = SequencedFakeExecutor(
        responses={"uv lock": [_r(1, stderr="x was not found in the registry")]},
        default=_r(1),
    )
    with patch.object(resolve_mod, "_offending_root_names", side_effect=spy):
        resolve_mod.resolve_closure(
            [(None, "x")],
            ex,
            target_env=env,
            project_dir=str(tmp_path),
            audit_root_names=frozenset({"x"}),
        )
    assert captured.get("audit") == frozenset({"x"})


def test_build_threads_repaired_into_resolve_audit_root_names(tmp_path):
    """After a repair adds an AUDIT root, build_dep_graph passes the repaired set
    as audit_root_names to the next resolve_closure call."""
    import python_deps.depgraph.build as build_mod

    repo = _repo(tmp_path, "import yaml\n")
    ex = _fallback_executor(["PyYAML==6.0\n    # via -r -\n"])
    provider = _provider({"PyYAML": {"yaml"}})

    seen = []
    orig = build_mod.resolve_closure

    def spy(*args, **kwargs):
        seen.append(kwargs.get("audit_root_names"))
        return orig(*args, **kwargs)

    with patch.object(build_mod, "resolve_closure", side_effect=spy):
        build_dep_graph(repo, ex, host_executor=ex, record_provider=provider)

    # Round 2's resolve carries the repaired dist (canon) in audit_root_names.
    assert any(a and "pyyaml" in a for a in seen)
