import inspect
from python_deps.depgraph.advise import build_advisory_for_repo


def test_advisory_accepts_target_python():
    sig = inspect.signature(build_advisory_for_repo)
    assert "target_python" in sig.parameters
    assert sig.parameters["target_python"].default is None


def test_advisory_forwards_target_python_to_build():
    # guards against accepting the param but ignoring it
    src = inspect.getsource(build_advisory_for_repo)
    assert "target_python=target_python" in src
