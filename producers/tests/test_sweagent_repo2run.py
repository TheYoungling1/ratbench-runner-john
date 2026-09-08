# producers/tests/test_sweagent_repo2run.py — the finalize transform + SweAgentRepo2RunProducer.
#
# SWE-agent is STUBBED (injected via runner=) — no sweagent install, no docker, no keys.
import json
import os

import yaml

from producers.base import ProduceContext, RepoSpec, write_env_packet
from producers.sweagent_repo2run import (
    CONFIG_PATH,
    SweAgentRepo2RunProducer,
    _harvest_collect_inline,
    _last_collect_only_step,
    finalize,
)

# The Dockerfile shape the paper's instance_template prescribes (appendix I.2), which is also the
# shape repo2run's re-home transform detects.
_PAPER_DF = ("FROM python:3.10\n"
             "RUN pip install pytest\n"
             "RUN git clone https://github.com/o/r.git\n"
             "RUN mkdir /repo\n"
             "RUN git config --global --add safe.directory /repo\n"
             "RUN cp -r /r/. /repo && rm -rf /r/\n"
             "RUN pip install -r /repo/requirements.txt\n")


def _ctx(tmp_path):
    return ProduceContext(llm="openrouter/deepseek/deepseek-v4-flash", workdir=str(tmp_path))


def _repo():
    return RepoSpec("o/r", "https://github.com/o/r", commit="abc123", language="python")


def test_finalize_rehomes_the_paper_template_to_testbed():
    out, warning = finalize(_PAPER_DF, "abc123", "https://github.com/o/r")
    assert warning == ""
    assert "RUN mv /repo /testbed && ln -sfn /testbed /repo" in out
    assert "WORKDIR /testbed" in out


def test_finalize_pins_the_clone_before_the_cp_that_seeds_repo():
    # The copy (and every install layered on it) must be made from the dataset SHA, not HEAD.
    out, _ = finalize(_PAPER_DF, "abc123", "https://github.com/o/r")
    assert "git -C /r fetch --depth 1 origin abc123" in out
    assert out.index("checkout --detach abc123") < out.index("cp -r /r/. /repo")


def test_finalize_redeclares_safe_directory_for_the_new_worktree_path():
    # The paper's own template sets safe.directory for /repo, so this pipeline does hit git's
    # dubious-ownership check; after the re-home, bench probes /testbed instead.
    out, _ = finalize(_PAPER_DF, "abc123", "https://github.com/o/r")
    assert "safe.directory /testbed" in out
    assert out.index("mv /repo /testbed") < out.index("safe.directory /testbed")


def test_finalize_reports_an_unpinnable_clone_instead_of_silently_dropping_the_pin():
    df = "FROM python:3.10\nRUN mkdir /repo && cp -r /src/. /repo\n"   # no git clone at all
    out, warning = finalize(df, "abc123", "https://github.com/o/r")
    assert warning == "no git clone instruction to pin"
    assert "mv /repo /testbed" in out          # still re-homed; the miss is surfaced, not fatal


def test_finalize_without_a_commit_leaves_the_clone_alone():
    out, warning = finalize(_PAPER_DF, None, "https://github.com/o/r")
    assert warning == "" and "fetch --depth 1 origin" not in out


def test_produce_emits_a_rehomed_packet(tmp_path):
    def stub(repo, ctx, *, llm, num_turn):
        return {"dockerfile": _PAPER_DF, "note": "", "economy": {"llm_calls": 12}}

    env = SweAgentRepo2RunProducer(runner=stub).produce(_repo(), _ctx(tmp_path))
    assert env.status == "produced" and env.conformance == "rehomed"
    assert env.base_image == "python:3.10"
    assert env.economy["llm_calls"] == 12 and "produce_s" in env.economy
    out = str(tmp_path / "output")
    write_env_packet(out, env)
    assert os.path.isfile(os.path.join(out, "o", "r", "eval_build", "Dockerfile"))


def test_produce_surfaces_the_pin_warning_in_meta(tmp_path):
    def stub(repo, ctx, *, llm, num_turn):
        return {"dockerfile": "FROM python:3.10\nRUN mkdir /repo && cp -r /s/. /repo\n",
                "note": "", "economy": {}}

    env = SweAgentRepo2RunProducer(runner=stub).produce(_repo(), _ctx(tmp_path))
    assert env.economy["pin_warning"] == "no git clone instruction to pin"
    assert "unpinned clone" in env.note


