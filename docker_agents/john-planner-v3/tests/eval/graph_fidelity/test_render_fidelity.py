"""TDD tests for src/eval/graph_fidelity/render_fidelity.py — the Phase-2
RENDER-FIDELITY checker (graph-fidelity eval loop, item 2): does
``render_build_script(graph)`` faithfully turn a graph into a script — every
reciped node emitted once, in valid topo order, valid bash, deterministic?

Pure: no Docker, no network, no LLM. ``hypothesis`` is NOT installed in this
environment (checked before writing this file), so the "many valid graphs"
property test below uses a hand-rolled seeded generator (``random.Random``)
instead — no new dependency.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src"
for _p in (_REPO_ROOT, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from python_deps.depgraph.build_script import render_build_script
from python_deps.depgraph.schema import (
    DepGraph, DiscoveredBy, Edge, EdgeType, Layer, Node, NodeType, State,
)

from src.eval.graph_fidelity.render_fidelity import check_render, is_deterministic


# ---------------------------------------------------------------------------
# Node builders (mirror tests/depgraph/test_build_script.py's _pkg/_apt style)
# ---------------------------------------------------------------------------

def _pkg(id_, name, version, layer=Layer.PIP, state=State.MISSING, **kw):
    return Node(id=id_, type=NodeType.PACKAGE, name=name, layer=layer,
                discovered_by=DiscoveredBy.RESOLVER, state=state,
                version=version, **kw)


def _apt(id_, name, fix, type_=NodeType.SYSTEM_LIB, layer=Layer.SYSTEM,
         state=State.MISSING, **kw):
    return Node(id=id_, type=type_, name=name, layer=layer,
                discovered_by=DiscoveredBy.PROBE, state=state,
                chosen_fix=fix, **kw)


# ---------------------------------------------------------------------------
# 1. Synthetic generator: deterministic (seeded), builds many valid DepGraphs.
# ---------------------------------------------------------------------------

_STATES = (State.MISSING, State.SATISFIED, State.UNKNOWN)


def _generate_graph(seed: int, n_packages: int = 8, n_roots: int = 3) -> DepGraph:
    """A deterministic (seeded) valid DepGraph: ``n_roots`` apt SYSTEM_LIB/TOOL
    nodes plus ``n_packages`` PACKAGE nodes, with random-but-seeded REQUIRES
    edges from each package to any earlier-constructed node (a root or an
    earlier package). Construction order guarantees a DAG — never a cycle."""
    rng = random.Random(seed)
    graph = DepGraph()
    candidates: list[Node] = []

    for i in range(n_roots):
        is_tool = i % 2 == 1
        node = _apt(
            f"syslib:s{seed}-{i}", f"s{seed}-{i}", f"apt:s{seed}-{i}",
            type_=NodeType.TOOL if is_tool else NodeType.SYSTEM_LIB,
            layer=Layer.TOOLCHAIN if is_tool else Layer.SYSTEM,
            state=rng.choice(_STATES),
        )
        graph = graph.with_node(node)
        candidates.append(node)

    for i in range(n_packages):
        node = _pkg(f"pkg:p{seed}-{i}", f"p{seed}-{i}", f"{1 + i % 4}.{i}.0",
                    state=rng.choice(_STATES))
        graph = graph.with_node(node)
        n_deps = rng.randint(0, min(2, len(candidates)))
        for dep in rng.sample(candidates, n_deps):
            graph = graph.with_edge(Edge(src=node.id, dst=dep.id,
                                         relation=EdgeType.REQUIRES))
        candidates.append(node)

    return graph


class TestSyntheticGraphs:
    def test_many_seeded_graphs_render_faithfully(self):
        for seed in range(50):
            graph = _generate_graph(seed)
            script = render_build_script(graph)
            result = check_render(graph, script)
            assert result.all_reciped_emitted, (seed, result.missing_from_script)
            assert result.single_emit, (seed, result.duplicated)
            assert result.topo_order_ok, (seed, result.order_violations)
            assert result.valid_bash is not False, (seed, result.bash_error)

    def test_many_seeded_graphs_are_deterministic(self):
        for seed in range(50):
            graph = _generate_graph(seed)
            assert is_deterministic(graph)


# ---------------------------------------------------------------------------
# 2. Golden tests: hand-crafted shapes mirroring
#    src/eval/graph_fidelity/edge_cases/ (soname->apt, build-essential
#    toolchain required by several packages).
# ---------------------------------------------------------------------------

class TestGoldenShapes:
    def test_soname_syslib_precedes_dependent_package(self):
        # mirrors edge_cases/soname_opencv: libgl1 (SystemLib) required by
        # opencv-python's compiled extension.
        g = DepGraph(nodes=(
            _apt("syslib:libgl1", "libgl1", "apt:libgl1"),
            _pkg("pkg:opencv-python", "opencv-python", "4.9.0.80"),
        ))
        g = g.with_edge(Edge(src="pkg:opencv-python", dst="syslib:libgl1",
                             relation=EdgeType.REQUIRES))
        script = render_build_script(g)
        result = check_render(g, script)
        assert result.all_reciped_emitted
        assert result.single_emit
        assert result.topo_order_ok
        assert (script.index("#@node syslib:libgl1")
                < script.index("#@node pkg:opencv-python"))

    def test_build_essential_precedes_multiple_dependent_packages(self):
        # mirrors edge_cases/nowheel_buildessential: one TOOL required by
        # several sdist-build packages.
        g = DepGraph(nodes=(
            _apt("tool:build-essential", "build-essential", "apt:build-essential",
                 type_=NodeType.TOOL, layer=Layer.TOOLCHAIN),
            _pkg("pkg:numpy", "numpy", "1.21.0", build_from_source=True),
            _pkg("pkg:scipy", "scipy", "1.7.0", build_from_source=True),
        ))
        for pkg_id in ("pkg:numpy", "pkg:scipy"):
            g = g.with_edge(Edge(src=pkg_id, dst="tool:build-essential",
                                 relation=EdgeType.REQUIRES))
        script = render_build_script(g)
        result = check_render(g, script)
        assert result.all_reciped_emitted
        assert result.topo_order_ok
        tool_idx = script.index("#@node tool:build-essential")
        assert tool_idx < script.index("#@node pkg:numpy")
        assert tool_idx < script.index("#@node pkg:scipy")


# ---------------------------------------------------------------------------
# 3. Mutation self-check: prove the checker actually catches violations.
# ---------------------------------------------------------------------------

def _line_starts_new_block(line: str) -> bool:
    return (line.startswith("#@node ") or line.startswith("#@need ")
            or line.startswith("# ===="))


def _block_span(lines: list[str], start_idx: int) -> int:
    """End index (exclusive) of the block beginning at ``lines[start_idx]``:
    everything up to the next blank line or new-block marker."""
    end = start_idx + 1
    while end < len(lines) and lines[end].strip() and not _line_starts_new_block(lines[end]):
        end += 1
    return end


def _find_node_line(lines: list[str], node_id: str) -> int:
    return next(i for i, ln in enumerate(lines) if ln.startswith(f"#@node {node_id}"))


def _good_two_node_graph() -> DepGraph:
    g = DepGraph(nodes=(
        _apt("syslib:libpq-dev", "libpq-dev", "apt:libpq-dev"),
        _pkg("pkg:psycopg2", "psycopg2", "2.9.9"),
    ))
    return g.with_edge(Edge(src="pkg:psycopg2", dst="syslib:libpq-dev",
                            relation=EdgeType.REQUIRES))


class TestMutationTeeth:
    def test_baseline_script_passes_clean(self):
        g = _good_two_node_graph()
        script = render_build_script(g)
        result = check_render(g, script)
        assert result.all_reciped_emitted
        assert result.single_emit
        assert result.topo_order_ok

    def test_deleting_a_node_line_breaks_all_reciped_emitted(self):
        g = _good_two_node_graph()
        lines = render_build_script(g).splitlines()
        idx = _find_node_line(lines, "pkg:psycopg2")
        mutated = "\n".join(lines[:idx] + lines[idx + 1:]) + "\n"
        result = check_render(g, mutated)
        assert result.all_reciped_emitted is False
        assert "pkg:psycopg2" in result.missing_from_script

    def test_swapping_blocks_breaks_topo_order(self):
        g = _good_two_node_graph()
        lines = render_build_script(g).splitlines()
        dep_idx = _find_node_line(lines, "syslib:libpq-dev")
        dep_end = _block_span(lines, dep_idx)
        dependent_idx = _find_node_line(lines, "pkg:psycopg2")
        dependent_end = _block_span(lines, dependent_idx)
        assert dep_idx < dependent_idx  # sanity: original order is correct

        dep_block = lines[dep_idx:dep_end]
        dependent_block = lines[dependent_idx:dependent_end]
        mutated_lines = (
            lines[:dep_idx] + dependent_block
            + lines[dep_end:dependent_idx] + dep_block
            + lines[dependent_end:]
        )
        mutated = "\n".join(mutated_lines) + "\n"
        result = check_render(g, mutated)
        assert result.topo_order_ok is False
        assert ("pkg:psycopg2", "syslib:libpq-dev") in result.order_violations

    def test_duplicating_a_block_breaks_single_emit(self):
        g = _good_two_node_graph()
        lines = render_build_script(g).splitlines()
        idx = _find_node_line(lines, "syslib:libpq-dev")
        end = _block_span(lines, idx)
        block = lines[idx:end]
        mutated_lines = lines[:end] + block + lines[end:]
        mutated = "\n".join(mutated_lines) + "\n"
        result = check_render(g, mutated)
        assert result.single_emit is False
        assert "syslib:libpq-dev" in result.duplicated


# ---------------------------------------------------------------------------
# 4. emitted_but_uncertified: Phase-2's key first-pass error-risk metric.
# ---------------------------------------------------------------------------

class TestUncertifiedTracking:
    def test_missing_state_reciped_node_flagged_uncertified(self):
        g = DepGraph(nodes=(
            _pkg("pkg:psycopg2", "psycopg2", "2.9.9", state=State.MISSING),
        ))
        script = render_build_script(g)
        result = check_render(g, script)
        assert "pkg:psycopg2" in result.emitted_but_uncertified

    def test_satisfied_reciped_node_not_flagged(self):
        g = DepGraph(nodes=(
            _pkg("pkg:requests", "requests", "2.31.0", state=State.SATISFIED),
        ))
        script = render_build_script(g)
        result = check_render(g, script)
        assert "pkg:requests" not in result.emitted_but_uncertified

    def test_mixed_states_only_missing_flagged(self):
        g = DepGraph(nodes=(
            _pkg("pkg:requests", "requests", "2.31.0", state=State.SATISFIED),
            _pkg("pkg:psycopg2", "psycopg2", "2.9.9", state=State.MISSING),
        ))
        script = render_build_script(g)
        result = check_render(g, script)
        assert result.emitted_but_uncertified == ("pkg:psycopg2",)


# ---------------------------------------------------------------------------
# editable_last: the repo-under-test's `pip install -e .` must render AFTER
# every dependency (finding A — the editable install is the capstone).
# ---------------------------------------------------------------------------

def _project(name="myproj", installable=True):
    return Node(id=f"project:{name}", type=NodeType.PROJECT, name=name,
                layer=Layer.PIP, discovered_by=DiscoveredBy.STATIC_SCAN,
                state=State.UNKNOWN, data={"installable": installable})


class TestEditableInstall:
    def test_editable_last_true_when_project_rendered_after_deps(self):
        g = DepGraph(nodes=(_pkg("pkg:requests", "requests", "2.31.0"), _project()))
        result = check_render(g, render_build_script(g))
        assert result.editable_last is True

    def test_editable_last_none_when_no_installable_project(self):
        g = DepGraph(nodes=(_pkg("pkg:requests", "requests", "2.31.0"),))
        result = check_render(g, render_build_script(g))
        assert result.editable_last is None

    def test_editable_last_false_when_installable_project_absent_from_script(self):
        # finding-A regression: installable project but the script never emits it.
        g = DepGraph(nodes=(_pkg("pkg:requests", "requests", "2.31.0"), _project()))
        script = (
            "#!/usr/bin/env bash\n"
            "#@node pkg:requests  version=2.31.0\n"
            "python3 -m pip install --break-system-packages --no-deps requests==2.31.0\n"
        )
        assert check_render(g, script).editable_last is False

    def test_editable_last_false_when_project_before_a_dep(self):
        # the editable install must not precede a dependency it needs installed.
        g = DepGraph(nodes=(_pkg("pkg:requests", "requests", "2.31.0"), _project()))
        mutated = (
            "#!/usr/bin/env bash\n"
            "#@node project:myproj  requires=-\n"
            "python3 -m pip install --break-system-packages --no-deps -e .\n"
            "#@node pkg:requests  version=2.31.0\n"
            "python3 -m pip install --break-system-packages --no-deps requests==2.31.0\n"
        )
        assert check_render(g, mutated).editable_last is False


# ---------------------------------------------------------------------------
# valid_bash: real bash -n syntax check on the rendered artifact.
# ---------------------------------------------------------------------------

class TestValidBash:
    def test_clean_script_is_valid_bash(self):
        g = _good_two_node_graph()
        script = render_build_script(g)
        result = check_render(g, script)
        assert result.valid_bash is True
        assert result.bash_error is None

    def test_broken_bash_syntax_is_caught(self):
        g = _good_two_node_graph()
        script = render_build_script(g) + "if true; then\n"  # unterminated if
        result = check_render(g, script)
        assert result.valid_bash is False
        assert result.bash_error
