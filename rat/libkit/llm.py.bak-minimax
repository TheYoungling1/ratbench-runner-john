import os
import time
from datetime import datetime

from openai import OpenAI

from libkit.config import config


def _is_openrouter(base_url: str, model: str) -> bool:
    """Return True when the client is talking to OpenRouter."""
    return (
        "openrouter" in (base_url or "")
        or os.getenv("LLM_API_PROVIDER") == "openrouter"
        or "/" in model
    )


def _openrouter_extra_body(existing: dict | None = None) -> dict:
    """Build (or merge into) an extra_body dict with the provider-pin block."""
    providers = [
        p.strip()
        for p in os.getenv("OPENROUTER_PROVIDER", "Alibaba").split(",")
        if p.strip()
    ]
    body = dict(existing) if existing else {}
    body.setdefault("provider", {}).update(
        {"order": providers, "allow_fallbacks": False}
    )
    return body


class LLMChat:
    def __init__(self, model: str):
        self.model = model

        model_config = config.get_llm_config(self.model)
        self._base_url = model_config.get("base_url", "")
        self.client = OpenAI(
            base_url=self._base_url, api_key=model_config["key"]
        )

    def chat(self, messages, temperature=0.0, n=1, max_tokens=4096):
        max_retry = 5
        count = 0
        while count < max_retry:
            try:
                kwargs: dict = dict(
                    model=self.model,
                    messages=messages,
                    temperature=temperature,
                    n=n,
                    max_tokens=max_tokens,
                )
                if _is_openrouter(self._base_url, self.model):
                    kwargs["extra_body"] = _openrouter_extra_body(
                        kwargs.get("extra_body")
                    )
                response = self.client.chat.completions.create(**kwargs)
                return response.choices[0].message.content, response.usage
            except Exception as e:
                print(f"Error: {e}")
                count += 1
                time.sleep(3)
        return None, None
