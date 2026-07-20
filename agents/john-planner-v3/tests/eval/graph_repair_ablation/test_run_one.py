"""Task 5 tests: run_one agent wiring, mock-testable (NO Docker, NO live LLM).

Monkeypatches the module-level `base_image_for_repo`, `build_graph_construction_only`,
`render_build_script`, and `_MountedContainer` names on `run`; passes a fake
`agent_client` whose `.chat.completions.create(...)` returns a scripted response
(mirrors `tests/envstate/test_v3_build_agent_propose.py`'s `_Client` fixture) and a
`_FakeBox` (mirrors `tests/eval/build_script_eval/test_replay_ladder.py`'s `_FakeBox`).
"""
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src"
for _p in (_REPO_ROOT, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from python_deps.depgraph.executor import CommandResult  # noqa: E402
from python_deps.depgraph.patch import PatchProposal, ProviderSpec  # noqa: E402
from python_deps.depgraph.schema import DepGraph, DiscoveredBy, Edge, Layer, Node, NodeType  # noqa: E402
from src.eval.graph_repair_ablation import run  # noqa: E402
from src.eval.graph_repair_ablation.oracle import PILOT_INJECTIONS  # noqa: E402

BY = {i.failure_class: i for i in PILOT_INJECTIONS}


# --------------------------------------------------------------------------
# Fixtures: a tiny real DepGraph (same shape as test_context.py's `_graph`),
# a fake Docker-free box, and a fake OpenAI-compatible client that records
# every `messages` list it was called with (so we can inspect the rendered
# scope text the agent actually saw -- incl. the arm's context block).
# --------------------------------------------------------------------------

def _graph():
    proj = Node("project:pygraphviz", NodeType.PROJECT, "pygraphviz", Layer.PIP, DiscoveredBy.GOAL)
    pkg = Node("pkg:requests==2.0", NodeType.PACKAGE, "requests", Layer.PIP,
               DiscoveredBy.RESOLVER, version="2.0")
    tool = Node("tool:libgraphviz-dev", NodeType.TOOL, "libgraphviz-dev", Layer.TOOLCHAIN,
                DiscoveredBy.RESOLVER, chosen_fix="apt:libgraphviz-dev")
    g = DepGraph((proj, pkg, tool))
    g = g.with_edge(Edge("project:pygraphviz", "pkg:requests==2.0"))
    g = g.with_edge(Edge("project:pygraphviz", "tool:libgraphviz-dev"))
    return g


class _FakeBox:
    """Stands in for `_MountedContainer` -- no Docker. `.run` returns rc1 for the
    `bash -x /setup.sh` install command (so `run_one` proceeds into repair) and
    rc0 for everything else (the agent's read-only diagnostics). Mirrors the
    `_FakeBox` pattern in tests/eval/build_script_eval/test_replay_ladder.py."""

    def __init__(self):
        self.container_dir = "/workspace/repo"
        self.name = "fake-ablation-probe"
        self.commands: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, command: str, *, timeout: int = 300) -> CommandResult:
        self.commands.append(command)
        if "bash -x /setup.sh" in command:
            return CommandResult(command=command, returncode=1, stdout="",
                                  stderr="ERROR: could not locate package libgraphviz-dev")
        return CommandResult(command=command, returncode=0, stdout="ok", stderr="")


class _AlwaysGreenBox(_FakeBox):
    """Install never fails -- simulates a corpus/injection bug."""

    def run(self, command: str, *, timeout: int = 300) -> CommandResult:
        self.commands.append(command)
        return CommandResult(command=command, returncode=0, stdout="ok", stderr="")


class _Msg:
    def __init__(self, c):
        self.content = c
        self.reasoning = None
        self.model_extra = {}


class _Choice:
    def __init__(self, c):
        self.message = _Msg(c)


class _Resp:
    def __init__(self, c):
        self.choices = [_Choice(c)]
        self.usage = None


class _RecordingClient:
    """Fake OpenAI-compatible client: pops scripted turn contents in order, and
    records every `messages` list `create()` was called with -- so tests can
    inspect the exact scope text (incl. arm-augmented context) the agent saw."""

    def __init__(self, *contents):
        self._queue = list(contents)
        self.calls: list[list[dict]] = []
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, **kw):
        self.calls.append(kw["messages"])
        return _Resp(self._queue.pop(0))


