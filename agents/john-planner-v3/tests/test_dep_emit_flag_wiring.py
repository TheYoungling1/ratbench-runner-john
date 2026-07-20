import inspect
from src.envstate.orchestrator import run_v1


def test_run_v1_exposes_enable_dep_emit():
    sig = inspect.signature(run_v1)
    assert "enable_dep_emit" in sig.parameters
    assert sig.parameters["enable_dep_emit"].default is False


