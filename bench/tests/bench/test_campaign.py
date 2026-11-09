# tests/bench/test_campaign.py
#
# Campaign-level scoring: point at ONE agent's run root (runs/<arm>/) and get both datasets
# scored, without hand-picking run directories.
#
# The hand-picking is what this replaces. tools/essr_gold.py hardcodes arm -> run-dir lists that
# go stale, and every arm's root holds the real runs alongside one-repo smokes, renamed duplicates
# and pooled merges. Selection is driven by each run's own _run_manifest.json, and every candidate
# is reported so nothing is chosen silently.
import json
import os

import pytest

from bench.campaign import (DATASET_GROUPS, GROUP_DATASET, GROUP_GOLD_CORPUS, GROUPS,
                            discover_runs, select_runs)


def _run(tmp_path, name, dataset, n_rows, agent="a"):
    d = tmp_path / name
    (d / "measure").mkdir(parents=True)
    (d / "_run_manifest.json").write_text(json.dumps(
        {"variety": agent, "dataset": {"path": f"datasets/{dataset}", "sha256": "x" * 8}}))
    for i in range(n_rows):
        rd = d / "measure" / agent / "o" / f"r{i}"
        rd.mkdir(parents=True)
        (rd / "row.json").write_text(json.dumps({"repo": f"o/r{i}", "status": "executed"}))
    return d


# ── discovery ─────────────────────────────────────────────────────────────────────────────

def test_discover_reads_dataset_and_row_count_from_each_run(tmp_path):
    _run(tmp_path, "run-A", "rat_python50.json", 50)
    _run(tmp_path, "run-B", "envbench_python100.json", 100)
    found = {r.name: r for r in discover_runs(str(tmp_path))}
    assert set(found) == {"run-A", "run-B"}
    assert found["run-A"].dataset == "rat_python50.json" and found["run-A"].n_rows == 50
    assert found["run-B"].group == "envbench100"


def test_discover_skips_a_dir_with_no_manifest_and_no_rows(tmp_path):
    (tmp_path / "junk").mkdir()
    _run(tmp_path, "real", "rat_python50.json", 50)
    assert [r.name for r in discover_runs(str(tmp_path))] == ["real"]


def test_discover_tolerates_a_corrupt_manifest_rather_than_crashing(tmp_path):
    d = _run(tmp_path, "bad", "rat_python50.json", 3)
    (d / "_run_manifest.json").write_text("{not json")
    runs = {r.name: r for r in discover_runs(str(tmp_path))}
    assert runs["bad"].dataset is None and runs["bad"].group is None
    assert runs["bad"].n_rows == 3            # rows still counted; only the label is unknown


def test_every_known_dataset_maps_to_a_group():
    assert DATASET_GROUPS["rat_python50.json"] == ("rat50", "direct")
    assert DATASET_GROUPS["envbench_python100.json"] == ("envbench100", "direct")
    assert DATASET_GROUPS["envbench_python70.json"] == ("envbench100", "part")
    assert DATASET_GROUPS["envbench_python100_minus70.json"] == ("envbench100", "part")
    assert DATASET_GROUPS["rat_python50_new.json"] == ("rat50new", "direct")


def test_every_group_has_a_gold_corpus_and_a_canonical_dataset():
    # A group missing either entry KeyErrors deep inside score_agent, after the rows are loaded.
    # rat50new was the case that proved it: the run dir was discovered, then silently ignored.
    for g in GROUPS:
        assert g in GROUP_GOLD_CORPUS, f"{g} has no gold corpus"
        assert g in GROUP_DATASET, f"{g} has no canonical dataset"


def test_rat50new_is_scored_not_ignored(tmp_path):
    # Before the wiring, a rat_python50_new run landed in `ignored` and never reached the pool,
    # so the 200-repo figure silently stayed a 150-repo one.
    _run(tmp_path, "rat50new-run", "rat_python50_new.json", 50)
    sel = select_runs(discover_runs(str(tmp_path)))
    assert sel["ignored"] == []
    assert [os.path.basename(p) for p in sel["rat50new"].paths] == ["rat50new-run"]


def test_the_three_datasets_are_separate_groups(tmp_path):
    # rat50 and rat50new are disjoint corpora, not two runs of the same one: pooling them into a
    # single group would halve the repo count and pick a winner between them.
    _run(tmp_path, "old", "rat_python50.json", 50)
    _run(tmp_path, "new", "rat_python50_new.json", 50)
    sel = select_runs(discover_runs(str(tmp_path)))
    assert [os.path.basename(p) for p in sel["rat50"].paths] == ["old"]
    assert [os.path.basename(p) for p in sel["rat50new"].paths] == ["new"]
    assert sel["rat50"].rejected == [] and sel["rat50new"].rejected == []


# ── selection ─────────────────────────────────────────────────────────────────────────────

