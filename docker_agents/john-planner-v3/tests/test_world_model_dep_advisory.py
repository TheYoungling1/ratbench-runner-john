"""Phase-0: WorldModelMap.dep_advisory carrier (field + serialization + merge)."""

from __future__ import annotations

from src.envstate.world_model import (
    initial_map,
    map_from_dict,
    map_to_dict,
    merge_map,
)

ADVISORY = "[DEPENDENCY GRAPH - advisory]\nFRONTIER:\n  SYSTEM libgl1 MISSING"


def _base_map():
    return initial_map(
        base_image="python:3.11-slim",
        workdir="/app",
        language="python 3.11",
        build_system="pip",
        repo_layout=("pyproject.toml",),
    )


def test_dep_advisory_defaults_empty():
    assert _base_map().dep_advisory == ""


def test_initial_map_accepts_dep_advisory():
    m = initial_map(
        base_image="python:3.11-slim",
        workdir="/app",
        language="python 3.11",
        build_system="pip",
        repo_layout=("pyproject.toml",),
        dep_advisory=ADVISORY,
    )
    assert m.dep_advisory == ADVISORY


def test_merge_map_sets_and_preserves_dep_advisory():
    m = _base_map()
    m2 = merge_map(m, dep_advisory=ADVISORY)
    assert m2.dep_advisory == ADVISORY
    # a merge that does not touch dep_advisory preserves it
    m3 = merge_map(m2, done_flag=True)
    assert m3.dep_advisory == ADVISORY


def test_round_trip_preserves_dep_advisory():
    m = merge_map(_base_map(), dep_advisory=ADVISORY)
    assert map_from_dict(map_to_dict(m)) == m
    assert map_to_dict(m)["dep_advisory"] == ADVISORY


def test_from_dict_back_compat_missing_key():
    """Old serialized maps (no dep_advisory key) load with default ''."""
    d = map_to_dict(_base_map())
    d.pop("dep_advisory", None)
    assert map_from_dict(d).dep_advisory == ""
