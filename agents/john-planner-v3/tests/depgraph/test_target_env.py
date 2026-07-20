"""Task 7 — ``TargetEnv``: one env describing the TARGET container, not the host.

``packaging.markers.Marker.evaluate()`` fills any key missing from the dict you
pass it from ``packaging.markers.default_environment()`` — which reflects the
HOST running the resolve, not the container being built. On a non-x86_64/
non-linux dev host this silently mis-evaluates ``sys_platform`` /
``platform_machine`` / ``os_name`` gated dependencies. ``TargetEnv.marker_env()``
must return every PEP 508 field so no key is ever left for the host default to
fill in.
"""

from __future__ import annotations

from python_deps.depgraph.executor import CommandResult
from python_deps.depgraph.target_env import TargetEnv, detect_target_env


def _target(**overrides) -> TargetEnv:
    base = dict(
        python_full="3.11.0",
        python_version="3.11",
        platform_machine="x86_64",
        sys_platform="linux",
        os_name="posix",
        platform_system="Linux",
        python_platform_tag="x86_64-manylinux_2_28",
    )
    base.update(overrides)
    return TargetEnv(**base)


def test_marker_env_has_all_platform_fields():
    t = _target()
    env = t.marker_env()
    assert env["platform_machine"] == "x86_64"
    assert env["sys_platform"] == "linux"
    assert env["os_name"] == "posix"


def test_marker_env_has_both_python_keys():
    t = _target(python_full="3.12.4", python_version="3.12")
    env = t.marker_env()
    assert env["python_version"] == "3.12"
    assert env["python_full_version"] == "3.12.4"


def test_marker_env_has_platform_system():
    t = _target(platform_system="Linux")
    assert t.marker_env()["platform_system"] == "Linux"


def test_marker_env_has_implementation_trio():
    # marker_env() now covers the interpreter-implementation fields the target
    # confidently controls, so `packaging` never host-fills them.
    env = _target().marker_env()
    assert env["platform_python_implementation"] == "CPython"
    assert env["implementation_name"] == "cpython"
    # CPython: implementation_version renders exactly python_full.
    assert env["implementation_version"] == "3.11.0"


def test_marker_env_implementation_version_tracks_python_full():
    env = _target(python_full="3.12.4", python_version="3.12").marker_env()
    assert env["implementation_version"] == "3.12.4"


def test_marker_env_withholds_only_kernel_and_extra():
    # Genuinely-unknowable kernel strings + the per-requirement `extra` flag are
    # DELIBERATELY absent (roots._env_marker_excludes keeps deps gated on them).
    env = _target().marker_env()
    assert "platform_release" not in env
    assert "platform_version" not in env
    assert "extra" not in env


def test_marker_env_threads_probed_pypy_implementation():
    env = _target(
        platform_python_implementation="PyPy", implementation_name="pypy"
    ).marker_env()
    assert env["platform_python_implementation"] == "PyPy"
    assert env["implementation_name"] == "pypy"


def test_target_env_is_frozen():
    t = _target()
    try:
        t.python_version = "3.9"  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("TargetEnv must be frozen (immutable)")


