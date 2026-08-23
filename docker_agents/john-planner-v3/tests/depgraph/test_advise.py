"""Phase-0 advisory render (advise.py). Pure: hand-built graphs, no Docker."""

from __future__ import annotations

from python_deps.depgraph.advise import (
    _best_evidence_line,
    render_dep_graph_advisory,
)
from python_deps.depgraph.ids import (
    TEST_NODE_ID,
    import_id,
    package_id,
    project_id,
    syslib_id,
)
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


def _req(src: str, dst: str) -> Edge:
    return Edge(src=src, dst=dst, relation=EdgeType.REQUIRES, origin="test")


def _cv2_like_graph() -> DepGraph:
    """opencv-python satisfied, cv2 import missing (libGL), numpy satisfied."""
    test = Node(
        id=TEST_NODE_ID, type=NodeType.TEST, name="repo_tests_pass",
        layer=Layer.TESTS, discovered_by=DiscoveredBy.GOAL, state=State.MISSING,
    )
    proj = Node(
        id=project_id("visionapp"), type=NodeType.PROJECT, name="visionapp",
        layer=Layer.PIP, discovered_by=DiscoveredBy.STATIC_SCAN,
    )
    opencv = Node(
        id=package_id("opencv-python", "4.9.0.80"), type=NodeType.PACKAGE,
        name="opencv-python", version="4.9.0.80", layer=Layer.PIP,
        discovered_by=DiscoveredBy.RESOLVER, state=State.SATISFIED,
    )
    numpy = Node(
        id=package_id("numpy", "1.26.4"), type=NodeType.PACKAGE, name="numpy",
        version="1.26.4", layer=Layer.PIP, discovered_by=DiscoveredBy.RESOLVER,
        state=State.SATISFIED,
    )
    libgl = Node(
        id=syslib_id("libgl1"), type=NodeType.SYSTEM_LIB, name="libgl1",
        layer=Layer.SYSTEM, discovered_by=DiscoveredBy.PROBE, state=State.MISSING,
        fix_candidates=("apt:libgl1",),
    )
    cv2 = Node(
        id=import_id("cv2"), type=NodeType.IMPORT, name="cv2", layer=Layer.NAMING,
        discovered_by=DiscoveredBy.STATIC_SCAN, state=State.MISSING,
        evidence=(
            "Traceback (most recent call last):\n"
            '  File "<string>", line 1, in <module>\n'
            "ImportError: libGL.so.1: cannot open shared object file: "
            "No such file or directory"
        ),
    )
    g = DepGraph()
    for n in (test, proj, opencv, numpy, libgl, cv2):
        g = g.with_node(n)
    g = g.with_edge(_req(TEST_NODE_ID, proj.id))
    g = g.with_edge(_req(proj.id, opencv.id))
    g = g.with_edge(_req(TEST_NODE_ID, cv2.id))
    g = g.with_edge(_req(cv2.id, opencv.id))
    g = g.with_edge(_req(opencv.id, libgl.id))  # opencv needs libGL
    return g


def test_header_goal_and_project_lines() -> None:
    out = render_dep_graph_advisory(_cv2_like_graph())
    assert out.startswith("[DEPENDENCY GRAPH")
    assert "GOAL" in out and "repo_tests_pass" in out
    assert "PROJECT" in out and "visionapp" in out
    # project line lists its declared dep
    assert "opencv-python" in out.split("UNSATISFIED")[0]


def test_frontier_block_has_fix_and_needed_by() -> None:
    out = render_dep_graph_advisory(_cv2_like_graph())
    assert "UNSATISFIED" in out
    # the system lib appears with its apt fix and is attributed to opencv-python
    assert "libgl1" in out
    assert "fix-candidate: apt:libgl1" in out
    assert "needed by: opencv-python" in out


def test_evidence_uses_real_error_not_traceback_header() -> None:
    out = render_dep_graph_advisory(_cv2_like_graph())
    # the cv2 frontier block must surface the ImportError, NOT the generic header
    assert "ImportError: libGL.so.1" in out
    assert "evidence: Traceback (most recent call last):" not in out


