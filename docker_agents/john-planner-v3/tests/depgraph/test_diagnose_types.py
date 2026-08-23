"""Tests for diagnose types + is_local_import (pure, no Docker)."""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from python_deps.depgraph.diagnose import (
    Diagnosis, Mode, RepoContext, is_local_import,
)
from python_deps.depgraph.scan import local_module_names


def test_repo_context_defaults_are_empty():
    ctx = RepoContext()
    assert ctx.local_names == frozenset()
    assert ctx.invalid_names == frozenset()


def test_is_local_import_matches_top_level_segment():
    local = frozenset({"docs_src", "myapp"})
    assert is_local_import("docs_src", local) is True
    assert is_local_import("docs_src.helpers", local) is True   # dotted -> top segment
    assert is_local_import("requests", local) is False
    assert is_local_import("", local) is False


def test_public_local_module_names_delegates(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "solo.py").write_text("x = 1")
    names = local_module_names(str(tmp_path))
    assert "pkg" in names
    assert "solo" in names


def test_diagnosis_is_frozen():
    d = Diagnosis(mode=Mode.AMBIGUOUS, discovery=None, reason="x")
    import dataclasses
    assert dataclasses.is_dataclass(d)
    import pytest
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.reason = "y"  # type: ignore[misc]
