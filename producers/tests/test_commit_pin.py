# producers/tests/test_commit_pin.py — commit-pin enforcement on EMITTED Dockerfiles.
#
# No LLM, no docker. Covers the shared inject_clone_pin helper (all clone-line shapes bench
# rebuilds) plus the dockeragent produce path (RepoSpec.commit -> checkout after the clone).
from producers.base import ProduceContext, RepoSpec, inject_clone_pin
from producers.dockeragent import DockerAgentProducer


def _ctx(tmp_path):
    return ProduceContext(llm="deepseek/deepseek-v4-flash", workdir=str(tmp_path))


def _pin_after_clone(out: str) -> int:
    """Index of the injected pin line among physical lines (asserts it exists)."""
    lines = out.split("\n")
    return next(i for i, l in enumerate(lines) if l.startswith("RUN git -C ")
                and "checkout --detach" in l)


# ── inject_clone_pin: the four real clone shapes ─────────────────────────────
def test_pin_flagged_url_then_dest_testbed():
    # Pattern 1: `--depth=1 <url> /testbed` -> dest /testbed
    df = "FROM x\nRUN git clone --depth=1 https://github.com/o/r /testbed\nWORKDIR /testbed\n"
    out, injected = inject_clone_pin(df, "abc123")
    assert injected is True
    lines = out.split("\n")
    ci = next(i for i, l in enumerate(lines) if "git clone" in l)
    assert lines[ci + 1] == (
        "RUN git -C /testbed fetch --depth 1 origin abc123 "
        "&& git -C /testbed checkout --detach abc123")
    assert out.endswith("\n")  # trailing newline preserved


def test_pin_bare_url_then_dest_testbed():
    # Pattern 2: `<url> /testbed` (no flags) -> dest /testbed
    df = "FROM x\nRUN git clone https://github.com/o/r /testbed\n"
    out, injected = inject_clone_pin(df, "abc123")
    assert injected is True
    assert "git -C /testbed checkout --detach abc123" in out


def test_pin_url_only_uses_basename_dest():
    # Pattern 3 (repo2run): `<url>.git` with NO dest -> dest = /<basename> (WORKDIR /)
    df = "FROM python:3.10\nRUN git clone https://github.com/o/r.git\nRUN cp -r /r/. /repo\n"
    out, injected = inject_clone_pin(df, "abc123")
    assert injected is True
    assert "git -C /r fetch --depth 1 origin abc123 && git -C /r checkout --detach abc123" in out
    # inserted BEFORE the cp so the re-homed /testbed carries the pinned tree
    assert out.index("checkout --detach") < out.index("cp -r /r/. /repo")


def test_pin_aiida_basename_leading_slash():
    df = "RUN git clone https://github.com/aiidateam/aiida-core.git\n"
    out, injected = inject_clone_pin(df, "deadbeef")
    assert injected is True
    assert "git -C /aiida-core checkout --detach deadbeef" in out


def test_pin_backslash_continuation_inserts_after_last_line():
    # Pattern 4: a `\`-continued RUN — insert AFTER the LAST physical line, never mid-instruction.
    df = ("FROM x\n"
          "RUN git clone --depth=1 https://github.com/o/r /testbed \\\n"
          "    && cd /testbed && echo hi\n"
          "RUN pip install .\n")
    out, injected = inject_clone_pin(df, "cafef00d")
    assert injected is True
    lines = out.split("\n")
    pin_i = _pin_after_clone(out)
    # the pin sits immediately after the continuation line, before `RUN pip install .`
    assert lines[pin_i - 1].lstrip().startswith("&& cd /testbed")
    assert lines[pin_i + 1] == "RUN pip install ."
    # the continued clone instruction is NOT broken: its first line still ends with a backslash
    clone_i = next(i for i, l in enumerate(lines) if "git clone" in l)
    assert lines[clone_i].rstrip().endswith("\\")
    assert "git -C /testbed checkout --detach cafef00d" in out


def test_pin_branch_space_form_flag_value_skipped():
    # `-b <branch>` (space form) value must not be mistaken for the URL/dest.
    df = "RUN git clone -b main --depth 1 https://github.com/o/r /testbed\n"
    out, injected = inject_clone_pin(df, "abc123")
    assert injected is True
    assert "git -C /testbed checkout --detach abc123" in out


def test_pin_no_clone_instruction_returns_false_unchanged():
    df = "FROM x\nRUN pip install .\n"
    out, injected = inject_clone_pin(df, "abc123")
    assert injected is False
    assert out == df   # byte-identical when there is nothing to pin


