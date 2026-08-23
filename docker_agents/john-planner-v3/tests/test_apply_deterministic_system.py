# tests/test_apply_deterministic_system.py
from types import SimpleNamespace
from src.envstate.world_model import initial_map, merge_map, apply_deterministic, Fact, OpenProblem


def _snap(installed=(), env=None, system_installed=()):
    return SimpleNamespace(installed=installed, env=env or {"arch": "x86_64"},
                           system_installed=system_installed)


def _man(build_system="pip", required=()):
    return SimpleNamespace(build_system=build_system, required=required)


def _base():
    return initial_map(base_image="python:3.12", workdir="/app", language="python",
                       build_system="unknown", repo_layout=())


def test_system_installed_replaced_from_snapshot():
    snap = _snap(system_installed=(Fact("pg_config", "tool"),))
    m = apply_deterministic(_base(), snap, _man())
    assert {f.name for f in m.system_installed} == {"pg_config"}


def test_system_problem_auto_resolved_and_progress_recovers():
    prior = merge_map(_base(), open_problems=(OpenProblem("Error: pg_config executable not found", "x", "system"),))
    snap = _snap(system_installed=(Fact("pg_config", "tool"),))
    m = apply_deterministic(prior, snap, _man())
    assert m.open_problems == ()              # system problem auto-resolved
    assert m.progress["system"] is True        # layer recovers (no system problem)


def test_back_compat_snapshot_without_system_field():
    # old duck-typed snapshot lacking .system_installed must not crash
    snap = SimpleNamespace(installed=(), env={"arch": "x86_64"})
    m = apply_deterministic(_base(), snap, _man())
    assert m.system_installed == ()