def test_satisfied_is_summary_not_per_node() -> None:
    out = render_dep_graph_advisory(_cv2_like_graph())
    assert "SATISFIED (summary):" in out
    assert "pip 2" in out  # opencv + numpy collapsed to a count
    # the satisfied transitive package is NOT given its own frontier-style line
    sat_section = out.split("SATISFIED (summary):")[1]
    assert "numpy" not in sat_section


def test_needed_by_excludes_same_name_naming_link() -> None:
    """import:psycopg2 -> pkg:psycopg2 must not render as 'needed by: psycopg2'."""
    proj = Node(
        id=project_id("dbapp"), type=NodeType.PROJECT, name="dbapp",
        layer=Layer.PIP, discovered_by=DiscoveredBy.STATIC_SCAN,
    )
    pkg = Node(
        id=package_id("psycopg2", "2.9.9"), type=NodeType.PACKAGE, name="psycopg2",
        version="2.9.9", layer=Layer.PIP, discovered_by=DiscoveredBy.RESOLVER,
        state=State.MISSING, fix_candidates=("pip:psycopg2",),
    )
    imp = Node(
        id=import_id("psycopg2"), type=NodeType.IMPORT, name="psycopg2",
        layer=Layer.NAMING, discovered_by=DiscoveredBy.STATIC_SCAN,
        state=State.MISSING,
    )
    g = DepGraph().with_node(proj).with_node(pkg).with_node(imp)
    g = g.with_edge(_req(proj.id, pkg.id))
    g = g.with_edge(_req(imp.id, pkg.id))

    out = render_dep_graph_advisory(g)
    pkg_block = [ln for ln in out.splitlines() if "needed by" in ln and "dbapp" in ln]
    assert pkg_block, "expected pkg frontier needed-by line listing dbapp"
    # the same-name import link is suppressed
    assert "needed by: dbapp, psycopg2" not in out
    assert "needed by: psycopg2" not in out


def test_empty_graph_renders_nothing() -> None:
    assert render_dep_graph_advisory(DepGraph()) == ""


def test_build_advisory_degrades_gracefully(monkeypatch) -> None:
    """ANY failure building the graph must yield ('', None), never raise — so a
    run proceeds exactly as if the feature were off (graceful degradation)."""
    import python_deps.depgraph.advise as advise_mod
    from python_deps.depgraph.advise import build_advisory_for_repo

    class _BoomExecutor:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            raise RuntimeError("docker unavailable")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(advise_mod, "DockerExecutor", _BoomExecutor)
    advisory, graph = build_advisory_for_repo("/nonexistent", "python:3.11-slim")
    assert advisory == ""
    assert graph is None


def test_best_evidence_line_helper() -> None:
    assert _best_evidence_line(None) is None
    assert _best_evidence_line("") is None
    tb = "Traceback (most recent call last):\n  ...\nModuleNotFoundError: no 'x'"
    assert _best_evidence_line(tb) == "ModuleNotFoundError: no 'x'"
    # falls back to last line when nothing looks like an error
    assert _best_evidence_line("just a note\nsecond line") == "second line"


# --- Task 11: CONFIG tier in advisory ---

def test_advisory_renders_missing_config_node_with_value_needed():
    from python_deps.depgraph.schema import (
        DepGraph, Node, NodeType, Layer, DiscoveredBy, State,
    )
    from python_deps.depgraph.advise import render_dep_graph_advisory

    cfg = Node(id="config:SECRET_KEY", type=NodeType.CONFIG, name="SECRET_KEY",
               layer=Layer.CONFIG, discovered_by=DiscoveredBy.STATIC_SCAN,
               state=State.MISSING, check_command="printenv SECRET_KEY",
               fix_candidates=("env:SECRET_KEY=?",))
    out = render_dep_graph_advisory(DepGraph().with_node(cfg))
    assert "SECRET_KEY" in out
    assert "CONFIG" in out
    assert "value needed" in out


