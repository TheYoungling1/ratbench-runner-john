"""Characterization of the pure scorer (score.score_records) on the seed fixture.

score_records consumes InstallRecord instances, not raw dicts, so the seed JSON
is mapped through record_from_dict first (a bare dict has no `.mode`).
"""
import json
import pathlib

from src.eval.package_installability.score import record_from_dict, score_records

_SEED = pathlib.Path("src/eval/package_installability/seed_records.json")


def test_score_records_on_seed_produces_metric():
    records = [record_from_dict(r) for r in json.loads(_SEED.read_text())]
    metric = score_records(records)
    # headline is a rate in [0,1]; diagnostics present
    assert 0.0 <= metric.installable_rate <= 1.0
    assert metric.by_mode and metric.by_stratum
    # seed has 5 records, 2 pass / 2 fail / 1 error -> installable_rate == 0.5
    assert metric.n_rows == 5
    assert metric.installable_rate == 0.5
