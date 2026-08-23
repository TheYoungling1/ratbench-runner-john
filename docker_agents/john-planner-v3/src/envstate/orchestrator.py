"""EnvState orchestrator loops.

run_v1() — three-role planner-driven loop (spec §4):
    initial_map → planner.decide → (done/giveup → break)
                → build_agent.run → maintainer.update
                → (done_flag → break)
                → repeat up to max_cycles

run_v3() — graph-scheduler loop (no planner):
    initial_map → dep-graph scheduler (next_decision) → build_agent.run
                → maintainer.update → (done_flag/giveup/budget → break)
                → repeat up to max_cycles
"""
from __future__ import annotations

import enum
import os
from typing import TYPE_CHECKING, Any, Callable, Tuple

from src.envstate._loop_common import host_refresh_facts
from src.envstate.constants import VERIFY_TEST_CMD  # re-exported for back-compat
from src.envstate.constants import NO_PROGRESS_CYCLES, RESIDUAL_GIVEUP_CYCLES
from src.envstate.gate_signature import outcome_signature, next_stall
from src.envstate.ledger import ActionLedger, make_action_event
from src.envstate.done_gate import _verified_test_run_passed as _gate_passed
from src.envstate.repair_loop import run_structured_repair
from src.envstate.world_model import (
    CommandRecord,
    PlannerDecision,
    RecipePatch,
    TaskReport,
    WorldModelMap,
    merge_map,
)

if TYPE_CHECKING:
    from src.sandbox import InstallResult

# Sentinel type aliases (readable names only, no runtime cost).
Executor = Callable[[str], Tuple[bool, str]]

# Module-level constants (spec §8).
MAX_CYCLES: int = 12
LOCAL_BUDGET: int = 8
# Canonical collect-only command — kept for back-compat (some tests/modules import it).
COLLECT_ONLY_CMD: str = "pytest --collect-only -q --disable-warnings"

# VERIFY_TEST_CMD now lives in src.envstate.constants and is imported above; it is
# re-exported from this module so existing ``from ...orchestrator import VERIFY_TEST_CMD``
# call sites keep working.


# ---------------------------------------------------------------------------
# Internal termination taxonomy (v3 / graph-scheduler arm)
# ---------------------------------------------------------------------------

class TerminationReason(enum.Enum):
    """Typed termination cause for the v1/v3 loops.

    Maps to the external stop_reason strings via ``_to_stop_reason``. Introduced
    so *internal* code (logging, future extension) can distinguish the three
    logically-distinct giveup causes. The external contract still collapses all
    three to ``"planner_giveup"`` — callers observing stop_reason strings cannot
    tell them apart, by design.
    """
    DONE            = "done"
    DONE_FLAG       = "done_flag"
    GIVEUP_RESIDUAL = "giveup_residual"   # LLM giveup or runtime divergence
    GIVEUP_BUDGET   = "giveup_budget"     # LLM repair turns exhausted
    GIVEUP_STUCK    = "giveup_stuck"      # no new graph obligations (discover rounds)
    GIVEUP_NO_PROGRESS = "giveup_no_progress"  # verified test-gate signature unchanged
                                           # for NO_PROGRESS_CYCLES cycles despite repair
                                           # activity — a residual/phantom the graph
                                           # cannot close (design: residual-giveup-fix.md)
    GIVEUP_CONFIG   = "giveup_config"     # targeted obligation but no exec_readonly/client:
                                           # typed repair is impossible, and there is no
                                           # free-text fallback to silently downgrade to
    GIVEUP_REPLAY   = "giveup_replay"     # a success door (scheduler "done" OR maintainer
                                           # done_flag) opened but the latest per-cycle
                                           # replay (Model B's sole executor) did not
                                           # reproduce from base (None or rc!=0) — a build
                                           # that doesn't build is never reported as success
                                           # (guarded by `_finalize_if_replayed` in run_v3)
    MAX_CYCLES      = "max_cycles"


_TERMINATION_TO_STOP_REASON: dict[TerminationReason, str] = {
    TerminationReason.DONE:            "planner_done",
    TerminationReason.DONE_FLAG:       "done_flag",
    TerminationReason.GIVEUP_RESIDUAL: "planner_giveup",
    TerminationReason.GIVEUP_BUDGET:   "planner_giveup",
    TerminationReason.GIVEUP_STUCK:    "planner_giveup",
    TerminationReason.GIVEUP_NO_PROGRESS: "planner_giveup",
    TerminationReason.GIVEUP_CONFIG:   "planner_giveup",
    TerminationReason.GIVEUP_REPLAY:   "planner_giveup",
    TerminationReason.MAX_CYCLES:      "max_cycles",
}


def _to_stop_reason(reason: TerminationReason) -> str:
    """Map an internal ``TerminationReason`` to the external stop-reason string.

    The returned string is identical to the literal that was previously
    hard-coded at each return site — callers observing stop_reason strings
    are unaffected.
    """
    return _TERMINATION_TO_STOP_REASON[reason]


# ---------------------------------------------------------------------------
# run_v1 — three-role planner-driven loop
# ---------------------------------------------------------------------------

