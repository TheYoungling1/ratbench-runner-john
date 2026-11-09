# tests/bench/test_harvest.py
import json
from bench.harvest import discover


def _write(base, owner_repo, dockerfile=None, meta=None, subdir="", scripts=None):
    d = base / owner_repo
    (d / subdir).mkdir(parents=True, exist_ok=True) if subdir else d.mkdir(parents=True, exist_ok=True)
    tgt = d / subdir if subdir else d
    if dockerfile is not None:
        (tgt / "Dockerfile").write_text(dockerfile)
    for name, content in (scripts or {}).items():
        (tgt / name).write_text(content)
    if meta is not None:
        (d / "bench_meta.json").write_text(json.dumps(meta))


def test_discovers_dockerfile_and_meta(tmp_path):
    root = tmp_path / "v3run"
    _write(root, "o/r1", dockerfile="FROM x\nCOPY setup.sh /tmp/s\nRUN bash /tmp/s",
           meta={"tokens_in": 10, "tokens_out": 20, "base_image": "python:3.13-slim"},
           scripts={"setup.sh": "echo hi"})
    envs = discover({"v3": str(root)})
    assert len(envs) == 1
    e = envs[0]
    # legacy _meta (no `status` key) + Dockerfile present -> legacy_ok (design §2.5)
    assert e.agent == "v3" and e.repo.full_name == "o/r1" and e.status == "legacy_ok"
    assert e.dockerfile.startswith("FROM x") and e.setup_scripts["setup.sh"] == "echo hi"
    assert e.base_image == "python:3.13-slim" and e.meta["tokens_in"] == 10


def test_eval_build_subdir_layout(tmp_path):
    root = tmp_path / "v3run"
    _write(root, "o/r2", dockerfile="FROM y", subdir="eval_build",
           meta={"base_image": "python:3.12-slim"})
    envs = discover({"v3": str(root)})
    assert len(envs) == 1 and envs[0].dockerfile == "FROM y" and envs[0].status == "legacy_ok"


def test_missing_dockerfile_is_status_legacy_missing(tmp_path):
    root = tmp_path / "v3run"
    _write(root, "o/r3", dockerfile=None, meta={"tokens_in": 5})
    envs = discover({"v3": str(root)})
    # legacy _meta (no `status`) + no Dockerfile -> legacy_missing (NOT "missing"): a pre-contract
    # non-producer must not be forced into a counted EBSR-0 (design §2.5).
    assert len(envs) == 1 and envs[0].status == "legacy_missing" and envs[0].dockerfile is None
    assert envs[0].meta["tokens_in"] == 5


def test_no_meta_gives_empty_meta(tmp_path):
    root = tmp_path / "v3run"
    _write(root, "o/r4", dockerfile="FROM z", meta=None)
    envs = discover({"v3": str(root)})
    assert envs[0].meta == {} and envs[0].status == "legacy_ok"


def test_meta_status_is_passed_through(tmp_path):
    # A producer-written _meta.status wins over the legacy fallback.
    root = tmp_path / "v3run"
    _write(root, "o/r7", dockerfile=None, meta={"status": "unmeasurable"})
    _write(root, "o/r8", dockerfile="FROM p", meta={"status": "produced"})
    envs = {e.repo.full_name: e for e in discover({"v3": str(root)})}
    assert envs["o/r7"].status == "unmeasurable" and envs["o/r7"].dockerfile is None
    assert envs["o/r8"].status == "produced" and envs["o/r8"].dockerfile == "FROM p"


def test_copy_with_chown_and_multisource(tmp_path):
    root = tmp_path / "v3run"
    _write(root, "o/r5",
           dockerfile='FROM x\nCOPY --chown=1000:1000 setup.sh /tmp/s\nCOPY a.sh b.sh /tmp/',
           scripts={"setup.sh": "echo hi", "a.sh": "echo a", "b.sh": "echo b"})
    e = discover({"v3": str(root)})[0]
    assert e.setup_scripts["setup.sh"] == "echo hi"
    assert e.setup_scripts["a.sh"] == "echo a" and e.setup_scripts["b.sh"] == "echo b"