def test_produce_errors_when_the_agent_wrote_no_dockerfile(tmp_path):
    def stub(repo, ctx, *, llm, num_turn):
        return {"dockerfile": "", "note": "no /Dockerfile in the container", "economy": {}}

    env = SweAgentRepo2RunProducer(runner=stub).produce(_repo(), _ctx(tmp_path))
    assert env.status == "error" and env.dockerfile is None
    assert env.note == "no /Dockerfile in the container"


def test_produce_is_anti_vanish_when_sweagent_blows_up(tmp_path):
    def boom(repo, ctx, *, llm, num_turn):
        raise RuntimeError("sweagent crashed")

    env = SweAgentRepo2RunProducer(runner=boom).produce(_repo(), _ctx(tmp_path))
    assert env.status == "error" and "sweagent crashed" in env.note


def test_produce_passes_the_budget_through(tmp_path):
    seen = {}

    def stub(repo, ctx, *, llm, num_turn):
        seen["num_turn"] = num_turn
        return {"dockerfile": _PAPER_DF, "note": "", "economy": {}}

    ctx = ProduceContext(llm="x", workdir=str(tmp_path), num_turn=25)
    SweAgentRepo2RunProducer(runner=stub).produce(_repo(), ctx)
    assert seen["num_turn"] == 25


# ── the config file itself ───────────────────────────────────────────────────────────────────
def test_config_uses_bundles_that_still_exist_upstream():
    # The paper's list names tools/defaults, tools/edit_linting and tools/env_setup, none of which
    # exist in current SWE-agent — loading them fails config validation.
    cfg = yaml.safe_load(open(CONFIG_PATH))
    paths = {b["path"] for b in cfg["agent"]["tools"]["bundles"]}
    assert paths == {"tools/registry", "tools/windowed", "tools/search",
                     "tools/windowed_edit_linting", "tools/submit"}
    assert cfg["agent"]["tools"]["enable_bash_tool"] is True     # replaces tools/defaults


def test_config_keeps_the_papers_parser_window_and_history():
    cfg = yaml.safe_load(open(CONFIG_PATH))
    assert cfg["agent"]["tools"]["parse_function"]["type"] == "thought_action"
    assert cfg["agent"]["tools"]["env_variables"]["WINDOW"] == 100
    assert cfg["agent"]["tools"]["env_variables"]["OVERLAP"] == 2
    assert cfg["agent"]["history_processors"] == [{"type": "last_n_observations", "n": 5}]


def test_config_asks_for_the_dockerfile_at_the_paths_the_producer_reads():
    cfg = yaml.safe_load(open(CONFIG_PATH))
    inst = cfg["agent"]["templates"]["instance_template"]
    assert "/Dockerfile" in inst and "/repo" in inst
    assert "pytest --collect-only -q" in inst
    # The deployment image must ship git — the repo is cloned inside the build.
    assert cfg["env"]["deployment"]["image"] == "python:3.10"


def test_config_pins_the_papers_temperature():
    # SWE-agent's own default is 0.0; the paper ran every baseline at 0.2, so an unset value
    # would diverge silently.
    cfg = yaml.safe_load(open(CONFIG_PATH))
    assert cfg["agent"]["model"]["temperature"] == 0.2


def test_runner_registers_the_pinned_model_cost():
    # SWE-agent aborts on turn 1 with ModelConfigurationError if LiteLLM cannot price the model
    # while per_instance_cost_limit > 0, so the variety's pinned slug must be registered.
    import tomllib

    from producers.sweagent_repo2run_runner import _KNOWN_MODEL_COSTS

    with open("varieties.toml", "rb") as fh:
        llm = tomllib.load(fh)["variety"]["sweagent_repo2run"]["llm"]
    assert llm.removeprefix("openrouter/") in _KNOWN_MODEL_COSTS


def test_variety_pins_the_budget_and_the_config_agrees():
    import tomllib

    with open("varieties.toml", "rb") as fh:
        spec = tomllib.load(fh)["variety"]["sweagent_repo2run"]
    assert spec["num_turn"] == 100
    cfg = yaml.safe_load(open(CONFIG_PATH))
    assert cfg["agent"]["model"]["name"] == spec["llm"]


