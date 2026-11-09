# tools/tests/test_adapt_pjy.py
#
# The adapter's whole job is to not lie about a foreign artifact bundle. Each test below pins one
# way it could: mislabel a repo, invent a token count, or let a Dockerfile that cannot possibly
# build look like one that can.
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from adapt_pjy import _check, _meta, _repo_dirs, adapt


def _repo(root, full_name, dockerfile=None, tokens=None, extra=()):
    d = os.path.join(root, *full_name.split("/"))
    os.makedirs(d, exist_ok=True)
    if dockerfile is not None:
        open(os.path.join(d, "Dockerfile"), "w").write(dockerfile)
    if tokens is not None:
        json.dump(tokens, open(os.path.join(d, "token_usage.json"), "w"))
    for name in extra:
        p = os.path.join(d, name)
        os.makedirs(p, exist_ok=True) if name.endswith("/") else open(p, "w").write("x")
    return d


# ── layout ────────────────────────────────────────────────────────────────────────────────

def test_a_normal_owner_repo_pair_is_read_as_one_repo(tmp_path):
    _repo(str(tmp_path), "rq/rq", "FROM python")
    assert [n for n, _ in _repo_dirs(str(tmp_path))] == ["rq/rq"]


def test_their_writer_flattens_a_repo_whose_owner_matches_its_name(tmp_path):
    # peering-manager/peering-manager really does land as output/peering-manager/Dockerfile. Read
    # as an owner dir it would vanish from the run entirely and silently cost one repo.
    _repo(str(tmp_path), "peering-manager", "FROM python")
    assert [n for n, _ in _repo_dirs(str(tmp_path))] == ["peering-manager/peering-manager"]


def test_a_build_context_subdir_is_not_mistaken_for_a_repo(tmp_path):
    # Repo2Run writes `patch/` beside the Dockerfile. It is a COPY source, not a repository.
    _repo(str(tmp_path), "rq/rq", "FROM python", extra=("patch/",))
    assert [n for n, _ in _repo_dirs(str(tmp_path))] == ["rq/rq"]


# ── tokens ────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, tin, tout", [
    ({"input_tokens": 10, "output_tokens": 2}, 10, 2),          # Graph2Env / Repo2Run spelling
    ({"prompt_tokens": 7, "completion_tokens": 1}, 7, 1),       # RAT spelling
])
def test_every_token_spelling_normalises_to_the_harness_field(tmp_path, raw, tin, tout):
    d = _repo(str(tmp_path), "a/b", "FROM python", tokens=raw)
    m = _meta(d)
    assert (m["tokens_in"], m["tokens_out"]) == (tin, tout)


def test_an_unreadable_token_file_is_reported_not_silently_zeroed(tmp_path):
    d = _repo(str(tmp_path), "a/b", "FROM python")
    open(os.path.join(d, "token_usage.json"), "w").write("{not json")
    m = _meta(d)
    assert "token_usage_error" in m and m.get("tokens_in") is None


def test_no_token_file_leaves_the_field_absent_rather_than_zero(tmp_path):
    # A zero would pull the arm's mean token count down as if the agent were free.
    assert _meta(_repo(str(tmp_path), "a/b", "FROM python")).get("tokens_in") is None


# ── status ────────────────────────────────────────────────────────────────────────────────

def test_a_repo_with_a_dockerfile_is_measurable(tmp_path):
    assert _meta(_repo(str(tmp_path), "a/b", "FROM python"))["status"] == "produced"


def test_a_repo_with_no_dockerfile_counts_as_a_failure_not_a_hole(tmp_path):
    # bench.harvest maps "missing" to a counted EBSR-0. Anything else would quietly shrink the
    # denominator and flatter the arm.
    assert _meta(_repo(str(tmp_path), "a/b"))["status"] == "missing"


# ── build-context checks ──────────────────────────────────────────────────────────────────

def test_a_copy_source_absent_from_the_bundle_is_flagged(tmp_path):
    # RAT's `COPY repo /repo` — the checkout was never shipped, so the image cannot be rebuilt.
    assert _check(_repo(str(tmp_path), "a/b", "FROM python\nCOPY repo /repo\n")) == ["repo"]


