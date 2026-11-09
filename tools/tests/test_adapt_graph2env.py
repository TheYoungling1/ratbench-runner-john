# tools/tests/test_adapt_graph2env.py
#
# The adapter's whole job is to not lie about the ablation. Each test below pins one way it could:
# forge a commit it did not build at, quietly shrink a denominator so a weaker arm looks equal,
# let two source roots overwrite each other, or anchor ESSR to an under-counted gold manifest.
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from adapt_graph2env import _head_sha, _meta, adapt_arm, build_gold, main, unbuildable

SHA_A = "a" * 40
SHA_B = "b" * 40
_DF = "FROM python:3.11-slim\nARG SOURCE_SHA={sha}\nCOPY setup.sh /tmp/setup.sh\nRUN bash /tmp/setup.sh\n"


def _repo(root, full_name, *, dockerfile=None, src_rev=None, tokens=None, setup=True):
    d = os.path.join(root, *full_name.split("/"))
    os.makedirs(d, exist_ok=True)
    if dockerfile is not None:
        open(os.path.join(d, "Dockerfile"), "w").write(dockerfile)
        if setup:
            open(os.path.join(d, "setup.sh"), "w").write("echo hi\n")
    if src_rev is not None:
        json.dump({"head_sha": src_rev}, open(os.path.join(d, "source_revision.json"), "w"))
    if tokens is not None:
        json.dump(tokens, open(os.path.join(d, "token_usage.json"), "w"))
    return d


def _dataset(tmp_path, names):
    p = tmp_path / "ds.json"
    p.write_text(json.dumps({"repos": [{"full_name": n, "commit": SHA_B} for n in names]}))
    return str(p)


# --- head_sha: what was BUILT, never what was asked for -------------------------------------

def test_head_sha_prefers_the_runners_source_revision(tmp_path):
    d = _repo(str(tmp_path), "o/r", dockerfile=_DF.format(sha=SHA_B), src_rev=SHA_A)
    assert _head_sha(d) == SHA_A


def test_head_sha_falls_back_to_the_build_verified_dockerfile_arg(tmp_path):
    d = _repo(str(tmp_path), "o/r", dockerfile=_DF.format(sha=SHA_B))
    assert _head_sha(d) == SHA_B


def test_head_sha_is_none_rather_than_the_datasets_pinned_commit(tmp_path):
    # A repo with no Dockerfile built nothing. Reporting the dataset's commit here would forge the
    # exact alignment gold_essr checks, turning "never built" into "built at the right commit".
    d = _repo(str(tmp_path), "o/r")
    assert _head_sha(d) is None


# --- status: a failed repo is a counted zero, not an absent measurement ----------------------

def test_a_repo_without_a_dockerfile_is_missing_not_unmeasurable(tmp_path):
    # bench.metrics drops {unmeasurable, legacy_missing} from every denominator; "missing" stays in
    # and scores EBSR 0. Getting this wrong inflates the arm by shrinking n.
    assert _meta(_repo(str(tmp_path), "o/r"))["status"] == "missing"


def test_a_repo_with_a_dockerfile_is_produced(tmp_path):
    d = _repo(str(tmp_path), "o/r", dockerfile=_DF.format(sha=SHA_A))
    assert _meta(d)["status"] == "produced"


def test_a_corrupt_token_file_is_reported_not_swallowed(tmp_path):
    d = _repo(str(tmp_path), "o/r", dockerfile=_DF.format(sha=SHA_A))
    open(os.path.join(d, "token_usage.json"), "w").write("{not json")
    m = _meta(d)
    assert "token_usage_error" in m and m.get("tokens_in") is None


# --- arms: every arm scores over the same N -------------------------------------------------

