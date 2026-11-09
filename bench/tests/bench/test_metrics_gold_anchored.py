# tests/bench/test_metrics_gold_anchored.py
#
# ESSR is GOLD-ANCHORED and there is only one of it:
#
#     ESSR_r = min(passed_r / D_r, 1.0)        ESSR = mean(ESSR_r)        # macro
#
# D_r is the independently certified manifest size. The ÷exec and ÷all variants were removed
# (2026-09-15): both divided by the agent's OWN pytest collection, so an environment that
# collected 12 tests and passed 11 scored 0.92 — identical to one that collected 1400 and passed
# 1288. Three differently-denominated numbers all called "ESSR" is how a paper ends up quoting one
# while its method section defines another.
#
# Distinct from the dormant node-id-intersection metric in bench/gold.py (test_metrics_gold.py).
import json

import pytest

from bench.metrics import compute_metrics, gold_essr, load_gold_counts
from bench.schema import MeasureRow


def _row(repo, passed=0, total=0, status="executed", collect_clean=True, collected=(), sha=None):
    return MeasureRow(agent="a", repo=repo, env_status="ok", build_ok=True,
                      status=status, collect_clean=collect_clean,
                      collected_node_ids=tuple(collected),
                      executed=status == "executed", total=total, passed=passed,
                      meta={"head_sha": sha} if sha else {})


def _gold(**kw):
    """{name_lower: {size, sha, status}} in the shape load_gold_counts returns."""
    return {k.lower(): {"size": v, "sha": None, "status": "CERTIFIED"} for k, v in kw.items()}


# ── the metric ────────────────────────────────────────────────────────────────────────────

def test_essr_is_the_macro_mean_of_capped_ratios():
    rows = [_row("o/r1", passed=2), _row("o/r2", passed=1)]
    m = gold_essr(rows, _gold(**{"o/r1": 4, "o/r2": 2}))
    assert m["ESSR"] == round((0.5 + 0.5) / 2, 4)
    assert m["n_gold_scored"] == 2


def test_essr_uses_the_certified_denominator_not_the_agents_own():
    # The whole reason for the change: 2 passed of a certified 100 is 0.02, however few tests the
    # broken environment actually managed to collect.
    m = gold_essr([_row("o/r1", passed=2, total=4)], _gold(**{"o/r1": 100}))
    assert m["ESSR"] == 0.02


def test_a_repo_that_never_built_scores_zero_and_stays_in_the_mean():
    rows = [_row("o/r1", passed=4), _row("o/r2", passed=0, status="build_fail", collect_clean=False)]
    m = gold_essr(rows, _gold(**{"o/r1": 4, "o/r2": 10}))
    assert m["ESSR"] == round((1.0 + 0.0) / 2, 4)
    assert m["n_gold_scored"] == 2 and m["n_gold_zero"] == 1


def test_passed_above_gold_is_capped_and_reported_never_silent():
    # An arm can pass tests outside the gold manifest; a repo "scoring" 74.0 is a pairing bug, not
    # a win. sweagent_modern really did hit 1333/18 on valory-xyz/trader.
    m = gold_essr([_row("o/r1", passed=1333)], _gold(**{"o/r1": 18}))
    assert m["ESSR"] == 1.0
    assert m["n_gold_capped"] == 1 and m["gold_capped"][0][0] == "o/r1"


def test_repo_with_no_gold_count_is_excluded_with_a_reason():
    rows = [_row("o/r1", passed=2), _row("o/nogold", passed=99)]
    m = gold_essr(rows, _gold(**{"o/r1": 4}))
    assert m["n_gold_scored"] == 1
    assert m["ESSR"] == 0.5                       # o/nogold must not move the mean
    assert ("o/nogold", "gold_missing") in m["gold_excluded"]


def test_gold_repo_with_no_row_scores_zero_when_the_population_says_it_was_due():
    # The arm was supposed to cover o/never_ran and did not: a failure of the arm, so 0.0 stays in
    # the mean — counted separately from a repo that ran and passed nothing.
    m = gold_essr([_row("o/r1", passed=4)], _gold(**{"o/r1": 4, "o/never_ran": 10}),
                  population=["o/r1", "o/never_ran"])
    assert m["ESSR"] == round((1.0 + 0.0) / 2, 4)
    assert m["n_gold_no_row"] == 1 and "o/never_ran" in m["gold_no_row"]


def test_without_a_population_an_unattempted_repo_is_not_charged_to_the_arm():
    # metrics.py runs automatically on EVERY run, `--only` smokes included. Scoring the whole gold
    # manifest there reported ESSR 0.0208 for a one-repo run whose repo passed everything, because
    # the 47 repos it never attempted were each scored 0.0.
    m = gold_essr([_row("o/r1", passed=4)], _gold(**{"o/r1": 4, "o/never_ran": 10}))
    assert m["ESSR"] == 1.0
    assert m["n_gold_scored"] == 1 and m["n_gold_no_row"] == 0


