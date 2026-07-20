"""Scenario (Task 8c): an invalid apt provider name is disproven by a real
fresh-replay install failure ("No matching distribution found for
libplacebodev"), the SAME repair turn does NOT retry that exact name, and a
valid replacement is admitted and certified.

Uses a FRONTIER SystemLib node (no chosen_fix yet — undiscovered) so the
scheduler routes it through the task-branch obligation-repair site. The FIRST
targeted-repair attempt on a fresh obligation carries an empty EvidenceBundle
(``orchestrator.py``'s task branch literally constructs ``EvidenceBundle()`` for
a target with no existing manual block — see ``_repair_or_route``'s "no manual
block targets this node yet" call site) — ``validate_proposal`` requires an
``evidence_ref`` present in ``known_evidence_ids`` for BOTH ``add_requirements``
and ``script_patches``, so a bad-guess-then-correction sequence can only be
demonstrated with REAL admission via a proposal shape that needs no evidence
citation at all: ``add_providers`` (a chosen_fix correction). Both propose calls
below use ``add_providers`` for exactly this reason.

The mechanism this scenario exercises is the repair-loop's OWN internal
"do not repeat the identical failing attempt" signal: ``run_structured_repair``
threads the fresh replay's real stderr into ``RepairScope.failed_command`` /
``.failed_output`` on the RETRY within the SAME call (repair_loop.py's ``ki`` /
``scope.known_invalid``) — the fake ``propose`` below reads that real evidence
and avoids repeating the disproven name, exactly as a real LLM prompted with
"DO NOT propose these (already failed)" would. The cross-CYCLE router-level
mechanism (``Mode.INVALID_ATTEMPT`` / ``RepoContext.invalid_names``, which
short-circuits ``run_structured_repair`` entirely on a REPEAT of the same
failure text) is already covered by
``tests/envstate/test_repair_routing.py::test_invalid_attempt_records_normalized_name_no_repair``
and is not duplicated here — this scenario adds the "and then a valid
replacement gets admitted" half that test does not cover, all real
(unmocked) admission/certify.
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
from python_deps.depgraph.patch import PatchProposal, ProviderSpec
from python_deps.depgraph.schema import DepGraph, DiscoveredBy, Layer, Node, NodeType, State

_BAD_NAME = "libplacebodev"
_GOOD_NAME = "libplacebo-dev"


class _FakeClient:
    """Non-None sentinel for the ``getattr(build_agent, "client", None)`` guard."""


class _NoopMaintainer:
    def update(self, world_map, report):
        return world_map


def _frontier_map():
    """A FRONTIER SystemLib: MISSING, chosen_fix=None (undiscovered) -> the
    scheduler targets it as a task-branch obligation, not the deterministic
    emit path."""
    node = Node(
        id="syslib:libplacebo.so.2", type=NodeType.SYSTEM_LIB, name="libplacebo.so.2",
        layer=Layer.SYSTEM, discovered_by=DiscoveredBy.RESOLVER, state=State.MISSING,
        check_command="ldconfig -p | grep -q libplacebo.so.2",
    )
    base = initial_map(base_image="python:3.11-slim", workdir="/repo", language="python",
                       build_system="pip", repo_layout=())
    return merge_map(base, dep_graph=DepGraph().with_node(node))


def test_bad_provider_not_retried_valid_replacement_accepted():
    state = {"installed": False}

    def sandbox_execute(cmd):
        if cmd == orchestrator.VERIFY_TEST_CMD:
            return (True, "1 passed in 0.01s") if state["installed"] else (False, "no tests ran")
        return (True, "ok")

    def exec_readonly(cmd):
        if "libplacebo.so.2" in cmd:
            return (0, "") if state["installed"] else (1, "")
        return (1, "")

    def reset_to_base():
        pass

    def run_install_script(script):
        # The fake sandbox doesn't parse the script's apt semantics — it just
        # scripts a deterministic double, the same pattern already used
        # throughout tests/envstate/test_repair_routing.py.
        if _BAD_NAME in script:
            return InstallResult(
                rc=1,
                failing_command=(f"apt-get update && apt-get install -y "
                                 f"--no-install-recommends {_BAD_NAME}"),
                lineno=None,
                stderr=f"No matching distribution found for {_BAD_NAME}",
            )
        if _GOOD_NAME in script:
            state["installed"] = True
            return InstallResult(rc=0, failing_command=None, lineno=None, stderr="")
        return InstallResult(rc=0, failing_command=None, lineno=None, stderr="")

    propose_calls = {"n": 0}

    class _Agent:
        client = _FakeClient()
        model = "fake-model"

        def propose(self, scope, exec_readonly=None, **kwargs):
            propose_calls["n"] += 1
            if propose_calls["n"] == 1:
                # First guess: a plausible-but-wrong apt package name.
                return PatchProposal(
                    rationale={"why": "install libplacebo.so.2 via apt"},
                    add_providers=(ProviderSpec(
                        id=f"apt:{_BAD_NAME}", kind="apt",
                        command=f"apt-get install -y {_BAD_NAME}",
                        provides=("syslib:libplacebo.so.2",),
                    ),),
                )
            # Second attempt (same run_structured_repair call, same repair turn
            # sequence): the failed replay's REAL stderr is now visible on the
            # scope — avoid the disproven name, propose the correct one.
            assert _BAD_NAME in (scope.failed_command or "") + scope.failed_output, (
                "propose #2 did not receive the real evidence of the first "
                "attempt's failure"
            )
            return PatchProposal(
                rationale={"why": "correct apt package name"},
                add_providers=(ProviderSpec(
                    id=f"apt:{_GOOD_NAME}", kind="apt",
                    command=f"apt-get install -y {_GOOD_NAME}",
                    provides=("syslib:libplacebo.so.2",), override=True,
                ),),
            )

    tracer = RunTracer(repo="scenario/bad-provider")
    captured: dict = {}

    def gate_observer(gates):
        installability, testability = gates
        captured["installability"] = dataclasses.asdict(installability)
        captured["testability"] = dataclasses.asdict(testability)

    final_map, stop = orchestrator.run_v3(
        build_agent=_Agent(),
        maintainer=_NoopMaintainer(),
        initial_world_map=_frontier_map(),
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

    assert propose_calls["n"] == 2, "expected exactly one bad guess + one correction"
    node = final_map.dep_graph.get("syslib:libplacebo.so.2")
    assert node.state is State.SATISFIED
    assert node.chosen_fix == f"apt:{_GOOD_NAME}", (
        "the FINAL chosen_fix must be the valid replacement, not the disproven name"
    )

    from python_deps.depgraph.build_script import render_build_script
    script_text = render_build_script(final_map.dep_graph, final_map.manual_blocks)
    assert _GOOD_NAME in script_text
    assert _BAD_NAME not in script_text, (
        "the disproven provider name must not survive into the final artifact"
    )

    assert stop == "planner_done"
    assert verify_canonical_trace(trace) == []