def run_v1(
    planner,
    build_agent,
    maintainer,
    initial_world_map: WorldModelMap,
    ledger: ActionLedger,
    sandbox_execute: Executor,
    max_cycles: int = MAX_CYCLES,
    local_budget: int = LOCAL_BUDGET,
    on_cycle=None,
    *,
    probe=None,
    manifest=None,
    exec_readonly=None,
    enable_dep_emit: bool = False,
    enable_runtime_feedback: bool = False,
):
    """Top-level v1 three-role orchestrator loop (planner-driven).

    Returns ``(final_map, stop_reason)`` where ``stop_reason`` is one of:
      ``'done_flag'``     — maintainer set WorldModelMap.done_flag=True
      ``'planner_done'``  — planner emitted action='done'
      ``'planner_giveup'``— planner emitted action='giveup'
      ``'max_cycles'``    — loop ran for max_cycles without terminating

    The loop terminates the instant done_flag is set — it does NOT wait
    for the next planner.decide call (structural fix for the 'reached gate
    but never committed' failure mode).

    New optional kwargs (all default off — every existing test and the A1 arm
    are byte-for-byte unchanged):
      exec_readonly         — callable(cmd) -> (rc: int, out: str) for read-only probes
    """
    current_map: WorldModelMap = initial_world_map
    # Monotonic step counter for ledger offsets (avoids cycle-based aliasing).
    global_step: int = 0
    # Runtime-feedback high-water mark: index into ledger.events() up to which we
    # have already ingested. Starts at 0 so the first ingest captures every event
    # not yet ingested — including any written before the loop began (e.g. a
    # pre-seeded failure). _runtime_ingest_phase advances it.
    _rt_mark: int = 0

    def _dep_emit_phase(cycle: int) -> None:
        nonlocal current_map, global_step
        if not enable_dep_emit or current_map.dep_graph is None:
            return
        if exec_readonly is None:                      # R3(c): no certify path -> no emit
            return
        from python_deps.depgraph.schema import NodeType, State
        from src.envstate.world_model import Fact
        from src.envstate.depgraph_live import certify_refresh, emit_drain, ensure_python_shim
        from python_deps.depgraph.advise import render_depgraph_planner
        # Make a bare `python` resolve to python3 before any check runs, else a
        # python3-only base fails every `python -m pip show` and nothing certifies.
        ensure_python_shim(sandbox_execute)
        graph = certify_refresh(current_map.dep_graph, exec_readonly, cycle)
        # Certify still runs (populating the frontier); emit_drain runs as a
        # deterministic prefix regardless of arm so the LLM only sees the
        # irreducible non-emittable residual. global_step is advanced here only
        # if emit_drain consumed steps, so LLM turns are NOT counted.
        graph, _reports, steps = emit_drain(
            graph, build_agent, sandbox_execute, ledger, exec_readonly,
            step_offset=global_step, cycle=cycle,
        )
        if steps:
            global_step += steps
        # Fold emit-certified packages into installed so the synthesizer's closure
        # recipe includes them even when the planner finalizes immediately.
        sat = tuple(Fact(n.name, n.version or "") for n in graph.nodes
                    if n.type is NodeType.PACKAGE and n.state is State.SATISFIED)
        have = {f.name for f in current_map.installed}
        installed = current_map.installed + tuple(f for f in sat if f.name not in have)
        advisory = render_depgraph_planner(graph)
        current_map = merge_map(
            current_map, dep_graph=graph, dep_advisory=advisory, installed=installed,
        )

    def _runtime_ingest_phase() -> None:
        nonlocal current_map, _rt_mark
        if not enable_runtime_feedback or current_map.dep_graph is None:
            return
        try:
            from python_deps.depgraph.advise import render_depgraph_planner
            from python_deps.depgraph.runtime_ingest import ingest_runtime_failures
            from python_deps.depgraph.runtime_classify import classify_observation
            events = ledger.events()
            new_events = events[_rt_mark:]
            obs = [(e.cmd, e.stdout) for e in new_events if e.rc != 0]
            if not obs:
                return
            pre_graph = current_map.dep_graph

            # Deterministic regex classifier only — no LLM tier in the v1 arm.
            classifiers = (classify_observation,)

            new_graph, found = ingest_runtime_failures(pre_graph, obs, classifiers=classifiers)
            # Advance the mark ONLY after a successful ingest call returns — so an
            # exception mid-ingest does not permanently drop those events (they are
            # re-read next cycle). (spec §11; C4 event-loss fix.)
            _rt_mark = len(events)
            if not found:
                return
            advisory = render_depgraph_planner(new_graph)
            current_map = merge_map(current_map, dep_graph=new_graph, dep_advisory=advisory)
        except (NameError, AttributeError, ImportError, TypeError) as exc:
            # Programming errors are always bugs, never operational. Still don't
            # break the run (spec §11), but log LOUDLY with a traceback — a bare
            # suppress here once hid a missing `import os` that silently disabled
            # the runtime-feedback loop for an entire benchmark arm.
            import logging
            logging.getLogger(__name__).error(
                "runtime_ingest_phase: PROGRAMMING BUG suppressed: %s", exc,
                exc_info=True,
            )
        except Exception as exc:  # noqa: BLE001 — operational errors must never break the run (spec §11)
            import logging
            logging.getLogger(__name__).warning(
                "runtime_ingest_phase: exception suppressed: %s", exc
            )

    current_map = host_refresh_facts(current_map, probe, manifest)

    for cycle in range(1, max_cycles + 1):
        # ── 0. Graph-first: certify + emit the certified closure ────────────
        _dep_emit_phase(cycle)
        # ── 0b. Runtime feedback: ingest ledger failures from the PREVIOUS cycle
        #        into the live dep-graph. Runs once per cycle before any branch so
        #        it fires regardless of which branch returns (I2 done-path fix).
        _runtime_ingest_phase()
        # ── 1. Planner decides what to do next ──────────────────────────────
        decision: PlannerDecision = planner.decide(current_map)

        if decision.action == "done":
            if on_cycle is not None:
                on_cycle(cycle, current_map, decision, None)
            return current_map, _to_stop_reason(TerminationReason.DONE)

        if decision.action == "giveup":
            if on_cycle is not None:
                on_cycle(cycle, current_map, decision, None)
            return current_map, _to_stop_reason(TerminationReason.GIVEUP_RESIDUAL)

        # ── 2. Recipe patch branch ───────────────────────────────────────────
        if decision.action == "apply_recipe_patch":
            recipe: RecipePatch | None = decision.recipe_patch
            if recipe is None or not recipe.steps:
                # Empty recipe — nothing to execute; let maintainer decide.
                empty_report = TaskReport("recipe", "done", (), "empty recipe")
                current_map = maintainer.update(current_map, empty_report)
                if on_cycle is not None:
                    on_cycle(cycle, current_map, decision, empty_report)
                if current_map.done_flag:
                    return current_map, _to_stop_reason(TerminationReason.DONE_FLAG)
                continue

            # Execute the whole recipe as a single unified run.
            report: TaskReport = build_agent.run_recipe(
                recipe,
                sandbox_execute,
                ledger,
                step_offset=global_step,
            )
            global_step += len(report.commands)

            # Deterministic facts after recipe (before the maintainer).
            current_map = host_refresh_facts(current_map, probe, manifest)

            # ── 3. Maintainer updates the world model ─────────────────────
            current_map = maintainer.update(current_map, report)

            if on_cycle is not None:
                on_cycle(cycle, current_map, decision, report)

            # ── 4. Hard-stop on done_flag — do NOT check mid-recipe ───────
            if current_map.done_flag:
                return current_map, _to_stop_reason(TerminationReason.DONE_FLAG)

            continue

        # ── 3. Legacy build-agent flow (action == "task") ────────────────────
        assert decision.task is not None, (
            f"PlannerDecision action='task' but .task is None (cycle {cycle})"
        )
        task = decision.task

        report = build_agent.run(
            task,
            sandbox_execute,
            ledger,
            step_offset=global_step,
            check=None,
            budget=local_budget,
        )
        global_step += len(report.commands)

        # ── 3b. Deterministic facts (read-only probe, OFF the ledger) ────────
        current_map = host_refresh_facts(current_map, probe, manifest)

        # ── 4. Maintainer updates the world model ────────────────────────────
        current_map = maintainer.update(current_map, report)

        # ── 5. Notify caller (optional telemetry hook) ───────────────────────
        if on_cycle is not None:
            on_cycle(cycle, current_map, decision, report)

        # ── 6. Hard-stop on done_flag — do NOT re-enter planner ──────────────
        if current_map.done_flag:
            return current_map, _to_stop_reason(TerminationReason.DONE_FLAG)

    # Exhausted all cycles without termination.
    return current_map, _to_stop_reason(TerminationReason.MAX_CYCLES)


# ---------------------------------------------------------------------------
# run_v3 — graph-scheduler loop (no planner)
# ---------------------------------------------------------------------------

def _build_install_evidence(result, failed_id, cycle):
    """Wrap a FAILED binding-install InstallResult as a single-item EvidenceBundle so the
    repair proposer sees the install stderr (RepairScope.failed_output) AND can cite it
    (PatchGate requires every proposed requirement/script-patch to reference a known evidence
    id). The install runs from a fresh-from-base container = 'fresh_replay'.

    ``failed_id`` is whatever localize_install_failure resolved — a #@node id OR a #@block id —
    so it is written to BOTH Evidence.node_id and Evidence.block_id. run_structured_repair looks
    the id up as a block_id, and build_repair_scope copies stderr only when
    ``ev.block_id == failed_block.block_id``; setting block_id keeps the stderr visible on a
    #@block failure, while node_id stays correct for the common graph-node case.
    """
    from python_deps.depgraph.evidence_log import Evidence, EvidenceBundle
    ev = Evidence(
        evidence_id=f"install.{cycle}.{failed_id or 'unknown'}",
        container_kind="fresh_replay",
        command=result.failing_command or "(install script)",
        rc=result.rc,
        output_excerpt=(result.stderr or "")[-2000:],
        cycle=cycle,
        node_id=failed_id,
        block_id=failed_id,
    )
    return EvidenceBundle().with_item(ev)


def _build_testgate_evidence(out, cycle, target_id):
    """Wrap the latest FAILING VERIFY_TEST_CMD output as a single citable Evidence
    item so a task-branch obligation repair with no attached manual block sees the
    real failure text AND can cite it — instead of the empty EvidenceBundle() that
    forces the proposer to hallucinate an evidence_ref against an empty citation
    list before finding the add_providers loophole. Mirrors _build_install_evidence
    (which threads install stderr on the binding path)."""
    from python_deps.depgraph.evidence_log import Evidence, EvidenceBundle
    if not out:
        return EvidenceBundle()
    ev = Evidence(
        evidence_id=f"testgate.{cycle}.{target_id or 'unknown'}",
        container_kind="fresh_replay",
        command=VERIFY_TEST_CMD,
        rc=1,
        output_excerpt=(out or "")[-2000:],
        cycle=cycle,
        node_id=target_id,
        block_id=target_id,
    )
    return EvidenceBundle().with_item(ev)