@pytest.mark.parametrize("bad", [0, -5, None, "264", 3.5, True])
def test_an_unusable_denominator_is_excluded_not_divided_by(bad):
    rows = [_row("o/r1", passed=2), _row("o/bad", passed=2)]
    g = _gold(**{"o/r1": 4})
    g["o/bad"] = {"size": bad, "sha": None, "status": "CERTIFIED"}
    m = gold_essr(rows, g)                        # must not raise ZeroDivisionError/TypeError
    assert m["n_gold_scored"] == 1 and m["ESSR"] == 0.5
    assert ("o/bad", "gold_invalid_size") in m["gold_excluded"]


def test_sha_mismatch_is_reported_but_does_not_change_the_score():
    # A count taken at a different commit is not ground truth for the pinned one. Score anyway so
    # no published number moves, but make the mismatch countable rather than invisible.
    g = {"o/r1": {"size": 4, "sha": "a" * 40, "status": "CERTIFIED"}}
    m = gold_essr([_row("o/r1", passed=2, sha="b" * 40)], g)
    assert m["ESSR"] == 0.5
    assert m["n_gold_sha_mismatch"] == 1 and "o/r1" in m["gold_sha_mismatch"]


def test_essr_is_none_without_gold_never_zero():
    # None means "not scored" and must stay distinguishable from a real 0.0.
    m = gold_essr([_row("o/r1", passed=2)], {})
    assert m["ESSR"] is None and m["n_gold_scored"] == 0


# ── hollow-pass diagnostic ────────────────────────────────────────────────────────────────

def test_collection_coverage_exposes_hollow_ebsr_passes():
    # collect_clean accepts pytest rc 5 ("no tests collected"), so a repo collecting 0 of 912 can
    # still take EBSR credit. Gold makes that measurable instead of averaged in.
    rows = [_row("o/hollow", passed=0, collected=(), status="no_tests_collected"),
            _row("o/real", passed=10, collected=tuple(f"t.py::{i}" for i in range(10)))]
    m = gold_essr(rows, _gold(**{"o/hollow": 912, "o/real": 10}))
    assert m["n_gold_hollow"] == 1 and m["gold_hollow"][0][0] == "o/hollow"
    assert m["mean_collection_coverage"] == round((0.0 + 1.0) / 2, 4)


# ── the loader ────────────────────────────────────────────────────────────────────────────

_COMBINED = {
    "generated_at": "2026-09-14",
    "corpora": {
        "rat_python50": {"repos": {
            "O/Cert": {"full_name": "O/Cert", "sha": "a" * 40, "status": "CERTIFIED",
                       "manifest_size": 20, "node_ids": ["t.py::a"]},
        }},
        "envbench_python100": {"repos": {
            "O/Human": {"full_name": "O/Human", "sha": None, "status": "HUMAN_COUNTED",
                        "manifest_size": 199, "node_ids": None},
            "O/Rejected": {"full_name": "O/Rejected", "sha": "c" * 40, "status": "REJECTED",
                           "manifest_size": 0, "node_ids": None},
        }},
    },
}


def _write(tmp_path, doc=None, text=None):
    p = tmp_path / "gold.json"
    p.write_text(text if text is not None else json.dumps(doc))
    return str(p)


def test_loader_reads_one_corpus_and_lowercases_names(tmp_path):
    g = load_gold_counts(_write(tmp_path, _COMBINED), corpus="rat_python50")
    assert g == {"o/cert": {"size": 20, "sha": "a" * 40, "status": "CERTIFIED"}}


def test_loader_keeps_human_counted_and_drops_unusable_sizes(tmp_path):
    # HUMAN_COUNTED carries a hand-verified count with no node-ids and no sha — usable as a
    # denominator (the operator's call), unlike REJECTED whose size is 0.
    g = load_gold_counts(_write(tmp_path, _COMBINED), corpus="envbench_python100")
    assert set(g) == {"o/human"}
    assert g["o/human"]["size"] == 199 and g["o/human"]["status"] == "HUMAN_COUNTED"


def test_loader_without_a_corpus_merges_every_corpus(tmp_path):
    assert set(load_gold_counts(_write(tmp_path, _COMBINED))) == {"o/cert", "o/human"}


def test_loader_rejects_an_unknown_corpus_by_name(tmp_path):
    with pytest.raises(KeyError) as e:
        load_gold_counts(_write(tmp_path, _COMBINED), corpus="nope")
    assert "rat_python50" in str(e.value)          # the error names what IS available


def test_loader_missing_file_fails_loudly(tmp_path):
    # A silently-absent gold file would publish a table with no anchor and no warning.
    with pytest.raises(FileNotFoundError):
        load_gold_counts(str(tmp_path / "absent.json"))


def test_loader_rejects_malformed_json_with_the_path_in_the_message(tmp_path):
    with pytest.raises(ValueError) as e:
        load_gold_counts(_write(tmp_path, text="{not json"))
    assert "gold.json" in str(e.value)


