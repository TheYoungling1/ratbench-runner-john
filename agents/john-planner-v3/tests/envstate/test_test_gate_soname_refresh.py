from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from python_deps.depgraph.ids import syslib_id
from python_deps.depgraph.schema import DepGraph
from src.envstate.depgraph_live import test_gate_soname_refresh

TEST_CMD = "python -m pytest -q"


def _noop_exec(cmd):
    return (1, "")


def test_test_gate_event_resolves_soname_from_table():
    events = [(TEST_CMD, "ImportError: libGL.so.1: cannot open shared object file")]
    out = test_gate_soname_refresh(DepGraph(), _noop_exec, events, TEST_CMD)
    assert out.get(syslib_id("libGL.so.1")).chosen_fix == "apt:libgl1"


def test_non_test_events_are_ignored():
    events = [("python -m pip install foo", "libGL.so.1: cannot open shared object file")]
    out = test_gate_soname_refresh(DepGraph(), _noop_exec, events, TEST_CMD)
    assert [n for n in out.nodes] == []


def test_none_graph_is_noop():
    assert test_gate_soname_refresh(None, _noop_exec, [], TEST_CMD) is None
