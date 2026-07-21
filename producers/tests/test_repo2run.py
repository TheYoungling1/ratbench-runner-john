# producers/tests/test_repo2run.py — the pure re-home transform + Repo2RunProducer (design §3.3)
#
# The real repo2run tool is STUBBED (injected via runner=) — no real repo, no docker, no keys.
import os

from producers.base import ProduceContext, ProducedEnv, RepoSpec, write_env_packet
from producers.repo2run import Repo2RunProducer, rehome_dockerfile

# The verified repo2run pattern: clone -> mkdir /repo -> cp -r into /repo -> WORKDIR /repo.
_R2R_DF = "FROM python:3.10\nRUN git clone x && mkdir /repo && cp -r /x/. /repo\nWORKDIR /repo\n"


def _ctx(tmp_path):
    return ProduceContext(llm="deepseek/deepseek-v4-flash", workdir=str(tmp_path))


def test_rehome_appends_mv_and_workdir():
    out = rehome_dockerfile(_R2R_DF)
    # The installed /repo is MOVED to /testbed and a reverse symlink keeps /repo resolving.
    assert "RUN mv /repo /testbed && ln -sfn /testbed /repo" in out
    assert out.rstrip().endswith("WORKDIR /testbed")
    # The original body is preserved (append, not rewrite).
    assert out.startswith("FROM python:3.10")


def test_rehome_makes_testbed_a_real_dir_not_a_symlink():
    # C1 requires /testbed be a REAL dir: a symlinked /testbed defeats the guard's `find -P`.
    # So the transform must `mv` into /testbed and must NOT create /testbed via `ln -s ... /testbed`.
    out = rehome_dockerfile(_R2R_DF)
    assert "mv /repo /testbed" in out
    # No `ln -s`/`ln -sfn` whose LINK NAME is /testbed (the reverse symlink targets /testbed instead).
    import re
    assert not re.search(r"ln\s+-s\S*\s+\S+\s+/testbed(?:\s|$)", out)


def test_rehome_detects_nonstandard_dest():
    df = "FROM python:3.10\nRUN mkdir /srv/app && cp -r /clone/. /srv/app\nWORKDIR /srv/app\n"
    out = rehome_dockerfile(df)
    assert "RUN mv /srv/app /testbed && ln -sfn /testbed /srv/app" in out


def test_rehome_defaults_to_repo_when_undetectable():
    df = "FROM python:3.10\nRUN pip install .\n"   # no mkdir/cp/WORKDIR repo signal
    out = rehome_dockerfile(df)
    assert "RUN mv /repo /testbed && ln -sfn /testbed /repo" in out


def test_rehome_punctuated_dest_falls_back_to_default():
    # FIX B: a dest carrying shell punctuation (`;`, `&`, ...) must NOT flow into the `mv` stanza
    # (injection / broken build). Detection rejects it and falls back to the safe default /repo.
    df = ("FROM python:3.10\n"
          "RUN mkdir /repo;evil && cp -r /x/. /repo;evil\n"
          "WORKDIR /repo;evil\n")
    out = rehome_dockerfile(df)
    assert "RUN mv /repo /testbed && ln -sfn /testbed /repo" in out
    assert "mv /repo;evil" not in out                # the punctuated dest never reaches the stanza
    assert "/testbed;evil" not in out


def test_rehome_workdir_punctuated_dest_falls_back():
    # FIX B via the WORKDIR/cp cross-check branch — the _SAFE_DEST guard rejects the punctuated
    # `/repo;rm` even though the WORKDIR and cp dest agree, so it defaults to /repo.
    df = ("FROM python:3.10\n"
          "RUN cp -r /x/. /repo;rm\n"
          "WORKDIR /repo;rm\n")
    out = rehome_dockerfile(df)
    assert "RUN mv /repo /testbed && ln -sfn /testbed /repo" in out
    assert "mv /repo;rm" not in out                    # the punctuated dest never reaches the stanza


def test_rehome_honors_repo_dest_fallback_argument():
    df = "FROM python:3.10\nRUN pip install .\n"   # undetectable -> fall back to the argument
    out = rehome_dockerfile(df, repo_dest="/workspace")
    assert "RUN mv /workspace /testbed && ln -sfn /testbed /workspace" in out


def test_producer_produce_success_is_rehomed(tmp_path):
    def _stub(repo, ctx, **kw):
        return {"dockerfile": _R2R_DF, "base_image": "python:3.10", "head_sha": "abc123"}

    p = Repo2RunProducer(llm="deepseek/deepseek-v4-flash", runner=_stub)
    env = p.produce(RepoSpec("o/r", "https://github.com/o/r"), _ctx(tmp_path))
    assert env.status == "produced"
    assert env.conformance == "rehomed"
    assert env.producer_name == "repo2run"
    assert "RUN mv /repo /testbed" in env.dockerfile        # the re-home was applied
    assert env.base_image == "python:3.10"
    assert env.head_sha == "abc123"
    assert env.economy["produce_s"] is not None