def test_a_dataset_repo_the_arm_never_emitted_is_stubbed_as_missing(tmp_path):
    src = tmp_path / "out"
    _repo(str(src), "o/ran", dockerfile=_DF.format(sha=SHA_A), src_rev=SHA_A)
    ds = _dataset(tmp_path, ["o/ran", "o/never"])
    runs = str(tmp_path / "runs")
    assert adapt_arm("arm", [str(src)], json.load(open(ds))["repos"], ds, "x", runs) == 0
    stub = os.path.join(runs, "arm", "ds", "output", "o", "never", "_meta.json")
    assert json.load(open(stub))["status"] == "missing"


def test_off_dataset_repos_are_dropped_not_scored(tmp_path):
    src = tmp_path / "out"
    _repo(str(src), "o/ran", dockerfile=_DF.format(sha=SHA_A))
    _repo(str(src), "o/stranger", dockerfile=_DF.format(sha=SHA_A))
    ds = _dataset(tmp_path, ["o/ran"])
    runs = str(tmp_path / "runs")
    adapt_arm("arm", [str(src)], json.load(open(ds))["repos"], ds, "x", runs)
    out = os.path.join(runs, "arm", "ds", "output", "o")
    assert sorted(os.listdir(out)) == ["ran"]


def test_two_source_roots_claiming_one_repo_is_an_error_not_a_silent_overwrite(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _repo(str(a), "o/r", dockerfile=_DF.format(sha=SHA_A))
    _repo(str(b), "o/r", dockerfile=_DF.format(sha=SHA_B))
    ds = _dataset(tmp_path, ["o/r"])
    assert adapt_arm("arm", [str(a), str(b)], json.load(open(ds))["repos"], ds, "x",
                     str(tmp_path / "runs")) == 2


def test_a_dockerfile_whose_copy_source_is_absent_is_flagged(tmp_path):
    src = tmp_path / "out"
    _repo(str(src), "o/r", dockerfile=_DF.format(sha=SHA_A), setup=False)   # no setup.sh on disk
    ds = _dataset(tmp_path, ["o/r"])
    assert adapt_arm("arm", [str(src)], json.load(open(ds))["repos"], ds, "x",
                     str(tmp_path / "runs")) == 1


# --- gold: an under-counted denominator inflates the repos with the weakest ground truth -----

def _combined(tmp_path, entries):
    root = tmp_path / "repo"
    (root / "datasets").mkdir(parents=True)
    (root / "datasets" / "gold_manifests_combined.json").write_text(
        json.dumps({"corpora": {"c": {"repos": {e["full_name"].lower(): e for e in entries}}}}))
    return str(root)


def test_partial_gold_entries_are_excluded_by_default(tmp_path):
    repo_root = _combined(tmp_path, [
        {"full_name": "o/good", "sha": SHA_A, "status": "CERTIFIED", "manifest_size": 10},
        {"full_name": "o/part", "sha": SHA_B, "status": "PARTIAL", "manifest_size": 5},
    ])
    out = str(tmp_path / "gold.json")
    build_gold([{"full_name": "o/good"}, {"full_name": "o/part"}], [], out,
               include_partial=False, repo_root=repo_root)
    doc = json.load(open(out))
    assert list(doc["corpora"]["c"]["repos"]) == ["o/good"]
    assert doc["totals"]["repos_without_gold"] == ["o/part"]


def test_partial_gold_can_be_opted_into(tmp_path):
    repo_root = _combined(tmp_path, [
        {"full_name": "o/part", "sha": SHA_B, "status": "PARTIAL", "manifest_size": 5}])
    out = str(tmp_path / "gold.json")
    build_gold([{"full_name": "o/part"}], [], out, include_partial=True, repo_root=repo_root)
    assert json.load(open(out))["corpora"]["c"]["repos"]["o/part"]["manifest_size"] == 5


def test_a_repo_with_no_gold_anywhere_is_named_not_silently_dropped(tmp_path):
    repo_root = _combined(tmp_path, [
        {"full_name": "o/good", "sha": SHA_A, "status": "CERTIFIED", "manifest_size": 10}])
    out = str(tmp_path / "gold.json")
    build_gold([{"full_name": "o/good"}, {"full_name": "o/orphan"}], [], out,
               include_partial=False, repo_root=repo_root)
    assert json.load(open(out))["totals"]["repos_without_gold"] == ["o/orphan"]


def test_the_emitted_gold_file_loads_through_the_harness_loader(tmp_path):
    # The file is only useful if bench.metrics can read it; pin the contract rather than the shape.
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "bench"))
    from bench.metrics import load_gold_counts
    repo_root = _combined(tmp_path, [
        {"full_name": "o/good", "sha": SHA_A, "status": "CERTIFIED", "manifest_size": 10}])
    out = str(tmp_path / "gold.json")
    build_gold([{"full_name": "o/good"}], [], out, include_partial=False, repo_root=repo_root)
    assert load_gold_counts(out)["o/good"]["size"] == 10


