"""Fix #4a: build_dep_graph must resolve for the container's ACTUAL interpreter,
not a hardcoded 3.11 — otherwise the closure pins wheels for the wrong python
(a 3.11-resolved ``pyarrow==2.0.0`` cannot build on a 3.13 container)."""
from __future__ import annotations

from python_deps.depgraph.build import _detect_target_python
from python_deps.depgraph.executor import CommandResult


class _PyVerExecutor:
    """Minimal Executor returning canned results keyed by command string."""

    def __init__(self, mapping):
        self.mapping = mapping

    def run(self, command, *, timeout=300):
        rc, out, err = self.mapping.get(command, (127, "", "command not found"))
        return CommandResult(command=command, returncode=rc, stdout=out, stderr=err)


def test_detect_from_python3_stdout():
    ex = _PyVerExecutor({"python3 --version": (0, "Python 3.13.14\n", "")})
    assert _detect_target_python(ex) == "3.13"


def test_detect_reads_stderr_and_falls_through_to_python():
    # python3 missing (rc 127 default); legacy `python --version` prints to stderr
    ex = _PyVerExecutor({"python --version": (0, "", "Python 3.10.6\n")})
    assert _detect_target_python(ex) == "3.10"


def test_detect_falls_back_to_default_when_unprobeable():
    assert _detect_target_python(_PyVerExecutor({})) == "3.11"