def test_a_copy_source_that_is_a_directory_is_no_longer_flagged(tmp_path):
    # INVERTED 2026-09-23. This used to assert a directory IS flagged, because bench.measure built
    # from a tmpdir of FILE siblings only and a directory never reached the context. harvest.py's
    # _sibling_scripts now walks a directory COPY source and emits `<dirname>/<relpath>` keys, so
    # Repo2Run's `COPY patch /patch` builds. Keeping the old assertion would have gone on reporting
    # 60 RAT repos as unbuildable when 35 of them build fine.
    d = _repo(str(tmp_path), "a/b", "FROM python\nCOPY patch /patch\n", extra=("patch/",))
    assert _check(d) == []


def test_a_satisfied_file_copy_is_not_flagged(tmp_path):
    d = _repo(str(tmp_path), "a/b", "FROM python\nCOPY setup.sh /tmp/setup.sh\n", extra=("setup.sh",))
    assert _check(d) == []


def test_copy_flags_are_not_mistaken_for_sources(tmp_path):
    d = _repo(str(tmp_path), "a/b", "FROM python\nCOPY --chown=1:1 setup.sh /s\n", extra=("setup.sh",))
    assert _check(d) == []


def test_no_dockerfile_means_nothing_to_check(tmp_path):
    assert _check(_repo(str(tmp_path), "a/b")) == []


# ── end to end ────────────────────────────────────────────────────────────────────────────

def _fixture(tmp_path, full_names, dockerfile="FROM python"):
    pjy = tmp_path / "pjy"
    out = pjy / "RatBench_Graph2Env" / "ebsr_evaluation" / "output"
    for fn in full_names:
        _repo(str(out), fn, dockerfile, tokens={"input_tokens": 5, "output_tokens": 1})
    ds = tmp_path / "datasets"
    ds.mkdir()
    (ds / "rat_python50.json").write_text(json.dumps([{"full_name": f} for f in full_names]))
    return pjy, ds


def test_adapt_writes_a_manifest_the_campaign_scorer_can_discover(tmp_path):
    from bench.campaign import DATASET_GROUPS

    pjy, ds = _fixture(tmp_path, ["rq/rq"])
    adapt(str(pjy), str(tmp_path / "runs"), str(ds))
    mf = json.load(open(tmp_path / "runs" / "pjy-graph2env" / "rat50-pjy" / "_run_manifest.json"))
    assert os.path.basename(mf["dataset"]["path"]) in DATASET_GROUPS
    assert mf["repos_linked"] == 1 and mf["repos_with_dockerfile"] == 1


def test_adapt_links_rather_than_copies_so_the_source_tree_is_untouched(tmp_path):
    pjy, ds = _fixture(tmp_path, ["rq/rq"])
    adapt(str(pjy), str(tmp_path / "runs"), str(ds))
    dest = tmp_path / "runs" / "pjy-graph2env" / "rat50-pjy" / "output" / "rq" / "rq"
    # NARROWED 2026-09-23: this used to assert the Dockerfile itself is a symlink. It is now a
    # real file so _write_dockerfile can re-home it to /testbed (a symlink would feed the original
    # un-rehomed text to the build). The property this test is actually about -- we mirror their
    # bundle by reference and never write into their tree -- is unchanged, so it is now pinned on
    # a non-Dockerfile entry plus the no-writeback check below.
    assert os.path.islink(dest / "token_usage.json")
    assert not (dest / "Dockerfile").is_symlink()     # rehomed in place, by design
    src = pjy / "RatBench_Graph2Env" / "ebsr_evaluation" / "output" / "rq" / "rq"
    assert not (src / "_meta.json").exists()          # the shim lives in the mirror, not their data


def test_a_repo_outside_the_canonical_dataset_is_dropped_and_named(tmp_path):
    # An unfiltered pool is not a number about the dataset. Dropping silently is the trap.
    pjy, ds = _fixture(tmp_path, ["rq/rq"])
    _repo(str(pjy / "RatBench_Graph2Env" / "ebsr_evaluation" / "output"), "who/dis", "FROM python")
    adapt(str(pjy), str(tmp_path / "runs"), str(ds))
    mf = json.load(open(tmp_path / "runs" / "pjy-graph2env" / "rat50-pjy" / "_run_manifest.json"))
    assert mf["off_dataset_dropped"] == ["who/dis"] and mf["repos_linked"] == 1


