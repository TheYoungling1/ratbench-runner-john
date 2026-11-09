# tools/tests/test_verify_run.py — the Tier-0 flagger decides which repos an agent ever reads.
#
# The dangerous failure is silence: a clean-looking table over data that should have been flagged.
# These pin the flags that gate a paper number (commit drift, cost provenance, merge provenance)
# and the one false-positive that would matter (a short/full sha pair treated as a mismatch).
import json
import os

from tools.verify_run import check_merge, check_repo, phase2_state, severity

_OK = {
    "full_name": "o/good", "status": "produced", "head_sha": "abc123def456",
    "llm_calls": 7, "total_tokens": 900, "cost_usd": 0.4, "usage_source": "computed",
    "deploy_image_digest": "sha256:aa", "unreplayed": False, "note": "phase2=True",
}


def _repo(tmp_path, meta, dockerfile=True):
    d = tmp_path / meta["full_name"]
    (d / "eval_build").mkdir(parents=True)
    if dockerfile:
        (d / "eval_build" / "Dockerfile").write_text("FROM python:3.11\n")
    (d / "_meta.json").write_text(json.dumps(meta))
    return str(d)


def test_clean_repo_has_no_flags(tmp_path):
    flags, _ = check_repo(_repo(tmp_path, _OK), _OK, "abc123def456")
    assert flags == [], flags
    assert severity(flags) == "OK"


def test_short_sha_is_not_a_mismatch(tmp_path):
    # Datasets pin full shas, _meta may carry an abbreviation (or vice versa). Flagging that
    # would bury the real drift cases under 100 false positives.
    flags, _ = check_repo(_repo(tmp_path, _OK), _OK, "abc123d")
    assert "head_sha_mismatch" not in flags


def test_real_commit_drift_is_high(tmp_path):
    flags, detail = check_repo(_repo(tmp_path, _OK), _OK, "ffffffffffff")
    assert "head_sha_mismatch" in flags and severity(flags) == "HIGH"
    assert "abc123def456" in detail["head_sha"]


def test_cli_priced_cost_is_flagged(tmp_path):
    # usage_source="cli" overstates a bridge-routed run by ~50-100x; it must never reach a figure.
    meta = {**_OK, "usage_source": "cli"}
    flags, _ = check_repo(_repo(tmp_path, meta), meta, "abc123def456")
    assert "cost_provenance" in flags and severity(flags) == "HIGH"


def test_zero_tokens_with_calls_is_flagged(tmp_path):
    meta = {**_OK, "total_tokens": 0}
    flags, _ = check_repo(_repo(tmp_path, meta), meta, "abc123def456")
    assert "token_blackhole" in flags


def test_produced_without_dockerfile_is_flagged(tmp_path):
    flags, _ = check_repo(_repo(tmp_path, _OK, dockerfile=False), _OK, "abc123def456")
    assert "missing_dockerfile" in flags


def test_phase2_states():
    assert phase2_state("phase2=True: ok") == "True"
    assert phase2_state("phase2=None: [error] budget exhausted") == "None"
    assert phase2_state("nothing here") is None


def test_phase2_none_is_flagged_but_not_high(tmp_path):
    meta = {**_OK, "note": "phase2=None: [error] LLM call budget exhausted"}
    flags, detail = check_repo(_repo(tmp_path, meta), meta, "abc123def456")
    assert "phase2_incomplete" in flags and detail["phase2"] == "None"
    assert severity(flags) == "MED"


def test_merge_flags_mixed_agent_commits(tmp_path):
    ds = tmp_path / "ds.json"
    ds.write_text(json.dumps([{"full_name": "o/good", "commit": "abc123def456"}]))
    manifest = {
        "dataset": {"path": str(ds), "sha256": __import__("hashlib").sha256(ds.read_bytes()).hexdigest()},
        "harness_commit_consistent_across_sources": False,
        "source_runs": [{"run": "a", "agent_commit": "943870b"}, {"run": "b", "agent_commit": "fb47717"}],
    }
    (tmp_path / "_run_manifest.json").write_text(json.dumps(manifest))
    problems = check_merge(str(tmp_path), {"o/good"}, {"o/good": "abc123def456"})
    assert any("multiple agent commits" in p and "943870b" in p for p in problems), problems


def test_merge_flags_membership_drift(tmp_path):
    ds = tmp_path / "ds.json"
    ds.write_text(json.dumps([{"full_name": "o/good", "commit": "abc"}]))
    (tmp_path / "_run_manifest.json").write_text(json.dumps({
        "dataset": {"path": str(ds), "sha256": __import__("hashlib").sha256(ds.read_bytes()).hexdigest()},
        "harness_commit_consistent_across_sources": True, "source_runs": [],
    }))
    # The envbench70 stray-repo trap: an extra repo inflates n and skews the headline score.
    problems = check_merge(str(tmp_path), {"o/good", "o/stray"}, {"o/good": "abc"})
    assert any("outside the dataset" in p for p in problems), problems


def test_no_manifest_means_no_merge_claims(tmp_path):
    assert check_merge(str(tmp_path), {"o/good"}, {"o/good": "abc"}) == []


def test_armwide_null_digest_becomes_a_note_not_100_flags(tmp_path):
    # setupx/claudecode never populate deploy_image_digest. Flagging every row would drown the
    # Tier-1 worklist; flagging none when it IS populated elsewhere would hide a real outlier.
    from tools.verify_run import verify

    ds = tmp_path / "ds.json"
    out = tmp_path / "run" / "output"
    names = ["o/a", "o/b"]
    ds.write_text(json.dumps([{"full_name": n, "commit": "abc"} for n in names]))
    for n in names:
        d = out / n / "eval_build"
        d.mkdir(parents=True)
        (d / "Dockerfile").write_text("FROM x\n")
        (out / n / "_meta.json").write_text(json.dumps(
            {**_OK, "full_name": n, "head_sha": "abc", "deploy_image_digest": None}))

    res = verify(str(tmp_path / "run"), str(ds))
    assert res["notes"] and "all 2 repos" in res["notes"][0]
    assert all("no_image_digest" not in r["flags"] for r in res["repos"])

    # One populated row makes the other's null a genuine outlier — flag survives.
    (out / "o/a" / "_meta.json").write_text(json.dumps(
        {**_OK, "full_name": "o/a", "head_sha": "abc", "deploy_image_digest": "sha256:aa"}))
    res = verify(str(tmp_path / "run"), str(ds))
    assert res["notes"] == []
    assert [r["flags"] for r in res["repos"] if r["repo"] == "o/b"] == [["no_image_digest"]]


def test_null_total_tokens_with_components_is_not_a_blackhole(tmp_path):
    # sweagent populates tokens_in/tokens_out but never the total_tokens rollup. Treating that as
    # "provider omitted usage" produced 97 false HIGH flags and the wrong conclusion about the arm.
    meta = {**_OK, "total_tokens": None, "tokens_in": 2583751, "tokens_out": 6567}
    flags, detail = check_repo(_repo(tmp_path, meta), meta, "abc123def456")
    assert "token_blackhole" not in flags
    assert "total_tokens_underived" in flags and severity(flags) == "MED"
    assert "2590318" in detail["tokens"]


def test_genuinely_absent_usage_is_still_high(tmp_path):
    meta = {**_OK, "total_tokens": None, "tokens_in": None, "tokens_out": None}
    flags, _ = check_repo(_repo(tmp_path, meta), meta, "abc123def456")
    assert "token_blackhole" in flags and severity(flags) == "HIGH"