def test_base_image_is_read_from_the_dockerfile(tmp_path):
    # image_delta_mb needs it; harvest reads it from _meta, and Graph2Env writes no meta at all.
    d = _repo(str(tmp_path), "o/r", dockerfile=_DF.format(sha=SHA_A))
    assert _meta(d)["base_image"] == "python:3.11-slim"


# --- skipping: a narrowed denominator, never a silent zero -----------------------------------

def test_a_dockerfile_wrapping_an_empty_setup_is_unbuildable(tmp_path):
    # Graph2Env renders its generic template even when the agent is SIGKILLed; that template
    # BUILDS, so measuring it scores a false green off an OOM (google/trax, rc=-9).
    d = _repo(str(tmp_path), "o/r", dockerfile=_DF.format(sha=SHA_A), setup=False)
    open(os.path.join(d, "setup.sh"), "w").write("#!/usr/bin/env bash\ntrue\n")
    assert "empty setup.sh" in unbuildable(d)


def test_a_best_effort_setup_with_real_commands_is_buildable(tmp_path):
    d = _repo(str(tmp_path), "o/r", dockerfile=_DF.format(sha=SHA_A), setup=False)
    open(os.path.join(d, "setup.sh"), "w").write("#!/usr/bin/env bash\npip install -e .\n")
    assert unbuildable(d) is None


def test_a_missing_dockerfile_is_unbuildable(tmp_path):
    assert unbuildable(_repo(str(tmp_path), "o/r")) == "no Dockerfile"


def test_a_skipped_repo_is_unmeasurable_not_missing(tmp_path):
    # bench.metrics drops {unmeasurable, legacy_missing} from every denominator; "missing" would
    # score a counted EBSR 0. The two must not be confused.
    d = _repo(str(tmp_path), "o/r", dockerfile=_DF.format(sha=SHA_A))
    assert _meta(d, "no Dockerfile")["status"] == "unmeasurable"
    assert _meta(d)["status"] == "produced"


def test_the_skip_set_is_the_union_so_arms_score_over_the_same_repos(tmp_path, capsys):
    # arm A builds both repos; arm B fails one. Skipping per-arm would score A over 2 and B over
    # 1 -- the arm that crashed more would also get the easier subset.
    a, b = tmp_path / "a", tmp_path / "b"
    for root in (a, b):
        _repo(str(root), "o/ok", dockerfile=_DF.format(sha=SHA_A))
    _repo(str(a), "o/flaky", dockerfile=_DF.format(sha=SHA_A))
    _repo(str(b), "o/flaky")                       # no Dockerfile in arm B only
    ds = _dataset(tmp_path, ["o/ok", "o/flaky"])
    runs = str(tmp_path / "runs")
    main(["--dataset", ds, "--runs-root", runs, "--skip-unbuildable",
          f"--arm=A={a}", f"--arm=B={b}"])
    for arm in ("A", "B"):
        base = os.path.join(runs, arm, "ds", "output", "o")
        assert json.load(open(os.path.join(base, "flaky", "_meta.json")))["status"] == "unmeasurable"
        assert json.load(open(os.path.join(base, "ok", "_meta.json")))["status"] == "produced"


