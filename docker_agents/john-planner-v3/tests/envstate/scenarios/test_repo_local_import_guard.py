"""Scenario (Task 8c): a fresh-replay failure whose traceback references a
REPO-LOCAL module (``docs_src``, present in the real repo tree passed as
``repo_path``) must never be mistaken for a missing PyPI package. The diagnosis
router (``Mode.REPO_INTERNAL_REF``) must route this OUT of typed repair entirely —
no ``build_agent.propose`` call, no ``pkg:docs-src`` node, no ``pip install
docs-src``.

Uses a REAL filesystem tree (not a monkeypatched ``scan.local_module_names``) so
``python_deps.depgraph.scan.local_module_names`` is exercised for real, per the
brief's explicit ask.

Mechanism note: this repo's ACTUAL local-import guard lives in
``_repair_or_route``'s diagnosis of the bundle produced by a real fresh-replay
install failure (``_dep_emit_phase``'s main-loop repair site) — see
``tests/envstate/test_repair_routing.py::test_repo_internal_ref_bundle_skips_repair``,
which already proves this mechanism with a monkeypatched ``local_module_names``.
This scenario mirrors that PROVEN mechanism (with a real repo tree) rather than
the discover-gate/runtime-ingest path: ``_runtime_ingest_phase`` classifies
observations with the raw ``classify_observation`` regex classifier only — it
does NOT consult the diagnosis router/RepoContext — so a repo-local import
reaching the graph via THAT path would not currently be filtered (a separate,
pre-existing gap outside this task's scope: no ``diagnose.py`/repair-routing
changes are permitted here). ``verify_local_import_guard`` is therefore checked
here as a real (if in this flow trivially-empty) assertion, backed by the
stronger direct assertions below (no pkg: node ever created; propose never
called; patchgate never invoked).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from src.envstate import orchestrator
from src.envstate.ledger import ActionLedger
from src.envstate.run_trace import RunTracer
from src.envstate.trace_verify import verify_local_import_guard
from src.envstate.world_model import initial_map, merge_map
from src.sandbox import InstallResult
from python_deps.depgraph.schema import DepGraph, DiscoveredBy, Layer, Node, NodeType, State


class _FakeClient:
    """Non-None sentinel for the ``getattr(build_agent, "client", None)`` guard."""


class _NoopMaintainer:
    def update(self, world_map, report):
        return world_map


def _syslib_map():
    node = Node(
        id="syslib:libpq.so", type=NodeType.SYSTEM_LIB, name="libpq.so",
        layer=Layer.SYSTEM, discovered_by=DiscoveredBy.RESOLVER, state=State.MISSING,
        check_command="ldconfig -p | grep -q libpq",
        chosen_fix="apt:libpq-dev",
    )
    base = initial_map(base_image="python:3.11-slim", workdir="/repo", language="python",
                       build_system="pip", repo_layout=())
    return merge_map(base, dep_graph=DepGraph().with_node(node))


def test_repo_local_import_guard_blocks_repair_and_adds_no_package(tmp_path):
    # Real repo tree: tmp_path/docs_src/__init__.py — scan.local_module_names(repo_path)
    # discovers this for real (not monkeypatched).
    pkg_dir = tmp_path / "docs_src"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")

    def sandbox_execute(cmd):
        return (True, "ok")

    def exec_readonly(cmd):
        return (1, "")   # syslib check always fails -> node stays MISSING every cycle

    def reset_to_base():
        pass

    def run_install_script(script):
        return InstallResult(
            rc=1, failing_command="python3 -c \"import docs_src\"", lineno=None,
            stderr="ModuleNotFoundError: No module named 'docs_src'",
        )

    propose_calls = {"n": 0}

    class _Agent:
        client = _FakeClient()
        model = "fake-model"

        def propose(self, scope, exec_readonly=None, **kwargs):
            propose_calls["n"] += 1
            return None

    tracer = RunTracer(repo="scenario/repo-local-import")

    final_map, stop = orchestrator.run_v3(
        build_agent=_Agent(),
        maintainer=_NoopMaintainer(),
        initial_world_map=_syslib_map(),
        ledger=ActionLedger(),
        sandbox_execute=sandbox_execute,
        max_cycles=3,
        exec_readonly=exec_readonly,
        enable_dep_emit=True,
        reset_to_base=reset_to_base,
        run_install_script=run_install_script,
        repo_path=str(tmp_path),
        tracer=tracer,
    )
    trace = tracer.snapshot(stop_reason=stop, gates={})

    assert propose_calls["n"] == 0, (
        "propose was called for a repo-internal reference; the diagnosis router "
        "must skip repair for Mode.REPO_INTERNAL_REF"
    )
    all_ids = tuple(n.id for n in final_map.dep_graph.nodes)
    assert not any(nid.startswith("pkg:docs") for nid in all_ids), (
        f"a pkg:docs-src-like node was added for a repo-local import: {all_ids}"
    )
    assert trace.patchgate == (), (
        "no PatchGateRecord should exist: run_structured_repair must never be "
        "invoked for a REPO_INTERNAL_REF diagnosis"
    )
    assert verify_local_import_guard(trace) == []
