import os
import sys

# Make both `producers` (repo root) and `bench.*` (<repo>/bench) importable no matter where
# pytest is invoked from, so the producer<->bench round-trip test works regardless of import order.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))          # producers/tests -> producers -> repo root
_BENCH_DIR = os.path.join(_REPO_ROOT, "bench")               # contains the `bench` package
for _p in (_REPO_ROOT, _BENCH_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)


import pytest  # noqa: E402

# The shared endpoint vars (README, "API keys") switch every arm's routing. env.sh loads .env into
# the test shell, so clear them per test; tests that exercise them set them explicitly.
_SHARED_LLM_VARS = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")


@pytest.fixture(autouse=True)
def _no_shared_llm_env(monkeypatch):
    for k in _SHARED_LLM_VARS:
        monkeypatch.delenv(k, raising=False)
