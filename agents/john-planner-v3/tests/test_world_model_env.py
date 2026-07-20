# tests/test_world_model_env.py
from src.envstate.world_model import (
    initial_map, merge_map, map_to_dict, map_from_dict, Fact,
)


def _base():
    return initial_map(
        base_image="python:3.12", workdir="/app", language="python",
        build_system="unknown", repo_layout=("pyproject.toml",),
    )


def test_initial_map_has_empty_env():
    m = _base()
    assert m.env == {}


def test_merge_map_replaces_env_and_defensive_copies():
    m = _base()
    src = {"python_version": "Python 3.12.1"}
    m2 = merge_map(m, env=src)
    assert m2.env == {"python_version": "Python 3.12.1"}
    src["python_version"] = "MUTATED"
    assert m2.env["python_version"] == "Python 3.12.1"  # not aliased


def test_merge_map_replaces_build_system_and_language():
    m = _base()
    m2 = merge_map(m, build_system="poetry", language="python 3.12.1")
    assert m2.build_system == "poetry"
    assert m2.language == "python 3.12.1"
    assert m.build_system == "unknown"  # original unchanged (frozen)


def test_env_round_trips_through_dict():
    m = merge_map(_base(), env={"arch": "x86_64"}, installed=(Fact("flask", "3.0.0"),))
    restored = map_from_dict(map_to_dict(m))
    assert restored.env == {"arch": "x86_64"}
    assert restored.installed == (Fact("flask", "3.0.0"),)