_DIAG_TURN = "Action: apt-cache search libgraphviz-dev"
_FINAL_PATCH = '''Final Patch:
```json
{"patch": {"add_providers": [{"id": "apt:libgraphviz-dev", "kind": "apt",
 "command": "apt-get install -y libgraphviz-dev"}]}}
```'''


def _patch_construction(monkeypatch, box):
    # Includes the "libgraphviz-dev" apt line so the SYSLIB_MISSING injection's
    # `strip_line` mutation has something to strip (apply_injection raises
    # ValueError, per Task 2, when its match is absent from the script).
    fake_script = "apt-get install -y libgraphviz-dev\npip install -e .\n"
    monkeypatch.setattr(run, "base_image_for_repo",
                         lambda repo_dir: ("python:3.11-slim", "3.11", "pinned"))
    monkeypatch.setattr(run, "build_graph_construction_only",
                         lambda repo_dir, image, minor: _graph())
    monkeypatch.setattr(run, "render_build_script", lambda graph, blocks: fake_script)
    monkeypatch.setattr(run, "_MountedContainer", lambda image, host_dir: box)
    monkeypatch.setattr(run, "_write_file", lambda box_, path, content: None)


# --------------------------------------------------------------------------
# (a) RecordingExec captures the ordered commands (pure, direct).
# --------------------------------------------------------------------------

def test_recording_exec_captures_ordered_commands():
    box = _FakeBox()
    rec = run.RecordingExec(box)
    rc1, out1 = rec("apt-cache search libgraphviz-dev")
    rc2, out2 = rec("ls /usr/lib")
    assert rec.calls == ["apt-cache search libgraphviz-dev", "ls /usr/lib"]
    assert rc1 == 0 and "ok" in out1
    assert rc2 == 0 and "ok" in out2


# --------------------------------------------------------------------------
# (b) normalize_patch: expected {kind,target} for an install patch, and the
# drop-convention (both branches) for a drop-class injection.
# --------------------------------------------------------------------------

def test_normalize_patch_none_proposal_is_always_none():
    assert run.normalize_patch(None, BY["OVERINCLUDE"]) is None
    assert run.normalize_patch(None, BY["SYSLIB_MISSING"]) is None


def test_normalize_patch_install_class_from_add_providers():
    inj = BY["SYSLIB_MISSING"]  # correct target apt:libgraphviz-dev
    proposal = PatchProposal(add_providers=(ProviderSpec(
        id="apt:libgraphviz-dev", kind="apt", command="apt-get install -y libgraphviz-dev"),))
    assert run.normalize_patch(proposal, inj) == {"kind": "install", "target": "apt:libgraphviz-dev"}


def test_normalize_patch_drop_class_mislocalization_when_proposal_installs_phantom():
    inj = BY["OVERINCLUDE"]  # correct action: drop the optional pkg
    proposal = PatchProposal(add_providers=(ProviderSpec(
        id="pip:this-optional-pkg-fails-to-build", kind="pip",
        command="pip install this-optional-pkg-fails-to-build==0.0.0"),))
    assert run.normalize_patch(proposal, inj) == {
        "kind": "install", "target": "this-optional-pkg-fails-to-build"}


def test_normalize_patch_drop_class_correct_when_proposal_avoids_phantom():
    inj = BY["OVERINCLUDE"]
    proposal = PatchProposal(request_checks=("pip show python-dotenv",))
    assert run.normalize_patch(proposal, inj) == {
        "kind": "drop", "target": "this-optional-pkg-fails-to-build"}


# --------------------------------------------------------------------------
# (c) arm_context / the augmented render: C1 includes the graph_context
# block, C0 contributes nothing.
# --------------------------------------------------------------------------

def test_arm_context_c1_has_graph_block_c0_is_empty():
    g = _graph()
    assert run.arm_context("C0", g) == ""
    s = run.arm_context("C1", g)
    assert "Dependency graph (typed, tiered):" in s
    assert "libgraphviz-dev" in s