def test_the_biggest_run_wins_so_smokes_never_do(tmp_path):
    _run(tmp_path, "smoke-1", "rat_python50.json", 1)
    _run(tmp_path, "real-50", "rat_python50.json", 50)
    _run(tmp_path, "smoke-2", "rat_python50.json", 1)
    sel = select_runs(discover_runs(str(tmp_path)))
    assert [os.path.basename(p) for p in sel["rat50"].paths] == ["real-50"]


def test_a_direct_envbench100_run_beats_pooling_the_two_parts(tmp_path):
    _run(tmp_path, "direct-100", "envbench_python100.json", 100)
    _run(tmp_path, "part-70", "envbench_python70.json", 70)
    _run(tmp_path, "part-39", "envbench_python100_minus70.json", 39)
    sel = select_runs(discover_runs(str(tmp_path)))
    assert [os.path.basename(p) for p in sel["envbench100"].paths] == ["direct-100"]
    assert sel["envbench100"].pooled is False


def test_the_two_parts_are_pooled_when_there_is_no_direct_run(tmp_path):
    # envbench70 and minus70 are disjoint and together cover the canonical 100.
    _run(tmp_path, "part-70", "envbench_python70.json", 70)
    _run(tmp_path, "part-39", "envbench_python100_minus70.json", 39)
    sel = select_runs(discover_runs(str(tmp_path)))
    assert sorted(os.path.basename(p) for p in sel["envbench100"].paths) == ["part-39", "part-70"]
    assert sel["envbench100"].pooled is True


def test_only_one_part_present_still_scores_but_is_flagged_incomplete(tmp_path):
    # Half a pool is not envbench100. Score it, but never let it pass as the full set.
    _run(tmp_path, "part-70", "envbench_python70.json", 70)
    sel = select_runs(discover_runs(str(tmp_path)))
    assert sel["envbench100"].pooled is True
    assert sel["envbench100"].incomplete is True


def test_unrecognised_datasets_are_reported_not_silently_dropped(tmp_path):
    _run(tmp_path, "real-50", "rat_python50.json", 50)
    _run(tmp_path, "odd", "three_light.json", 3)
    sel = select_runs(discover_runs(str(tmp_path)))
    assert [os.path.basename(p) for p, _ in sel["ignored"]] == ["odd"]


def test_every_rejected_candidate_is_kept_for_reporting(tmp_path):
    # A duplicate or a smoke must be visible in the output, so a wrong pick is auditable.
    _run(tmp_path, "real-50", "rat_python50.json", 50)
    _run(tmp_path, "smoke", "rat_python50.json", 1)
    sel = select_runs(discover_runs(str(tmp_path)))
    assert [os.path.basename(p) for p in sel["rat50"].rejected] == ["smoke"]


def test_a_tie_on_row_count_is_broken_deterministically_and_flagged(tmp_path):
    # setupx really does carry `run-20260904-173735` and `ratbench50_run-20260904-173735`, both
    # 50 rows. Pick one by name so two machines agree, and say that it happened.
    _run(tmp_path, "b-copy", "rat_python50.json", 50)
    _run(tmp_path, "a-copy", "rat_python50.json", 50)
    sel = select_runs(discover_runs(str(tmp_path)))
    assert [os.path.basename(p) for p in sel["rat50"].paths] == ["a-copy"]
    assert sel["rat50"].ambiguous is True


def test_a_missing_dataset_group_is_absent_not_an_error(tmp_path):
    _run(tmp_path, "real-50", "rat_python50.json", 50)
    sel = select_runs(discover_runs(str(tmp_path)))
    assert "rat50" in sel and "envbench100" not in sel


def test_an_empty_agent_dir_selects_nothing_and_does_not_raise(tmp_path):
    assert select_runs(discover_runs(str(tmp_path)))["ignored"] == []


def test_discover_on_a_nonexistent_path_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        discover_runs(str(tmp_path / "nope"))


# ── pooled scoring ────────────────────────────────────────────────────────────────────────
#
# The datasets are disjoint (verified: 50 + 100 = 150 unique repos, 0 overlap), so the honest
# cross-dataset number is one mean over one pile of repos — no per-dataset means, no reweighting,
# no imputing the repos that have no gold.

from bench.campaign import pool_metrics


class _R:
    """Minimal row stand-in: compute_metrics only reads these fields for EBSR/ESSR."""
    def __init__(self, repo, passed=0, credited=True):
        self.repo, self.passed = repo, passed
        self.status = "executed" if credited else "build_fail"
        self.collect_clean = credited
        self.build_ok = credited
        self.executed = credited
        self.collected_node_ids = ()
        self.total = self.failed = self.errors = self.skipped = 0
        self.ebsr = credited
        self.pass_rate = 0.0
        self.collect_rc = 0 if credited else 1
        self.collect_error_count = 0
        self.setup_compile_ok = credited
        self.image_delta_mb = self.installed_pkg_count = None
        self.tokens_in = self.tokens_out = self.llm_calls = self.turns_used = None
        self.cost_usd = self.produce_s = self.build_s = self.test_s = None
        self.meta = {}