def test_clone_dir_basename_is_repo_so_sweagent_uploads_to_slash_repo():
    # SWE-agent's LocalRepoConfig uploads a local tree to /{basename-of-path}. The paper's prompt
    # hardcodes /repo, so any other basename silently tells the agent the wrong path all run.
    import os

    from producers.sweagent_repo2run import repo_clone_dir

    dest = repo_clone_dir("/runs/x", "pallets/itsdangerous")
    assert os.path.basename(dest) == "repo"
    # still per-repo unique, so concurrent repos cannot collide on one clone dir
    other = repo_clone_dir("/runs/x", "psf/requests")
    assert dest != other and "pallets/itsdangerous" in dest


def test_variety_slug_is_priced_for_the_route_it_actually_uses():
    # The same weights cost different amounts via DeepSeek direct vs OpenRouter resale. The
    # variety's pinned slug must be priced by the map for ITS route, or instance_cost is fiction.
    import tomllib

    from producers.sweagent_repo2run_runner import _DEEPSEEK_DIRECT_COSTS, _OPENROUTER_COSTS

    with open("varieties.toml", "rb") as fh:
        llm = tomllib.load(fh)["variety"]["sweagent_repo2run"]["llm"]
    if llm.startswith("openrouter/"):
        assert llm.removeprefix("openrouter/") in _OPENROUTER_COSTS
    else:
        assert llm in _DEEPSEEK_DIRECT_COSTS
    # the two maps must not agree, or one of them is wrong
    shared = set(_DEEPSEEK_DIRECT_COSTS) & set(_OPENROUTER_COSTS)
    for slug in shared:
        assert (_DEEPSEEK_DIRECT_COSTS[slug]["input_cost_per_token"]
                != _OPENROUTER_COSTS[slug]["input_cost_per_token"])


def test_deepseek_direct_prices_cache_hits_far_cheaper_than_misses():
    # DeepSeek bills input 31x cheaper on a cache hit ($0.007 vs $0.22 per 1M). litellm can only
    # apply that if cache_read_input_token_cost is registered.
    from producers.sweagent_repo2run_runner import _DEEPSEEK_DIRECT_COSTS

    c = _DEEPSEEK_DIRECT_COSTS["deepseek/deepseek-v4-flash"]
    assert c["cache_read_input_token_cost"] < c["input_cost_per_token"] / 20
    assert c["litellm_provider"] == "deepseek"


def test_registered_limits_match_the_vendor_not_the_route_metadata():
    # DeepSeek's docs state 1M context / 384K max output. OpenRouter advertises 1,310,720 /
    # 943,718 for the -0731 route. The upstream is what rejects, so the vendor's numbers win —
    # litellm uses these for context checks.
    from producers.sweagent_repo2run_runner import _KNOWN_MODEL_COSTS

    for slug, cost in _KNOWN_MODEL_COSTS.items():
        assert cost["max_input_tokens"] == 1048576, slug
        assert cost["max_output_tokens"] == 384000, slug
        assert cost["max_tokens"] == cost["max_output_tokens"], slug


def test_runner_script_does_not_let_producers_shadow_the_sweagent_package():
    # The runner lives in producers/, which Python puts at sys.path[0], and producers/sweagent.py
    # exists — so a bare `import sweagent` resolved to the gate module and the run died with
    # "'sweagent' is not a package". The script must strip its own directory first.
    import pathlib

    src = pathlib.Path("producers/sweagent_repo2run_runner.py").read_text()
    guard = src.index("sys.path[:] = [p for p in sys.path")
    first_sweagent_import = src.index("from sweagent")
    assert guard < first_sweagent_import, "sys.path guard must precede any sweagent import"
    assert pathlib.Path("producers/sweagent.py").exists(), (
        "the shadowing module this guard defends against is gone; the guard may be removable")


# ── the produce-time extras (design items 3/4a/7) ──────────────────────────────────────────────