def run_v3(
    build_agent,
    maintainer,
    initial_world_map: WorldModelMap,
    ledger: ActionLedger,
    sandbox_execute: Executor,
    max_cycles: int = MAX_CYCLES,
    on_cycle=None,
    *,
    probe=None,
    manifest=None,
    exec_readonly=None,
    enable_dep_emit: bool = True,
    enable_runtime_feedback: bool = True,
    graph_scheduler_attempt_cap: int = 3,
    enable_gate_observability: bool = False,   # Stage 1 — observability only, byte-identical off
    gate_observer=None,                        # Callable[[tuple[GateResult, GateResult]], None] | None
    reset_to_base=None,                        # Callable[[], None] | None  (Sandbox.reset_to_base) — REQUIRED
    run_install_script=None,                   # Callable[[str], InstallResult] | None — REQUIRED
    repo_path: str | None = None,              # repo root — seeds RepoContext.local_names for
                                                # the diagnosis router (Phase 6); None -> empty set
    tracer=None,                                # RunTracer | None (Task 8) — append-only, host-owned
                                                # observability recorder. Every ``tracer.record_*``/
                                                # ``tracer.set_*`` call below is guarded by
                                                # ``if tracer is not None:`` so passing ``None`` (the
                                                # default) leaves run_v3's behavior byte-identical —
                                                # the tracer only OBSERVES, it never influences a
                                                # decision, a certify, or a write.
):
    """Top-level v3 graph-scheduler orchestrator loop (no planner).

    Returns ``(final_map, stop_reason)`` where ``stop_reason`` is one of:
      ``'done_flag'``      — maintainer set WorldModelMap.done_flag=True
      ``'planner_done'``   — scheduler returned action='done'
      ``'planner_giveup'`` — scheduler gave up (budget/residual/stuck)
      ``'max_cycles'``     — loop ran for max_cycles without terminating

    Planner is ABSENT: ``next_decision`` from the dep-graph scheduler drives
    every cycle. dep_emit and runtime_feedback are on by default (their params
    are kept for caller/test compatibility — the bodies treat them as always-on
    once the guards below are passed).

    The graph scheduler replaces the three-role LLM planner with a pure-function
    certify → emit → decide pipeline. LLM turns are bounded by ``_repair_turns``
    (seeded to ``max_cycles``).

    run_v3 is fresh-replay-only (Phase 4): every cycle's dep-emit renders the
    WHOLE certified graph to one install-only script, resets the container to
    base, and replays it (Model B). ``reset_to_base``/``run_install_script``
    are therefore required, not optional — there is no other executor, and no
    flag selects one (Phase 9 removed the vestigial ``enable_script_materialization``/
    ``enable_binding_install`` deprecation flags; for incremental/legacy
    execution, use ``run_v1`` (its real entry point is ``emit_drain``) or the
    ``block_emit`` module — a quarantined ablation baseline, not a runnable
    entry point; a full ablation loop is future work).

    Every failure bundle is diagnosed (``python_deps.depgraph.diagnose``) BEFORE
    typed repair is attempted (Phase 6): a repo-internal reference or a residual
    bug never spends a repair turn, and a pip-disproven package name is never
    retried. ``repo_path`` (optional) seeds the router's ``RepoContext.local_names``
    so a repo-local import is never mistaken for a missing PyPI package.
    """
    from src.envstate.graph_scheduler import (
        next_decision, unsatisfied_provisionable_services,
    )
    # Task 8: pure record-type imports (no behavior). Cheap/unconditional — only
    # the actual ``tracer.record_*``/``tracer.set_*`` CALLS below are guarded by
    # ``if tracer is not None:``, not this import.
    from src.envstate.run_trace import DiscoverRecord, FreshReplayRecord, PatchGateRecord
    # run_v3 has exactly one executor: fresh full-script replay from base. The
    # executor callables are therefore mandatory (there is no fallback branch
    # to silently drop into anymore).
    if reset_to_base is None or run_install_script is None:
        raise ValueError(
            "run_v3 is fresh-replay-only: reset_to_base and run_install_script are required "
            "(use the block_emit ablation or run_v1 for incremental/legacy execution)")
    # ── Container generation + VERIFY_TEST_CMD memo ──────────────────────────
    # `_container_gen` is a monotonic token bumped on EVERY container mutation
    # reachable from run_v3 — reset_to_base / run_install_script (the complete
    # set: the Python shim is baked into the rendered setup.sh and therefore
    # runs INSIDE run_install_script, not as a separate live call). It keys
    # `_verify_cache`, which lets the scheduler probe (`_run_tests_verified`)
    # and the discover gate (`_run_discover_gate`) share ONE pytest run when
    # nothing mutated the container between them, while any mutation
    # invalidates the memo so a stale pass/fail can never be served.
    from src.envstate.verify_cache import VerifyTestCache
    _container_gen: int = 0

    def _bump_gen() -> None:
        nonlocal _container_gen
        _container_gen += 1

    # Wrap the two mutating primitives at the SOURCE (not the call sites) so
    # every _binding_emit caller — per-cycle emit, every run_structured_repair
    # retry, and the task-branch replay-once — bumps the generation by
    # construction.
    _raw_reset_to_base = reset_to_base
    _raw_run_install_script = run_install_script

    def reset_to_base():
        _bump_gen()
        return _raw_reset_to_base()

    def run_install_script(script):
        _bump_gen()
        return _raw_run_install_script(script)

    _verify_cache = VerifyTestCache(
        exec_test=lambda: sandbox_execute(VERIFY_TEST_CMD),
        gen=lambda: _container_gen,
    )

    current_map: WorldModelMap = initial_world_map
    # Monotonic step counter for ledger offsets (avoids cycle-based aliasing).
    global_step: int = 0
    # Runtime-feedback high-water mark (same semantics as in run_v1).
    _rt_mark: int = 0
    _residual_giveup: str | None = None   # set when a residual is non-env / divergent (spec §3 G3, §8)
    _handed: dict[str, int] = {}          # per-obligation hand-out counts
    _sched_stuck: int = 0                 # consecutive discover cycles with no new obligations
    _sched_last_nodes: int = -1           # dep-graph node count at the last discover cycle
    _prev_gate_sig: str | None = None     # verified test-gate signature from the previous cycle
    _gate_stall: int = 0                  # consecutive cycles sharing that failing signature
    _residual_ids: set[str] = set()       # node ids whose repair diagnosed RESIDUAL
                                          # (unrepairable by env) -> excluded from the
                                          # actionable frontier via next_decision so the
                                          # scheduler stops re-handing them (part a).
    _residual_stall: int = 0              # consecutive cycles whose only repair was
                                          # residual (handout-immune; part b).
    _cycle_had_residual: bool = False     # per-cycle flags set by _repair_or_route;
    _cycle_had_env_repair: bool = False   # reset at the top of every cycle.
    _repair_turns: int = max_cycles       # LLM-repair budget (NOT mechanical installs)
    _budget_exhausted: bool = False
    # Resolved once here (not per-call) so next_decision's 'done' door and the
    # loop's fast-termination below apply the IDENTICAL service anti-hollow guard.
    _allow_services: bool = (
        os.environ.get("DOCKERAGENT_ENABLE_SERVICE_PROVISION") == "1"
    )
    _known_invalid: set[str] = set()
    _manual_blocks: tuple = ()            # persists ScriptPatch blocks across cycles
    MAX_REPAIRS_PER_BLOCK: int = 5
    # Latest per-cycle fresh-replay InstallResult (Phase 7). Model B replays the
    # WHOLE certified graph from base every cycle (no memoization), so this always
    # reflects the current graph+manual_blocks — it IS the installability proof,
    # not a proxy for it. Set by `_binding_emit` (the sole run_v3 executor);
    # threaded into `evaluate_gates` and gates the "done" decision below.
    _last_replay_result: "InstallResult | None" = None

    def _loop_log(msg: str) -> None:
        """Env-gated live loop trace (``V3_LOOP_VERBOSE=1``). Off by default →
        no output and byte-identical behavior; purely observational."""
        import os
        if os.getenv("V3_LOOP_VERBOSE"):
            print(f"[v3-loop] {msg}", flush=True)

    def _run_tests_verified() -> bool:
        """Run VERIFY_TEST_CMD and return True only if the full anti-hollow-success gate passes.

        Replaces the bare ``sandbox_execute(VERIFY_TEST_CMD)[0]`` rc=0 check used by
        the v1gs done-gate so the graph scheduler cannot finalize on a hollow pytest
        exit-code (e.g. zero tests collected, venv-wrapped run, all-skipped).
        """
        ok, out = _verify_cache.run()   # memoized per container generation (design §1)
        verify_report = TaskReport(
            "sched-verify",
            "done" if ok else "blocked",
            (CommandRecord(VERIFY_TEST_CMD, 0 if ok else 1, (out or "")[-2000:]),),
            "scheduler test probe",
        )
        passed = _gate_passed(verify_report)
        _loop_log(f"test-gate: {'PASS' if passed else 'fail'} "
                  f"(`{VERIFY_TEST_CMD}` rc={0 if ok else 1})")
        # Task 8 gap-fix: back-fill the LAST fresh-replay record with this
        # test-gate result. `_binding_emit` (the sole run_v3 executor) always
        # records test_rc=None/test_summary="" — the test gate is a SEPARATE
        # call that runs later in the cycle (here), so without this the
        # replay record's test fields could never reflect a real test run
        # and `proof.canonical_success` would be permanently unreachable.
        # Uses the VERIFIED gate result (`passed`), not the raw pytest rc —
        # a hollow pass (e.g. zero tests collected) must still record test_rc=1.
        # Under Model B every cycle replays before the scheduler calls this,
        # so there is always a last replay to back-fill.
        if tracer is not None:
            tracer.set_last_replay_tests(0 if passed else 1, (out or "")[-500:])
        return passed

    def _run_discover_gate(task, cycle: int) -> TaskReport:
        """Deterministic discover-task gate (Task 5b) — the sole executor for
        empty-``target_node_ids`` tasks. Runs exactly ``VERIFY_TEST_CMD`` (no
        LLM, no free-text mutation) and appends ONE ledger event as evidence.

        ``done_when`` must be the canonical gate; discover tasks are built with
        ``done_when=VERIFY_TEST_CMD`` (``graph_scheduler._discover_task``), so
        normalize defensively here rather than trusting a possibly-divergent
        ``task.done_when``.

        Does NOT ingest this cycle's ledger event — that happens at the TOP of
        the NEXT cycle's ``_runtime_ingest_phase`` (existing seam, keyed off
        ``_rt_mark``). Under Model B this runs against the fresh post-replay
        container automatically: the sandbox is one container object that
        ``_dep_emit_phase`` already ``reset_to_base``'d and replayed at the
        top of THIS cycle, so no separate replay is needed here.
        """
        cmd = VERIFY_TEST_CMD
        ok, out = _verify_cache.run()   # memoized per container generation (design §1)
        ledger.append(make_action_event(
            step=global_step, cmd=cmd, success=ok, stdout=(out or ""),
            env_revision_before=global_step, env_revision_after=global_step,  # discover mutates nothing
            mutation_class=None, container_id=getattr(build_agent, "container_id", ""),
        ))
        return TaskReport(
            task.goal, "done" if ok else "blocked",
            (CommandRecord(cmd, 0 if ok else 1, (out or "")[-2000:]),),
            "deterministic discover gate")

    def _finish(reason):
        _loop_log(f"STOP reason={getattr(reason, 'value', reason)}")
        # Task 8: record the final governed manual-block set on the way out (any
        # exit path) — same "fires once on exit" contract as the gate
        # observability block below, kept as a separate guard/statement so the
        # tracer-off path never even constructs the generator.
        if tracer is not None:
            tracer.set_manual_blocks(tuple(b.block_id for b in _manual_blocks))
        # Stage 1 two-gate observability: fires once on the way out (any exit path).
        # Reads existing signals, writes nothing. OFF -> no-op (byte-identical result).
        if enable_gate_observability:
            from src.envstate.gates import evaluate_gates
            # Phase 7: thread the latest per-cycle fresh-replay result so the
            # installability gate is BINDING on the canonical path (Model B
            # guarantees `_last_replay_result` is set by the time any `_finish`
            # exit fires — every cycle replays before the scheduler decides).
            _g = evaluate_gates(
                current_map.dep_graph, _run_tests_verified, replay=_last_replay_result
            )
            if gate_observer is not None:
                gate_observer(_g)
        _stop = _to_stop_reason(reason)
        return current_map, _stop

    def _finalize_if_replayed(reason):
        """Shared guard for EVERY success-reason exit (Phase 7 gap-fix).

        There are two success doors out of run_v3: the scheduler deciding
        action='done' (-> TerminationReason.DONE) and the maintainer setting
        WorldModelMap.done_flag in the task branch (-> TerminationReason.
        DONE_FLAG). Both must be equally bound to a green fresh replay — a
        run must never report success (`planner_done` / `done_flag`) unless
        the latest per-cycle replay (`_last_replay_result`, set by
        `_binding_emit`, Model B's sole executor) actually reproduced the
        environment from base. Any other exit reason (budget/residual/stuck/
        config giveups, max_cycles) is already a terminal failure and is NOT
        routed through this guard — only success reasons need downgrading.

        IMPORTANT — this guard's success (a ``stop_reason`` of ``done`` /
        ``planner_done`` / ``done_flag``) is a WEAKER signal than
        ``src.envstate.proof.canonical_success``. This guard only checks that
        the latest fresh replay's install rc was 0; ``canonical_success`` is
        strictly stronger — it also requires the replay's ``test_rc == 0``,
        no unsatisfied reciped nodes (a reciped node's ``check_command`` can
        still fail even though the install script that produced it returned
        rc0), no legacy-path usage, and an artifact-complete script. A run
        can legitimately stop here with success (build rc0, tests pass)
        while still carrying an unsatisfied reciped node — that is NOT a
        bug in this guard; ``canonical_success`` is the paper/report
        success metric, not this function's ``stop_reason``.
        """
        if _last_replay_result is None or _last_replay_result.rc != 0:
            import logging
            logging.getLogger(__name__).warning(
                "graph-scheduler: %s decided but the latest fresh replay "
                "did not reproduce from base (rc=%s, failing_command=%r) — "
                "giving up instead of reporting success",
                reason,
                _last_replay_result.rc if _last_replay_result is not None else None,
                _last_replay_result.failing_command if _last_replay_result is not None else None,
            )
            return _finish(TerminationReason.GIVEUP_REPLAY)
        return _finish(reason)

    def _binding_emit(graph, manual_blocks, cycle):
        """Canonical fresh-replay executor (Phase 4 — Model B, the SOLE run_v3 executor).

        Render the WHOLE certified graph to one install-only script, reset the
        container to base, replay the script, then certify reciped nodes
        against the live host. Hoisted to ``run_v3`` scope (not nested in
        ``_dep_emit_phase``) so both the per-cycle emit below AND every
        ``run_structured_repair`` retry call through this SAME closure — one
        replay implementation, no duplicated render/reset/install logic.

        Records the raw ``InstallResult`` into ``_last_replay_result`` (Phase 7)
        before returning — this is the one place the executor actually runs
        ``run_install_script``, so it is the right place to capture the binding
        installability proof for ``evaluate_gates``/the "done" guard.

        Returns ``(graph, evidence_bundle_or_None, failed_node_id_or_None)``.
        """
        nonlocal _last_replay_result
        from python_deps.depgraph.build_script import render_build_script
        from python_deps.depgraph.emit import _is_reciped
        from src.envstate.install_localizer import localize_install_failure, certify_reciped_only
        # Defense-in-depth: a repair proposal must not add a reciped node that can't be certified.
        _missing = [n.id for n in graph.nodes if _is_reciped(n) and not n.check_command]
        if _missing:
            raise ValueError(
                f"binding-install repair: reciped nodes lack a check_command: {_missing}")
        script = render_build_script(graph, manual_blocks)
        # Model B: every cycle is a real fresh replay from base — no skip/memoization
        # (the replay produces the current evidence bundle; caching is deferred to
        # the docker-build future work).
        reset_to_base()
        result = run_install_script(script)
        _last_replay_result = result
        graph, unsat = certify_reciped_only(graph, exec_readonly, cycle)
        _loop_log(f"cycle {cycle}: fresh-replay install rc={result.rc}"
                  + (f" FAIL@{result.failing_command!r}" if result.rc != 0 else "")
                  + f" | reciped-unsatisfied={len(unsat)}")
        if tracer is not None:
            # Task 8: one FreshReplayRecord per cycle (Model B — every cycle
            # replays). test_rc/test_summary are not available at THIS site
            # (the test gate is a separate call, e.g. _run_tests_verified /
            # _run_discover_gate) — recorded as None/"" here; this record is
            # the INSTALL result only.
            from python_deps.depgraph.schema import State
            _certified_ids = tuple(sorted(
                n.id for n in graph.nodes if _is_reciped(n) and n.state is State.SATISFIED
            ))
            tracer.record_replay(FreshReplayRecord(
                ran=True,
                setup_rc=result.rc,
                failing_command=result.failing_command if result.rc != 0 else None,
                certified_node_ids=_certified_ids,
                unsatisfied_node_ids=tuple(unsat),
                test_rc=None,
                test_summary="",
            ))
        if result.rc != 0:
            _node = (localize_install_failure(script, result.failing_command).node_id
                     or (unsat[0] if unsat else None))
            # Carry the fresh install stderr as evidence into the next repair scope.
            return graph, _build_install_evidence(result, _node, cycle), _node
        return graph, None, (unsat[0] if unsat else None)

    # ── Diagnosis router (Phase 6) ────────────────────────────────────────────
    # RepoContext.local_names is built once from the repo (repo_path may be None
    # in unit tests that never construct a real filesystem tree — the router then
    # degrades to "no known local names", never REPO_INTERNAL_REF). invalid_names
    # accumulates as pip disproves package names across cycles; the context is
    # rebuilt on every call so a name disproven THIS cycle is honored next cycle.
    from python_deps.depgraph.diagnose import RepoContext, Mode, diagnose_all
    from python_deps.depgraph import scan
    from python_deps.import_mapping import normalize_package_name
    _local_names = frozenset(scan.local_module_names(repo_path)) if repo_path else frozenset()
    _invalid_names: set[str] = set()

    def _repo_ctx() -> RepoContext:
        return RepoContext(local_names=_local_names, invalid_names=frozenset(_invalid_names))

    def _repair_or_route(graph, failed_id, bundle, cycle, *, target_hint=None, cap_failed_id=False):
        """Diagnose the failure that produced ``bundle`` BEFORE typed repair.

        ENVIRONMENT / AMBIGUOUS      -> run_structured_repair (AMBIGUOUS spends a
                                        propose turn to disambiguate — see repair_scope).
        REPO_INTERNAL_REF / RESIDUAL -> non-environment: return graph unchanged (no repair).
        INVALID_ATTEMPT (and nothing environment-shaped) -> record the normalized
                                        disproven name; no repair.

        This is the SINGLE ``run_structured_repair`` call site in ``run_v3`` — both
        the main-loop (``_dep_emit_phase``) and the task-branch obligation-repair
        site route through this helper so diagnosis is applied identically.

        Note (deviation from the design pseudocode): ``diagnose.Diagnosis.discovery``
        is only ever populated for ``Mode.ENVIRONMENT`` — every ``INVALID_ATTEMPT``
        diagnosis carries ``discovery=None`` (see diagnose.py's two INVALID_ATTEMPT
        return sites). The disproven name is therefore re-derived here via
        ``classify_dependency_failure`` on the SAME (command, output) pair rather
        than read off ``d.discovery.name`` (which would always be ``None``).
        """
        nonlocal _manual_blocks, _known_invalid, _repair_turns, _budget_exhausted
        nonlocal _residual_ids, _cycle_had_residual, _cycle_had_env_repair
        observations = (
            tuple((it.command, it.output_excerpt) for it in bundle.items) if bundle else ()
        )
        diags = diagnose_all(observations, _repo_ctx()) if observations else ()
        modes = {d.mode for d in diags}
        _loop_log(f"cycle {cycle}: diagnose failed={failed_id} "
                  f"modes={sorted(m.value for m in modes) or ['(none)']}")
        if Mode.REPO_INTERNAL_REF in modes or Mode.RESIDUAL in modes:
            if Mode.RESIDUAL in modes:
                _cycle_had_residual = True
                if failed_id:
                    # Unrepairable by env: drop from the actionable frontier so the
                    # scheduler stops re-handing it (part a). The residual-stall
                    # counter (main loop) owns convergence; a handout never resets it.
                    _residual_ids.add(failed_id)
            return graph
        if Mode.INVALID_ATTEMPT in modes and not (modes & {Mode.ENVIRONMENT, Mode.AMBIGUOUS}):
            from python_deps.failure_classifier import classify_dependency_failure
            for (cmd, out), d in zip(observations, diags):
                if d.mode is not Mode.INVALID_ATTEMPT:
                    continue
                name = classify_dependency_failure(cmd, out).package_name
                if name:
                    _invalid_names.add(normalize_package_name(name))
            return graph
        _loop_log(f"cycle {cycle}: LLM repair → propose "
                  f"(node={failed_id}, turns_left={_repair_turns})")
        _cycle_had_env_repair = True   # a real ENVIRONMENT/AMBIGUOUS repair this cycle
        _out = run_structured_repair(
            graph, failed_id, bundle, cycle,
            propose=lambda s, **k: build_agent.propose(s, exec_readonly, **k),
            emit=lambda g, mb: _binding_emit(g, mb, cycle),   # REPLAY emit (Model B)
            manual_blocks=_manual_blocks, known_invalid=_known_invalid,
            max_repairs=MAX_REPAIRS_PER_BLOCK, repair_budget=_repair_turns,
            target_hint=target_hint, cap_failed_id=cap_failed_id)
        if tracer is not None:
            # Task 8: RepairOutcome (repair_loop.RepairOutcome) does NOT expose
            # an "accepted"/"accepted_node_ids"/"errors" surface — it only
            # returns the resulting graph/manual_blocks/still_failing_id/
            # turns_spent/budget_exhausted. Derive the PatchGateRecord fields
            # from a before/after diff instead of inventing fields that don't
            # exist on RepairOutcome:
            #   accepted_node_ids  — node ids present in _out.graph but not in
            #                        the graph passed INTO run_structured_repair
            #                        (i.e. admitted add_requirements).
            #   accepted_block_ids — block ids present in _out.manual_blocks but
            #                        not in the manual_blocks passed in (i.e.
            #                        admitted script_patches).
            #   accepted           — True iff the graph OR manual_blocks changed
            #                        at all (covers a providers-only admission —
            #                        e.g. a chosen_fix correction — which adds no
            #                        new node/block id but DOES mutate the graph).
            #   errors             — RepairOutcome does not surface per-attempt
            #                        validate_proposal() errors (they are
            #                        swallowed inside the internal retry loop);
            #                        left empty rather than fabricated.
            _before_node_ids = frozenset(n.id for n in graph.nodes) if graph is not None else frozenset()
            _before_block_ids = frozenset(b.block_id for b in _manual_blocks)
            _after_node_ids = (frozenset(n.id for n in _out.graph.nodes)
                               if _out.graph is not None else frozenset())
            _after_block_ids = frozenset(b.block_id for b in _out.manual_blocks)
            _new_node_ids = tuple(sorted(_after_node_ids - _before_node_ids))
            _new_block_ids = tuple(sorted(_after_block_ids - _before_block_ids))
            _ev_ref = (bundle.items[0].evidence_id
                      if (bundle is not None and bundle.items) else None)
            tracer.record_patchgate(PatchGateRecord(
                cycle=cycle,
                failed_block_id=failed_id,
                evidence_ref=_ev_ref,
                accepted=(_out.graph != graph) or (_out.manual_blocks != _manual_blocks),
                accepted_node_ids=_new_node_ids,
                accepted_block_ids=_new_block_ids,
                errors=(),
            ))
        _manual_blocks = _out.manual_blocks
        _known_invalid = set(_out.known_invalid)
        _repair_turns -= _out.turns_spent
        _loop_log(f"cycle {cycle}: repair outcome turns_spent={_out.turns_spent} "
                  f"still_failing={getattr(_out, 'still_failing_id', None)} "
                  f"budget_exhausted={_out.budget_exhausted}")
        if _out.budget_exhausted or _repair_turns <= 0:
            _budget_exhausted = True
        return _out.graph

    def _dep_emit_phase(cycle: int) -> None:
        nonlocal current_map, _manual_blocks
        if not enable_dep_emit or current_map.dep_graph is None:
            return
        if exec_readonly is None:                      # R3(c): no certify path -> no emit
            return
        from python_deps.depgraph.schema import NodeType, State
        from src.envstate.world_model import Fact
        from src.envstate.depgraph_live import certify_refresh
        from python_deps.depgraph.emit import partition
        graph = certify_refresh(current_map.dep_graph, exec_readonly, cycle)
        # Snapshot: was the graph already fully certified BEFORE this cycle's
        # fresh replay? (Same predicate as the provisional installability
        # gate, gates.py:97-99.) If so, a clean replay reproduces the
        # identical container this certify just certified against, so the
        # post-emit re-certify below is a redundant no-op (design:
        # testgate-certify.md §3).
        _pre = partition(graph)
        _pre_fully_certified = not (_pre.emittable + _pre.frontier)
        # Certify still runs (populating the frontier); the fresh-replay emit
        # below is the SOLE executor (Phase 4) — LLM turns are counted only
        # through run_structured_repair (_repair_turns), never global_step,
        # since the emit itself makes no LLM calls.
        graph, _bundle, _failed_node = _binding_emit(graph, _manual_blocks, cycle)
        if _failed_node is not None and getattr(build_agent, "client", None) is not None:
            # On an install failure, seed the repair with the install stderr as citable
            # evidence; on an rc0-but-unsatisfied node there is no install command failure,
            # so the scope falls back to the node's requirement slice (bundle None).
            # _failed_node is a NODE id (from certify/localize), not a block id; the
            # binding path has no block-keyed bundle, so seed target_hint so the repair
            # scope still resolves the unsatisfied node's requirement slice.
            # Budget note: this call reseeds max_repairs=MAX_REPAIRS_PER_BLOCK for
            # THIS node; combined with the task-branch call site below, a stubborn
            # node can be charged up to ~2x MAX_REPAIRS attempts in a single cycle —
            # the GLOBAL bound is still _repair_turns (checked as `_repair_turns <= 0`).
            graph = _repair_or_route(
                graph, _failed_node, _bundle, cycle,
                target_hint=_failed_node, cap_failed_id=True)
        # Post-emit re-certify — only when the emit could have changed a
        # node's certification versus the start-of-cycle certify above: the
        # fresh install failed / a reciped node stayed unsatisfied
        # (_failed_node is not None), OR there was installable work
        # outstanding at the start of the cycle that this cycle's fresh
        # reinstall may have satisfied (not _pre_fully_certified). When the
        # graph was already fully certified AND the replay was clean, the
        # reinstall reproduces the exact container already certified against,
        # so re-certifying here would be a redundant probe sweep. Idempotent
        # when it does run.
        if _failed_node is not None or not _pre_fully_certified:
            graph = certify_refresh(graph, exec_readonly, cycle)
        # Fold emit-certified packages into installed so the synthesizer's closure
        # recipe includes them even when the planner finalizes immediately.
        sat = tuple(Fact(n.name, n.version or "") for n in graph.nodes
                    if n.type is NodeType.PACKAGE and n.state is State.SATISFIED)
        have = {f.name for f in current_map.installed}
        installed = current_map.installed + tuple(f for f in sat if f.name not in have)
        # Under the graph scheduler the planner is bypassed, so the planner-facing
        # dep_advisory render is dead work; reuse the prior advisory and skip the
        # compute. The dep_graph + installed merge is UNCHANGED — the scheduler reads
        # current_map.dep_graph, so it must still be merged every cycle.
        advisory = current_map.dep_advisory
        current_map = merge_map(
            current_map, dep_graph=graph, dep_advisory=advisory, installed=installed,
            manual_blocks=_manual_blocks,
        )

    def _runtime_ingest_phase(cycle: int) -> None:
        nonlocal current_map, _rt_mark, _residual_giveup
        if not enable_runtime_feedback or current_map.dep_graph is None:
            return
        try:
            from python_deps.depgraph.advise import render_depgraph_planner
            from python_deps.depgraph.runtime_ingest import ingest_runtime_failures
            from python_deps.depgraph.diagnose import make_diagnostic_classifier, is_local_import
            events = ledger.events()
            new_events = events[_rt_mark:]
            obs = [(e.cmd, e.stdout) for e in new_events if e.rc != 0]
            if not obs:
                return
            pre_graph = current_map.dep_graph
            _out_of_scope: list[tuple[str, str]] = []   # non-env diagnoses; Task 6 reads this

            # Deterministic tier: route through the SAME diagnosis router used by
            # ``_repair_or_route`` (Phase 6), not the raw ``classify_observation``
            # regex classifier — otherwise a repo-local import surfaced by the
            # discover gate (``VERIFY_TEST_CMD``) would be mis-ingested as a PyPI
            # package here even though ``_repair_or_route`` would have refused to
            # repair it (the local-import guard must hold on BOTH the install-
            # failure/repair path and this discover-gate/ingest path). The temp-0
            # LLM tier is appended when a client exists (spec §6 cascade).
            classifiers = (make_diagnostic_classifier(_repo_ctx()),)
            if getattr(build_agent, "client", None) is not None:
                from src.envstate.llm_classifier import make_llm_classifier
                from src.envstate.llm_response import complete_with_retry
                from src.envstate.jsonutil import extract_json_object

                def _complete(messages):
                    text, _usage, _resp = complete_with_retry(
                        build_agent.client, build_agent.model, messages,
                        accept=lambda t: extract_json_object(t) is not None,
                        temperature=0, max_attempts=2,
                    )
                    return text

                _llm = make_llm_classifier(
                    _complete,
                    note_out_of_scope=lambda c, r: _out_of_scope.append((c, r)),
                )
                # Bound LLM fan-out: spec §6 is "LLM only on the misses", but a cycle
                # with 30 failed events would fire 30 synchronous temp-0 calls. Classify
                # each UNIQUE error tail at most once, capped per cycle.
                _seen_errs: set[str] = set()
                _MAX_LLM_PER_CYCLE = 5

                def _bounded_llm(cmd, out):
                    key = (out or "")[-500:]
                    if key in _seen_errs or len(_seen_errs) >= _MAX_LLM_PER_CYCLE:
                        return None
                    _seen_errs.add(key)
                    return _llm(cmd, out)

                # The deterministic tier above already applies the local-import
                # guard; the LLM tier bypasses ``diagnose()`` entirely (it is a
                # free-text classifier), so it must be guarded independently here
                # or a repo-local import that dodges the deterministic regexes
                # could still be proposed as a package by the LLM.
                _ctx = _repo_ctx()

                def _guarded_llm(cmd, out):
                    disc = _bounded_llm(cmd, out)
                    if disc is None:
                        return None
                    imp = (disc.data or {}).get("import_name") or disc.name
                    if is_local_import(imp, _ctx.local_names):
                        return None
                    return disc

                classifiers = (make_diagnostic_classifier(_repo_ctx()), _guarded_llm)

            new_graph, found = ingest_runtime_failures(pre_graph, obs, classifiers=classifiers)
            # Design §3: the testability gate is the DESIGNATED dlopen-tail oracle.
            # The classify tier above discovers the soname (bare SystemLib node);
            # this pass resolves it to apt via the SAME extract_needs+resolve path
            # as import_probe, collapsing onto syslib:<soname> so the dlopen-tail
            # need becomes renderable into setup.sh. ldd+import remain the PARTIAL
            # backstop (DT_NEEDED + eager module-init); the test run owns the rest.
            from src.envstate.depgraph_live import test_gate_soname_refresh
            new_graph = test_gate_soname_refresh(
                new_graph, exec_readonly, obs, VERIFY_TEST_CMD
            )
            if tracer is not None:
                # Task 8: run_v3's ONLY ledger writer is _run_discover_gate (the
                # deterministic VERIFY_TEST_CMD gate — no free-text mutation
                # exists in run_v3), so every event in `obs` at this point
                # originates from a discover-gate cycle; recording ONE
                # DiscoverRecord per _runtime_ingest_phase call (this is the
                # "next-cycle ingest that consumes [the gate's] event" from the
                # brief) is therefore correct without needing a second record
                # call inside _run_discover_gate itself.
                #
                # diagnosis_modes is computed here PURELY for observability via
                # the SAME diagnose_all/_repo_ctx() used by _repair_or_route —
                # this is a read-only, side-effect-free re-classification of
                # the same (command, output) pairs already computed above; it
                # does not feed into `found`/`new_graph` or any decision.
                _pre_ids = frozenset(n.id for n in pre_graph.nodes) if pre_graph is not None else frozenset()
                _post_ids = frozenset(n.id for n in new_graph.nodes) if new_graph is not None else frozenset()
                _new_ids = tuple(sorted(_post_ids - _pre_ids))
                _diags = diagnose_all(tuple(obs), _repo_ctx())
                tracer.record_discover(DiscoverRecord(
                    cycle=cycle,
                    command=VERIFY_TEST_CMD,
                    used_llm_mutation=False,
                    new_node_ids=_new_ids,
                    diagnosis_modes=tuple(d.mode.value for d in _diags),
                ))
            # Advance the mark ONLY after a successful ingest call returns — so an
            # exception mid-ingest does not permanently drop those events (they are
            # re-read next cycle). (spec §11; C4 event-loss fix.)
            _rt_mark = len(events)
            # Honest give-up (spec §3 G3 / §8): a residual that maps to an already-
            # SATISFIED node (divergence) or that the LLM judged non-env, when the
            # deterministic frontier is clean, is not fixable by adding nodes. Record
            # the reason; the main loop returns planner_giveup. done_flag is NEVER set.
            import logging
            from python_deps.depgraph.runtime_ingest import diverged_node_ids
            from python_deps.depgraph.emit import partition
            from python_deps.depgraph.schedule import scheduler_frontier
            diverged = diverged_node_ids(pre_graph, found)
            # Match the real scheduler call (next_decision): the give-up
            # frontier must see Service/binding obligations on-arm, or it
            # reports "no actionable" while a binding is genuinely pending
            # and the agent gives up prematurely (spurious give-up).
            _allow_services = (
                os.environ.get("DOCKERAGENT_ENABLE_SERVICE_PROVISION") == "1"
            )
            no_actionable = not scheduler_frontier(
                new_graph, allow_services=_allow_services
            )
            if diverged and no_actionable and not partition(new_graph).emittable:
                _residual_giveup = (
                    f"graph-scheduler: residual not an environment obligation "
                    f"(diverged={list(diverged)}, out_of_scope={len(_out_of_scope)})"
                )
                logging.getLogger(__name__).info(
                    "residual-handler give-up: %s", _residual_giveup
                )
            if not found:
                return
            advisory = render_depgraph_planner(new_graph)
            current_map = merge_map(current_map, dep_graph=new_graph, dep_advisory=advisory)
        except (NameError, AttributeError, ImportError, TypeError) as exc:
            # Programming errors are always bugs, never operational. Still don't
            # break the run (spec §11), but log LOUDLY with a traceback — a bare
            # suppress here once hid a missing `import os` that silently disabled
            # the runtime-feedback loop for an entire benchmark arm.
            import logging
            logging.getLogger(__name__).error(
                "runtime_ingest_phase: PROGRAMMING BUG suppressed: %s", exc,
                exc_info=True,
            )
        except Exception as exc:  # noqa: BLE001 — operational errors must never break the run (spec §11)
            import logging
            logging.getLogger(__name__).warning(
                "runtime_ingest_phase: exception suppressed: %s", exc
            )

    # Refresh contract — why each host_refresh_facts call is necessary and not
    # redundant with the next _dep_emit_phase certify:
    #
    #   host_refresh_facts  → probe() → apply_deterministic()
    #     Updates: installed (full set), env, language, system_installed,
    #              import_results, build_system, required, open_problems, progress.
    #
    #   _dep_emit_phase certify → certify_refresh() → merge_map()
    #     Updates: dep_graph (node states) + installed (PACKAGE nodes that reached
    #              State.SATISFIED).  Does NOT call probe(); never touches env,
    #              language, system_installed, import_results, build_system,
    #              required, open_problems, or progress.
    #
    # ① Pre-loop (here): initial_world_map may be stale from the caller; the
    #   first cycle's _dep_emit_phase certify does NOT call probe(), so it
    #   cannot fill in these fields.  Required.
    #
    # ② Post-task (inside task branch): build_agent just mutated the container.
    #   The NEXT cycle's certify won't call probe(), and maintainer.update in
    #   THIS cycle needs the fresh probe facts.  Required.
    #
    # Conclusion: both sites are the minimal set — neither can be removed
    # without leaving the maintainer or the scheduler with stale probe data.
    current_map = host_refresh_facts(current_map, probe, manifest)

    for cycle in range(1, max_cycles + 1):
        _loop_log(f"══════ cycle {cycle}/{max_cycles} ══════")
        _cycle_had_residual = False
        _cycle_had_env_repair = False
        # ── 0. Graph-first: certify + emit the certified closure ────────────
        _dep_emit_phase(cycle)
        if _budget_exhausted:
            # graph-scheduler: LLM turn budget exhausted — bounded repair gave up
            return _finish(TerminationReason.GIVEUP_BUDGET)
        # ── 0b. Runtime feedback: ingest ledger failures from the PREVIOUS cycle
        #        into the live dep-graph. Runs once per cycle before any branch so
        #        it fires regardless of which branch returns (I2 done-path fix).
        _runtime_ingest_phase(cycle)
        if _residual_giveup is not None:
            return _finish(TerminationReason.GIVEUP_RESIDUAL)
        # ── 1. Graph-scheduler decides what to do next ──────────────────────
        decision, chosen = next_decision(
            current_map.dep_graph,
            _run_tests_verified,
            handed=_handed,
            attempt_cap=graph_scheduler_attempt_cap,
            residual_ids=frozenset(_residual_ids),
            allow_services=_allow_services,
        )
        _loop_log(f"cycle {cycle}: scheduler decision={decision.action}"
                  + (f" node={chosen}" if chosen is not None else ""))
        if chosen is not None:
            _handed[chosen] = _handed.get(chosen, 0) + 1
            _sched_stuck = 0
        elif decision.action == "task":          # discover task → sufficiency-stuck
            n_nodes = len(current_map.dep_graph.nodes) if current_map.dep_graph else 0
            _sched_stuck = _sched_stuck + 1 if n_nodes <= _sched_last_nodes else 0
            _sched_last_nodes = n_nodes
            # Bound is intentional (Task 5c): the discover gate (Task 5b) never spends
            # LLM-repair budget, so without this counter an all-discover run with an
            # unclassifiable failure would loop until max_cycles instead of giving up.
            if _sched_stuck >= 2:                # consecutive discover rounds revealed no new obligations
                if on_cycle is not None:
                    on_cycle(cycle, current_map, decision, None)
                return _finish(TerminationReason.GIVEUP_STUCK)

        # ── 1b. No-progress detector (design: residual-giveup-fix.md) ────────
        # The single missing invariant: "did satisfying obligations change the
        # TEST outcome?" Model B reinstalls the whole certified graph every
        # cycle, so an unchanged VERIFIED test-gate signature across
        # NO_PROGRESS_CYCLES consecutive cycles means repairs are landing but the
        # suite is not moving toward green — a residual (test-logic) failure or a
        # phantom obligation no environment change can fix. Give up honestly
        # instead of churning to max_cycles / a watchdog kill.
        #
        # Sampled here (post-emit, pre-task-repair): on a discover-clean cycle
        # next_decision already ran the gate (memoized → free); on a targeted-
        # obligation cycle next_decision returned before running it, so this
        # forces exactly ONE memoized pytest run against this cycle's freshly
        # replayed container. A verified PASS never trips it (the scheduler owns
        # the 'done' door); a CHANGED failing signature is progress and resets.
        _gate_passed_now = _run_tests_verified()
        _gate_out = _verify_cache.run()[1]
        _gate_sig = outcome_signature(_gate_passed_now, _gate_out)
        _gate_stall = next_stall(_prev_gate_sig, _gate_sig, _gate_stall)
        _prev_gate_sig = _gate_sig
        _loop_log(f"cycle {cycle}: test-gate sig={_gate_sig} "
                  f"stall={_gate_stall}/{NO_PROGRESS_CYCLES}")

        # ── 1c. Fast-termination on a verified test pass (design: fast-termination) ──
        # The scheduler only reaches its 'done' door when the frontier is EMPTY,
        # so an over-predicted OPTIONAL node (a phantom tool, an unused optional
        # import) keeps the frontier non-empty and next_decision hands it out
        # attempt_cap times WITHOUT re-checking tests — burning ~3 cycles per
        # stuck node even though the suite already passes. Recall-first: a
        # VERIFIED test pass is the SUFFICIENT signal; a node still MISSING when
        # tests pass WITHOUT it is an over-prediction, not a requirement. When the
        # scheduler wants to keep working (action != 'done') but the verified gate
        # passes, take the SAME replay-bound DONE door the scheduler's own 'done'
        # branch uses — after honoring the SAME service anti-hollow guard (an
        # unsatisfied provisionable service the tests actually need is NOT an
        # over-prediction, so it still blocks success). _gate_passed_now is the
        # anti-hollow-verified result, so a hollow pass (zero collected / all
        # skipped) is already False here and never fast-terminates.
        if (_gate_passed_now
                and decision.action != "done"
                and not unsatisfied_provisionable_services(
                    current_map.dep_graph, allow_services=_allow_services)):
            _loop_log(f"cycle {cycle}: fast-terminate — verified test pass with "
                      f"{'frontier node ' + str(chosen) if chosen is not None else 'a discover task'} "
                      f"still pending (over-prediction) → DONE")
            if on_cycle is not None:
                on_cycle(cycle, current_map, decision, None)
            return _finalize_if_replayed(TerminationReason.DONE)

        if _gate_stall >= NO_PROGRESS_CYCLES:
            if on_cycle is not None:
                on_cycle(cycle, current_map, decision, None)
            return _finish(TerminationReason.GIVEUP_NO_PROGRESS)

        if decision.action == "done":
            # Phase 7: "done" is authoritative only if the latest per-cycle
            # replay (Model B's sole executor) actually reproduced from base.
            # Tests already run inside that same fresh-replayed container, so
            # rc!=0 here should never coincide with a real "done" — this is a
            # defensive assertion, not the normal path. Never report done on a
            # build that didn't build. `_finalize_if_replayed` is the SAME
            # guard used by the done_flag exit below — one implementation for
            # both success doors (review fix wave).
            if on_cycle is not None:
                on_cycle(cycle, current_map, decision, None)
            return _finalize_if_replayed(TerminationReason.DONE)

        if decision.action == "giveup":
            if on_cycle is not None:
                on_cycle(cycle, current_map, decision, None)
            return _finish(TerminationReason.GIVEUP_RESIDUAL)

        # ── 3. Graph-scheduler task branch (action == "task") ────────────────
        assert decision.task is not None, (
            f"PlannerDecision action='task' but .task is None (cycle {cycle})"
        )
        task = decision.task
        _targets = getattr(task, "target_node_ids", ()) or ()
        if _targets:
            if exec_readonly is None or getattr(build_agent, "client", None) is None:
                # Canonical v3 cannot do typed repair without a read-only executor +
                # client. Do NOT silently downgrade to free-text mutation (that path
                # no longer exists in run_v3) — give up honestly instead.
                return _finish(TerminationReason.GIVEUP_CONFIG)
            from python_deps.depgraph.patch_gate import compose_script
            from python_deps.depgraph.evidence_log import EvidenceBundle
            _g = current_map.dep_graph
            _blocks = compose_script(_g, _manual_blocks) if _g is not None else ()
            _tid = _targets[0]
            _fb = next((b.block_id for b in _blocks if _tid in b.target_node_ids), None)
            # Budget note (both branches below): each _repair_or_route call reseeds
            # max_repairs=MAX_REPAIRS_PER_BLOCK per node, so a stubborn node routed
            # through both this task-branch call and the main-loop call above can be
            # charged up to ~2x MAX_REPAIRS attempts in a single cycle — the GLOBAL
            # bound is still _repair_turns (checked as `_repair_turns <= 0`).
            if _fb is None:
                # No manual block targets this node yet — nothing to pre-check.
                # Thread THIS cycle's failing test-gate output as citable evidence
                # (reusing the `_gate_out`/`_gate_passed_now` already sampled by the
                # no-progress detector) instead of an empty EvidenceBundle() that
                # would make the proposer burn turns hallucinating an evidence_ref.
                # Only when the gate actually FAILED; a frontier obligation handed
                # out while the gate passes has no failure to cite, so the scope
                # falls back to the node's requirement slice via target_hint.
                _ev = (_build_testgate_evidence(_gate_out, cycle, _tid)
                       if not _gate_passed_now else EvidenceBundle())
                _g = _repair_or_route(_g, _tid, _ev, cycle, target_hint=_tid)
            else:
                # A manual block already targets this node (prior-cycle repair) — replay
                # once (Model B) to see whether it still fails before spending another
                # repair turn.
                _g, _b2, _f2 = _binding_emit(_g, _manual_blocks, cycle)
                if _f2 is not None:
                    _g = _repair_or_route(_g, _f2, _b2, cycle, target_hint=_tid)
            current_map = merge_map(current_map, dep_graph=_g, manual_blocks=_manual_blocks)
            report = TaskReport(task.goal, "done", (), "structured-repair task")
        else:
            # Discover task (empty target_node_ids): run the deterministic gate and
            # record ledger evidence; the NEXT cycle's _runtime_ingest_phase turns a
            # failure into typed obligations. No LLM call here, so _repair_turns is
            # untouched — the discover path is bounded by _sched_stuck (Task 5c), not
            # the LLM-repair budget.
            report = _run_discover_gate(task, cycle)

        # ── Residual-churn giveup (design: residual-node-drop.md) ─────────────
        # A node whose repair diagnosed RESIDUAL is unrepairable by any env
        # change: part (a) already dropped it from the frontier (_residual_ids)
        # so it stops being re-handed. This counter converges the loop even when
        # a FRESH phantom is minted every cycle and the outcome signature is
        # unstable (defeating the gate-signature no-progress detector above). A
        # node HANDOUT never resets it; a real ENVIRONMENT repair (this cycle)
        # does — so a genuinely-progressing multi-cycle repair is never cut off.
        if _cycle_had_env_repair:
            _residual_stall = 0
        elif _cycle_had_residual:
            _residual_stall += 1
            if _residual_stall >= RESIDUAL_GIVEUP_CYCLES:
                if on_cycle is not None:
                    on_cycle(cycle, current_map, decision, report)
                return _finish(TerminationReason.GIVEUP_RESIDUAL)

        if _budget_exhausted or _repair_turns <= 0:
            return _finish(TerminationReason.GIVEUP_BUDGET)
        # Scheduler mode: a passing host check can return zero commands; floor the
        # advance at 1 so the ledger step offset never aliases across cycles.
        global_step += max(len(report.commands), 1)

        # ② Probe refresh after task — see refresh contract above.
        # Task mutated the container; next cycle's certify won't call probe(),
        # and maintainer.update below needs fresh probe facts (OFF the ledger).
        current_map = host_refresh_facts(current_map, probe, manifest)

        # ── 4. Maintainer updates the world model ────────────────────────────
        current_map = maintainer.update(current_map, report)

        # ── 5. Notify caller (optional telemetry hook) ───────────────────────
        if on_cycle is not None:
            on_cycle(cycle, current_map, decision, report)

        # ── 6. Hard-stop on done_flag — do NOT re-enter scheduler ────────────
        # Review fix wave: done_flag is the SECOND success door (the maintainer
        # can set it in the task branch independently of the scheduler's "done"
        # decision) — it must be bound to a green replay exactly like DONE is,
        # via the same `_finalize_if_replayed` guard, not left ungated.
        if current_map.done_flag:
            return _finalize_if_replayed(TerminationReason.DONE_FLAG)

    # Exhausted all cycles without termination.
    return _finish(TerminationReason.MAX_CYCLES)
