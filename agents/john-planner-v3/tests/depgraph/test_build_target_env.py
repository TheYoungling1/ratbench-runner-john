"""Task 7 — ``build_dep_graph`` builds ONE ``TargetEnv`` (via
``detect_target_env``) instead of two separate ``_detect_target_python`` /
``_detect_target_platform`` probes, and threads it (as ``target_env``) into
:func:`resolve_closure` so the HOST never leaks into the resolve.

``uv.lock`` is universal/cross-platform: ``uv lock`` accepts ``--python``
(the interpreter target) but has NO ``--python-platform`` flag -- platform
targeting happens downstream at PARSE time, via ``parse_uv_lock``/
``native_risk_from_lock`` evaluating the detected/overridden
``target_env.python_platform_tag`` (and the raw ``platform_machine`` inside
``target_env``) against the lock's markers and wheel artifacts. These tests
therefore assert the detected/overridden platform tag reaches THAT seam
(``resolve_closure``'s ``target_env`` argument), and that the emitted
``uv lock`` command itself never carries ``--python-platform``.
``target_python`` / ``target_platform`` remain accepted as caller overrides
that patch the detected env (tests / callers that already know the target
skip trusting the probe for that field)."""

from __future__ import annotations

import python_deps.depgraph.build as build_module
from conftest import FakeExecutor, make_result

from python_deps.depgraph.build import build_dep_graph


def _make_repo(tmp_path):
    # Declared-only roots: a manifest dep must exist for a root to reach the
    # resolver and trigger `uv lock` (imports never generate roots). PyYAML is
    # the declared root; the `yaml` import is kept only as scan realism. An
    # undeclared import would now never reach the resolver, defeating these
    # tests' purpose of exercising target_env/platform-tag threading through
    # `uv lock`.
    (tmp_path / "app.py").write_text("import yaml\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="fx"\nversion="0"\ndependencies=["PyYAML"]\n'
    )
    return str(tmp_path)


def _lock_calls(ex):
    return [c for c in ex.calls if "uv lock" in c]


def _spy_on_resolve_closure(monkeypatch):
    """Capture the ``target_env`` build_dep_graph threads into resolve_closure,
    while delegating to the real implementation so end-to-end behavior
    (including the FakeExecutor-driven lock failure/retry path) is unchanged."""
    captured: dict = {}
    original = build_module.resolve_closure

    def _spy(*args, **kwargs):
        captured["target_env"] = kwargs["target_env"]
        return original(*args, **kwargs)

    monkeypatch.setattr(build_module, "resolve_closure", _spy)
    return captured


def test_build_lock_command_carries_detected_platform_tag(tmp_path, monkeypatch):
    # No probe response canned -> detect_target_env degrades to the default
    # (x86_64-manylinux_2_28); that tag must reach resolve_closure's
    # target_env, and the emitted `uv lock` command must NOT carry
    # --python-platform (uv `lock` has no such flag).
    captured = _spy_on_resolve_closure(monkeypatch)
    ex = FakeExecutor(default=make_result(returncode=127))
    build_dep_graph(_make_repo(tmp_path), ex, host_executor=ex)

    assert captured["target_env"].python_platform_tag == "x86_64-manylinux_2_28"
    lock_calls = _lock_calls(ex)
    assert lock_calls, "build_dep_graph must attempt `uv lock`"
    assert "--python-platform" not in lock_calls[0]


def test_build_lock_command_reflects_detected_arm_musl_target(tmp_path, monkeypatch):
    captured = _spy_on_resolve_closure(monkeypatch)
    ex = FakeExecutor(
        responses={
            "import platform,sys,os": make_result(
                stdout="3.12.1 posix linux aarch64 Linux\n"
            ),
            "ldd --version": make_result(
                returncode=1, stderr="musl libc (aarch64)\n"
            ),
        },
        default=make_result(returncode=127),
    )
    build_dep_graph(_make_repo(tmp_path), ex, host_executor=ex)

    assert captured["target_env"].python_platform_tag == "aarch64-musllinux_1_2"
    lock_calls = _lock_calls(ex)
    assert lock_calls
    assert "--python 3.12" in lock_calls[0]
    assert "--python-platform" not in lock_calls[0]


def test_build_target_python_override_wins_over_detected_probe(tmp_path):
    # The probe reports 3.9; an explicit target_python must still win.
    ex = FakeExecutor(
        responses={
            "import platform,sys,os": make_result(
                stdout="3.9.0 posix linux x86_64 Linux\n"
            ),
        },
        default=make_result(returncode=127),
    )
    build_dep_graph(
        _make_repo(tmp_path), ex, host_executor=ex, target_python="3.13"
    )

    lock_calls = _lock_calls(ex)
    assert lock_calls
    assert "--python 3.13" in lock_calls[0]


def test_build_target_platform_override_wins_over_detected_probe(tmp_path, monkeypatch):
    # The probe (silently) reports x86_64; an explicit target_platform must
    # win -- and must reach resolve_closure's target_env (the parse-time
    # seam), not a `uv lock --python-platform` flag (uv `lock` has none).
    captured = _spy_on_resolve_closure(monkeypatch)
    ex = FakeExecutor(default=make_result(returncode=127))
    build_dep_graph(
        _make_repo(tmp_path),
        ex,
        host_executor=ex,
        target_platform="aarch64-manylinux_2_28",
    )

    assert captured["target_env"].python_platform_tag == "aarch64-manylinux_2_28"
    lock_calls = _lock_calls(ex)
    assert lock_calls
    assert "--python-platform" not in lock_calls[0]
