import os

from producers.base import ProduceContext, RepoSpec
from producers.pipreqs_baseline import run_pipreqs


def _ctx(tmp_path):
    return ProduceContext(llm=None, workdir=str(tmp_path))


class _FakeCompletedProcess:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = b""
        self.stderr = b""


def test_run_pipreqs_clones_pins_and_scans(tmp_path):
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append((cmd, kwargs))
        # Call 1 is `git clone`, which is what CREATES the destination dir in production —
        # run_pipreqs only makedirs() ctx.workdir itself. The stub must do the same or the
        # pipreqs call below has nowhere to write.
        if isinstance(cmd, list) and "clone" in cmd:
            os.makedirs(cmd[-1], exist_ok=True)
        # The 4th call is the `pipreqs` invocation itself — write the file it's told to write,
        # since run_pipreqs() reads that file back after the subprocess call returns.
        if isinstance(cmd, list) and cmd and cmd[0] == "pipreqs":
            savepath = cmd[cmd.index("--savepath") + 1]
            with open(savepath, "w") as f:
                f.write("Flask==3.1.3\nrequests==2.34.2\n")
        return _FakeCompletedProcess()

    repo = RepoSpec("o/r", "https://github.com/o/r", commit="c0ffee")
    result = run_pipreqs(repo, _ctx(tmp_path), runner=fake_runner)

    assert result["requirements"] == "Flask==3.1.3\nrequests==2.34.2\n"
    assert result["produce_s"] >= 0

    # Call 1: clone. Call 2 & 3: pin (fetch, checkout). Call 4: pipreqs itself.
    assert len(calls) == 4
    clone_cmd = calls[0][0]
    assert "git" in clone_cmd and "clone" in clone_cmd
    assert "https://github.com/o/r.git" in clone_cmd
    fetch_cmd = calls[1][0]
    assert "fetch" in fetch_cmd and "c0ffee" in fetch_cmd
    checkout_cmd = calls[2][0]
    assert "checkout" in checkout_cmd and "c0ffee" in checkout_cmd
    pipreqs_cmd = calls[3][0]
    assert pipreqs_cmd[0] == "pipreqs"
    assert "--mode" in pipreqs_cmd and "no-pin" in pipreqs_cmd
    assert "--force" in pipreqs_cmd


def test_run_pipreqs_no_commit_skips_pin(tmp_path):
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append(cmd)
        if isinstance(cmd, list) and "clone" in cmd:      # git clone creates the dest dir
            os.makedirs(cmd[-1], exist_ok=True)
        if isinstance(cmd, list) and cmd and cmd[0] == "pipreqs":
            savepath = cmd[cmd.index("--savepath") + 1]
            with open(savepath, "w") as f:
                f.write("\n")
        return _FakeCompletedProcess()

    repo = RepoSpec("o/r", "https://github.com/o/r")  # commit=None
    result = run_pipreqs(repo, _ctx(tmp_path), runner=fake_runner)

    # "\n" is what real pipreqs 0.4.13 writes for a stdlib-only repo (verified: the savepath file
    # is one byte, b"\n", not zero bytes) — a VALID result, not an error. The stub above writes
    # the same thing so the test asserts a true fact about the tool.
    assert result["requirements"] == "\n"
    assert len(calls) == 2                        # clone + pipreqs only — no fetch/checkout


def test_run_pipreqs_missing_output_file_raises(tmp_path):
    # pipreqs "succeeds" (returncode 0) but never writes the savepath — must raise, not silently
    # return an empty dict (Task 2 depends on this raising to know the difference between
    # "genuinely no third-party imports" (Task above, an empty string) and "something is wrong").
    def fake_runner(cmd, **kwargs):
        return _FakeCompletedProcess()   # never writes the savepath file

    repo = RepoSpec("o/r", "https://github.com/o/r")
    try:
        run_pipreqs(repo, _ctx(tmp_path), runner=fake_runner)
        assert False, "expected an exception"
    except FileNotFoundError:
        pass


