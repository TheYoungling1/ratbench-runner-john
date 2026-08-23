# tests/test_world_model_autoresolve.py
from src.envstate.world_model import _auto_resolve_problems, Fact, OpenProblem


def test_drops_problem_when_package_installed():
    problems = (OpenProblem("ModuleNotFoundError: flask", "missing flask", "deps"),)
    kept = _auto_resolve_problems(problems, (Fact("flask", "3.0.0"),))
    assert kept == ()


def test_match_is_case_insensitive():
    problems = (OpenProblem("ImportError: Flask not found", "x", "deps"),)
    kept = _auto_resolve_problems(problems, (Fact("flask"),))
    assert kept == ()


def test_keeps_unrelated_problem():
    problems = (OpenProblem("pg_config not found", "needs libpq", "system"),)
    kept = _auto_resolve_problems(problems, (Fact("flask"),))
    assert kept == problems


def test_keeps_problem_when_nothing_installed():
    problems = (OpenProblem("ModuleNotFoundError: flask", "x", "deps"),)
    assert _auto_resolve_problems(problems, ()) == problems