def test_an_arm_whose_dockerfiles_cannot_build_exits_nonzero(tmp_path):
    pjy, ds = _fixture(tmp_path, ["rq/rq"], dockerfile="FROM python\nCOPY repo /repo\n")
    assert adapt(str(pjy), str(tmp_path / "runs"), str(ds)) == 1


def test_a_clean_arm_exits_zero(tmp_path):
    pjy, ds = _fixture(tmp_path, ["rq/rq"])
    assert adapt(str(pjy), str(tmp_path / "runs"), str(ds)) == 0


def test_a_missing_pjy_root_fails_loudly(tmp_path):
    assert adapt(str(tmp_path / "nope"), str(tmp_path / "runs"), str(tmp_path)) == 2


# ── re-homing to /testbed ─────────────────────────────────────────────────────────────────
#
# The harness contract requires the repo at /testbed: contract.probe_testbed marks anything else
# `non_conforming`, and metrics._DISQUALIFIED contains that status, so the repo scores EBSR 0.
# ALL 47 Repo2Run Dockerfiles home the clone at /repo and none mention /testbed -- measured, not
# assumed. Adapting them unchanged wrote off the whole arm: 9 of the first 14 measured rows came
# back non_conforming before this was caught.
#
# producers/repo2run.py::rehome_dockerfile already solves this for the native repo2run variety
# (varieties.toml marks it `measure = "rehome"`), so the adapter reuses it rather than inventing
# a second stanza.

from adapt_pjy import _write_dockerfile


def test_a_repo_dockerfile_is_rehomed_to_testbed(tmp_path):
    src = _repo(str(tmp_path / "src"), "a/b", "FROM python\nRUN mkdir /repo\nWORKDIR /repo\n")
    dest = tmp_path / "dest" / "a" / "b"
    dest.mkdir(parents=True)
    assert _write_dockerfile(src, str(dest)) is True
    out = (dest / "Dockerfile").read_text()
    assert "mv /repo /testbed" in out and "ln -sfn /testbed /repo" in out
    assert out.strip().endswith("WORKDIR /testbed")


def test_the_written_dockerfile_is_a_real_file_not_a_symlink(tmp_path):
    # _mirror symlinks every entry; a symlinked Dockerfile would send the ORIGINAL (un-rehomed)
    # text to the build and silently undo this.
    src = _repo(str(tmp_path / "src"), "a/b", "FROM python\nRUN mkdir /repo\n")
    dest = tmp_path / "dest" / "a" / "b"
    dest.mkdir(parents=True)
    (dest / "Dockerfile").symlink_to(os.path.join(src, "Dockerfile"))
    _write_dockerfile(src, str(dest))
    assert not (dest / "Dockerfile").is_symlink()


def test_a_dockerfile_already_using_testbed_is_left_alone(tmp_path):
    # Other pipelines may already conform. Appending a second `mv` would break a correct image.
    df = "FROM python\nRUN git clone x /testbed\nWORKDIR /testbed\n"
    src = _repo(str(tmp_path / "src"), "a/b", df)
    dest = tmp_path / "dest" / "a" / "b"
    dest.mkdir(parents=True)
    _write_dockerfile(src, str(dest))
    assert (dest / "Dockerfile").read_text() == df
    assert "mv" not in (dest / "Dockerfile").read_text()


def test_no_dockerfile_reports_false_and_writes_nothing(tmp_path):
    src = _repo(str(tmp_path / "src"), "a/b", dockerfile=None)
    dest = tmp_path / "dest" / "a" / "b"
    dest.mkdir(parents=True)
    assert _write_dockerfile(src, str(dest)) is False
    assert not (dest / "Dockerfile").exists()


# ── materialising a `COPY repo` checkout ──────────────────────────────────────────────────
#
# RAT's dockerfile arm ends with `COPY repo /repo`, a fixed-revision checkout the Dockerfile's own
# comment says was "saved with this artifact" -- but it was never shipped. _check flags it on 48 of
# 50 RatBench repos and 99 of 99 EnvBench ones, and without it every one of those builds fails for
# a missing path rather than anything the agent did.
#
# It is reconstructible: _ebsr_result.json records source_revision.expected_commit alongside
# dataset_row.clone_url, and RAT verified the saved tree really was at that commit
# (source_revision.matches == true on 48/50). Cloning at that pin reproduces it exactly.
#
# These tests clone from a LOCAL git repo -- no network, so they run anywhere.
import subprocess