class _FakeExecutor:
    """Minimal Executor returning canned results keyed by command substring."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls: list[str] = []

    def run(self, command, *, timeout=300):
        self.calls.append(command)
        for key, (rc, out, err) in self.mapping.items():
            if key in command:
                return CommandResult(command=command, returncode=rc, stdout=out, stderr=err)
        return CommandResult(command=command, returncode=127, stdout="", stderr="not found")


def test_detect_target_env_parses_probe_output():
    ex = _FakeExecutor(
        {
            "import platform,sys,os": (
                0,
                "3.11.4 posix linux x86_64 Linux\n",
                "",
            ),
            "ldd --version": (0, "ldd (GNU libc) 2.36\n", ""),
        }
    )
    t = detect_target_env(ex)
    assert t.python_full == "3.11.4"
    assert t.python_version == "3.11"
    assert t.os_name == "posix"
    assert t.sys_platform == "linux"
    assert t.platform_machine == "x86_64"
    assert t.platform_system == "Linux"
    assert t.python_platform_tag == "x86_64-manylinux_2_28"


def test_detect_target_env_threads_probed_implementation():
    # The extended 7-token probe reports platform.python_implementation() and
    # sys.implementation.name; detect_target_env threads both onto the TargetEnv.
    ex = _FakeExecutor(
        {
            "import platform,sys,os": (
                0,
                "7.3.16 posix linux x86_64 Linux PyPy pypy\n",
                "",
            ),
            "ldd --version": (0, "ldd (GNU libc) 2.36\n", ""),
        }
    )
    t = detect_target_env(ex)
    assert t.platform_python_implementation == "PyPy"
    assert t.implementation_name == "pypy"
    # And a marker gated on the implementation now sees the probed value.
    assert t.marker_env()["platform_python_implementation"] == "PyPy"


def test_detect_target_env_legacy_5token_probe_defaults_cpython():
    # A 5-token probe output (no trailing implementation tokens) must still parse
    # the python/platform facts and degrade the implementation to CPython — the
    # only base this pipeline builds — never crash or lose the row.
    ex = _FakeExecutor(
        {
            "import platform,sys,os": (0, "3.11.4 posix linux x86_64 Linux\n", ""),
            "ldd --version": (0, "ldd (GNU libc) 2.36\n", ""),
        }
    )
    t = detect_target_env(ex)
    assert t.python_full == "3.11.4"
    assert t.platform_python_implementation == "CPython"
    assert t.implementation_name == "cpython"


def test_detect_target_env_arm_musl_maps_to_musllinux():
    ex = _FakeExecutor(
        {
            "import platform,sys,os": (
                0,
                "3.12.1 posix linux aarch64 Linux\n",
                "",
            ),
            "ldd --version": (1, "", "musl libc (aarch64)\nVersion 1.2.3\n"),
        }
    )
    t = detect_target_env(ex)
    assert t.platform_machine == "aarch64"
    assert t.python_platform_tag == "aarch64-musllinux_1_2"


def test_detect_target_env_degrades_to_defaults_on_probe_failure():
    ex = _FakeExecutor({})  # every command 127s.
    t = detect_target_env(ex)
    # Never crashes; sensible glibc/linux/x86_64 defaults.
    assert t.sys_platform == "linux"
    assert t.os_name == "posix"
    assert t.python_platform_tag.endswith("manylinux_2_28")


def test_detect_target_env_degrades_on_malformed_probe_output():
    ex = _FakeExecutor({"import platform,sys,os": (0, "garbage\n", "")})
    t = detect_target_env(ex)
    assert t.sys_platform == "linux"
    assert t.python_platform_tag.endswith("manylinux_2_28")


def test_detect_target_env_never_crashes_on_executor_exception():
    class _RaisingExecutor:
        def run(self, command, *, timeout=300):
            raise RuntimeError("container is gone")

    t = detect_target_env(_RaisingExecutor())
    assert t.sys_platform == "linux"
    assert t.os_name == "posix"


# ── python3-only probe, no `python` fallback (review P2) ─────────────────────
#
# The rest of the construction/certification pipeline (build.py's runtime
# check, runtime_classify.py's import checks, emit.py's install steps)
# hard-codes `python3`. A `python`-only image is not a supported target: a
# `python` fallback here would let detection SUCCEED on an image the rest of
# the pipeline cannot actually build (it would later fail with `python3: not
# found`). So absent/unusable `python3` must degrade to the known-good
# default TargetEnv, not probe a `python` shim.


def test_python3_absent_degrades_to_default():
    """No `python3` in the (simulated) image => the probe command is not-ok,
    so detection degrades to the default TargetEnv rather than trying a
    `python` fallback."""
    ex = _FakeExecutor({})  # every command 127s — simulates python3 absent.
    t = detect_target_env(ex)
    assert t.python_version == "3.11"
    assert t.python_full == "3.11.0"
    assert t.platform_machine == "x86_64"
    assert ex.calls, "detect_target_env never invoked the executor"
    probe_cmd = ex.calls[0]
    assert probe_cmd.startswith("python3 -c")
    assert " || " not in probe_cmd
    assert "python -c" not in probe_cmd


# ── machine-alias normalization: tag-only, NOT the marker value (review P2 fix) ─
#
# The target is always a Linux container: `arm64`/`amd64` are non-canonical
# aliases some images report instead of `aarch64`/`x86_64` — the values wheel
# tags expect. But `packaging` evaluates PEP 508 `platform_machine` markers
# against `platform.machine()` VERBATIM, so `TargetEnv.platform_machine` must
# stay RAW (un-normalized) or a marker like `platform_machine == 'arm64'`
# would wrongly evaluate False. Normalization applies ONLY to
# `python_platform_tag` (the wheel/uv platform-tag naming; consumed at PARSE
# time by parse_uv_lock/native_risk_from_lock -- `uv lock` itself has no
# `--python-platform` flag, unlike `uv pip compile`/`uv export`).


def test_machine_alias_arm64_kept_raw_for_marker_but_tag_normalized():
    ex = _FakeExecutor(
        {
            "import platform,sys,os": (
                0,
                "3.12.1 posix linux arm64 Linux\n",
                "",
            ),
            "ldd --version": (0, "ldd (GNU libc) 2.36\n", ""),
        }
    )
    t = detect_target_env(ex)
    # RAW — feeds marker_env() / PEP 508 evaluation verbatim.
    assert t.platform_machine == "arm64"
    # Normalized — feeds the wheel/uv platform tag (parse-time matching) only.
    assert t.python_platform_tag.startswith("aarch64-")


def test_machine_alias_amd64_kept_raw_for_marker_but_tag_normalized():
    ex = _FakeExecutor(
        {
            "import platform,sys,os": (
                0,
                "3.12.1 posix linux amd64 Linux\n",
                "",
            ),
            "ldd --version": (0, "ldd (GNU libc) 2.36\n", ""),
        }
    )
    t = detect_target_env(ex)
    assert t.platform_machine == "amd64"
    assert t.python_platform_tag.startswith("x86_64-")


def test_machine_x86_64_probe_raw_and_tag_both_x86_64():
    ex = _FakeExecutor(
        {
            "import platform,sys,os": (
                0,
                "3.12.1 posix linux x86_64 Linux\n",
                "",
            ),
            "ldd --version": (0, "ldd (GNU libc) 2.36\n", ""),
        }
    )
    t = detect_target_env(ex)
    assert t.platform_machine == "x86_64"
    assert t.python_platform_tag.startswith("x86_64-")


# ── Python-2 rejection (review P2, bug 2) ────────────────────────────────────
#
# Some images alias `python3` to a legacy 2.x interpreter. That must NOT be
# accepted: it would make `uv lock --python 2.7` disagree with build.py's
# runtime node (always python3), a self-inconsistent graph. Degrade to the
# known-good default instead — this is input validation on the probe output,
# independent of the (now-removed) `python` fallback.


def test_python2_probe_degrades_to_default():
    ex = _FakeExecutor(
        {
            "import platform,sys,os": (
                0,
                "2.7.18 posix linux x86_64 Linux\n",
                "",
            ),
            "ldd --version": (0, "ldd (GNU libc) 2.36\n", ""),
        }
    )
    t = detect_target_env(ex)
    assert t.python_version == "3.11"
    assert t.python_full == "3.11.0"
    assert t.platform_machine == "x86_64"
