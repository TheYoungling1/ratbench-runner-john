"""Regression: evidence.py must read declarations on Python < 3.11.

The 3.10 benchmark box has no stdlib ``tomllib`` (3.11+). evidence.py used to
``except ModuleNotFoundError: tomllib = None`` with NO ``tomli`` fallback, so the
whole declared-dependency reader silently returned ZERO requirements on <3.11 —
select_roots got no roots, and every repo's dev/test group (pytest-cov, xdist,
...) vanished from the closure (the mvt/anthropic-sdk 0-score bug).

The tomllib-absent path is exercised in a SUBPROCESS so blocking ``tomllib`` and
re-importing ``evidence`` cannot pollute this interpreter's module state (a fresh
``evidence`` module gets sibling-incompatible class objects otherwise).
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("tomli")  # the backport must be present to exercise the fallback

_ROOT = Path(__file__).resolve().parents[2]


def test_evidence_reads_pyproject_without_stdlib_tomllib(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\n'
        'name = "demo"\n'
        'requires-python = ">= 3.10"\n'
        'dependencies = ["requests"]\n'
        '[dependency-groups]\n'
        'dev = ["pytest>=7.0", "pytest-cov>=4.1.0"]\n'
    )
    code = textwrap.dedent(f"""
        import sys, builtins
        _real = builtins.__import__
        def _fake(name, *a, **k):
            if name == "tomllib":                       # simulate Python < 3.11
                raise ModuleNotFoundError("No module named 'tomllib'")
            return _real(name, *a, **k)
        builtins.__import__ = _fake
        sys.path[:0] = [{str(_ROOT)!r}, {str(_ROOT / "src")!r}]
        from python_deps.evidence import (
            collect_python_dependency_evidence, tomllib as parser,
        )
        assert parser is not None, "evidence.py left NO toml parser on <3.11"
        ev = collect_python_dependency_evidence({str(tmp_path)!r})
        names = {{r.name for r in ev.declared_dependencies}}
        assert "requests" in names, "runtime dep lost on <3.11: %s" % sorted(names)
        assert "pytest-cov" in names, "PEP735 dev dep lost on <3.11: %s" % sorted(names)
        print("FALLBACK_OK")
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0 and "FALLBACK_OK" in r.stdout, (
        f"tomllib-absent evidence read failed:\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
    )
