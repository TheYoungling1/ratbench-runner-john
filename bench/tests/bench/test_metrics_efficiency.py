# tests/bench/test_metrics_efficiency.py
from bench.schema import MeasureRow
from bench.metrics import compute_metrics


def _row(**kw):
    base = dict(agent="a", repo="r", env_status="ok", build_ok=True, executed=True)
    base.update(kw)
    return MeasureRow(**base)


def test_efficiency_means_skip_none():
    rows = [
        _row(repo="r1", ebsr=True, pass_rate=1.0, image_delta_mb=100.0,
             tokens_in=10, tokens_out=90, turns_used=5, produce_s=30.0, build_s=20.0, test_s=10.0),
        _row(repo="r2", ebsr=True, pass_rate=0.9, image_delta_mb=None, tokens_in=None, tokens_out=None),
    ]
    m = compute_metrics(rows)
    assert m["mean_image_delta_mb"] == 100.0
    assert m["n_token_reporting"] == 1 and m["mean_tokens"] == 100.0 and m["mean_tokens_out"] == 90.0


def test_tokens_per_success_use_success_denominators():
    # tokens_per_ebsr denominator is now EBSR-success = collect_clean (collect-only exit 0)
    rows = [_row(repo="r1", ebsr=True, collect_clean=True, pass_rate=1.0, tokens_in=50, tokens_out=150),
            _row(repo="r2", ebsr=True, collect_clean=True, pass_rate=0.5, tokens_in=100, tokens_out=100)]
    m = compute_metrics(rows)
    assert m["tokens_per_ebsr"] == round(400 / 2, 4)
    assert m["tokens_per_real_success"] == round(400 / 1, 4)


def test_rebuild_and_unreplayed_rates():
    rows = [_row(repo="r1", build_ok=True, meta={}), _row(repo="r2", build_ok=True, meta={"unreplayed": True}),
            _row(repo="r3", build_ok=False, meta={})]
    m = compute_metrics(rows)
    assert m["rebuild_ok_rate"] == round(2 / 3, 4) and m["unreplayed_rate"] == round(1 / 3, 4)


def test_no_token_reporting_gives_none_not_zero():
    m = compute_metrics([_row(repo="r1", ebsr=True, pass_rate=1.0)])
    assert m["mean_tokens"] is None and m["tokens_per_real_success"] is None
