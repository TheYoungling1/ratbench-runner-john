"""Provider routing in run_v3_e2e._resolve_llm_endpoint.

The bug this guards: with BOTH OPENROUTER_API_KEY and MINIMAX_API_KEY in the
env (a common .env), a `--model minimax-m3` run must reach MiniMax
(MINIMAX_API_BASE) — not fall through to OpenRouter (which would burn OpenRouter
credits AND skip the "minimaxi" thinking-off gate). No Docker, no network.
"""
import scripts.run_v3_e2e as e2e


def _clear(monkeypatch):
    for var in ("OPENROUTER_API_KEY", "OPENROUTER_API_BASE",
                "MINIMAX_API_KEY", "MINIMAX_API_BASE",
                "OPENAI_API_KEY", "OPENAI_API_BASE", "LLM_API_PROVIDER"):
        monkeypatch.delenv(var, raising=False)


def test_minimax_slug_prefers_minimax_even_with_openrouter_set(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("MINIMAX_API_KEY", "mm-key")
    monkeypatch.setenv("MINIMAX_API_BASE", "https://api.minimaxi.com/v1")

    key, base = e2e._resolve_llm_endpoint("minimax-m3")

    assert key == "mm-key"
    assert base == "https://api.minimaxi.com/v1"


def test_abab_slug_routes_to_minimax(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("MINIMAX_API_KEY", "mm-key")
    monkeypatch.setenv("MINIMAX_API_BASE", "https://api.minimaxi.com/v1")

    key, base = e2e._resolve_llm_endpoint("abab6.5s-chat")

    assert key == "mm-key"
    assert base == "https://api.minimaxi.com/v1"


def test_provider_env_forces_minimax(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("LLM_API_PROVIDER", "minimax")
    monkeypatch.setenv("MINIMAX_API_KEY", "mm-key")
    monkeypatch.setenv("MINIMAX_API_BASE", "https://api.minimaxi.com/v1")

    key, base = e2e._resolve_llm_endpoint("gpt-4o")  # non-minimax slug

    assert key == "mm-key"
    assert base == "https://api.minimaxi.com/v1"


def test_non_minimax_slug_keeps_openrouter_fallback(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("MINIMAX_API_KEY", "mm-key")

    key, base = e2e._resolve_llm_endpoint("deepseek/deepseek-chat")

    assert key == "or-key"
    assert base == "https://openrouter.ai/api/v1"


def test_minimax_slug_with_unset_minimax_env_returns_none(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")  # present but must NOT be used

    key, base = e2e._resolve_llm_endpoint("minimax-m3")

    assert key is None  # -> _run prints an explicit MiniMax-not-configured error
    assert base is None
