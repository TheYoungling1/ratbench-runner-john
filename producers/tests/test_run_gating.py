# producers/tests/test_run_gating.py — the runtime measure-lane gate + live-score fallback (FIX 4).
#
# Two helper-level surfaces from harness_cli.run, proven WITHOUT mocking run.main() (provision /
# symlink_glue / subprocess would make that brittle):
#   * _is_native_lane(model, declared_measure) — derives "no rebuildable artifact" from the
#     EFFECTIVE model's producer.measurable (design §4), falling back to the declared `measure`
#     tag only when the model is unregistered.
#   * _write_live_scores(out, spec, model) — always lands live_scores.json as a marker (score=None
#     on any capture failure), recording the effective `model`.
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))          # producers/tests -> producers -> repo root
_HARNESS_DIR = os.path.join(_REPO_ROOT, "harness")

# harness_cli lives under <repo>/harness; put it on the path (mirrors test_measure_config.py).
if _HARNESS_DIR not in sys.path:
    sys.path.insert(0, _HARNESS_DIR)
from harness_cli import run  # noqa: E402


class _Spec:
    """Minimal stand-in: _write_live_scores only reads spec.name."""
    def __init__(self, name="v"):
        self.name = name


@pytest.fixture(autouse=True)
def _isolate_compute_essr():
    """Fully isolate the compute_essr import between tests: pop any cached module AND restore
    sys.path, so a scripts/ dir inserted by the success test can never let the failure test find a
    stale compute_essr (cross-contamination via sys.path, not just sys.modules)."""
    saved_path = list(sys.path)
    sys.modules.pop("compute_essr", None)
    try:
        yield
    finally:
        sys.modules.pop("compute_essr", None)
        sys.path[:] = saved_path


# ── _is_native_lane: producer.measurable is the source of truth (FIX 1) ───────────────────────
def test_measurable_false_wins_over_declared_conforming():
    # The exact FIX-1 hole: `bench radical --model rat` — rat's producer is measurable=False, so it
    # is native-lane even though the *variety's* declared measure is "conforming".
    assert run._is_native_lane("rat", "conforming") is True


def test_measurable_true_forces_harvest_even_if_mistagged_none():
    # A measurable producer must be harvested even if a variety mistags it measure="none".
    assert run._is_native_lane("dockeragent", "none") is False


def test_unregistered_model_falls_back_to_declared_tag():
    assert run._is_native_lane("does-not-exist", "none") is True
    assert run._is_native_lane("does-not-exist", "conforming") is False


# ── _write_live_scores: always writes the marker; records the effective model ──────────────────
def test_live_scores_written_with_null_score_on_capture_failure(tmp_path, monkeypatch):
    # HARNESS_ROOT points at a dir with NO scripts/compute_essr.py -> the import fails -> the marker
    # is still written with score=None and the effective model recorded.
    out = tmp_path / "run"
    out.mkdir()
    monkeypatch.setattr(run, "HARNESS_ROOT", str(tmp_path / "no_scripts_here"))
    run._write_live_scores(str(out), _Spec("rat"), "rat")

    payload = json.load(open(out / "live_scores.json"))
    assert payload["score"] is None
    assert payload["method"] == "rat"
    assert payload["measure"] == "none" and payload["source"] == "inline"


def test_live_scores_captures_native_inline_score(tmp_path, monkeypatch):
    # HARNESS_ROOT/scripts/compute_essr.py defines score_agent -> its keys flow into `score`.
    harness = tmp_path / "harness_root"
    scripts = harness / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "compute_essr.py").write_text(
        "def score_agent(p):\n"
        "    return {'n': 3, 'n_exec': 2, 'coverage': 0.6667, 'n_ebsr': 2,\n"
        "            'EBSR_build_execute': 0.66, 'n_agent_goal': 1, 'agent_goal_rate': 0.33,\n"
        "            'ESSR_avg_pass_rate_official': 0.9, 'pass_rate_over_all': 0.6,\n"
        "            'n_collect_success': 2, 'collect_success_all': 0.66}\n"
    )
    out = tmp_path / "run"
    out.mkdir()
    monkeypatch.setattr(run, "HARNESS_ROOT", str(harness))
    run._write_live_scores(str(out), _Spec("sweagent"), "sweagent")

    payload = json.load(open(out / "live_scores.json"))
    assert payload["method"] == "sweagent"
    assert payload["score"] is not None
    assert payload["score"]["n"] == 3 and payload["score"]["n_exec"] == 2
    assert payload["score"]["EBSR_build_execute"] == 0.66
    assert payload["score"]["ESSR_avg_pass_rate_official"] == 0.9
    # every key _write_live_scores reads is present
    for k in ("n", "n_exec", "coverage", "n_ebsr", "EBSR_build_execute", "n_agent_goal",
              "agent_goal_rate", "ESSR_avg_pass_rate_official", "pass_rate_over_all",
              "n_collect_success", "collect_success_all"):
        assert k in payload["score"]