# ── hardening: target-match, comment-skip, compound-refuse, query-strip ──────
def test_pin_targets_repo_not_first_helper_clone():
    # A helper tool is cloned FIRST; the TARGET repo second. With repo_url given, pin the target's
    # /testbed clone, NOT the helper's /tool clone.
    df = ("FROM x\nRUN git clone https://github.com/some/helper /tool\n"
          "RUN git clone https://github.com/o/r /testbed\n")
    out, injected = inject_clone_pin(df, "abc123", "https://github.com/o/r")
    assert injected is True
    assert "git -C /testbed checkout --detach abc123" in out
    assert "git -C /tool" not in out


def test_pin_skips_comment_line_mentioning_git_clone():
    df = "FROM x\n# RUN git clone https://github.com/o/r /testbed\nCOPY . /testbed\n"
    out, injected = inject_clone_pin(df, "abc123", "https://github.com/o/r")
    assert injected is False and out == df


def test_pin_refuses_compound_clone_cp_rm_run():
    # A single RUN that clones then cp+rm the clone: pinning AFTER it would target a deleted dir —
    # refuse (visible miss), never a false pin.
    df = ("FROM x\nWORKDIR /\n"
          "RUN git clone https://github.com/o/r.git && cp -r /r/. /repo && rm -rf /r\n")
    out, injected = inject_clone_pin(df, "abc123", "https://github.com/o/r")
    assert injected is False and out == df


def test_pin_basename_strips_query_and_git():
    df = "FROM x\nWORKDIR /\nRUN git clone https://github.com/o/r.git?ref=main\n"
    out, injected = inject_clone_pin(df, "abc123", "https://github.com/o/r")
    assert injected is True
    assert "git -C /r fetch" in out and "?ref=main" not in out.split("checkout")[1]


# ── RepoSpec carries the pin ─────────────────────────────────────────────────
def test_repospec_accepts_commit_default_none():
    assert RepoSpec("o/r", "https://github.com/o/r").commit is None
    assert RepoSpec("o/r", "https://github.com/o/r", commit="deadbeef").commit == "deadbeef"


# ── dockeragent produce end-to-end (fake adapter) ────────────────────────────
class _CloneAdapter:
    """Adapter stub whose Dockerfile clones into /testbed (the line the pin targets)."""
    def __init__(self, output_dir=None):
        self.output_dir = output_dir

    def process_single_instance(self, inst, **kw):
        return {inst["instance_id"]: {
            "dockerfile": "FROM x\nRUN git clone --depth=1 https://github.com/o/r /testbed\n",
            "base_image": "python:3.11", "head_sha": "sha"}}


def test_produce_with_commit_injects_checkout(tmp_path):
    env = DockerAgentProducer(adapter_cls=_CloneAdapter).produce(
        RepoSpec("o/r", "https://github.com/o/r", commit="cafef00d"), _ctx(tmp_path))
    assert env.status == "produced"
    assert "checkout --detach cafef00d" in env.dockerfile
    assert env.dockerfile.index("git clone") < env.dockerfile.index("checkout --detach")
    assert env.note == ""            # clean injection => no warning


def test_produce_commit_but_no_clone_sets_warning(tmp_path):
    class _NoClone(_CloneAdapter):
        def process_single_instance(self, inst, **kw):
            return {inst["instance_id"]: {
                "dockerfile": "FROM x\nRUN pip install .\n", "base_image": "python:3.11"}}

    env = DockerAgentProducer(adapter_cls=_NoClone).produce(
        RepoSpec("o/r", "https://github.com/o/r", commit="cafef00d"), _ctx(tmp_path))
    assert env.status == "produced"
    assert env.note == "no git clone instruction to pin"
    assert env.economy.get("pin_warning") == "no git clone instruction to pin"


def test_produce_without_commit_leaves_dockerfile_unpinned(tmp_path):
    env = DockerAgentProducer(adapter_cls=_CloneAdapter).produce(
        RepoSpec("o/r", "https://github.com/o/r"), _ctx(tmp_path))
    assert env.status == "produced"
    assert "checkout --detach" not in env.dockerfile
    assert env.note == ""


def test_produce_passes_commit_into_adapter_instance(tmp_path):
    captured = {}

    class _Capturing(_CloneAdapter):
        def process_single_instance(self, inst, **kw):
            captured["commit"] = inst.get("commit")
            return super().process_single_instance(inst, **kw)

    DockerAgentProducer(adapter_cls=_Capturing).produce(
        RepoSpec("o/r", "https://github.com/o/r", commit="feedface"), _ctx(tmp_path))
    assert captured["commit"] == "feedface"
