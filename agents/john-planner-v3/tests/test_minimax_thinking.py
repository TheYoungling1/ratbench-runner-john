"""MiniMax M3 thinking-off injection + native tool-call normalization.

Covers the central mechanism added to ``src/envstate/llm_response.py``:

* ``apply_minimax_thinking`` merges ``extra_body={"thinking": {"type": ...}}``
  ONLY for a MiniMax-base-url client, gated on ``MINIMAX_THINKING`` (default
  ``disabled`` = thinking OFF), never clobbering an existing ``extra_body``.
* the injection flows automatically through ``complete_with_retry``.
* ``strip_minimax_toolcall`` unwraps MiniMax's native ``<minimax:tool_call>``
  XML so ``extract_json_object`` sees clean JSON.

All fakes are local (no network). Env is isolated per-test via monkeypatch.
"""
from types import SimpleNamespace

from src.envstate.jsonutil import extract_json_object
from src.envstate.llm_response import (
    apply_minimax_thinking,
    complete_with_retry,
    response_text,
    strip_minimax_toolcall,
)


def _response(content="ok"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


class _RecordingClient:
    """Fake OpenAI-compatible client: exposes ``base_url`` and records the
    kwargs each ``chat.completions.create`` call receives."""

    def __init__(self, base_url):
        self.base_url = base_url
        self.received = []

        def _create(*, model, messages, **kwargs):
            self.received.append(kwargs)
            return _response("ok")

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=_create))


_MINIMAX_URL = "https://api.minimaxi.com/v1"
_OPENROUTER_URL = "https://openrouter.ai/api/v1"


# ── apply_minimax_thinking: gate on base_url ─────────────────────────────────

def test_thinking_injected_for_minimax_client_by_default(monkeypatch):
    monkeypatch.delenv("MINIMAX_THINKING", raising=False)  # default => disabled
    client = _RecordingClient(_MINIMAX_URL)
    out = apply_minimax_thinking(client, {"temperature": 0})
    assert out["extra_body"] == {"thinking": {"type": "disabled"}}
    assert out["temperature"] == 0


def test_thinking_not_injected_for_openrouter_client(monkeypatch):
    monkeypatch.delenv("MINIMAX_THINKING", raising=False)
    client = _RecordingClient(_OPENROUTER_URL)
    out = apply_minimax_thinking(client, {"temperature": 0})
    assert "extra_body" not in out


def test_thinking_not_injected_when_base_url_missing(monkeypatch):
    monkeypatch.delenv("MINIMAX_THINKING", raising=False)
    client = SimpleNamespace()  # no base_url attribute at all
    out = apply_minimax_thinking(client, {"temperature": 0})
    assert "extra_body" not in out


# ── MINIMAX_THINKING env gate ────────────────────────────────────────────────

def test_env_enabled_turns_thinking_on(monkeypatch):
    monkeypatch.setenv("MINIMAX_THINKING", "enabled")
    out = apply_minimax_thinking(_RecordingClient(_MINIMAX_URL), {})
    assert out["extra_body"] == {"thinking": {"type": "enabled"}}


def test_env_adaptive(monkeypatch):
    monkeypatch.setenv("MINIMAX_THINKING", "Adaptive")  # case-insensitive
    out = apply_minimax_thinking(_RecordingClient(_MINIMAX_URL), {})
    assert out["extra_body"] == {"thinking": {"type": "adaptive"}}


def test_env_invalid_value_leaves_model_default(monkeypatch):
    monkeypatch.setenv("MINIMAX_THINKING", "off")  # not in disabled|adaptive|enabled
    out = apply_minimax_thinking(_RecordingClient(_MINIMAX_URL), {"temperature": 0})
    assert "extra_body" not in out


def test_env_explicit_disabled(monkeypatch):
    monkeypatch.setenv("MINIMAX_THINKING", "disabled")
    out = apply_minimax_thinking(_RecordingClient(_MINIMAX_URL), {})
    assert out["extra_body"] == {"thinking": {"type": "disabled"}}


# ── no-clobber + immutability ────────────────────────────────────────────────

def test_existing_provider_extra_body_preserved(monkeypatch):
    monkeypatch.delenv("MINIMAX_THINKING", raising=False)
    provider_pin = {"provider": {"order": ["MiniMax"], "allow_fallbacks": False}}
    out = apply_minimax_thinking(
        _RecordingClient(_MINIMAX_URL), {"extra_body": dict(provider_pin)}
    )
    assert out["extra_body"]["provider"] == provider_pin["provider"]
    assert out["extra_body"]["thinking"] == {"type": "disabled"}


def test_existing_thinking_key_not_clobbered(monkeypatch):
    monkeypatch.setenv("MINIMAX_THINKING", "disabled")
    out = apply_minimax_thinking(
        _RecordingClient(_MINIMAX_URL),
        {"extra_body": {"thinking": {"type": "enabled"}}},  # caller already set it
    )
    assert out["extra_body"]["thinking"] == {"type": "enabled"}


def test_caller_kwargs_not_mutated(monkeypatch):
    monkeypatch.delenv("MINIMAX_THINKING", raising=False)
    original = {"temperature": 0, "extra_body": {"provider": {"order": ["X"]}}}
    snapshot = {"temperature": 0, "extra_body": {"provider": {"order": ["X"]}}}
    apply_minimax_thinking(_RecordingClient(_MINIMAX_URL), original)
    assert original == snapshot  # untouched — a new dict was returned


# ── flows automatically through complete_with_retry ──────────────────────────

def test_complete_with_retry_injects_for_minimax(monkeypatch):
    monkeypatch.delenv("MINIMAX_THINKING", raising=False)
    client = _RecordingClient(_MINIMAX_URL)
    complete_with_retry(client, "minimax-m3", [{"role": "user", "content": "go"}],
                        temperature=0)
    assert client.received  # at least one create call
    assert client.received[0]["extra_body"] == {"thinking": {"type": "disabled"}}


def test_complete_with_retry_no_injection_for_openrouter(monkeypatch):
    monkeypatch.delenv("MINIMAX_THINKING", raising=False)
    client = _RecordingClient(_OPENROUTER_URL)
    complete_with_retry(client, "deepseek/deepseek-chat",
                        [{"role": "user", "content": "go"}], temperature=0)
    assert client.received
    assert "extra_body" not in client.received[0]


# ── strip_minimax_toolcall: unwrap native tool-call XML around JSON ──────────

def test_strip_toolcall_unwraps_json_payload():
    raw = ('<minimax:tool_call><invoke name="emit_patch">'
           '{"kind": "pip", "name": "six"}</invoke></minimax:tool_call>')
    cleaned = strip_minimax_toolcall(raw)
    assert cleaned.strip() == '{"kind": "pip", "name": "six"}'
    assert extract_json_object(cleaned) == {"kind": "pip", "name": "six"}


def test_strip_toolcall_noop_without_marker():
    plain = '{"kind": "pip", "name": "six"}'
    assert strip_minimax_toolcall(plain) == plain  # byte-identical passthrough


def test_strip_toolcall_none_and_empty():
    assert strip_minimax_toolcall(None) is None
    assert strip_minimax_toolcall("") == ""


def test_response_text_unwraps_toolcall_and_think():
    content = ('<think>deciding</think>'
               '<minimax:tool_call><invoke name="x">{"a": 1}</invoke></minimax:tool_call>')
    resp = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    assert response_text(resp) == '{"a": 1}'
