"""Scenario (Task 8c): a real (unmocked) typed-repair admits an LLM-proposed
``ScriptPatch`` (a governed manual block, not a graph node/provider). Proves the
graph/script/fresh-replay artifact contract (claim 2 of the e2e-proof design):
the manual block that got the node SATISFIED (a) is recorded in
``trace.manual_block_ids`` (set once, on the way out, via ``_finish``), (b) is
present in the per-cycle replay script that actually installed it, AND (c) is
present in a POST-HOC, independently-rendered
``render_build_script(final_graph, final_manual_blocks)`` — i.e. the artifact
you'd hand to a human/CI is not missing the block that made the run succeed.
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from src.envstate import orchestrator
from src.envstate.ledger import ActionLedger
from src.envstate.run_trace import RunTracer
from src.envstate.trace_verify import verify_artifact_consistency, verify_canonical_trace
from src.envstate.world_model import initial_map, merge_map
from src.sandbox import InstallResult
from python_deps.depgraph.build_script import render_build_script
from python_deps.depgraph.patch import PatchProposal, ScriptPatch
from python_deps.depgraph.schema import DepGraph, DiscoveredBy, Layer, Node, NodeType, State

_BLOCK_ID = "system.manual-libfoo"
_REAL_INSTALL_CMD = "apt-get update && apt-get install -y --no-install-recommends libfoo1-real"


class _FakeClient:
    """Non-None sentinel for the ``getattr(build_agent, "client", None)`` guard."""


class _NoopMaintainer:
    def update(self, world_map, report):
        return world_map


def _syslib_map():
    node = Node(
        id="syslib:libfoo.so.1", type=NodeType.SYSTEM_LIB, name="libfoo.so.1",
        layer=Layer.SYSTEM, discovered_by=DiscoveredBy.RESOLVER, state=State.MISSING,
        check_command="ldconfig -p | grep -q libfoo.so.1",
        chosen_fix="apt:libfoo-WRONG",
    )
    base = initial_map(base_image="python:3.11-slim", workdir="/repo", language="python",
                       build_system="pip", repo_layout=())
    return merge_map(base, dep_graph=DepGraph().with_node(node))


def test_manual_block_artifact_preserved():
    state = {"installed": False}
    replay_scripts: list[str] = []

    def sandbox_execute(cmd):
        if cmd == orchestrator.VERIFY_TEST_CMD:
            return (True, "1 passed in 0.01s") if state["installed"] else (False, "no tests ran")
        return (True, "ok")

    def exec_readonly(cmd):
        if "libfoo.so.1" in cmd:
            return (0, "") if state["installed"] else (1, "")
        return (1, "")

    def reset_to_base():
        pass

    def run_install_script(script):
        replay_scripts.append(script)
        if _BLOCK_ID in script:
            state["installed"] = True
            return InstallResult(rc=0, failing_command=None, lineno=None, stderr="")
        return InstallResult(
            rc=1,
            failing_command=("apt-get update && apt-get install -y "
                             "--no-install-recommends libfoo-WRONG"),
            lineno=None,
            stderr="ImportError: libfoo.so.1: cannot open shared object file: No such file or directory",
        )

    class _Agent:
        client = _FakeClient()
        model = "fake-model"

        def propose(self, scope, exec_readonly=None, **kwargs):
            ev_ids = sorted(scope.known_evidence_ids)
            assert ev_ids, "propose called with no evidence to cite"
            # Force a ScriptPatch (governed manual block) admission — the
            # scenario under test — rather than an add_providers correction.
            return PatchProposal(
                rationale={"why": "manual script block installs libfoo.so.1"},
                script_patches=(ScriptPatch(
                    block_id=_BLOCK_ID, wave="system",
                    commands=(_REAL_INSTALL_CMD,),
                    target_node_ids=("syslib:libfoo.so.1",),
                    checks=("ldconfig -p | grep -q libfoo.so.1",),
                    evidence_ref=ev_ids[0],
                ),),
            )

    tracer = RunTracer(repo="scenario/manual-block-artifact")
    captured: dict = {}

    def gate_observer(gates):
        installability, testability = gates
        captured["installability"] = dataclasses.asdict(installability)
        captured["testability"] = dataclasses.asdict(testability)

    final_map, stop = orchestrator.run_v3(
        build_agent=_Agent(),
        maintainer=_NoopMaintainer(),
        initial_world_map=_syslib_map(),
        ledger=ActionLedger(),
        sandbox_execute=sandbox_execute,
        max_cycles=4,
        exec_readonly=exec_readonly,
        enable_dep_emit=True,
        reset_to_base=reset_to_base,
        run_install_script=run_install_script,
        enable_gate_observability=True,
        gate_observer=gate_observer,
        tracer=tracer,
    )
    trace = tracer.snapshot(stop_reason=stop, gates=captured)

    assert stop == "planner_done"
    assert trace.manual_block_ids == (_BLOCK_ID,), (
        "trace.manual_block_ids must record the admitted ScriptPatch's block_id"
    )

    # (b) the per-cycle replay script that actually installed it contained the block.
    installing_scripts = [s for s in replay_scripts if _BLOCK_ID in s and _REAL_INSTALL_CMD in s]
    assert installing_scripts, "no per-cycle replay script contained the admitted manual block"

    # (c) a POST-HOC, independent render from the final graph/manual_blocks still
    #     contains it — the artifact is not missing the block that won the run.
    final_script = render_build_script(final_map.dep_graph, final_map.manual_blocks)
    assert _BLOCK_ID in final_script
    assert _REAL_INSTALL_CMD in final_script

    assert verify_artifact_consistency(final_script, trace.manual_block_ids) == []
    assert verify_canonical_trace(trace) == []
