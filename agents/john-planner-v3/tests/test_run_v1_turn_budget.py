# tests/test_run_v1_turn_budget.py — source-level guard
from pathlib import Path
_SRC = Path(__file__).resolve().parents[1] / "src" / "envstate" / "orchestrator.py"


def test_turn_budget_present_and_not_in_deterministic_drain():
    src = _SRC.read_text()
    assert "_repair_turns" in src
    assert "_budget_exhausted" in src
    assert "LLM turn budget exhausted" in src
    # Phase 4 (fresh-replay-only run_v3): emit_drain no longer runs inside
    # run_v3 at all — the deterministic-drain-vs-turn-budget question this
    # test used to answer for run_v3 is moot there (there is no drain to
    # check). emit_drain survives only in run_v1 (and the future ablation
    # entry point); pin that split explicitly — INVERTED from the pre-Phase-4
    # assertion, which required emit_drain present in run_v3.
    run_v1_start = src.index("def run_v1(")
    run_v3_start = src.index("def run_v3(")
    run_v1_body = src[run_v1_start:run_v3_start]
    run_v3_body = src[run_v3_start:]
    # "emit_drain(" (a call, not just a docstring/comment mention) pins that
    # run_v1 still calls it while run_v3 no longer does.
    assert "emit_drain(" in run_v1_body, "emit_drain must still run in run_v1 (baseline)"
    assert "emit_drain(" not in run_v3_body, (
        "emit_drain must be ABSENT from run_v3 (Phase 4: fresh-replay is the sole executor)"
    )