from adapt_pjy import _materialise_repo, _repo_revision


def _git_origin(tmp_path, content="hello"):
    """A real local git repo to clone from, plus its HEAD sha."""
    o = tmp_path / "origin"
    o.mkdir()
    run = lambda *a: subprocess.run(a, cwd=str(o), check=True, capture_output=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@t"); run("git", "config", "user.name", "t")
    (o / "file.txt").write_text(content)
    run("git", "add", "-A"); run("git", "commit", "-qm", "c1")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(o),
                         capture_output=True, text=True).stdout.strip()
    return str(o), sha


def _rat_repo(root, full_name, url, sha, dockerfile="FROM python\nCOPY repo /repo\n"):
    d = _repo(root, full_name, dockerfile)
    json.dump({"full_name": full_name, "commit": sha,
               "source_revision": {"expected_commit": sha, "actual_commit": sha, "matches": True},
               "dataset_row": {"clone_url": url}},
              open(os.path.join(d, "_ebsr_result.json"), "w"))
    return d


def test_revision_is_read_from_the_ebsr_result(tmp_path):
    url, sha = _git_origin(tmp_path)
    src = _rat_repo(str(tmp_path / "src"), "a/b", url, sha)
    assert _repo_revision(src) == (url, sha)


def test_a_missing_repo_copy_is_cloned_at_the_pinned_commit(tmp_path):
    url, sha = _git_origin(tmp_path, content="pinned content")
    src = _rat_repo(str(tmp_path / "src"), "a/b", url, sha)
    dest = tmp_path / "dest" / "a" / "b"; dest.mkdir(parents=True)
    assert _materialise_repo(src, str(dest)) is None            # None == success
    assert (dest / "repo" / "file.txt").read_text() == "pinned content"
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(dest / "repo"),
                          capture_output=True, text=True).stdout.strip()
    assert head == sha, "must land on the pinned revision, not the default branch tip"


def test_a_repo_that_was_actually_shipped_is_left_alone(tmp_path):
    # Repo2Run ships its COPY sources; only RAT's are absent. Never clone over real artifacts.
    url, sha = _git_origin(tmp_path)
    src = _rat_repo(str(tmp_path / "src"), "a/b", url, sha)
    os.makedirs(os.path.join(src, "repo"))
    open(os.path.join(src, "repo", "theirs.txt"), "w").write("original")
    dest = tmp_path / "dest" / "a" / "b"; dest.mkdir(parents=True)
    assert _materialise_repo(src, str(dest)) is None
    assert not (dest / "repo" / "file.txt").exists(), "should not have cloned"


def test_a_dockerfile_with_no_repo_copy_is_skipped(tmp_path):
    src = _repo(str(tmp_path / "src"), "a/b", "FROM python\nCOPY setup.sh /s\n")
    dest = tmp_path / "dest" / "a" / "b"; dest.mkdir(parents=True)
    assert _materialise_repo(src, str(dest)) is None
    assert not (dest / "repo").exists()


def test_no_recorded_revision_reports_a_reason_instead_of_guessing(tmp_path):
    # EnvBench_RAT(dockerfile) has no clone_url on any of its 100 repos. Cloning a guessed URL at
    # an unverified revision would fabricate an environment, so it must decline and say so.
    src = _repo(str(tmp_path / "src"), "a/b", "FROM python\nCOPY repo /repo\n")
    dest = tmp_path / "dest" / "a" / "b"; dest.mkdir(parents=True)
    reason = _materialise_repo(src, str(dest))
    assert reason and "revision" in reason.lower()
    assert not (dest / "repo").exists()


def test_a_commit_that_does_not_exist_upstream_fails_loudly_and_cleans_up(tmp_path):
    url, _ = _git_origin(tmp_path)
    bogus = "0" * 40
    src = _rat_repo(str(tmp_path / "src"), "a/b", url, bogus)
    dest = tmp_path / "dest" / "a" / "b"; dest.mkdir(parents=True)
    reason = _materialise_repo(src, str(dest))
    assert reason, "an unreachable commit must be reported, never silently left at branch tip"
    assert not (dest / "repo").exists(), "a half-made checkout must not reach the build"