def test_copy_json_array_form(tmp_path):
    root = tmp_path / "v3run"
    _write(root, "o/r6", dockerfile='FROM x\nCOPY ["run.sh", "/tmp/run.sh"]',
           scripts={"run.sh": "echo run"})
    e = discover({"v3": str(root)})[0]
    assert e.setup_scripts["run.sh"] == "echo run"


def test_discover_threads_language_from_meta(tmp_path):
    root = tmp_path / "agentX"
    repo = root / "o" / "gorepo"
    (repo / "eval_build").mkdir(parents=True)
    (repo / "eval_build" / "Dockerfile").write_text("FROM golang:1.22\n")
    (repo / "_meta.json").write_text(json.dumps({"status": "produced", "language": "golang"}))
    envs = discover({"agentX": str(root)})
    assert len(envs) == 1 and envs[0].repo.language == "golang"


def test_discover_defaults_language_to_python_when_absent(tmp_path):
    root = tmp_path / "agentY"
    repo = root / "o" / "pyrepo"
    (repo / "eval_build").mkdir(parents=True)
    (repo / "eval_build" / "Dockerfile").write_text("FROM python:3.13-slim\n")
    (repo / "_meta.json").write_text(json.dumps({"status": "produced"}))
    envs = discover({"agentY": str(root)})
    assert len(envs) == 1 and envs[0].repo.language == "python"


# ── post-contract runs must not hide a producer failure ───────────────────────────────────
#
# `legacy_missing` exists for runs that PREDATE the _meta.json contract: those agents never
# promised a Dockerfile, so an EBSR-0 would libel them (design §2.5). But metrics.py:55 and :214
# drop `legacy_missing` from EVERY denominator, so the same status applied to a MODERN run means
# an agent that crashed before writing _meta.json silently leaves the population instead of
# scoring 0. Observed on TencentBlueKing/bk-lite (sweagent_repo2run_modern, rat50new): produce died
# with repo_error, and rat50new EBSR read 28/49 instead of 28/50.
#
# A post-contract run is identifiable: the runner writes `_run_manifest.json` at the run root.
# Its presence is the discriminator — manifest means the run promised artifacts.

def _manifest(root):
    """Mark `root` as the output/ dir of a post-contract run."""
    root.mkdir(parents=True, exist_ok=True)
    (root.parent / "_run_manifest.json").write_text(json.dumps({"variety": "someagent"}))


def test_post_contract_run_with_no_dockerfile_is_counted_missing(tmp_path):
    root = tmp_path / "run1" / "output"
    _manifest(root)
    _write(root, "o/crashed", dockerfile=None, meta=None)
    envs = discover({"a": str(root)})
    assert len(envs) == 1
    # "missing" stays IN every denominator -> a counted EBSR 0, which is the honest score for an
    # agent that was asked for a Dockerfile and produced none.
    assert envs[0].status == "missing" and envs[0].dockerfile is None


def test_post_contract_partial_meta_without_status_still_counts(tmp_path):
    # bk-lite's shape: the producer wrote SOME metadata but died before recording a status.
    root = tmp_path / "run2" / "output"
    _manifest(root)
    _write(root, "o/halfway", dockerfile=None, meta={"tokens_in": 5})
    envs = discover({"a": str(root)})
    assert envs[0].status == "missing" and envs[0].meta["tokens_in"] == 5


def test_pre_contract_run_keeps_legacy_missing(tmp_path):
    # No manifest -> genuinely old data -> unchanged, still excluded from the denominators.
    root = tmp_path / "oldrun"
    _write(root, "o/ancient", dockerfile=None, meta={"tokens_in": 5})
    envs = discover({"a": str(root)})
    assert envs[0].status == "legacy_missing"