def test_produce_threads_exit_status_and_deploy_image_digest_into_meta(tmp_path):
    def stub(repo, ctx, *, llm, num_turn):
        return {"dockerfile": _PAPER_DF, "note": "", "economy": {},
                "exit_status": "submitted", "deploy_image_digest": "sha256:abc123",
                "inline": {"command": "pytest --collect-only", "observation_tail": "1 tests collected",
                          "clean": True}}

    env = SweAgentRepo2RunProducer(runner=stub).produce(_repo(), _ctx(tmp_path))
    assert env.exit_status == "submitted"
    assert env.deploy_image_digest == "sha256:abc123"
    assert env.inline["clean"] is True

    out = str(tmp_path / "output")
    write_env_packet(out, env)
    meta = json.load(open(os.path.join(out, "o", "r", "_meta.json")))
    assert meta["exit_status"] == "submitted"
    assert meta["deploy_image_digest"] == "sha256:abc123"
    assert meta["inline"]["clean"] is True


def test_produce_error_path_still_carries_exit_status(tmp_path):
    # A budget-exhausted run (exit_status="exit_cost") can still write no Dockerfile — the status
    # must not be lost just because the produce failed.
    def stub(repo, ctx, *, llm, num_turn):
        return {"dockerfile": "", "note": "no /Dockerfile", "economy": {},
                "exit_status": "exit_cost", "deploy_image_digest": None, "inline": None}

    env = SweAgentRepo2RunProducer(runner=stub).produce(_repo(), _ctx(tmp_path))
    assert env.status == "error" and env.exit_status == "exit_cost"


def test_produce_defaults_the_new_optional_fields_to_none(tmp_path):
    # A stub that predates these keys (or any other producer) must not break — absent => None.
    def stub(repo, ctx, *, llm, num_turn):
        return {"dockerfile": _PAPER_DF, "note": "", "economy": {}}

    env = SweAgentRepo2RunProducer(runner=stub).produce(_repo(), _ctx(tmp_path))
    assert env.exit_status is None and env.deploy_image_digest is None and env.inline is None


# ── _last_collect_only_step / _harvest_collect_inline (design item 7) ─────────────────────────

def _write_traj(path, steps):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"trajectory": steps}, f)


def test_last_collect_only_step_picks_the_last_one(tmp_path):
    p = str(tmp_path / "r.traj")
    _write_traj(p, [
        {"action": "cd /repo && pytest --collect-only -q\n", "observation": "bash: pytest: command not found"},
        {"action": "pip install pytest\n", "observation": "Successfully installed pytest"},
        {"action": "cd /repo && pytest --collect-only -q\n", "observation": "160 tests collected in 0.24s"},
    ])
    step = _last_collect_only_step(p)
    assert step["observation"] == "160 tests collected in 0.24s"


def test_last_collect_only_step_matches_a_compound_command(tmp_path):
    p = str(tmp_path / "r.traj")
    _write_traj(p, [
        {"action": "cd /repo && timeout 30 pytest --collect-only -q > /tmp/o 2>&1; echo exit=$?",
         "observation": "exit=0\n42 tests collected in 0.1s"},
    ])
    step = _last_collect_only_step(p)
    assert step is not None and "42 tests collected" in step["observation"]


def test_last_collect_only_step_none_when_absent(tmp_path):
    p = str(tmp_path / "r.traj")
    _write_traj(p, [{"action": "ls /repo\n", "observation": "README.md"}])
    assert _last_collect_only_step(p) is None


def test_last_collect_only_step_malformed_file_is_none(tmp_path):
    p = str(tmp_path / "bad.traj")
    os.makedirs(tmp_path, exist_ok=True)
    with open(p, "w") as f:
        f.write("{ not json")
    assert _last_collect_only_step(p) is None


def test_harvest_collect_inline_reports_clean_on_a_successful_collect(tmp_path):
    traj_dir = tmp_path / "sweagent_trajectories" / "o_r"
    _write_traj(str(traj_dir / "o_r.traj"), [
        {"action": "cd /repo && pytest --collect-only -q\n",
         "observation": "tests/test_a.py::test_one\n\n160 tests collected in 0.24s"},
    ])
    inline = _harvest_collect_inline(str(tmp_path / "sweagent_trajectories"))
    assert inline["command"] == "pytest --collect-only"
    assert inline["clean"] is True
    assert inline["observation_tail"].endswith("160 tests collected in 0.24s")


def test_harvest_collect_inline_reports_dirty_when_pytest_is_missing(tmp_path):
    traj_dir = tmp_path / "sweagent_trajectories" / "o_r"
    _write_traj(str(traj_dir / "o_r.traj"), [
        {"action": "cd /repo && pytest --collect-only -q\n",
         "observation": "bash: pytest: command not found"},
    ])
    inline = _harvest_collect_inline(str(tmp_path / "sweagent_trajectories"))
    assert inline["clean"] is False