def test_an_extra_gold_file_in_combined_shape_is_read(tmp_path):
    # --extra-gold is how a gap in the primary manifest gets backfilled; reading only the flat
    # `repos` key took zero denominators from a `corpora`-shaped file and silently changed nothing.
    repo_root = _combined(tmp_path, [
        {"full_name": "o/certified", "sha": SHA_A, "status": "CERTIFIED", "manifest_size": 10}])
    extra = tmp_path / "extra.json"
    extra.write_text(json.dumps({"corpora": {"legacy": {"repos": {
        "o/human": {"full_name": "o/human", "sha": "", "status": "HUMAN_COUNTED",
                    "manifest_size": 220}}}}}))
    out = str(tmp_path / "gold.json")
    build_gold([{"full_name": "o/certified"}, {"full_name": "o/human"}], [str(extra)], out,
               include_partial=False, repo_root=repo_root)
    doc = json.load(open(out))
    assert doc["corpora"]["legacy"]["repos"]["o/human"]["manifest_size"] == 220
    assert doc["totals"]["repos_without_gold"] == []


def test_the_primary_manifest_wins_over_an_extra_for_the_same_repo(tmp_path):
    repo_root = _combined(tmp_path, [
        {"full_name": "o/r", "sha": SHA_A, "status": "CERTIFIED", "manifest_size": 10}])
    extra = tmp_path / "extra.json"
    extra.write_text(json.dumps({"corpora": {"legacy": {"repos": {
        "o/r": {"full_name": "o/r", "status": "HUMAN_COUNTED", "manifest_size": 999}}}}}))
    out = str(tmp_path / "gold.json")
    build_gold([{"full_name": "o/r"}], [str(extra)], out, include_partial=False, repo_root=repo_root)
    doc = json.load(open(out))
    assert doc["corpora"]["c"]["repos"]["o/r"]["manifest_size"] == 10
    assert "legacy" not in doc["corpora"]


def test_charge_counts_an_unbuildable_repo_instead_of_excluding_it(tmp_path):
    # The paper convention: EBSR is the mean over ALL measured repos, so an arm that emitted
    # nothing scores 0 and stays in n. `missing` is counted by metrics.py; `unmeasurable` is not.
    d = _repo(str(tmp_path), "o/r", dockerfile=_DF.format(sha=SHA_A))
    assert _meta(d, "no Dockerfile", charge=True)["status"] == "missing"
    assert _meta(d, "no Dockerfile")["status"] == "unmeasurable"


def test_charge_is_per_arm_so_one_arms_failure_does_not_zero_another(tmp_path):
    # arm A builds `flaky`, arm B does not. Under CHARGE the union would score A a 0 for a repo it
    # actually built -- penalising it for its neighbour's failure.
    a, b = tmp_path / "a", tmp_path / "b"
    for root in (a, b):
        _repo(str(root), "o/ok", dockerfile=_DF.format(sha=SHA_A))
    _repo(str(a), "o/flaky", dockerfile=_DF.format(sha=SHA_A))
    _repo(str(b), "o/flaky")                        # no Dockerfile in arm B only
    ds = _dataset(tmp_path, ["o/ok", "o/flaky"])
    runs = str(tmp_path / "runs")
    main(["--dataset", ds, "--runs-root", runs, "--charge-unbuildable",
          f"--arm=A={a}", f"--arm=B={b}"])
    got = lambda arm, repo: json.load(
        open(os.path.join(runs, arm, "ds", "output", "o", repo, "_meta.json")))["status"]
    assert got("A", "flaky") == "produced"          # A built it -- must still be measured
    assert got("B", "flaky") == "missing"           # B did not -- counted as a zero
