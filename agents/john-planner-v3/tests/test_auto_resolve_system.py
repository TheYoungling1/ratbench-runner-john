# tests/test_auto_resolve_system.py
from src.envstate.world_model import _auto_resolve_system_problems, Fact, OpenProblem


def _sys(sig):
    return OpenProblem(sig, "x", "system")


def test_pg_config_resolves_when_tool_present():
    probs = (_sys("Error: pg_config executable not found"),)
    kept = _auto_resolve_system_problems(probs, (Fact("pg_config", "tool"),))
    assert kept == ()


def test_pg_config_kept_when_tool_absent():
    probs = (_sys("Error: pg_config executable not found"),)
    assert _auto_resolve_system_problems(probs, (Fact("gcc", "tool"),)) == probs


def test_command_not_found_shape():
    probs = (_sys("gcc: command not found"),)
    assert _auto_resolve_system_problems(probs, (Fact("gcc", "tool"),)) == ()


def test_pkg_config_no_package_shape():
    probs = (_sys("No package 'libxml-2.0' found"),)
    assert _auto_resolve_system_problems(probs, (Fact("libxml-2.0", "pkgconfig"),)) == ()


def test_only_touches_system_layer():
    probs = (OpenProblem("pg_config not found", "x", "deps"),)  # mislabeled deps
    assert _auto_resolve_system_problems(probs, (Fact("pg_config", "tool"),)) == probs


def test_unrecognized_shape_is_kept():
    probs = (_sys("postgres connection refused on :5432"),)
    assert _auto_resolve_system_problems(probs, (Fact("pg_config", "tool"),)) == probs
