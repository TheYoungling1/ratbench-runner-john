# tests/bench/test_metrics_gold.py
import pytest

from bench.schema import MeasureRow
from bench.metrics import compute_metrics
from bench.gold import load_gold

# NOTE: gold-anchored scoring is currently disabled in compute_metrics (no gold JSON). The
# gold_coverage logic itself is still covered directly in test_gold.py; these two tests exercise
# the compute_metrics INTEGRATION and are skipped until the golden-set calc is re-enabled.
_GOLD_DISABLED = pytest.mark.skip(reason="golden-set calc disabled for now (no gold JSON)")


def _row(repo, sha, collected, passed, **kw):
    base = dict(agent="a", repo=repo, env_status="ok", build_ok=True, executed=True, ebsr=True,
                collected_node_ids=tuple(collected), passed_node_ids=tuple(passed),
                meta={"head_sha": sha})
    base.update(kw)
    return MeasureRow(**base)


_GOLD = load_gold({
    "repos": {
        "o/r1": {"full_name": "o/r1", "sha": "a" * 40, "status": "CERTIFIED", "manifest_size": 4,
                 "node_ids": ["t/a.py::a", "t/a.py::b", "t/a.py::c", "t/a.py::d"]},
        "o/r2": {"full_name": "o/r2", "sha": "b" * 40, "status": "CERTIFIED", "manifest_size": 2,
                 "node_ids": ["t/x.py::x", "t/x.py::y"]},
    }
})


@_GOLD_DISABLED
def test_gold_block_reports_C_and_P_over_fixed_denominator():
    rows = [
        # r1: collects all 4, passes 2 of 4  -> C=1.0, P=0.5
        _row("o/r1", "a" * 40,
             collected=["t/a.py::a", "t/a.py::b", "t/a.py::c", "t/a.py::d"],
             passed=["t/a.py::a", "t/a.py::b"]),
        # r2: collects+passes both -> C=1.0, P=1.0
        _row("o/r2", "b" * 40, collected=["t/x.py::x", "t/x.py::y"], passed=["t/x.py::x", "t/x.py::y"]),
    ]
    m = compute_metrics(rows, gold=_GOLD)
    assert m["n_gold_included"] == 2 and m["n_gold_excluded"] == 0
    assert m["EBSR_improved"] == round((1.0 + 1.0) / 2, 4)
    assert m["ESSR_improved"] == round((0.5 + 1.0) / 2, 4)


@_GOLD_DISABLED
def test_gold_block_excludes_sha_misaligned_with_reason():
    rows = [_row("o/r1", "deadbeef" + "0" * 32, collected=["t/a.py::a"], passed=["t/a.py::a"])]
    m = compute_metrics(rows, gold=_GOLD)
    assert m["n_gold_included"] == 0 and m["n_gold_excluded"] == 1
    assert m["gold_excluded"][0]["reason"] == "sha_misaligned"


def test_no_gold_arg_omits_gold_keys():
    m = compute_metrics([_row("o/r1", "a" * 40, collected=[], passed=[])])
    assert "ESSR_improved" not in m and "EBSR_improved" not in m