def test_producer_removes_raw_repo_homed_dockerfile(tmp_path):
    # FIX 1 (false-green): the repo2run tool writes the RAW /repo-homed Dockerfile at
    # output/<full_name>/Dockerfile. bench.harvest._find_dockerfile checks that path BEFORE
    # eval_build/Dockerfile, so it MUST be removed or harvest measures an empty /testbed. The stub
    # simulates the tool writing that raw file as a side effect; produce() must delete it and still
    # return status="produced" with the re-homed Dockerfile.
    full_name = "o/r"
    raw_path = os.path.join(str(tmp_path), "output", full_name, "Dockerfile")

    def _stub(repo, ctx, **kw):
        os.makedirs(os.path.dirname(raw_path), exist_ok=True)
        with open(raw_path, "w") as f:
            f.write(_R2R_DF)                              # the surviving RAW /repo Dockerfile
        return {"dockerfile": _R2R_DF, "base_image": "python:3.10", "head_sha": "abc123"}

    env = Repo2RunProducer(runner=_stub).produce(
        RepoSpec(full_name, "https://github.com/o/r"), _ctx(tmp_path))
    assert env.status == "produced"
    assert "RUN mv /repo /testbed" in env.dockerfile     # the re-home was applied
    assert not os.path.exists(raw_path)                  # FIX 1: raw /repo-homed file removed


def test_producer_no_dockerfile_is_error(tmp_path):
    def _stub(repo, ctx, **kw):
        return {"dockerfile": None, "base_image": "python:3.10"}

    env = Repo2RunProducer(runner=_stub).produce(
        RepoSpec("o/r", "https://github.com/o/r"), _ctx(tmp_path))
    assert env.status == "error" and env.dockerfile is None


def test_producer_tool_error_is_error_not_raise(tmp_path):
    # Anti-vanish (design §1): a repo2run tool failure yields status="error", never a raise.
    def _boom(repo, ctx, **kw):
        raise RuntimeError("repo2run exploded")

    env = Repo2RunProducer(runner=_boom).produce(
        RepoSpec("o/r", "https://github.com/o/r"), _ctx(tmp_path))
    assert env.status == "error" and env.dockerfile is None
    assert "repo2run exploded" in env.note


def test_producer_is_registered():
    from producers import PRODUCERS
    assert PRODUCERS["repo2run"] is Repo2RunProducer


def test_rehomed_is_only_dockerfile_harvest_can_find(tmp_path):
    # FIX A invariant: after the produce-only path lands the re-homed packet, the RAW /repo-homed
    # Dockerfile must be GONE so bench.harvest measures the re-homed one. harvest._find_dockerfile
    # checks repo_dir/Dockerfile BEFORE eval_build/Dockerfile — so a surviving raw file would
    # shadow the re-homed one (repo2run stays non_conforming). This locks that precedence + the
    # removal invariant the model's _finish_produce_only enforces (which returns error if the raw
    # file survives; the model itself needs the RAT tree so it can't be imported here).
    from bench.harvest import _find_dockerfile

    out_root = tmp_path / "output"
    out_root.mkdir()
    repo = RepoSpec("o/r", "https://github.com/o/r")
    env = ProducedEnv(repo=repo, dockerfile=rehome_dockerfile(_R2R_DF),
                      status="produced", conformance="rehomed", producer_name="repo2run")
    repo_dir = write_env_packet(str(out_root), env)          # writes eval_build/Dockerfile
    raw = os.path.join(repo_dir, "Dockerfile")
    with open(raw, "w") as f:
        f.write(_R2R_DF)                                     # simulate the surviving RAW /repo file

    # BUG state (raw present): harvest selects the RAW one, not the re-homed eval_build one.
    assert _find_dockerfile(repo_dir) == raw
    with open(_find_dockerfile(repo_dir)) as f:
        assert "mv /repo /testbed" not in f.read()

    # FIX A (raw removed + verified gone): harvest now finds ONLY the re-homed eval_build one.
    os.remove(raw)
    picked = _find_dockerfile(repo_dir)
    assert picked.endswith(os.path.join("eval_build", "Dockerfile"))
    with open(picked) as f:
        assert "mv /repo /testbed" in f.read()


def test_ensure_rat_on_path_resolves_env_bench_rat():
    # FIX C: the standalone live runners must add <repo>/rat (holding libkit/ + eval/) to sys.path
    # before their lazy libkit/eval imports. From this env-bench checkout it resolves to <repo>/rat.
    import sys as _sys

    from producers.base import ensure_rat_on_path
    root = ensure_rat_on_path()
    assert os.path.isfile(os.path.join(root, "libkit", "command.py"))
    assert root in _sys.path
