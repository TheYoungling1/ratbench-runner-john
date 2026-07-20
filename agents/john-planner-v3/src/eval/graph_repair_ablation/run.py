"""Runner core for the graph-repair-ablation pilot (Task 5, part 1).

Wires the injection oracle (`oracle.py`) + context treatments (`context.py`) +
the real repair agent (`src.envstate.v3_build_agent.V3BuildAgent`) into one
`run_one(inj, arm, ...)` call per (injection, arm) cell. The only thing that
differs between arms is the context string appended to the agent's rendered
repair scope (`arm_context`); everything else -- same agent, model,
max_diag_turns, container, injection, seed -- is held constant so a C1-vs-C0
difference in localization is attributable to the graph structure alone.

Reuse-by-import only: this module never edits `v3_build_agent.py`,
`repair_scope.py`, or `patch.py`. The arm context is injected by monkeypatching
`render_repair_scope` for the duration of the `propose()` call (see
`_augmented_render` below) -- `propose()` renders `scope` internally and does
not accept pre-rendered text, so string-concatenating into its public API is
not possible without this patch (see `scratchpad/task5-recon-report.md` §1).

DEVIATION FROM THE RECON/PLAN'S PATCH TARGET (verified by actually running the
agent-wiring test, not just reading): `propose()`'s `from src.envstate.repair_scope
import render_repair_scope` is a LOCAL import inside the method body (v3_build_agent.py
line 150), not a module-level one. A local import never adds an attribute to the
importing module's namespace -- it looks the name up on the SOURCE module
(`src.envstate.repair_scope`) fresh on every call and binds it as a function-local
variable. So `src.envstate.v3_build_agent` has no `render_repair_scope` attribute at
all, and `mock.patch("src.envstate.v3_build_agent.render_repair_scope", ...)` (the
plan's literal target) raises `AttributeError` -- confirmed by running the test.
The correct patch target is the SOURCE module's attribute,
`src.envstate.repair_scope.render_repair_scope`: since `propose()` re-resolves the
name from that module at call time, patching it there is picked up transparently.
This is still reuse-by-import + runtime `mock.patch` only; `v3_build_agent.py` is
untouched.
"""
from __future__ import annotations