# ── dataset fallback for the clone URL ────────────────────────────────────────────────────
#
# EnvBench_RAT(dockerfile) records a `commit` for all 100 repos but NO clone_url, so reading the
# artifact alone makes the checkout look unreconstructible. It is not: envbench_python100.json
# carries clone_url AND commit for all 100, and the artifact commit matches the dataset commit on
# 100 of 100 (verified). The dataset is the authoritative pin these runs were made against, so it
# supplies the URL -- and the two commits agreeing IS the verification.
#
# Fails closed on disagreement: if the artifact was built at a different revision than the dataset
# pins, cloning either one would misrepresent the run.

def test_the_dataset_supplies_the_url_when_the_artifact_lacks_one(tmp_path):
    url, sha = _git_origin(tmp_path, content="from dataset")
    src = _repo(str(tmp_path / "src"), "a/b", "FROM python\nCOPY repo /repo\n")
    json.dump({"full_name": "a/b", "commit": sha},
              open(os.path.join(src, "_ebsr_result.json"), "w"))
    dest = tmp_path / "dest" / "a" / "b"; dest.mkdir(parents=True)
    assert _materialise_repo(src, str(dest), dataset_row={"clone_url": url, "commit": sha}) is None
    assert (dest / "repo" / "file.txt").read_text() == "from dataset"


def test_a_commit_disagreement_between_artifact_and_dataset_is_refused(tmp_path):
    url, sha = _git_origin(tmp_path)
    src = _repo(str(tmp_path / "src"), "a/b", "FROM python\nCOPY repo /repo\n")
    json.dump({"full_name": "a/b", "commit": "b" * 40},
              open(os.path.join(src, "_ebsr_result.json"), "w"))
    dest = tmp_path / "dest" / "a" / "b"; dest.mkdir(parents=True)
    reason = _materialise_repo(src, str(dest), dataset_row={"clone_url": url, "commit": sha})
    assert reason and "mismatch" in reason.lower()
    assert not (dest / "repo").exists()


def test_the_artifacts_own_url_still_wins_when_present(tmp_path):
    url, sha = _git_origin(tmp_path, content="from artifact")
    src = _rat_repo(str(tmp_path / "src"), "a/b", url, sha)
    dest = tmp_path / "dest" / "a" / "b"; dest.mkdir(parents=True)
    assert _materialise_repo(src, str(dest), dataset_row={"clone_url": "http://wrong", "commit": sha}) is None
    assert (dest / "repo" / "file.txt").read_text() == "from artifact"


def test_no_url_from_either_source_still_declines(tmp_path):
    src = _repo(str(tmp_path / "src"), "a/b", "FROM python\nCOPY repo /repo\n")
    json.dump({"commit": "c" * 40}, open(os.path.join(src, "_ebsr_result.json"), "w"))
    dest = tmp_path / "dest" / "a" / "b"; dest.mkdir(parents=True)
    assert _materialise_repo(src, str(dest), dataset_row={}) is not None


# ── the second bundle layout ──────────────────────────────────────────────────────────────
#
# RAT_Python50_new_RAT(dockerfile) puts repos under `output/` directly, with a _run_config.json,
# instead of the `ebsr_evaluation/output` every other bundle uses. Hardcoding one layout silently
# skipped that whole arm ("skip ...: no ebsr_evaluation/output").

from adapt_pjy import _output_root


def test_the_ebsr_evaluation_layout_is_found(tmp_path):
    p = tmp_path / "Arm" / "ebsr_evaluation" / "output"; p.mkdir(parents=True)
    assert _output_root(str(tmp_path), "Arm") == str(p)


def test_the_bare_output_layout_is_found(tmp_path):
    p = tmp_path / "Arm" / "output"; p.mkdir(parents=True)
    assert _output_root(str(tmp_path), "Arm") == str(p)


def test_ebsr_evaluation_wins_when_both_exist(tmp_path):
    a = tmp_path / "Arm" / "ebsr_evaluation" / "output"; a.mkdir(parents=True)
    (tmp_path / "Arm" / "output").mkdir(parents=True)
    assert _output_root(str(tmp_path), "Arm") == str(a)


def test_neither_layout_returns_none(tmp_path):
    (tmp_path / "Arm").mkdir()
    assert _output_root(str(tmp_path), "Arm") is None
