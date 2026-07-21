# tests/bench/test_languages.py
from bench.languages import get_language
from bench.languages.python import PythonLanguage

W = "/testbed"


def test_get_language_defaults_to_python():
    assert isinstance(get_language("python"), PythonLanguage)
    assert isinstance(get_language(None), PythonLanguage)
    assert isinstance(get_language("does-not-exist"), PythonLanguage)


def test_python_commands_are_verbatim_today():
    lang = get_language("python")
    assert lang.name == "python"
    assert lang.short_circuit_gate is False
    assert lang.ensure_cmd(W) == (
        "python -m pip install -q --break-system-packages pytest pytest-timeout "
        "|| python -m pip install -q pytest pytest-timeout || true")
    assert lang.gate_cmd(W) == (
        "python -m pytest --collect-only -q --disable-warnings /testbed; exit ${PIPESTATUS[0]:-$?}")
    assert lang.collect_cmd(W) == (
        "python -m pytest --co -q --continue-on-collection-errors /testbed 2>&1 || true")
    assert lang.run_cmd(W, "/testbed/logs/junit.xml") == (
        'F=""; python -c "import pytest_timeout" >/dev/null 2>&1 && '
        'F="--timeout=120 --timeout-method=signal"; '
        "python -m pytest -q --continue-on-collection-errors --junit-xml=/testbed/logs/junit.xml $F || true")
    assert lang.junit_glob(W) == "/testbed/logs/junit.xml"
    assert lang.pkg_count_cmd(W) == "python -m pip list --format=freeze 2>/dev/null | wc -l"


def test_python_gate_pass_matches_repo2run():
    lang = get_language("python")
    assert lang.gate_pass(0) is True
    assert lang.gate_pass(5) is True
    assert lang.gate_pass(2) is False


def test_python_satisfies_language_protocol():
    from bench.languages import Language
    assert isinstance(get_language("python"), Language)
