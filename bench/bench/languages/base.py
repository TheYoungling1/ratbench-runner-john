# bench/languages/base.py
"""The Language strategy interface. measure() dispatches per-language through get_language() and
calls these hooks; implementations return shell-command STRINGS only and never touch Docker."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Language(Protocol):
    name: str
    short_circuit_gate: bool           # if True, a failed gate skips the run and records gate_fail

    def ensure_cmd(self, W: str) -> str: ...              # install test harness + JUnit reporter
    def gate_cmd(self, W: str) -> str: ...                # EBSR gate command
    def gate_pass(self, rc: int) -> bool: ...             # per-language gate pass predicate
    def collect_cmd(self, W: str) -> str: ...             # node-id collect; "" to skip
    def run_cmd(self, W: str, junit_out: str) -> str: ...  # run tests, emit JUnit XML
    def junit_glob(self, W: str) -> str: ...              # where the JUnit XML lands
    def pkg_count_cmd(self, W: str) -> str: ...           # optional diagnostic; "" to skip