def test_arm_context_unknown_arm_raises():
    with pytest.raises(ValueError):
        run.arm_context("C2", _graph())


# --------------------------------------------------------------------------
# (c') the base repair scope is GRAPH-FREE but carries the real failure text:
# fixes the floor-effect (agent must see the error) AND the slice_lines
# confound (no graph leaks into the C0 baseline). See `_build_ablation_scope`.
# --------------------------------------------------------------------------

def test_build_ablation_scope_is_graph_free_but_carries_error_text():
    g = _graph()
    out = ("+ apt-get install -y libgraphviz-dev\n"
           "E: Unable to locate package libgraphviz-dev\n")
    scope = run._build_ablation_scope(g, out)
    assert scope.slice_lines == ()                        # GRAPH-FREE base (no leak to C0)
    assert "libgraphviz-dev" in scope.failed_output       # agent sees the error (no floor-effect)
    assert scope.target_node_id == "project:pygraphviz"   # minimal label, same both arms
    assert scope.failed_command                            # a failing command was extracted

    # render must NOT contain a graph slice for the base scope (the only graph
    # the agent may see is C1's appended arm_context).
    rendered = run._render_repair_scope(scope)
    assert "Graph context:" not in rendered
    assert "Failure output:" in rendered


# --------------------------------------------------------------------------
# Full run_one integration (mocked): install fails -> repair loop runs ->
# trace/patch/score assembled. Exercised once per arm to also nail down (c)
# end-to-end: the rendered scope text the fake LLM actually received differs
# only by the graph_context block.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("arm,expect_graph_block", [("C1", True), ("C0", False)])
def test_run_one_wires_agent_and_augments_render_per_arm(monkeypatch, arm, expect_graph_block):
    box = _FakeBox()
    _patch_construction(monkeypatch, box)
    client = _RecordingClient(_DIAG_TURN, _FINAL_PATCH)

    inj = BY["SYSLIB_MISSING"]
    result = run.run_one(inj, arm, agent_client=client, model="fake-model",
                          smoke_root="/fake/smoke")

    assert result["install_failed"] is True
    assert result["injection_id"] == inj.injection_id
    assert result["arm"] == arm
    assert result["failure_class"] == "SYSLIB_MISSING"

    # (a) ordered commands captured
    assert result["trace"]["actions"] == ["apt-cache search libgraphviz-dev"]

    # (b) normalized patch
    assert result["trace"]["patch"] == {"kind": "install", "target": "apt:libgraphviz-dev"}

    # grading follows from (a)+(b): the action itself names the target -> rank 1
    assert result["score"]["localized_at_1"] is True
    assert result["score"]["mislocalized"] is False

    # (c) the FIRST create() call's user message is `_aug(scope)`'s output --
    # i.e. exactly what changes (or doesn't) between arms.
    first_call_messages = client.calls[0]
    user_content = first_call_messages[1]["content"]
    assert (first_call_messages[0]["role"], first_call_messages[1]["role"]) == ("system", "user")
    if expect_graph_block:
        assert "Dependency graph (typed, tiered):" in user_content
        assert "libgraphviz-dev" in user_content
    else:
        assert "Dependency graph (typed, tiered):" not in user_content

    # both arms: the agent MUST see the real failure output (fixes floor-effect;
    # previously build_repair_scope left failed_output empty on this path).
    assert "could not locate package libgraphviz-dev" in user_content


def test_run_one_skips_when_injection_does_not_fail_install(monkeypatch):
    box = _AlwaysGreenBox()
    _patch_construction(monkeypatch, box)
    client = _RecordingClient()  # must never be called

    # OVERINCLUDE's mutation is `add_install_pkg` (append-only -- never raises
    # regardless of script content), so this exercises the skip path itself
    # rather than an unrelated apply_injection ValueError.
    inj = BY["OVERINCLUDE"]
    result = run.run_one(inj, "C0", agent_client=client, model="fake-model",
                          smoke_root="/fake/smoke")

    assert result["install_failed"] is False
    assert result["trace"] is None
    assert result["score"] is None
    assert client.calls == []