def test_loader_accepts_the_legacy_flat_shape(tmp_path):
    # rat_python50_gold.json predates the combined file and has repos at the top level.
    doc = {"repos": {"O/R": {"full_name": "O/R", "sha": "d" * 40,
                             "status": "CERTIFIED", "manifest_size": 7}}}
    assert load_gold_counts(_write(tmp_path, doc))["o/r"]["size"] == 7


# ── integration with compute_metrics ──────────────────────────────────────────────────────

def test_compute_metrics_essr_is_the_gold_anchored_one():
    m = compute_metrics([_row("o/r1", passed=2, total=4)], gold=_gold(**{"o/r1": 4}))
    assert m["ESSR"] == 0.5 and m["n_gold_scored"] == 1


def test_the_floating_denominator_variants_are_gone():
    m = compute_metrics([_row("o/r1", passed=2, total=4)], gold=_gold(**{"o/r1": 4}))
    assert "ESSR_all" not in m
    assert "ESSR_gold" not in m                    # the gold one IS the ESSR now, not a variant


def test_compute_metrics_essr_is_none_without_gold_but_ebsr_still_works():
    # EBSR is gold-independent and must keep working on a run with no manifest.
    m = compute_metrics([_row("o/r1", passed=2, total=4)])
    assert "ESSR" in m and m["ESSR"] is None
    assert m["EBSR"] == 1.0 and m["n"] == 1


# ── untrusted repos (pypa/distutils) ──────────────────────────────────────────────────────
#
# Distinct from "no gold". A no-gold repo is one WE failed to certify — not the arm's failure, so
# it keeps its EBSR and leaves ESSR. An UNTRUSTED repo is one whose pinned commit is unreachable
# upstream, so no arm can legitimately build the commit under test — yet sweagent_modern scored
# status=executed / collect_clean=True on pypa/distutils, which can only mean it built a different,
# reachable commit. That result is not evidence about the pinned repo for ANY arm.

from bench.metrics import UNTRUSTED_REPOS


def test_distutils_is_untrusted_by_default():
    assert "pypa/distutils" in UNTRUSTED_REPOS


def test_an_untrusted_repo_is_denied_ebsr_credit_even_when_it_looks_clean():
    # This is exactly the sweagent_modern case: executed + collect_clean, and still not credited.
    rows = [_row("o/good", passed=1, status="executed", collect_clean=True),
            _row("pypa/distutils", passed=36, status="executed", collect_clean=True)]
    m = compute_metrics(rows)
    assert m["n"] == 2                            # STAYS in the denominator
    assert m["n_ebsr"] == 1 and m["EBSR"] == 0.5  # credited for o/good only


def test_an_untrusted_repo_keeps_the_denominator_it_would_have_had():
    # Forcing 0 rather than dropping keeps n constant across arms, so the column is comparable.
    only_good = compute_metrics([_row("o/good", passed=1)])
    with_bad = compute_metrics([_row("o/good", passed=1), _row("pypa/distutils", passed=36)])
    assert only_good["n"] == 1 and with_bad["n"] == 2
    assert only_good["EBSR"] == 1.0 and with_bad["EBSR"] == 0.5


def test_untrusted_matching_is_case_insensitive():
    m = compute_metrics([_row("PyPA/DistUtils", passed=36, status="executed", collect_clean=True)])
    assert m["n_ebsr"] == 0 and m["EBSR"] == 0.0


def test_an_untrusted_repo_is_excluded_from_essr_not_scored_zero():
    # "ESSR = mean of the other repos" — excluding is arithmetically that, and scoring 0 would
    # contradict the rule that an unscoreable repo is not the arm's failure.
    rows = [_row("o/good", passed=4), _row("pypa/distutils", passed=36)]
    g = _gold(**{"o/good": 4, "pypa/distutils": 117})
    m = compute_metrics(rows, gold=g)
    assert m["ESSR"] == 1.0                       # the mean of the others, distutils absent
    assert m["n_gold_scored"] == 1
    assert ("pypa/distutils", "untrusted") in m["gold_excluded"]


def test_an_untrusted_repo_cannot_claim_real_success_either():
    # real_success is EBSR credit + a high pass rate; if the build is not trusted, neither is it.
    rows = [_row("pypa/distutils", passed=36, status="executed", collect_clean=True)]
    m = compute_metrics(rows)
    assert m["real_success"] == 0.0


def test_the_untrusted_set_is_overridable():
    rows = [_row("o/weird", passed=1, status="executed", collect_clean=True)]
    assert compute_metrics(rows)["EBSR"] == 1.0
    assert compute_metrics(rows, untrusted={"o/weird"})["EBSR"] == 0.0


def test_overriding_untrusted_with_an_empty_set_restores_the_measured_result():
    # An explicit opt-out, for anyone who wants to see what the arm actually produced.
    rows = [_row("pypa/distutils", passed=36, status="executed", collect_clean=True)]
    assert compute_metrics(rows, untrusted=set())["EBSR"] == 1.0