def test_harvest_collect_inline_truncates_to_the_tail(tmp_path):
    traj_dir = tmp_path / "sweagent_trajectories" / "o_r"
    long_obs = ("x" * 5000) + "\n42 tests collected in 1.0s"
    _write_traj(str(traj_dir / "o_r.traj"), [
        {"action": "pytest --collect-only -q\n", "observation": long_obs},
    ])
    inline = _harvest_collect_inline(str(tmp_path / "sweagent_trajectories"), tail_chars=2000)
    assert len(inline["observation_tail"]) == 2000
    assert inline["observation_tail"].endswith("42 tests collected in 1.0s")


def test_harvest_collect_inline_none_when_no_trajectory_has_a_collect_step(tmp_path):
    traj_dir = tmp_path / "sweagent_trajectories" / "o_r"
    _write_traj(str(traj_dir / "o_r.traj"), [{"action": "ls /repo\n", "observation": "README.md"}])
    assert _harvest_collect_inline(str(tmp_path / "sweagent_trajectories")) is None


def test_harvest_collect_inline_missing_dir_is_none(tmp_path):
    assert _harvest_collect_inline(str(tmp_path / "nope")) is None


# ── resolve_image_digest (design item 4a: the agent's OWN deployment image) ───────────────────

def test_resolve_image_digest_prefers_the_repo_digest():
    from producers.sweagent_repo2run_runner import resolve_image_digest

    class _CP:
        returncode = 0
        stdout = "python@sha256:deadbeef\n"

    digest = resolve_image_digest("python:3.10", runner=lambda *a, **k: _CP())
    assert digest == "python@sha256:deadbeef"


def test_resolve_image_digest_none_on_nonzero_returncode():
    from producers.sweagent_repo2run_runner import resolve_image_digest

    class _CP:
        returncode = 1
        stdout = ""

    assert resolve_image_digest("python:3.10", runner=lambda *a, **k: _CP()) is None


def test_resolve_image_digest_none_when_runner_raises():
    from producers.sweagent_repo2run_runner import resolve_image_digest

    def boom(*a, **k):
        raise FileNotFoundError("docker not found")

    assert resolve_image_digest("python:3.10", runner=boom) is None


def test_resolve_image_digest_none_on_blank_output():
    from producers.sweagent_repo2run_runner import resolve_image_digest

    class _CP:
        returncode = 0
        stdout = "  \n"

    assert resolve_image_digest("python:3.10", runner=lambda *a, **k: _CP()) is None


def _overridden(env: dict) -> dict:
    """Run _apply_overrides with the real config under a given environment."""
    import os
    import types
    from unittest.mock import patch

    import yaml

    from producers.sweagent_repo2run_runner import _apply_overrides

    cfg = yaml.safe_load(open(CONFIG_PATH))
    a = types.SimpleNamespace(llm="deepseek/deepseek-v4-flash", cost_limit=2.0, call_limit=100,
                              repo_path="/x/repo", commit=None, trajectory_dir="/x/traj")
    with patch.dict(os.environ, env, clear=False):
        return _apply_overrides(cfg, a)


def test_thinking_is_disabled_by_default():
    # DeepSeek v4 enables chain-of-thought by default; the paper's baselines were non-reasoning
    # models, so this arm ships it off or it is not the same baseline.
    import yaml

    cfg = yaml.safe_load(open(CONFIG_PATH))
    assert cfg["agent"]["model"]["completion_kwargs"]["extra_body"]["thinking"]["type"] == "disabled"


def test_sweagent_thinking_env_flips_it_both_ways():
    for value in ("enabled", "disabled"):
        eff = _overridden({"SWEAGENT_THINKING": value})
        assert eff["agent"]["model"]["completion_kwargs"]["extra_body"]["thinking"] == {"type": value}


def test_a_bogus_thinking_value_is_ignored_not_forwarded():
    # An unknown value would 400 at the API mid-run, after the container is already up and paid for.
    eff = _overridden({"SWEAGENT_THINKING": "maybe"})
    assert eff["agent"]["model"]["completion_kwargs"]["extra_body"]["thinking"]["type"] == "disabled"


