# tests/bench/test_measure_shell.py
#
# Every command measure() runs inside the container goes through _sh(). Whether that is a LOGIN
# shell is not a style detail -- it decides which Python interpreter the benchmark measures.
#
# `bash -l` sources /etc/profile, and the Debian default overwrites PATH unconditionally
# (/etc/profile:5). That discards the `ENV PATH` the Dockerfile set. Graph2Env ends its images with
#     ENV PATH="/testbed/.venv/bin:/app/.venv/bin:${PATH}"
# so under a login shell the gate ran against the SYSTEM interpreter instead of the project venv:
# baserow/baserow reported 10 installed packages and could not import `requests`, while the very
# same image collected cleanly for another harness. 25 of 50 rat50 repos were scored collect_error
# and EBSR came out 0.42 against a true ~0.92.
#
# bench/languages/golang.py documents the same /etc/profile reset and works around it by prepending
# PATH to every Go command. These tests exist so the shared fix is not quietly reverted back into
# that per-language whack-a-mole.
import pytest

from bench.contract import _probe_script
from bench.measure import _sh


def test_container_commands_do_not_run_in_a_login_shell():
    assert _sh("echo hi") == ["bash", "-c", "echo hi"]


def test_the_login_flag_is_absent_because_it_resets_path():
    # -l in any spelling ("-l", "-lc", "--login") re-sources /etc/profile.
    flags = [a for a in _sh("x") if a.startswith("-")]
    assert not any("l" in f.lstrip("-") or f == "--login" for f in flags), (
        f"login shell reintroduced via {flags}: /etc/profile overwrites PATH and the image's "
        "ENV PATH (e.g. a project .venv) is discarded")


def test_the_command_is_passed_through_untouched():
    cmd = "python -m pytest --collect-only -q --disable-warnings /testbed"
    assert _sh(cmd)[-1] == cmd


@pytest.mark.parametrize("cmd", ["a && b", "x | y", "f() { :; }; f", 'echo "q"'])
def test_shell_syntax_still_reaches_bash(cmd):
    # -c keeps full shell semantics; only the login-time profile sourcing is dropped.
    assert _sh(cmd) == ["bash", "-c", cmd]


def test_the_testbed_probe_uses_the_same_non_login_shell():
    # contract.probe_testbed builds its own argv rather than calling _sh, so it can drift
    # independently. It runs in the same container and needs the same PATH.
    import inspect

    from bench import contract

    src = inspect.getsource(contract)
    assert '"-lc"' not in src and "'-lc'" not in src, (
        "probe_testbed is back on a login shell; repo_toplevel and the conformance verdict "
        "would again be computed against the wrong interpreter")
    assert '"bash", "-c"' in src


def test_probe_script_is_still_produced():
    # Guard the import above against silently testing an empty string.
    assert "/testbed" in _probe_script("/testbed")