def test_an_explicit_meta_status_still_wins_on_a_post_contract_run(tmp_path):
    # The native lanes (rat, live sweagent, live-claude) declare `unmeasurable` and have no
    # rebuildable artifact BY DESIGN. They must keep leaving the denominator.
    root = tmp_path / "run3" / "output"
    _manifest(root)
    _write(root, "o/native", dockerfile=None, meta={"status": "unmeasurable"})
    envs = discover({"a": str(root)})
    assert envs[0].status == "unmeasurable"


def test_post_contract_run_with_a_dockerfile_is_unaffected(tmp_path):
    root = tmp_path / "run4" / "output"
    _manifest(root)
    _write(root, "o/fine", dockerfile="FROM x", meta=None)
    envs = discover({"a": str(root)})
    assert envs[0].status == "legacy_ok" and envs[0].dockerfile == "FROM x"


# ── COPY of a DIRECTORY must reach the build context ──────────────────────────────────────
#
# measure() builds from a fresh tmpdir holding the Dockerfile plus whatever _sibling_scripts
# inlined, and that used to be `os.path.isfile` only -- so `COPY patch /patch`, where patch/ is a
# real directory, was dropped WITHOUT A WORD and the build failed on a missing path. Repo2Run does
# exactly this on 60 of the 100 RAT repos, 35 of which build fine in their own pipeline: those
# would have scored EBSR 0 and been read as the agent's failure.
#
# measure.py already creates parent dirs for nested keys, so carrying a directory is purely a
# matter of emitting `<dirname>/<relpath>` keys here.

def test_a_copied_directory_is_carried_into_the_build_context(tmp_path):
    # REWRITTEN 2026-09-23. This originally asserted the directory was INLINED into setup_scripts
    # as text. That shipped a real defect: errors="replace" corrupted binary content, so RAT's
    # `COPY repo` git checkout arrived with a mangled .git and the image scored non_conforming.
    # Directories are now carried as PATHS and copied on disk (see test_copy_dirs.py).
    root = tmp_path / "run" / "output"
    _manifest(root)
    d = root / "o" / "r"
    (d / "patch" / "nested").mkdir(parents=True)
    (d / "Dockerfile").write_text("FROM x\nCOPY patch /patch\nRUN ls /patch")
    (d / "patch" / "a.diff").write_text("--- a\n+++ b\n")
    (d / "patch" / "nested" / "b.diff").write_text("deep")
    envs = discover({"a": str(root)})
    assert envs[0].copy_dirs == {"patch": str(d / "patch")}
    assert not any(k.startswith("patch/") for k in envs[0].setup_scripts)


def test_a_copied_file_still_works_unchanged(tmp_path):
    # The overwhelmingly common case: no behaviour change for any arm without a directory COPY.
    root = tmp_path / "run" / "output"
    _manifest(root)
    _write(root, "o/r", dockerfile="FROM x\nCOPY setup.sh /tmp/s", scripts={"setup.sh": "echo hi"})
    envs = discover({"a": str(root)})
    assert envs[0].setup_scripts == {"setup.sh": "echo hi"}


def test_a_copy_source_that_does_not_exist_is_still_omitted(tmp_path):
    # RAT's `COPY repo /repo` names a checkout that was never shipped. Nothing to carry; the build
    # fails for a real reason and _check reports it.
    root = tmp_path / "run" / "output"
    _manifest(root)
    _write(root, "o/r", dockerfile="FROM x\nCOPY repo /repo")
    assert envs_scripts(discover({"a": str(root)})) == {}


def envs_scripts(envs):
    return envs[0].setup_scripts


def test_an_empty_copied_directory_does_not_crash(tmp_path):
    root = tmp_path / "run" / "output"
    _manifest(root)
    d = root / "o" / "r"
    (d / "patch").mkdir(parents=True)
    (d / "Dockerfile").write_text("FROM x\nCOPY patch /patch")
    envs = discover({"a": str(root)})
    assert envs[0].setup_scripts == {}
