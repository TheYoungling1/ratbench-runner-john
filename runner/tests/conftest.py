import pytest

# $LLM_MODEL overrides every variety's pinned model (runner/cli.py::resolve_llm). env.sh loads .env
# into the test shell, so clear the shared vars per test; tests that exercise them set them.


@pytest.fixture(autouse=True)
def _no_shared_llm_env(monkeypatch):
    for k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.delenv(k, raising=False)