def test_advisory_config_with_derived_value_has_no_marker():
    from python_deps.depgraph.schema import (
        DepGraph, Node, NodeType, Layer, DiscoveredBy, State,
    )
    from python_deps.depgraph.advise import render_dep_graph_advisory

    cfg = Node(id="config:DEBUG", type=NodeType.CONFIG, name="DEBUG",
               layer=Layer.CONFIG, discovered_by=DiscoveredBy.STATIC_SCAN,
               state=State.MISSING, check_command="printenv DEBUG",
               fix_candidates=("env:DEBUG=False",))
    out = render_dep_graph_advisory(DepGraph().with_node(cfg))
    assert "value needed" not in out


# --- Task 9: SERVICES tier in advisory ---

def _svc(name, **data):
    from python_deps.depgraph.schema import Node, NodeType, Layer, DiscoveredBy, State
    return Node(id=f"service:{name}", type=NodeType.SERVICE, name=name, layer=Layer.SERVICES,
                discovered_by=DiscoveredBy.STATIC_SCAN, state=State.UNKNOWN,
                fix_candidates=(f"service:{name}:16",), data=dict(data))


def test_advisory_renders_services_block():
    from python_deps.depgraph.schema import DepGraph
    from python_deps.depgraph.advise import render_dep_graph_advisory
    g = (DepGraph()
         .with_node(_svc("postgres", bound_config="DATABASE_URL"))
         .with_node(_svc("redis", inducing_package="celery")))
    out = render_dep_graph_advisory(g)
    assert "SERVICES" in out
    # Post-flip an advisory (non-setup) service always renders [inferred].
    assert "postgres" in out and "[inferred]" in out
    assert "addresses: DATABASE_URL" in out
    assert "redis" in out and "may be mocked" in out


def test_advisory_no_services_block_when_none():
    from python_deps.depgraph.schema import DepGraph, Node, NodeType, Layer, DiscoveredBy, State
    from python_deps.depgraph.advise import render_dep_graph_advisory
    pkg = Node(id="pkg:requests", type=NodeType.PACKAGE, name="requests", layer=Layer.PIP,
               discovered_by=DiscoveredBy.RESOLVER, state=State.SATISFIED)
    out = render_dep_graph_advisory(DepGraph().with_node(pkg))
    assert "SERVICES" not in out


# --- advisory-only (non-setup) service render ---

def test_advisory_advisory_only_service_unchanged():
    from python_deps.depgraph.schema import DepGraph, Node, NodeType, Layer, DiscoveredBy, State
    from python_deps.depgraph.advise import render_dep_graph_advisory
    svc = Node(id="service:redis", type=NodeType.SERVICE, name="redis",
               layer=Layer.SERVICES, discovered_by=DiscoveredBy.RESOLVER, state=State.UNKNOWN,
               fix_candidates=("service:redis:7",),
               data={})
    out = render_dep_graph_advisory(DepGraph().with_node(svc))
    assert "needs (System)" not in out
    assert "may be mocked" in out


# --- CR8 (Inc4 final-review): setup-shape Service renders [setup], not inferred ---

def test_advisory_setup_service_renders_as_setup_not_mocked():
    """A CLEAN setup-shape Service (has data['setup'], NO service_confidence) is a
    MANDATORY provisioned obligation — it must render as [setup] and MUST NOT carry
    the legacy '[inferred] … may be mocked' caveat (cross-consumer consistency with
    the scheduler/certify path which blocks 'done' on it)."""
    from python_deps.depgraph.schema import DepGraph, Node, NodeType, Layer, DiscoveredBy, State
    from python_deps.depgraph.advise import render_dep_graph_advisory
    svc = Node(id="service:redis", type=NodeType.SERVICE, name="redis",
               layer=Layer.SERVICES, discovered_by=DiscoveredBy.STATIC_SCAN, state=State.MISSING,
               fix_candidates=("service:redis:7",),
               data={"service_kind": "redis",
                     "setup": {"start": "redis-server --daemonize yes", "createdb": None}})
    out = render_dep_graph_advisory(DepGraph().with_node(svc))
    assert "[setup]" in out
    assert "[inferred]" not in out
    assert "may be mocked" not in out
    # the setup start-line branch (CR8) is still rendered
    assert "declared setup-service (kind: redis)" in out
    assert "start: redis-server --daemonize yes" in out
