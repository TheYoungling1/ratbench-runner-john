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
        "mkdir -p /testbed/logs; "
        "python -m pytest --version >/dev/null 2>&1 || "
        "python -m pip install -q --break-system-packages pytest || python -m pip install -q pytest || true; "
        "if ! python -c 'import pytest_timeout' >/dev/null 2>&1 && "
        "python -c 'import pytest, sys; sys.exit(int(pytest.__version__.split(\".\")[0]) < 7)' "
        ">/dev/null 2>&1; then "
        "{ python -m pip install -q --no-deps --break-system-packages pytest-timeout "
        "|| python -m pip install -q --no-deps pytest-timeout; } "
        "&& touch /testbed/logs/.bench_timeout_installed; fi; "
        "if python -c 'import pytest_timeout' >/dev/null 2>&1 && "
        "python -m pytest --timeout=120 --timeout-method=signal --help >/dev/null 2>&1; "
        "then touch /testbed/logs/.bench_timeout_ok; "
        "elif [ -f /testbed/logs/.bench_timeout_installed ]; then "
        "python -m pip uninstall -y -q --break-system-packages pytest-timeout "
        "|| python -m pip uninstall -y -q pytest-timeout; fi; true")
    assert lang.gate_cmd(W) == (
        "unset PYTEST_ADDOPTS PYTEST_PLUGINS; "
        "python -m pytest --collect-only -q --disable-warnings; exit ${PIPESTATUS[0]:-$?}")
    assert lang.collect_cmd(W) == (
        "unset PYTEST_ADDOPTS PYTEST_PLUGINS; "
        "python -m pytest --co -q --continue-on-collection-errors 2>&1 || true")
    assert lang.run_cmd(W, "/testbed/logs/junit.xml") == (
        'F=""; [ -f /testbed/logs/.bench_timeout_ok ] && F="--timeout=120 --timeout-method=signal"; '
        "unset PYTEST_ADDOPTS PYTEST_PLUGINS; "
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
