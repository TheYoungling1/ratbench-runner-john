# tests/test_apply_deterministic.py
from types import SimpleNamespace
from src.envstate.world_model import (
    initial_map, merge_map, apply_deterministic, Fact, OpenProblem,
)


def _snap(installed=(), env=None):
    return SimpleNamespace(installed=installed, env=env or {})


def _man(build_system="pip", required=()):
    return SimpleNamespace(build_system=build_system, required=required)


def _base():
    return initial_map(
        base_image="python:3.12", workdir="/app", language="python",
        build_system="unknown", repo_layout=(), )


def test_replaces_facts_from_snapshot_and_manifest():
    snap = _snap(installed=(Fact("flask", "3.0.0"),), env={"arch": "x86_64", "python_version": "Python 3.12.1"})
    man = _man(build_system="poetry", required=(Fact("flask"),))
    m = apply_deterministic(_base(), snap, man)
    assert m.installed == (Fact("flask", "3.0.0"),)
    assert m.build_system == "poetry"
    assert m.required == (Fact("flask"),)
    assert m.env["arch"] == "x86_64"
    assert m.language == "Python 3.12.1"


def test_empty_env_degrades_keeps_prior_facts():
    prior = merge_map(_base(), installed=(Fact("flask", "3.0.0"),), env={"arch": "x86_64"})
    snap = _snap(installed=(), env={})   # probe failure signal
    m = apply_deterministic(prior, snap, _man())
    assert m.installed == (Fact("flask", "3.0.0"),)
    assert m.env == {"arch": "x86_64"}


def test_auto_resolves_problem_and_derives_progress():
    prior = merge_map(_base(), open_problems=(OpenProblem("ModuleNotFoundError: flask", "x", "deps"),))
    snap = _snap(installed=(Fact("flask", "3.0.0"),), env={"python_version": "Python 3.12.1"})
    man = _man(build_system="pip", required=(Fact("flask"),))
    m = apply_deterministic(prior, snap, man)
    assert m.open_problems == ()          # auto-resolved
    assert m.progress["deps"] is True     # required subset of installed
    assert m.progress["runtime"] is True  # python_version present