import sys
import unittest.mock as _mock
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src"
for _p in (_REPO_ROOT, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from python_deps.depgraph.build_script import render_build_script  # noqa: E402
from python_deps.depgraph.schema import NodeType  # noqa: E402
from src.envstate.repair_scope import RepairScope  # noqa: E402
from src.envstate.repair_scope import render_repair_scope as _render_repair_scope  # noqa: E402
from src.envstate.v3_build_agent import V3BuildAgent  # noqa: E402
from src.eval.graph_repair_ablation.context import graph_context  # noqa: E402
from src.eval.graph_repair_ablation.grade import grade_localization  # noqa: E402
from src.eval.graph_repair_ablation.inject import apply_injection  # noqa: E402
from src.eval.graph_repair_ablation.oracle import Injection  # noqa: E402
from src.eval.language_package_eval.coverage import (  # noqa: E402
    _MountedContainer, _write_file, base_image_for_repo, build_graph_construction_only,
    first_failure_evidence,
)

ARMS: tuple[str, ...] = ("C0", "C1")


def arm_context(arm: str, graph) -> str:
    """The per-arm context treatment appended to the rendered repair scope.

    C0 (no-graph baseline) contributes nothing; C1 contributes the static
    `graph_context` block (typed/tiered nodes + chosen_fix + requires edges).
    Pilot C1 is a static rendered block, not the interactive query-tools C1
    (that's the scale-up refinement, out of scope here -- see the plan's
    Global Constraints)."""
    if arm not in ARMS:
        raise ValueError(f"unknown arm: {arm!r}")
    return graph_context(graph) if arm == "C1" else ""


class RecordingExec:
    """Adapts a `_MountedContainer`-shaped box (`.run(cmd) -> CommandResult`) to
    the plain `exec_readonly` callable contract `V3BuildAgent.propose` expects:
    `Callable[[str], tuple[int, str]]` -- NOT a `.run()`/`CommandResult`-typed
    object (confirmed at the call site, `v3_build_agent.py:205`:
    `rc, out = exec_readonly(action)`). Records every command the agent issues,
    in call order, so the trace/grader can see the diagnostic path taken."""

    def __init__(self, box) -> None:
        self.box = box
        self.calls: list[str] = []

    def __call__(self, command: str) -> tuple[int, str]:
        self.calls.append(command)
        r = self.box.run(command)
        return (r.returncode, (r.stdout or "") + (r.stderr or ""))


def _mentions(proposal, token: str) -> bool:
    """True if any additive field of `proposal` names `token` (case-insensitive
    substring match) -- the closest thing to "the agent tried to install X" the
    purely-additive `PatchProposal` schema can express (no remove/repin verb
    exists anywhere in `patch.py`; see recon §2)."""
    needle = token.lower()
    for p in proposal.add_providers:
        haystack = " ".join([p.id, p.command, *p.provides])
        if needle in haystack.lower():
            return True
    for r in proposal.add_requirements:
        haystack = " ".join([r.id, r.name or ""])
        if needle in haystack.lower():
            return True
    for sp in proposal.script_patches:
        haystack = " ".join(sp.commands)
        if needle in haystack.lower():
            return True
    return False


def normalize_patch(proposal, inj: Injection) -> dict | None:
    """Real `PatchProposal` (purely additive -- `add_requirements`/`add_providers`/
    `add_edges`/`script_patches`/`request_checks`; no native drop/repin verb) ->
    the `{"kind","target"}` shape `grade_localization` consumes.

    Injection-aware: this is the only way to express the OVERINCLUDE (drop)
    class given the additive schema.

    `proposal is None` -> `None` unconditionally (agent gave up within
    `max_diag_turns`; `V3BuildAgent.propose` never returns an empty non-None
    proposal). This is checked BEFORE any drop-class special-casing, so "gave
    up" is never conflated with "correctly recognized as droppable".

    PILOT DROP CONVENTION (new for this eval only -- not an upstream contract):
    for a drop-class injection (`inj.correct_action["kind"] == "drop"`), if the
    proposal's additive fields mention the phantom target anywhere (an
    `add_providers`/`add_requirements` entry naming it, or a `script_patches`
    command installing it) the agent tried to install the very thing it should
    have dropped -- reported as `{"kind": "install", "target": phantom}`, which
    `grade_localization`'s drop branch flags as a mislocalization. Otherwise --
    the (non-None) proposal touches nothing to do with the phantom -- reported
    as `{"kind": "drop", "target": phantom}`, i.e. the agent is presumed to
    have recognized the optional dependency as safely skippable. This is a
    real, documented risk (a proposal that ignores the phantom for unrelated
    reasons still reads as "drop"), accepted for the pilot per the plan.

    For install/repin classes: `add_providers[0]` -> `target = provides[0] or
    id`; else `add_requirements[0]` -> `target = name or id`; else `None`. The
    `kind` label is taken from the oracle's own `correct_action["kind"]`
    (there is no schema field distinguishing "install" from "repin" on a
    `PatchProposal`) -- `grade_localization`'s non-drop branch only requires a
    target-substring hit to score correct (`patch_correct = (p_kind == kind and
    hits) or hits`), so an exact kind label is not load-bearing there.
    """
    if proposal is None:
        return None

    correct_kind = inj.correct_action["kind"]
    target = inj.correct_action["target"]

    if correct_kind == "drop":
        if _mentions(proposal, target):
            return {"kind": "install", "target": target}
        return {"kind": "drop", "target": target}

    if proposal.add_providers:
        p = proposal.add_providers[0]
        return {"kind": correct_kind, "target": (p.provides[0] if p.provides else p.id)}
    if proposal.add_requirements:
        r = proposal.add_requirements[0]
        return {"kind": correct_kind, "target": (r.name or r.id)}
    return None


def _augmented_render(scope, arm: str, graph) -> str:
    """What gets monkeypatched in (as `src.envstate.repair_scope.render_repair_scope`
    -- see the module docstring's DEVIATION note for why that's the target and not
    `src.envstate.v3_build_agent.render_repair_scope`) for the duration of the
    `propose()` call. Renders the real scope, then appends the arm's context
    treatment -- never string-concatenated into `propose()`'s public API (it takes
    the `scope` object and renders it internally)."""
    base = _render_repair_scope(scope)
    ctx = arm_context(arm, graph)
    return base + ("\n\n" + ctx if ctx else "")


def _build_ablation_scope(graph, install_output: str) -> RepairScope:
    """Construct a GRAPH-FREE base `RepairScope` carrying the real failure text.

    Two experiment-validity reasons NOT to use `build_repair_scope` here:
    1. **Floor-effect fix.** `build_repair_scope` populates `failed_output` only
       from an `EvidenceBundle` (`bundle`), which this replay path has none of,
       so the agent's prompt would omit the error entirely and both arms would
       diagnose blind. Here we thread the container's actual failed command +
       stderr tail (via `first_failure_evidence`) straight into the scope.
    2. **Confound fix.** `build_repair_scope` also fills `slice_lines` with a
       graph requirement-slice, which `render_repair_scope` prints as "Graph
       context:" -- i.e. it would leak graph structure into the BASE scope that
       BOTH arms see, contaminating C0 (the no-graph baseline). We force
       `slice_lines=()`; the graph reaches the agent ONLY via C1's `arm_context`,
       so a C1-vs-C0 difference is attributable to the graph alone.

    `target_node_id` (the project node id) is a single minimal label identical
    across arms -- not graph structure -- so it is not a differential confound.
    """
    ff = first_failure_evidence(install_output)
    project = next((n for n in graph.nodes if n.type is NodeType.PROJECT), None)
    return RepairScope(
        target_node_id=(project.id if project else None),
        failed_command=ff["command"],
        failed_output=ff["stderr_tail"],
        slice_lines=(),
        known_invalid=(),
        constraints=(),
        known_evidence_ids=frozenset(),
    )


def run_one(inj: Injection, arm: str, *, agent_client, model: str, smoke_root,
            max_diag_turns: int = 4) -> dict:
    """Build the graph for `inj.repo`, inject its known failure into the
    rendered setup.sh, replay it in a fresh container, and -- if (as expected)
    the install fails -- run the real `V3BuildAgent` repair loop (augmented
    with the `arm` context) against it. Returns
    `{"injection_id","arm","failure_class","trace","score","install_failed"}`.

    `base_image_for_repo`, `build_graph_construction_only`, `render_build_script`,
    and `_MountedContainer` are module-level names specifically so unit tests
    can monkeypatch them (no Docker/LLM required to exercise the wiring)."""
    if arm not in ARMS:
        raise ValueError(f"unknown arm: {arm!r}")

    repo_dir = Path(smoke_root) / inj.repo
    image, minor, _reason = base_image_for_repo(str(repo_dir))
    graph = build_graph_construction_only(str(repo_dir), image, minor)
    script = render_build_script(graph, ())
    mutated = apply_injection(script, inj)

    with _MountedContainer(image, str(repo_dir)) as box:
        _write_file(box, "/setup.sh", mutated)
        install = box.run(f"cd {box.container_dir} && bash -x /setup.sh")
        if install.ok:
            # A corpus/injection bug (the mutation didn't survive construction) --
            # never silently counted as a data point. Log loudly and skip.
            print(
                f"WARNING: injection {inj.injection_id!r} did not cause an install "
                f"failure -- corpus/injection bug, skipping this cell",
                file=sys.stderr,
            )
            return {
                "injection_id": inj.injection_id, "arm": arm,
                "failure_class": inj.failure_class,
                "trace": None, "score": None, "install_failed": False,
            }

        # Graph-free base scope carrying the REAL failed command + stderr (so the
        # agent can diagnose) with EMPTY slice_lines (so the graph reaches the
        # agent only via C1's arm_context). See `_build_ablation_scope`.
        scope = _build_ablation_scope(graph, (install.stdout or "") + (install.stderr or ""))

        rec = RecordingExec(box)

        def _aug(s, _arm=arm, _graph=graph):
            return _augmented_render(s, _arm, _graph)

        with _mock.patch("src.envstate.repair_scope.render_repair_scope", _aug):
            proposal = V3BuildAgent(agent_client, model).propose(
                scope, rec, max_diag_turns=max_diag_turns)

    patch = normalize_patch(proposal, inj)
    trace = {"actions": rec.calls, "patch": patch}
    score = grade_localization(trace, inj)
    return {
        "injection_id": inj.injection_id, "arm": arm, "failure_class": inj.failure_class,
        "trace": trace, "score": score.__dict__, "install_failed": True,
    }


def aggregate(results: list[dict]) -> dict:
    """Group `run_one`-shaped result dicts by `(failure_class, arm)` -> mean
    `localized_at_1`, mean `localized_at_3`, mean `first_correct_rank` (over
    finite ranks only; `None` if every rank in the cell is `None`), mean
    `wasted_rate`, and a `mislocalized` count. This is the per-cell surface a
    C1-vs-C0 comparison reads off directly (`agg[(cls, "C1")]["localized_at_1"]
    - agg[(cls, "C0")]["localized_at_1"]`).

    Entries whose `score` is falsy (`run_one`'s `install_failed=False` guard
    returns `score=None` -- a corpus/injection bug already logged loudly at the
    source, never a real data point) are excluded from every cell, not counted
    as zero -- silently averaging them in would understate the effect for a
    reason that has nothing to do with either arm."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in results:
        score = r.get("score")
        if not score:
            continue
        groups.setdefault((r["failure_class"], r["arm"]), []).append(score)

    agg: dict[tuple[str, str], dict] = {}
    for key, scores in groups.items():
        n = len(scores)
        ranks = [s["first_correct_rank"] for s in scores if s.get("first_correct_rank") is not None]
        agg[key] = {
            "n": n,
            "localized_at_1": sum(1 for s in scores if s["localized_at_1"]) / n,
            "localized_at_3": sum(1 for s in scores if s["localized_at_3"]) / n,
            "first_correct_rank": (sum(ranks) / len(ranks)) if ranks else None,
            "wasted_rate": sum(s["wasted_rate"] for s in scores) / n,
            "mislocalized": sum(1 for s in scores if s["mislocalized"]),
        }
    return agg


def render_report_md(agg: dict) -> str:
    """Per-failure-class x per-arm markdown table -- the C1-vs-C0 comparison
    readable at a glance, one row per (class, arm) cell."""
    lines = ["# Graph-Repair-Ablation Report", ""]
    if not agg:
        lines.append("(no data)")
        return "\n".join(lines) + "\n"

    lines += [
        "| Failure Class | Arm | N | localized@1 | localized@3 | "
        "first_correct_rank | wasted_rate | mislocalized |",
        "|---|---|---|---|---|---|---|---|",
    ]
    classes = sorted({cls for cls, _arm in agg})
    for cls in classes:
        cell_arms = {a for c, a in agg if c == cls}
        ordered_arms = [a for a in ARMS if a in cell_arms] + sorted(cell_arms - set(ARMS))
        for arm in ordered_arms:
            cell = agg[(cls, arm)]
            rank = f"{cell['first_correct_rank']:.2f}" if cell["first_correct_rank"] is not None else "n/a"
            lines.append(
                f"| {cls} | {arm} | {cell['n']} | {cell['localized_at_1']:.0%} | "
                f"{cell['localized_at_3']:.0%} | {rank} | {cell['wasted_rate']:.2f} | "
                f"{cell['mislocalized']} |"
            )
    lines.append("")
    return "\n".join(lines)
