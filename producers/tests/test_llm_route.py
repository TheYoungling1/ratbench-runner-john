"""One OpenAI-compatible endpoint for every arm: LLM_API_KEY + LLM_BASE_URL (+ LLM_MODEL).

When neither LLM_API_KEY nor LLM_BASE_URL is set, every arm keeps its own legacy variables —
that behaviour is pinned by the per-arm test files, not here.
"""
import os
import types
from unittest.mock import patch

import pytest
import yaml

from producers.base import ensure_rat_on_path

CUSTOM = {"LLM_API_KEY": "sk-one", "LLM_BASE_URL": "https://api.example.com/v1"}
OPENROUTER = "https://openrouter.ai/api/v1"
SLUG = "openrouter/deepseek/deepseek-v4-flash-0731"

_AMBIENT = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "SETUPX_BASE_URL", "SETUPX_DB_DSN",
            "EMBEDDING_API_KEY", "EMBEDDING_BASE_URL", "EMBEDDING_MODEL", "EMBEDDING_DIM",
            "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN", "PROXY_UPSTREAM_HOST")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _AMBIENT:
        monkeypatch.delenv(k, raising=False)


# ── setupx ──────────────────────────────────────────────────────────────────────────────────

def _setupx(llm=SLUG):
    from producers.setupx import child_env
    return child_env(llm, "img", "ns", 100, "/tmp/log")


def test_setupx_uses_the_shared_endpoint_key_and_model(monkeypatch):
    for k, v in CUSTOM.items():
        monkeypatch.setenv(k, v)
    env = _setupx("openrouter/acme/model-7b")
    assert env["OPENAI_BASE_URL"] == CUSTOM["LLM_BASE_URL"]
    assert env["OPENAI_API_KEY"] == "sk-one"
    # only the litellm routing prefix goes; the provider's own `org/model` name is kept
    assert env["OPENAI_MODEL"] == "acme/model-7b"
    # the provider pin and reasoning switch are OpenRouter body fields; another endpoint may 400
    assert "SETUPX_EXTRA_BODY" not in env


def test_setupx_key_alone_defaults_to_openrouter_with_the_pin(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-one")
    env = _setupx()
    assert env["OPENAI_BASE_URL"] == OPENROUTER
    assert env["OPENAI_API_KEY"] == "sk-one"
    assert env["OPENAI_MODEL"] == "deepseek/deepseek-v4-flash-0731"
    assert "SETUPX_EXTRA_BODY" in env


def test_setupx_base_url_override_beats_the_shared_one(monkeypatch):
    for k, v in CUSTOM.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("SETUPX_BASE_URL", "https://other.example.com/v1")
    assert _setupx()["OPENAI_BASE_URL"] == "https://other.example.com/v1"


def test_setupx_xpu_embeddings_default_to_the_shared_openrouter_key(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-one")
    monkeypatch.setenv("SETUPX_DB_DSN", "postgresql://u:p@localhost:5433/xpu_run")
    env = _setupx()
    assert env["EMBEDDING_API_KEY"] == "sk-one"
    assert env["EMBEDDING_BASE_URL"] == OPENROUTER
    assert env["EMBEDDING_MODEL"] == "openai/text-embedding-3-small"
    assert env["EMBEDDING_DIM"] == "1536"
    assert env["XPU_ENABLED"] == "1"


def test_setupx_xpu_on_another_provider_needs_an_explicit_embedding_model(monkeypatch):
    # The embedding model's name is provider-specific, so it cannot be guessed off OpenRouter.
    for k, v in CUSTOM.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("SETUPX_DB_DSN", "postgresql://u:p@localhost:5433/xpu_run")
    with pytest.raises(RuntimeError, match="EMBEDDING_MODEL"):
        _setupx()
    monkeypatch.setenv("EMBEDDING_MODEL", "acme-embed")
    env = _setupx()
    assert env["EMBEDDING_API_KEY"] == "sk-one"
    assert env["EMBEDDING_BASE_URL"] == CUSTOM["LLM_BASE_URL"]
    assert env["EMBEDDING_MODEL"] == "acme-embed"


# ── claudecode-dockerfile ────────────────────────────────────────────────────────────────────

def test_claudecode_takes_the_shared_key_for_a_third_party_endpoint(monkeypatch):
    from producers._claudecode_helpers import agent_env
    monkeypatch.setenv("LLM_API_KEY", "sk-one")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://host.docker.internal:8931/api")
    assert agent_env()["ANTHROPIC_AUTH_TOKEN"] == "sk-one"
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "explicit")
    assert agent_env()["ANTHROPIC_AUTH_TOKEN"] == "explicit"


def test_claudecode_never_sends_the_shared_key_to_anthropic(monkeypatch):
    from producers._claudecode_helpers import agent_env
    monkeypatch.setenv("LLM_API_KEY", "sk-one")
    assert "ANTHROPIC_AUTH_TOKEN" not in agent_env()


# ── sweagent_repo2run* ───────────────────────────────────────────────────────────────────────

def _sweagent_model(env: dict) -> dict:
    from producers.sweagent_repo2run import CONFIG_PATH
    from producers.sweagent_repo2run_runner import _apply_overrides
    cfg = yaml.safe_load(open(CONFIG_PATH))
    a = types.SimpleNamespace(llm=SLUG, cost_limit=2.0, call_limit=100, repo_path="/x/repo",
                              commit=None, trajectory_dir="/x/traj")
    with patch.dict(os.environ, env, clear=False):
        return _apply_overrides(cfg, a)["agent"]["model"]


def test_sweagent_on_another_provider_goes_through_litellms_openai_route():
    m = _sweagent_model(CUSTOM)
    assert m["name"] == "openai/deepseek/deepseek-v4-flash-0731"
    assert m["api_base"] == CUSTOM["LLM_BASE_URL"]
    assert m["api_key"] == "$LLM_API_KEY"         # SWE-agent resolves `$VAR` itself
    # litellm cannot price an arbitrary model and SWE-agent aborts on turn 1 if a $ cap is set;
    # the call limit stays the budget
    assert m["per_instance_cost_limit"] == 0
    assert m["per_instance_call_limit"] == 100
    assert "provider" not in m.get("completion_kwargs", {}).get("extra_body", {})


def test_sweagent_key_alone_stays_on_openrouter_with_the_pin():
    m = _sweagent_model({"LLM_API_KEY": "sk-one"})
    assert m["name"] == SLUG
    assert m["api_key"] == "$LLM_API_KEY"
    assert "api_base" not in m
    assert m["per_instance_cost_limit"] == 2.0
    assert "provider" in m["completion_kwargs"]["extra_body"]


# ── rat / repo2run (rat/libkit) ──────────────────────────────────────────────────────────────

def test_rat_routes_every_model_to_the_shared_endpoint(monkeypatch):
    ensure_rat_on_path()
    from libkit.config import config
    for k, v in CUSTOM.items():
        monkeypatch.setenv(k, v)
    assert config.get_llm_config("deepseek-chat") == {
        "base_url": CUSTOM["LLM_BASE_URL"], "key": "sk-one"}
    monkeypatch.delenv("LLM_BASE_URL")
    assert config.get_llm_config("deepseek/deepseek-v4-flash-0731") == {
        "base_url": OPENROUTER, "key": "sk-one"}
