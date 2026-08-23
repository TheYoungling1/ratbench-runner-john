import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from python_deps.depgraph.patch import PatchProposal, ProviderSpec
from src.envstate.repair_loop import run_structured_repair, RepairOutcome
from src.envstate.repair_scope import RepairScope

def _scope_builder(graph, *, target_node_id, failed_block, bundle, known_invalid, constraints):
    # deterministic fake scope; carries the avoid-list so the memory test can observe it
    return RepairScope(target_node_id, "apt-get install -y libplacebodev", "not found",
                       (), tuple(known_invalid), (), frozenset({"ev.1.0"}))

_GOOD = PatchProposal(add_providers=(ProviderSpec(
    id="apt:libplacebo-dev", kind="apt", command="apt-get install -y libplacebo-dev",
    provides=("syslib:libplacebo",), override=True),))

class _Graph:  # minimal stand-in: admit_proposal is monkeypatched in these tests
    pass

def test_recovers_when_emit_passes_after_patch(monkeypatch):
    import src.envstate.repair_loop as rl
    monkeypatch.setattr(rl, "admit_proposal", lambda g, p, **k:
        type("R", (), {"accepted": True, "errors": (), "graph": g, "manual_blocks": ()})())
    monkeypatch.setattr(rl, "compose_script", lambda g, mb: ())
    emitted = {"n": 0}
    def emit(g, mb):
        emitted["n"] += 1
        return g, object(), (None if emitted["n"] >= 1 else "system.libplacebo")
    out = run_structured_repair(_Graph(), "system.libplacebo", object(), 1,
                                propose=lambda s, **k: _GOOD, emit=emit,
                                scope_builder=_scope_builder, max_repairs=5, repair_budget=10)
    assert out.still_failing_id is None and out.turns_spent == 1

def test_budget_exhaustion(monkeypatch):
    import src.envstate.repair_loop as rl
    monkeypatch.setattr(rl, "compose_script", lambda g, mb: ())
    out = run_structured_repair(_Graph(), "system.x", object(), 1,
                                propose=lambda s, **k: None, emit=lambda g, mb: (g, object(), "system.x"),
                                scope_builder=_scope_builder, max_repairs=5, repair_budget=0)
    assert out.budget_exhausted is True and out.still_failing_id == "system.x"

def test_known_invalid_grows_and_convergence_guard(monkeypatch):
    import src.envstate.repair_loop as rl
    monkeypatch.setattr(rl, "admit_proposal", lambda g, p, **k:
        type("R", (), {"accepted": True, "errors": (), "graph": g, "manual_blocks": ()})())
    monkeypatch.setattr(rl, "compose_script", lambda g, mb: ())
    out = run_structured_repair(_Graph(), "system.x", object(), 1,
        propose=lambda s, **k: _GOOD,
        emit=lambda g, mb: (g, object(), "system.x"),   # same command keeps failing
        scope_builder=_scope_builder, max_repairs=5, repair_budget=10)
    # convergence guard stops re-attempting the identical failing command (does not burn all 5)
    assert out.still_failing_id == "system.x"
    assert "apt-get install -y libplacebodev" in out.known_invalid
    assert out.turns_spent <= 2

def test_gate_reject_then_reprompt_then_skip(monkeypatch):
    import src.envstate.repair_loop as rl
    calls = {"n": 0}
    def admit(g, p, **k):
        calls["n"] += 1
        return type("R", (), {"accepted": False, "errors": ("bad id",),
                              "graph": g, "manual_blocks": ()})()
    monkeypatch.setattr(rl, "admit_proposal", admit)
    monkeypatch.setattr(rl, "compose_script", lambda g, mb: ())
    seen = []
    def propose(s, **k): seen.append(k.get("rejection_errors")); return _GOOD
    out = run_structured_repair(_Graph(), "system.x", object(), 1, propose=propose,
        emit=lambda g, mb: (g, object(), "system.x"), scope_builder=_scope_builder,
        max_repairs=5, repair_budget=10)
    assert calls["n"] == 2                      # admit called twice (initial + re-prompt)
    assert seen[1] == ("bad id",)               # second propose got the gate errors
    assert out.still_failing_id == "system.x"