def test_pooled_ebsr_is_one_mean_over_one_pile_of_repos():
    groups = {
        "rat50": {"rows": [_R("a/1", credited=True), _R("a/2", credited=False)],
                  "gold": {"a/1": {"size": 10, "sha": None, "status": "CERTIFIED"},
                           "a/2": {"size": 10, "sha": None, "status": "CERTIFIED"}}},
        "envbench100": {"rows": [_R("b/1", credited=True), _R("b/2", credited=True)],
                        "gold": {"b/1": {"size": 10, "sha": None, "status": "CERTIFIED"},
                                 "b/2": {"size": 10, "sha": None, "status": "CERTIFIED"}}},
    }
    p = pool_metrics(groups)
    assert p["EBSR"] == round(3 / 4, 4)            # 3 credited of 4 repos, flat
    assert p["n"] == 4


def test_pooled_essr_divides_by_the_repos_that_actually_have_gold():
    # b/2 has no gold entry: it keeps its EBSR but leaves the ESSR mean. No imputation.
    groups = {
        "rat50": {"rows": [_R("a/1", passed=10)],
                  "gold": {"a/1": {"size": 10, "sha": None, "status": "CERTIFIED"}}},
        "envbench100": {"rows": [_R("b/1", passed=5), _R("b/2", passed=99)],
                        "gold": {"b/1": {"size": 10, "sha": None, "status": "CERTIFIED"}}},
    }
    p = pool_metrics(groups)
    assert p["ESSR"] == round((1.0 + 0.5) / 2, 4)  # over 2 gold repos, not 3
    assert p["n_gold_scored"] == 2 and p["n"] == 3


def test_pooling_one_group_is_just_that_group():
    groups = {"rat50": {"rows": [_R("a/1", passed=10)],
                        "gold": {"a/1": {"size": 10, "sha": None, "status": "CERTIFIED"}}}}
    assert pool_metrics(groups)["n"] == 1


def test_pooling_nothing_is_none_not_a_crash():
    assert pool_metrics({}) is None


# ── economy (the paper's token column) ────────────────────────────────────────────────────
#
# metrics.py reports mean_tokens; the paper publishes a MEDIAN, and the two are far apart here
# because a few repos are enormously heavier than the rest. These pin the median and, just as
# importantly, the telemetry count that has to be published next to it.

from bench.campaign import economy


def _tok(repo, tin, tout, turns=None, cost=None):
    r = _R(repo)
    r.tokens_in, r.tokens_out, r.turns_used, r.cost_usd = tin, tout, turns, cost
    return r


def test_economy_is_a_median_not_a_mean():
    # mean would be 2,750,000 — dragged up by the one heavy repo. Median is 1,500.
    rows = [_tok("a/1", 500, 500), _tok("a/2", 1000, 1000), _tok("a/3", 5_000_000, 5_000_000)]
    e = economy(rows)
    assert e["median_tokens"] == 2000.0
    assert e["n_token_reporting"] == 3


def test_rows_without_telemetry_leave_the_median_rather_than_counting_as_zero():
    # Counting a missing row as 0 would halve the median and understate the arm's real spend.
    rows = [_tok("a/1", 1000, 0), _tok("a/2", 3000, 0), _R("a/3")]   # a/3 reports nothing
    e = economy(rows)
    assert e["median_tokens"] == 2000.0
    assert (e["n_token_reporting"], e["n_rows"]) == (2, 3)


def test_the_telemetry_count_is_reported_so_partial_coverage_is_visible():
    # SetupX carries tokens on 134 of 150 rows; a median over 89% of an arm is a different claim.
    e = economy([_tok("a/1", 1, 1), _R("a/2"), _R("a/3")])
    assert e["n_token_reporting"] == 1 and e["n_rows"] == 3


def test_no_telemetry_at_all_is_none_not_zero():
    # pipreqs spends nothing and reports nothing; 0 would assert a measurement that never happened.
    e = economy([_R("a/1"), _R("a/2")])
    assert e["median_tokens"] is None and e["n_token_reporting"] == 0


def test_turns_and_cost_are_medianed_independently_of_tokens():
    # An arm can report turns but no cost (setupx: cost_usd null on every run).
    rows = [_tok("a/1", 1, 1, turns=10), _tok("a/2", 1, 1, turns=30), _tok("a/3", 1, 1, turns=50)]
    e = economy(rows)
    assert e["median_turns"] == 30 and e["n_turn_reporting"] == 3
    assert e["median_cost_usd"] is None and e["n_cost_reporting"] == 0
