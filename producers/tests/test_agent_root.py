# producers/tests/test_agent_root.py — the agent_root plumbing (design §B, method family)
#
# `dockeragent` is a per-checkout METHOD FAMILY: each branch ships its OWN
# multi_docker_eval_adapter.py in its checkout. These tests prove the loader loads the EXACT
# adapter FILE at the checkout root (ctx.agent_root / $DOCKERAGENT_ROOT) — never the deleted
# harness copy, never a different checkout's cached/on-path adapter.
import sys

import pytest

from producers.base import ProduceContext
from producers.dockeragent import _load_adapter_cls

_ADAPTER = "multi_docker_eval_adapter.py"


def _write_adapter(root, source):
    """Write a fake adapter with a SOURCE sentinel into `root`, returns its path."""
    (root / _ADAPTER).write_text(
        "class MultiDockerEvalAdapter:\n"
        f"    SOURCE = {source!r}\n"
        "    def __init__(self, output_dir=None):\n"
        "        self.output_dir = output_dir\n"
    )
    return root / _ADAPTER


def test_produce_context_carries_agent_root():
    c = ProduceContext(llm=None, workdir="/tmp", agent_root="/some/path")
    assert c.agent_root == "/some/path"


def test_produce_context_agent_root_defaults_none():
    # back-compat: existing positional/kw constructions that omit agent_root keep working.
    assert ProduceContext(llm=None, workdir="/tmp").agent_root is None


@pytest.fixture
def _clean_adapter_modules():
    """Keep sys.path and the per-root adapter modules from leaking between tests."""
    saved_path = list(sys.path)
    saved_mods = {k: v for k, v in sys.modules.items()
                  if k == "multi_docker_eval_adapter" or k.startswith("_dockeragent_adapter_")}
    try:
        yield
    finally:
        sys.path[:] = saved_path
        for k in [k for k in sys.modules
                  if k == "multi_docker_eval_adapter" or k.startswith("_dockeragent_adapter_")]:
            del sys.modules[k]
        sys.modules.update(saved_mods)


def test_load_adapter_cls_loads_exact_checkout_file(tmp_path, _clean_adapter_modules):
    # Loads THAT root's file (sentinel proves it), not the deleted harness copy.
    root_a = tmp_path / "rootA"
    root_a.mkdir()
    _write_adapter(root_a, "rootA")
    cls = _load_adapter_cls(agent_root=str(root_a))
    assert cls.__name__ == "MultiDockerEvalAdapter"
    assert cls.SOURCE == "rootA"


def test_load_adapter_cls_no_root_raises(monkeypatch, _clean_adapter_modules):
    # No agent_root and no $DOCKERAGENT_ROOT => loud RuntimeError, never a silent dead-copy fallback.
    monkeypatch.delenv("DOCKERAGENT_ROOT", raising=False)
    with pytest.raises(RuntimeError, match="DOCKERAGENT_ROOT|agent_root"):
        _load_adapter_cls(agent_root=None)


def test_load_adapter_cls_missing_file_raises_no_fallthrough(tmp_path, _clean_adapter_modules):
    # A root WITHOUT its own adapter must raise — even when another checkout's adapter is already
    # on sys.path — instead of silently falling through to the wrong agent's adapter.
    other = tmp_path / "other"
    other.mkdir()
    _write_adapter(other, "OTHER")
    sys.path.insert(0, str(other))   # a different checkout's adapter is importable by bare name

    empty = tmp_path / "empty"       # this root ships NO adapter of its own
    empty.mkdir()
    with pytest.raises(RuntimeError, match="no multi_docker_eval_adapter.py"):
        _load_adapter_cls(agent_root=str(empty))


def test_load_adapter_cls_no_sys_modules_cross_contamination(tmp_path, _clean_adapter_modules):
    # Two checkouts with DIFFERENT-sentinel adapters loaded in the SAME process: each must get its
    # OWN adapter (proves exact-file load, not a name-cached sys.modules hit).
    root_a = tmp_path / "A"
    root_a.mkdir()
    _write_adapter(root_a, "A")
    root_b = tmp_path / "B"
    root_b.mkdir()
    _write_adapter(root_b, "B")

    assert _load_adapter_cls(agent_root=str(root_a)).SOURCE == "A"
    assert _load_adapter_cls(agent_root=str(root_b)).SOURCE == "B"


def test_load_adapter_cls_falls_back_to_env(tmp_path, monkeypatch, _clean_adapter_modules):
    # When ctx.agent_root is absent, $DOCKERAGENT_ROOT names the checkout.
    root = tmp_path / "env_root"
    root.mkdir()
    _write_adapter(root, "FROM_ENV")
    monkeypatch.setenv("DOCKERAGENT_ROOT", str(root))
    cls = _load_adapter_cls(agent_root=None)
    assert cls.SOURCE == "FROM_ENV"
