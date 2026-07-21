# bench/languages/python.py
"""The Python (pytest) Language — the four pytest-specific commands measure() used to hard-code,
extracted verbatim so existing Python rows stay byte-identical."""
from __future__ import annotations


class PythonLanguage:
    name = "python"
    short_circuit_gate = False   # the collect gate is diagnostic; the full run happens regardless

    _ENSURE = ("python -m pip install -q --break-system-packages pytest pytest-timeout "
               "|| python -m pip install -q pytest pytest-timeout || true")
    _TIMEOUT_GUARD = ('F=""; python -c "import pytest_timeout" >/dev/null 2>&1 && '
                      'F="--timeout=120 --timeout-method=signal"')

    def ensure_cmd(self, W: str) -> str:
        return self._ENSURE

    def gate_cmd(self, W: str) -> str:
        return f"python -m pytest --collect-only -q --disable-warnings {W}; exit ${{PIPESTATUS[0]:-$?}}"

    def gate_pass(self, rc: int) -> bool:
        return rc in (0, 5)

    def collect_cmd(self, W: str) -> str:
        return f"python -m pytest --co -q --continue-on-collection-errors {W} 2>&1 || true"

    def run_cmd(self, W: str, junit_out: str) -> str:
        return (f"{self._TIMEOUT_GUARD}; python -m pytest -q --continue-on-collection-errors "
                f"--junit-xml={junit_out} $F || true")

    def junit_glob(self, W: str) -> str:
        return f"{W}/logs/junit.xml"

    def pkg_count_cmd(self, W: str) -> str:
        return "python -m pip list --format=freeze 2>/dev/null | wc -l"