def test_run_pipreqs_clone_failure_propagates(tmp_path):
    import subprocess

    def fake_runner(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    repo = RepoSpec("o/r", "https://github.com/o/r")
    try:
        run_pipreqs(repo, _ctx(tmp_path), runner=fake_runner)
        assert False, "expected an exception"
    except subprocess.CalledProcessError:
        pass


from producers.base import ProducedEnv
from producers.pipreqs_baseline import PipreqsProducer


def test_producer_success_is_native(tmp_path):
    def _stub(repo, ctx, **kw):
        return {"requirements": "Flask==3.1.3\n", "produce_s": 1.23}

    p = PipreqsProducer(runner=_stub)
    env = p.produce(RepoSpec("o/r", "https://github.com/o/r", commit="c0ffee"), _ctx(tmp_path))

    assert env.status == "produced"
    assert env.conformance == "native"      # ProducedEnv's enum, NOT varieties.toml's "conforming"
    assert env.producer_name == "pipreqs"
    assert env.base_image == "python:3.10"
    assert env.head_sha == "c0ffee"
    assert env.setup_scripts == {"requirements_pipreqs.txt": "Flask==3.1.3\n"}
    assert "COPY requirements_pipreqs.txt /requirements_pipreqs.txt" in env.dockerfile
    assert "pip install -r /requirements_pipreqs.txt" in env.dockerfile
    assert env.economy["produce_s"] == 1.23


def test_producer_empty_requirements_still_produces(tmp_path):
    # A stdlib-only repo is a VALID pipreqs result, not an error: the Dockerfile still installs
    # pytest via its own fixed lines, and `pip install -r` on an empty file is a no-op.
    def _stub(repo, ctx, **kw):
        return {"requirements": "\n", "produce_s": 0.5}

    env = PipreqsProducer(runner=_stub).produce(
        RepoSpec("o/r", "https://github.com/o/r"), _ctx(tmp_path))
    assert env.status == "produced"
    assert env.setup_scripts == {"requirements_pipreqs.txt": "\n"}


def test_producer_pins_the_emitted_dockerfiles_own_clone(tmp_path):
    def _stub(repo, ctx, **kw):
        return {"requirements": "Flask==3.1.3\n", "produce_s": 1.0}

    env = PipreqsProducer(runner=_stub).produce(
        RepoSpec("o/r", "https://github.com/o/r", commit="c0ffee"), _ctx(tmp_path))
    assert "git -C /testbed fetch --depth 1 origin c0ffee" in env.dockerfile
    assert "git -C /testbed checkout --detach c0ffee" in env.dockerfile
    assert env.note == ""


def test_producer_no_commit_leaves_dockerfile_unpinned(tmp_path):
    def _stub(repo, ctx, **kw):
        return {"requirements": "Flask==3.1.3\n", "produce_s": 1.0}

    env = PipreqsProducer(runner=_stub).produce(
        RepoSpec("o/r", "https://github.com/o/r"), _ctx(tmp_path))   # commit=None
    assert "checkout --detach" not in env.dockerfile


def test_producer_run_pipreqs_failure_is_error_not_raise(tmp_path):
    def _boom(repo, ctx, **kw):
        raise RuntimeError("pipreqs exploded")

    env = PipreqsProducer(runner=_boom).produce(
        RepoSpec("o/r", "https://github.com/o/r"), _ctx(tmp_path))
    assert env.status == "error" and env.dockerfile is None
    assert "pipreqs exploded" in env.note


def test_producer_is_needs_llm_false():
    assert PipreqsProducer.needs_llm is False
    assert PipreqsProducer.measurable is True
    assert PipreqsProducer.name == "pipreqs"


def test_producer_is_registered():
    from producers import PRODUCERS
    assert PRODUCERS["pipreqs"] is PipreqsProducer


def test_producer_accepts_llm_kwarg_from_the_runner():
    # runner/benchmark.py:148 calls producers.get(name, llm=...) for EVERY produce-able model,
    # with no needs_llm check. A constructor without `llm` blows up with TypeError on the first
    # real run and NO direct-instantiation test catches it. This is that test.
    import producers
    prod = producers.get("pipreqs", llm="anything")
    assert isinstance(prod, PipreqsProducer)


def test_run_pipreqs_scopes_the_clone_per_repo(tmp_path):
    # ctx.workdir is the SHARED run root, not a per-repo scratch dir (runner/benchmark.py:159
    # passes root_path; producers/dockeragent.py:86 documents it as "the shared run root").
    # Cloning every repo to <workdir>/repo makes repo #2 of a 50-repo run fail with
    # "destination path already exists and is not an empty directory". Every sibling producer
    # scopes by repo.full_name; so must this one.
    clone_dests = []

    def fake_runner(cmd, **kwargs):
        if isinstance(cmd, list) and "clone" in cmd:
            clone_dests.append(cmd[-1])
            os.makedirs(cmd[-1], exist_ok=True)
        if isinstance(cmd, list) and cmd and cmd[0] == "pipreqs":
            with open(cmd[cmd.index("--savepath") + 1], "w") as f:
                f.write("\n")
        return _FakeCompletedProcess()

    ctx = _ctx(tmp_path)                       # ONE workdir, as a real multi-repo run has
    run_pipreqs(RepoSpec("o/one", "https://github.com/o/one"), ctx, runner=fake_runner)
    run_pipreqs(RepoSpec("o/two", "https://github.com/o/two"), ctx, runner=fake_runner)

    assert len(set(clone_dests)) == 2, f"clone destinations collide: {clone_dests}"
    assert "o/one" in clone_dests[0] and "o/two" in clone_dests[1]