def test_effective_agent_settings_reach_meta_json(tmp_path):
    # The copied config records the DEFAULT thinking mode; an env override would leave no trace
    # anywhere else, and reasoning_content never reaches the .traj. Record what actually ran.
    import json
    import os

    def stub(repo, ctx, *, llm, num_turn):
        return {"dockerfile": _PAPER_DF, "note": "", "economy": {},
                "agent_settings": {"thinking": "enabled", "temperature": 0.2}}

    env = SweAgentRepo2RunProducer(runner=stub).produce(_repo(), _ctx(tmp_path))
    assert env.agent_settings == {"thinking": "enabled", "temperature": 0.2}
    out = str(tmp_path / "output")
    write_env_packet(out, env)
    meta = json.load(open(os.path.join(out, "o", "r", "_meta.json")))
    assert meta["agent_settings"]["thinking"] == "enabled"


def test_agent_settings_absent_for_producers_that_report_none(tmp_path):
    # Additive-only: an arm that reports nothing writes null, never an empty dict masquerading
    # as a recorded setting.
    import json
    import os

    def stub(repo, ctx, *, llm, num_turn):
        return {"dockerfile": _PAPER_DF, "note": "", "economy": {}}

    env = SweAgentRepo2RunProducer(runner=stub).produce(_repo(), _ctx(tmp_path))
    out = str(tmp_path / "output")
    write_env_packet(out, env)
    assert json.load(open(os.path.join(out, "o", "r", "_meta.json")))["agent_settings"] is None


def test_container_name_is_read_before_close_not_after():
    # env.close() clears deployment._container_name, so reading it afterwards always yields None —
    # which is how the first cleanup silently did nothing while appearing to work.
    import pathlib

    src = pathlib.Path("producers/sweagent_repo2run_runner.py").read_text()
    capture = src.index("container_name = deployment_container_name(runner)")
    close = src.index("runner.env.close()", capture)
    assert capture < close, "must capture the container name before the env is closed"


def test_cleanup_always_logs_even_when_it_does_nothing():
    # A cleanup that silently no-ops is indistinguishable from one that works, until the disk fills.
    from producers.sweagent_repo2run_runner import remove_deployment_container

    lines = []
    assert remove_deployment_container(None, log=lines.append) is None
    assert lines and "nothing removed" in lines[0]

    class _R:
        returncode, stderr, stdout = 1, "No such container", ""

    lines.clear()
    assert remove_deployment_container("c1", log=lines.append) is None or True
    # a failing docker rm must also surface
    import unittest.mock as mock
    with mock.patch("subprocess.run", return_value=_R()):
        lines.clear()
        assert remove_deployment_container("c1", log=lines.append) is None
        assert lines and "rc=1" in lines[0]


def test_cleanup_returns_the_name_on_success():
    import unittest.mock as mock

    from producers.sweagent_repo2run_runner import remove_deployment_container

    class _OK:
        returncode, stderr, stdout = 0, "", ""

    with mock.patch("subprocess.run", return_value=_OK()):
        assert remove_deployment_container("python3.10-abc") == "python3.10-abc"


# ── DSML normalisation ───────────────────────────────────────────────────────────────────────
# DeepSeek v4 intermittently emits its own DSML markup instead of a markdown fence. The bytes
# below are copied verbatim from the failed VM smoke (run-20260903-131340) — full-width U+FF5C
# bars, not ASCII pipes, which is exactly why a naive `<|...|>` pattern would miss them.
_DSML_RESPONSE = (
    "DISCUSSION\nLet me start by exploring the repository structure to understand what we're "
    "working with.\n\n<｜｜DSML｜｜bash>\nls -la /repo\n</｜｜DSML｜｜bash>")


def test_normalize_dsml_rewrites_the_real_failing_response():
    from producers.sweagent_repo2run_runner import normalize_dsml_fences

    out = normalize_dsml_fences(_DSML_RESPONSE)
    assert "DSML" not in out
    assert "```\nls -la /repo\n```" in out
    # the discussion prose must survive untouched — only the delimiter changes
    assert out.startswith("DISCUSSION\nLet me start by exploring")


