# The `sweagent_repo2run_modern_graph` arm: the modern baseline's prompt and Dockerfile
# deliverable, plus ONE {{context_block}} line carrying the Graph2Env graph + compiled setup.sh.
# Everything here guards that the arm deviates from `sweagent_repo2run_modern` by that line only.
import copy
import json
import os

import yaml

from producers.base import ProduceContext, RepoSpec
from producers.sweagent_repo2run import (
    GRAPH_CONFIG_PATH,
    MODERN_CONFIG_PATH,
    SweAgentRepo2RunGraphProducer,
    graph_spec_path,
)
from producers.sweagent_repo2run_runner import apply_setup_spec, result_status

_DF = ("FROM python:3.10\nRUN pip install pytest\nRUN git clone https://github.com/o/r.git\n"
       "RUN mkdir /repo\nRUN cp -r /r/. /repo && rm -rf /r/\n")


def _load(path):
    with open(path) as fh:
        return yaml.safe_load(fh)


def test_graph_config_differs_from_modern_only_by_one_context_line():
    modern, graph = _load(MODERN_CONFIG_PATH), _load(GRAPH_CONFIG_PATH)
    m_tpl = modern["agent"]["templates"].pop("instance_template")
    g_tpl = graph["agent"]["templates"].pop("instance_template")
    assert graph == modern, "everything but instance_template must be byte-for-byte the baseline's"
    assert g_tpl == m_tpl + "\n{{context_block}}"


def test_dockerfile_deliverable_spec_only_substitutes_the_template():
    cfg = _load(GRAPH_CONFIG_PATH)
    before = copy.deepcopy(cfg)
    spec = {"deliverable": "dockerfile", "context_block": "\nADDITIONAL CONTEXT. x\n",
            "g2e_dir": "/tmp/g2e"}
    out = apply_setup_spec(cfg, spec)
    assert cfg == before, "must not mutate its input"
    assert out["env"] == before["env"], "deployment must stay the baseline's"
    assert out["agent"]["model"] == before["agent"]["model"], "model must stay the baseline's"
    rendered = out["agent"]["templates"]["instance_template"]
    modern = _load(MODERN_CONFIG_PATH)["agent"]["templates"]["instance_template"]
    assert rendered == modern + "\n" + spec["context_block"]


def test_result_status_setup_mode_still_credits_a_readable_setup():
    assert result_status("", {"artifacts_dir": "a"}, "") == "produced"
    assert result_status("", {"artifacts_dir": "a"}, "setup.sh: boom") == "error"


def test_result_status_dockerfile_mode_needs_a_dockerfile():
    spec = {"deliverable": "dockerfile"}
    assert result_status("", spec, "") == "error"
    assert result_status("FROM x\n", spec, "") == "produced"
    assert result_status("FROM x\n", None, "") == "produced"
    assert result_status("", None, "") == "error"


def _repo():
    return RepoSpec("o/r", "https://github.com/o/r", commit="a" * 40)


def test_graph_spec_path_is_per_repo(tmp_path):
    assert graph_spec_path(str(tmp_path), "o/r") == os.path.join(str(tmp_path), "o", "r", "spec.json")


def test_graph_producer_passes_its_config_and_the_repo_spec(tmp_path, monkeypatch):
    spec = tmp_path / "o" / "r" / "spec.json"
    spec.parent.mkdir(parents=True)
    spec.write_text(json.dumps({"deliverable": "dockerfile", "context_block": "c"}))
    monkeypatch.setenv("SWEAGENT_GRAPH_SPEC_ROOT", str(tmp_path))
    seen = {}

    def stub(repo, ctx, *, llm, num_turn, config_path, setup_spec):
        seen.update(config_path=config_path, setup_spec=setup_spec)
        return {"dockerfile": _DF, "note": "", "economy": {}}

    env = SweAgentRepo2RunGraphProducer(runner=stub).produce(
        _repo(), ProduceContext(llm="x", workdir=str(tmp_path / "w"), num_turn=100))
    assert env.status == "produced" and env.producer_name == "sweagent_repo2run_modern_graph"
    assert seen == {"config_path": GRAPH_CONFIG_PATH, "setup_spec": str(spec)}


def test_graph_producer_refuses_to_run_without_its_context(tmp_path, monkeypatch):
    # Running this arm with no context would silently produce a second baseline row.
    monkeypatch.setenv("SWEAGENT_GRAPH_SPEC_ROOT", str(tmp_path))
    called = []

    def stub(*a, **k):
        called.append(1)
        return {"dockerfile": _DF, "note": "", "economy": {}}

    env = SweAgentRepo2RunGraphProducer(runner=stub).produce(
        _repo(), ProduceContext(llm="x", workdir=str(tmp_path / "w"), num_turn=100))
    assert env.status == "error" and "spec.json" in env.note and not called


def test_graph_producer_refuses_an_unset_spec_root(tmp_path, monkeypatch):
    monkeypatch.delenv("SWEAGENT_GRAPH_SPEC_ROOT", raising=False)
    env = SweAgentRepo2RunGraphProducer(runner=lambda *a, **k: {}).produce(
        _repo(), ProduceContext(llm="x", workdir=str(tmp_path), num_turn=100))
    assert env.status == "error" and "SWEAGENT_GRAPH_SPEC_ROOT" in env.note


def test_graph_arm_is_registered_like_the_modern_arm():
    import producers
    from runner import benchmark, manifest

    assert isinstance(producers.get("sweagent_repo2run_modern_graph", llm="x", num_turn=100),
                      SweAgentRepo2RunGraphProducer)
    assert "sweagent_repo2run_modern_graph" in benchmark._PRODUCE_ABLE
    assert "sweagent_repo2run_modern_graph" in manifest._SWEAGENT_MODELS


def test_both_arms_cap_output_tokens_identically():
    # SWE-agent 1.1 sends max_tokens only for anthropic models, so without this a degenerate
    # DeepSeek generation runs to the 384K output limit while OpenRouter keepalives stop the
    # 300 s timeout from firing (observed 2026-09-23: 15+ min stalls on keyring/Spoolman).
    for path in (MODERN_CONFIG_PATH, GRAPH_CONFIG_PATH):
        kwargs = _load(path)["agent"]["model"]["completion_kwargs"]
        assert kwargs["max_tokens"] == 16384, path
