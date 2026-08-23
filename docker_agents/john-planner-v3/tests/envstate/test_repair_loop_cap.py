import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for p in (str(_ROOT), str(_ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from src.envstate.repair_loop import run_structured_repair, RepairOutcome


class _Res:
    accepted = True
    errors = ()
    def __init__(self, graph): self.graph = graph; self.manual_blocks = ()


def _fixture(monkeypatch):
    import src.envstate.repair_loop as rl
    # admit always accepts; compose_script / compose_replay_script returns one block matching failed id.
    class _Block:
        def __init__(self, bid): self.block_id = bid; self.target_node_ids = (bid,)
    # The loop calls compose_script at the committed base but compose_replay_script in the
    # current working-tree WIP; mock both so the test is correct in either state.
    monkeypatch.setattr(rl, "compose_script", lambda g, mb: (_Block("nodeX"),), raising=False)
    monkeypatch.setattr(rl, "compose_replay_script", lambda g, mb: (_Block("nodeX"),), raising=False)
    monkeypatch.setattr(rl, "admit_proposal", lambda g, p, **k: _Res(g))
    class _Scope:
        failed_command = "cmd"; known_evidence_ids = frozenset()
    # Return the scope_builder explicitly — the default param is pre-bound at import time,
    # so monkeypatching rl.build_repair_scope does not intercept default-arg calls.
    return lambda *a, **k: _Scope()


def test_cap_true_stops_on_pivot_returns_original(monkeypatch):
    sb = _fixture(monkeypatch)
    out = run_structured_repair(
        object(), "nodeX", None, 1,
        propose=lambda s, **k: {"p": 1},
        emit=lambda g, mb: (g, None, "nodeY"),   # pivots to a different node
        cap_failed_id=True, max_repairs=3,
        scope_builder=sb)
    assert out.still_failing_id == "nodeX"       # capped to original, not nodeY


def test_cap_false_allows_pivot(monkeypatch):
    sb = _fixture(monkeypatch)
    out = run_structured_repair(
        object(), "nodeX", None, 1,
        propose=lambda s, **k: {"p": 1},
        emit=lambda g, mb: (g, None, "nodeY"),
        cap_failed_id=False, max_repairs=1,
        scope_builder=sb)
    # default behavior: it chases nodeY (loops/continues), not the original
    assert out.still_failing_id in ("nodeY", None)


def test_cap_true_success_when_emit_clears(monkeypatch):
    sb = _fixture(monkeypatch)
    out = run_structured_repair(
        object(), "nodeX", None, 1,
        propose=lambda s, **k: {"p": 1},
        emit=lambda g, mb: (g, None, None),      # fixed
        cap_failed_id=True, max_repairs=3,
        scope_builder=sb)
    assert out.still_failing_id is None