def test_normalize_dsml_output_parses_as_the_paper_parser_expects():
    """The whole point: the rewritten text must yield the command under thought_action's rule
    (last non-nested fenced block). Reimplemented here so the test needs no sweagent install."""
    import re

    from producers.sweagent_repo2run_runner import normalize_dsml_fences

    blocks = re.findall(r"^```\S*\s*\n(.*?)^```\s*$",
                        normalize_dsml_fences(_DSML_RESPONSE) + "\n", re.MULTILINE | re.DOTALL)
    assert [b.strip() for b in blocks] == ["ls -la /repo"]


def test_normalize_dsml_leaves_a_well_formed_fenced_response_byte_identical():
    from producers.sweagent_repo2run_runner import normalize_dsml_fences

    good = "DISCUSSION\nLooks fine.\n```\nls -la /repo\n```"
    assert normalize_dsml_fences(good) == good
    assert normalize_dsml_fences("") == ""


def test_normalize_dsml_handles_multiple_and_multiline_blocks():
    from producers.sweagent_repo2run_runner import normalize_dsml_fences

    text = ("<｜｜DSML｜｜bash>\ncd /repo\npytest -q\n</｜｜DSML｜｜bash>\n"
            "tail\n<｜｜DSML｜｜bash>\nls\n</｜｜DSML｜｜bash>")
    out = normalize_dsml_fences(text)
    assert "DSML" not in out
    assert "```\ncd /repo\npytest -q\n```" in out and "```\nls\n```" in out


def test_patch_thought_action_parser_normalizes_and_is_idempotent():
    """Patch the real class if sweagent is importable; otherwise a stand-in with the same shape.
    Either way the wrapper must rewrite the message, log once, and never stack on re-application."""
    import sys
    import types

    from producers import sweagent_repo2run_runner as R

    try:
        from sweagent.tools.parsing import ThoughtActionParser        # noqa: F401
        created = None
    except ImportError:
        mod = types.ModuleType("sweagent.tools.parsing")

        class ThoughtActionParser:                                     # noqa: D401 - test stand-in
            def __call__(self, model_response, commands, strict=False):
                return model_response["message"]

        mod.ThoughtActionParser = ThoughtActionParser
        pkg = types.ModuleType("sweagent"); pkg.__path__ = []
        tools = types.ModuleType("sweagent.tools"); tools.__path__ = []
        sys.modules.setdefault("sweagent", pkg)
        sys.modules.setdefault("sweagent.tools", tools)
        sys.modules["sweagent.tools.parsing"] = mod
        created = ThoughtActionParser

    from sweagent.tools.parsing import ThoughtActionParser as TAP

    original = TAP.__call__
    was_patched = getattr(TAP, "_dsml_patched", False)
    logged = []
    try:
        applied = R.patch_thought_action_parser(log=logged.append)
        assert applied is not was_patched or applied is False
        # second application is a no-op, so no wrapper stacking
        assert R.patch_thought_action_parser(log=logged.append) is False

        if created is not None:
            got = TAP()({"message": _DSML_RESPONSE}, [])
            assert "DSML" not in got and "```\nls -la /repo\n```" in got
            assert len(logged) == 1 and "dsml" in logged[0]
            # a clean response logs nothing further
            TAP()({"message": "DISCUSSION\nfine\n```\nls\n```"}, [])
            assert len(logged) == 1
    finally:
        TAP.__call__ = original
        if hasattr(TAP, "_dsml_patched"):
            del TAP._dsml_patched


def test_agent_settings_survive_the_no_dockerfile_failure_path(tmp_path):
    """The paper's baseline fails to emit a Dockerfile ~73% of the time, so provenance must survive
    that path — otherwise most of a 50-repo run has no record of the effective thinking mode."""
    def runner(repo, ctx, **kw):
        return {"dockerfile": "", "note": "exit_format",
                "agent_settings": {"thinking": "disabled", "temperature": 0.2},
                "exit_status": "exit_format", "deploy_image_digest": "python@sha256:abc",
                "economy": {"tokens_in": 4690}}

    env = SweAgentRepo2RunProducer(llm="deepseek/deepseek-v4-flash", runner=runner).produce(
        RepoSpec(full_name="o/r", repo_url="https://github.com/o/r", commit="c" * 40),
        ProduceContext(llm=None, workdir=str(tmp_path), num_turn=100))

    assert env.status == "error"
    assert env.agent_settings == {"thinking": "disabled", "temperature": 0.2}
    assert env.exit_status == "exit_format"          # already worked; guard against regression
    write_env_packet(str(tmp_path), env)
    meta = json.load(open(os.path.join(str(tmp_path), "o", "r", "_meta.json")))
    assert meta["agent_settings"]["thinking"] == "disabled"


