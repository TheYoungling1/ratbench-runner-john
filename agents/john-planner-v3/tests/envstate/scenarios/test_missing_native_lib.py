"""Scenario (Task 8c): a reciped SystemLib node's fresh-replay install fails with a
native-library traceback (``libGL.so.1: cannot open shared object file``). The
diagnosis router (``python_deps.depgraph.diagnose``) must classify this ENVIRONMENT/
SYSTEM_LIB (not REPO_INTERNAL_REF/RESIDUAL/INVALID_ATTEMPT), routing it INTO typed
repair; a real (unmocked) ``run_structured_repair``/``admit_proposal`` admits a
provider correction; the SAME cycle's re-replay (inside ``run_structured_repair``'s
``emit`` callback) succeeds; the host certifies the node SATISFIED.

Everything here is REAL except sandbox_execute/exec_readonly/reset_to_base/
run_install_script/propose (the sanctioned fake-sandbox + fake-LLM seam) — this
is the SAME "reciped node whose install fails" harness shape as
tests/envstate/test_v3_repair_wiring.py and tests/envstate/test_repair_routing.py,
but WITHOUT mocking run_structured_repair, so admission/certify run for real.
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
from src.envstate.trace_verify import verify_canonical_trace
from src.envstate.world_model import initial_map, merge_map
from src.sandbox import InstallResult
from python_deps.depgraph.diagnose import Mode, RepoContext, diagnose
from python_deps.depgraph.patch import PatchProposal, ProviderSpec
from python_deps.depgraph.schema import DepGraph, DiscoveredBy, Layer, Node, NodeType, State


class _FakeClient:
    """Non-None sentinel for the ``getattr(build_agent, "client", None)`` guard."""


class _NoopMaintainer:
    def update(self, world_map, report):
        return world_map


_NATIVE_LIB_STDERR = (
    "ImportError: libGL.so.1: cannot open shared object file: No such file or directory"
)


def _syslib_map():
    """A reciped (chosen_fix already apt:-prefixed), MISSING SystemLib node — the
    resolver's initial guess at the apt package name is WRONG, so the first
    fresh replay fails."""
    node = Node(
        id="syslib:libGL.so.1", type=NodeType.SYSTEM_LIB, name="libGL.so.1",
        layer=Layer.SYSTEM, discovered_by=DiscoveredBy.RESOLVER, state=State.MISSING,
        check_command="ldconfig -p | grep -q libGL.so.1",
        chosen_fix="apt:libgl1-WRONG-name",
    )
    base = initial_map(base_image="python:3.11-slim", workdir="/repo", language="python",
                       build_system="pip", repo_layout=())
    return merge_map(base, dep_graph=DepGraph().with_node(node))


def _run(monkeypatch=None):
    state = {"installed": False}

    def sandbox_execute(cmd):
        if cmd == orchestrator.VERIFY_TEST_CMD:
            return (True, "1 passed in 0.01s") if state["installed"] else (False, "no tests ran")
        return (True, "ok")

    def exec_readonly(cmd):
        if "libGL.so.1" in cmd:
            return (0, "") if state["installed"] else (1, "")
        return (1, "")

    def reset_to_base():
        pass

    def run_install_script(script):
        if state["installed"]:
            return InstallResult(rc=0, failing_command=None, lineno=None, stderr="")
        return InstallResult(
            rc=1,
            failing_command=("apt-get update && apt-get install -y "
                             "--no-install-recommends libgl1-WRONG-name"),
            lineno=None, stderr=_NATIVE_LIB_STDERR,
        )

    propose_calls = {"n": 0}

    class _Agent:
        client = _FakeClient()
        model = "fake-model"

        def propose(self, scope, exec_readonly=None, **kwargs):
            propose_calls["n"] += 1
            # A real LLM would read scope.failed_output (cites the ACTUAL evidence)
            # and correct the apt package name via a providers-only patch — no
            # add_requirements/script_patches, so no evidence_ref citation needed
            # for THIS proposal shape (validate_proposal only requires evidence_ref
            # on add_requirements/script_patches, not add_providers).
            assert scope.known_evidence_ids, "propose called with no evidence to cite"
            state["installed"] = True   # simulate: the corrected name installs fine
            return PatchProposal(
                rationale={"why": "correct apt package name for libGL.so.1"},
                add_providers=(ProviderSpec(
                    id="apt:libgl1-mesa-glx", kind="apt",
                    command="apt-get install -y libgl1-mesa-glx",
                    provides=("syslib:libGL.so.1",), override=True,
                ),),
            )

    tracer = RunTracer(repo="scenario/missing-native-lib")
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
    return final_map, stop, trace, propose_calls


def test_diagnosis_router_classifies_native_lib_as_environment():
    """Sanity-pin the routing precondition this scenario depends on: the SAME
    (command, output) pair the fresh replay produces must classify ENVIRONMENT
    (not REPO_INTERNAL_REF/RESIDUAL/INVALID_ATTEMPT) via the real diagnose()."""
    d = diagnose(
        "apt-get update && apt-get install -y --no-install-recommends libgl1-WRONG-name",
        _NATIVE_LIB_STDERR, RepoContext(),
    )
    assert d.mode is Mode.ENVIRONMENT
    assert d.discovery is not None
    assert d.discovery.node_type is NodeType.SYSTEM_LIB


def test_missing_native_lib_repairs_and_certifies():
    final_map, stop, trace, propose_calls = _run()

    assert stop == "planner_done"
    assert propose_calls["n"] == 1, "diagnosis should route this straight into repair once"

    node = final_map.dep_graph.get("syslib:libGL.so.1")
    assert node.state is State.SATISFIED, "host must certify the node SATISFIED after repair"
    assert node.chosen_fix == "apt:libgl1-mesa-glx"

    assert len(trace.patchgate) == 1
    pg = trace.patchgate[0]
    assert pg.accepted is True
    assert pg.evidence_ref is not None and pg.evidence_ref.startswith("install.")

    assert trace.last_replay is not None
    assert trace.last_replay.setup_rc == 0
    assert not trace.last_replay.unsatisfied_node_ids

    assert verify_canonical_trace(trace) == []
