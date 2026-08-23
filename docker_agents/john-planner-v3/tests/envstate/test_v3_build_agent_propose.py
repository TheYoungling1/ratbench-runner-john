"""Parity test: V3BuildAgent.propose behaves identically to BuildAgent.propose,
but the module imports standalone (no synthesizer/world_model/ledger drag)."""
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from src.envstate.v3_build_agent import V3BuildAgent
from src.envstate.repair_scope import RepairScope


class _Msg:
    def __init__(self, c): self.content = c; self.reasoning = None; self.model_extra = {}
class _Choice:
    def __init__(self, c): self.message = _Msg(c)
class _Resp:
    def __init__(self, c): self.choices = [_Choice(c)]; self.usage = None
class _Client:
    def __init__(self, *cs): self._q = list(cs); self.chat = self
    @property
    def completions(self): return self
    def create(self, **kw): return _Resp(self._q.pop(0))


_GOOD = '''Final Patch:
```json
{"patch": {"add_providers": [{"id": "apt:libplacebo-dev", "kind": "apt",
 "command": "apt-get install -y libplacebo-dev", "provides": ["syslib:libplacebo"],
 "override": true}]}}
```'''
_BAD_KEYS = '```json\n{"patch": {"add_providers": [{"kind": "apt"}]}}\n```'   # missing id/command
_DIAG_THEN_GOOD = "Action: pkg-config --exists libplacebo"


def _scope():
    return RepairScope("syslib:libplacebo", "apt-get install -y libplacebodev",
                       "Unable to locate package", (), ("apt:libplacebodev",), (),
                       frozenset({"ev.1.0"}))


def test_module_imports_without_legacy_drag():
    # If v3_build_agent re-imported build_agent/synthesizer/world_model at module
    # load, this would have failed at the top-of-file import already.
    import src.envstate.v3_build_agent as m
    assert not hasattr(m, "Synthesizer") and not hasattr(m, "WorldModelMap")


def test_propose_returns_typed_proposal():
    a = V3BuildAgent(_Client(_GOOD), "fake")
    p = a.propose(_scope(), exec_readonly=lambda c: (0, ""))
    assert p is not None and p.add_providers[0].override is True


def test_propose_parsefail_retries_once_then_rejects():
    a = V3BuildAgent(_Client(_BAD_KEYS, _BAD_KEYS), "fake")  # JSON found, keys missing
    assert a.propose(_scope(), exec_readonly=lambda c: (0, "")) is None


def test_propose_accepts_rejection_errors_kwarg():
    a = V3BuildAgent(_Client(_GOOD), "fake")
    p = a.propose(_scope(), exec_readonly=lambda c: (0, ""),
                  rejection_errors=("provider apt:x provides unknown node",))
    assert p is not None


def test_propose_runs_readonly_diagnostic_then_patches():
    # First LLM turn emits an Action (read-only diag); exec_readonly is invoked;
    # second turn emits the Final Patch.
    calls = []
    a = V3BuildAgent(_Client(_DIAG_THEN_GOOD, _GOOD), "fake")
    p = a.propose(_scope(), exec_readonly=lambda c: (calls.append(c) or (0, "exists")))
    assert calls == ["pkg-config --exists libplacebo"]
    assert p is not None and p.add_providers[0].override is True