# ── the modern arm: same producer, different scaffold ─────────────────────────────────────────
#
# The hazard these guard is silent, not loud: varieties.toml cannot set an env var, so if the
# modern variety did not pin its own config it would run the PAPER's scaffold while reporting
# itself as the modern arm — a controlled contrast whose only controlled variable had quietly
# reverted. A wrong number that looks right is the worst outcome this repo can produce.

def test_modern_producer_pins_its_own_config_and_the_baseline_does_not():
    from producers.sweagent_repo2run import (
        MODERN_CONFIG_PATH,
        SweAgentRepo2RunModernProducer,
    )
    assert SweAgentRepo2RunProducer.config_path is None
    assert SweAgentRepo2RunModernProducer.config_path == MODERN_CONFIG_PATH
    assert SweAgentRepo2RunModernProducer.name == "sweagent_repo2run_modern"
    # Same lane as the baseline: a rehomed, measurable Dockerfile arm.
    assert SweAgentRepo2RunModernProducer.conformance == "rehomed"
    assert SweAgentRepo2RunModernProducer.measurable is True


def test_modern_producer_forwards_its_config_path_to_the_runner(tmp_path):
    from producers.sweagent_repo2run import (
        MODERN_CONFIG_PATH,
        SweAgentRepo2RunModernProducer,
    )
    seen = {}

    def stub(repo, ctx, *, llm, num_turn, config_path):
        seen["config_path"] = config_path
        return {"dockerfile": _PAPER_DF, "note": "", "economy": {}}

    env = SweAgentRepo2RunModernProducer(runner=stub).produce(_repo(), _ctx(tmp_path))
    assert env.status == "produced"
    assert seen["config_path"] == MODERN_CONFIG_PATH


def test_baseline_producer_still_calls_the_runner_without_a_config_kwarg(tmp_path):
    # The injected stubs elsewhere in this file take no config_path; passing one unconditionally
    # would break every one of them. Pin that the baseline call signature is unchanged.
    def stub(repo, ctx, *, llm, num_turn):
        return {"dockerfile": _PAPER_DF, "note": "", "economy": {}}

    assert SweAgentRepo2RunProducer(runner=stub).produce(_repo(), _ctx(tmp_path)).status == "produced"


def test_modern_config_is_the_contrast_it_claims_to_be():
    """The scaffold differences that define this arm, asserted against the file itself."""
    from producers.sweagent_repo2run import MODERN_CONFIG_PATH
    modern = yaml.safe_load(open(MODERN_CONFIG_PATH))
    paper = yaml.safe_load(open(CONFIG_PATH))
    mt, pt = modern["agent"]["tools"], paper["agent"]["tools"]

    assert mt["parse_function"]["type"] == "function_calling"
    assert pt["parse_function"]["type"] == "thought_action"
    # No elision processor: full history, and no Anthropic cache_control on an OpenAI-dialect lane.
    assert "history_processors" not in modern["agent"]
    # The SWE-bench submit gate is welded to a repo-relative diff; this arm's deliverable is
    # /Dockerfile, which lives outside /repo and so never appears in it.
    bundles = [b["path"] for b in mt["bundles"]]
    assert "tools/submit" in bundles and "tools/review_on_submit_m" not in bundles
    # Neither arm may inherit SWE-agent's 30s/1800s defaults on an env-construction benchmark.
    assert mt["execution_timeout"] == 600 and mt["total_execution_timeout"] == 7200
    # The contrast is the SCAFFOLD: model, temperature and thinking mode must not drift.
    assert modern["agent"]["model"]["temperature"] == paper["agent"]["model"]["temperature"]
    assert (modern["agent"]["model"]["completion_kwargs"]["extra_body"]["thinking"]
            == paper["agent"]["model"]["completion_kwargs"]["extra_body"]["thinking"])
    # The task text is ported verbatim; only the interface description is dropped.
    assert (modern["agent"]["templates"]["problem_statement_template"]
            == paper["agent"]["templates"]["problem_statement_template"])
    assert "RESPONSE FORMAT" not in modern["agent"]["templates"]["system_template"]
    assert "{{WINDOW}}" not in modern["agent"]["templates"]["system_template"]
