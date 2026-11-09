"""LLM_MODEL: one model name for every arm, between --llm and the variety's pinned llm."""
from runner.cli import resolve_llm


def test_llm_model_env_overrides_the_variety(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "acme/model-7b")
    assert resolve_llm(None, "deepseek/deepseek-v4-flash-0731") == "acme/model-7b"


def test_explicit_flag_beats_llm_model(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "acme/model-7b")
    assert resolve_llm("cli/model", "deepseek/deepseek-v4-flash-0731") == "cli/model"


def test_blank_values_fall_through(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "  ")
    assert resolve_llm(None, "pinned") == "pinned"
    monkeypatch.delenv("LLM_MODEL")
    assert resolve_llm(None, "pinned") == "pinned"
    assert resolve_llm(None, None) is None
