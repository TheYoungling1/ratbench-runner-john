"""Resolver v2 — ``uv.lock``-driven Package closure (resolve.py).

Pure parsers (``parse_uv_lock`` / ``native_risk_from_lock`` /
``parse_resolver_error``) are driven by CANNED ``uv.lock`` and CANNED stderr
text — no real uv, no Docker, no network.  The ``resolve_closure`` orchestrator
is exercised with a FakeExecutor that returns rc0 while the test pre-writes the
temp-project ``uv.lock`` (covering the read path), plus stubs that simulate lock
failure (diagnosis), per-root resilience, and the degraded ``uv pip compile``
fallback.

One test (marked ``@pytest.mark.integration``, skipped without a real ``uv`` on
PATH) runs the actual ``_lock_command`` shell command through a real
subprocess against a real tiny project -- a mocked ``FakeExecutor`` "succeeds"
regardless of whether the command is valid uv CLI syntax, so it can never
catch a uv-API drift (e.g. uv 0.10.4 rejecting ``uv lock --python-platform``).
"""

from __future__ import annotations

import os
import shutil
from types import SimpleNamespace

import pytest

from python_deps.depgraph.executor import CommandResult
from python_deps.depgraph.resolve import (
    DEFAULT_TARGET_PLATFORM,
    UV_BIN,
    _diagnosis_to_graph,
    _offending_root_names,
    native_risk_from_lock,
    parse_resolver_error,
    parse_uv_lock,
    resolve_closure,
)
from python_deps.depgraph.schema import (
    DiscoveredBy,
    EdgeType,
    Layer,
    NodeType,
    State,
)
from python_deps.depgraph.target_env import TargetEnv

# --------------------------------------------------------------------------- #
# Canned uv.lock fixtures.
# --------------------------------------------------------------------------- #
# Covers: a virtual root (skipped), a multi-dep package (pandas), a marker'd dep
# (pandas -> python-dateutil), an sdist-only package (psycopg2 -> build), a
# wheel-matching package (numpy), and a wrong-platform-wheel+sdist package
# (pillow -> build on linux).
CANNED_LOCK = """\
version = 1
requires-python = ">=3.11"

[[package]]
name = "depgraph-resolve-root"
version = "0.0.0"
source = { virtual = "." }
dependencies = [
    { name = "opencv-python" },
    { name = "pandas" },
    { name = "psycopg2" },
    { name = "pillow" },
]

[[package]]
name = "numpy"
version = "1.26.4"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://files.pythonhosted.org/x/numpy-1.26.4.tar.gz", hash = "sha256:np-sdist", size = 10 }
wheels = [
    { url = "https://files.pythonhosted.org/x/numpy-1.26.4-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", hash = "sha256:np-wheel", size = 20 },
]

[[package]]
name = "opencv-python"
version = "4.9.0.80"
source = { registry = "https://pypi.org/simple" }
dependencies = [
    { name = "numpy" },
]
wheels = [
    { url = "https://files.pythonhosted.org/x/opencv_python-4.9.0.80-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", hash = "sha256:cv-wheel" },
]

[[package]]
name = "pandas"
version = "2.2.2"
source = { registry = "https://pypi.org/simple" }
dependencies = [
    { name = "numpy" },
    { name = "python-dateutil", marker = "python_version >= '3.8'" },
]
wheels = [
    { url = "https://files.pythonhosted.org/x/pandas-2.2.2-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", hash = "sha256:pd-wheel" },
]

[[package]]
name = "python-dateutil"
version = "2.9.0"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "https://files.pythonhosted.org/x/python_dateutil-2.9.0-py2.py3-none-any.whl", hash = "sha256:du-wheel" },
]

[[package]]
name = "psycopg2"
version = "2.9.9"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://files.pythonhosted.org/x/psycopg2-2.9.9.tar.gz", hash = "sha256:pg-sdist" }

[[package]]
name = "pillow"
version = "10.3.0"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://files.pythonhosted.org/x/pillow-10.3.0.tar.gz", hash = "sha256:pil-sdist" }
wheels = [
    { url = "https://files.pythonhosted.org/x/pillow-10.3.0-cp311-cp311-macosx_11_0_arm64.whl", hash = "sha256:pil-mac" },
]
"""

# Task 8 (targeted extras) — a minimal lock as if the "test" optional-deps
# group were in scope (pytest + its transitive iniconfig), vs. a lock with
# only the runtime dep. Used to prove a `.[test]`-scoped closure carries the
# group's transitive deps while a no-extras closure does not.
CANNED_LOCK_WITH_TEST_EXTRA = """\
version = 1
requires-python = ">=3.11"

[[package]]
name = "depgraph-resolve-root"
version = "0.0.0"
source = { virtual = "." }
dependencies = [
    { name = "requests" },
    { name = "pytest" },
]

[[package]]
name = "requests"
version = "2.32.3"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "https://files.pythonhosted.org/x/requests-2.32.3-py3-none-any.whl", hash = "sha256:req-wheel" },
]

[[package]]
name = "pytest"
version = "8.3.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [
    { name = "iniconfig" },
]
wheels = [
    { url = "https://files.pythonhosted.org/x/pytest-8.3.0-py3-none-any.whl", hash = "sha256:pytest-wheel" },
]

[[package]]
name = "iniconfig"
version = "2.0.0"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "https://files.pythonhosted.org/x/iniconfig-2.0.0-py3-none-any.whl", hash = "sha256:ini-wheel" },
]
"""

CANNED_LOCK_NO_EXTRA = """\
version = 1
requires-python = ">=3.11"

[[package]]
name = "depgraph-resolve-root"
version = "0.0.0"
source = { virtual = "." }
dependencies = [
    { name = "requests" },
]

[[package]]
name = "requests"
version = "2.32.3"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "https://files.pythonhosted.org/x/requests-2.32.3-py3-none-any.whl", hash = "sha256:req-wheel" },
]
"""

LINUX_X86 = "x86_64-manylinux_2_28"
LINUX_ARM = "aarch64-manylinux_2_28"


def _target_env(
    platform_tag: str = DEFAULT_TARGET_PLATFORM,
    python_version: str = "3.11",
    *,
    machine: str | None = None,
) -> TargetEnv:
    """Test TargetEnv builder: python_version + a NORMALIZED platform_tag ->
    a full TargetEnv, with the linux/posix facts this codebase always targets.

    ``machine`` defaults to the tag's own leading arch token, which is fine
    for every test that only cares about canonical arches (LINUX_X86 /
    LINUX_ARM / DEFAULT_TARGET_PLATFORM are already canonical). Tests proving
    RAW != NORMALIZED (the Task 7 wiring bug) pass ``machine`` explicitly so
    the built TargetEnv's ``platform_machine`` diverges from its
    ``python_platform_tag``, exactly like a real container reporting a
    non-canonical alias (e.g. ``"arm64"``) alongside a normalized wheel tag.
    """
    parts = [p for p in python_version.split(".") if p]
    full = ".".join((parts + ["0", "0"])[:3]) if parts else python_version
    version = ".".join(parts[:2]) if len(parts) >= 2 else python_version
    arch = machine or (platform_tag.split("-", 1)[0] if platform_tag else "x86_64")
    return TargetEnv(
        python_full=full,
        python_version=version,
        platform_machine=arch,
        sys_platform="linux",
        os_name="posix",
        platform_system="Linux",
        python_platform_tag=platform_tag,
    )


def _node_by_name(nodes):
    return {n.name: n for n in nodes}


def _edge_set(edges):
    return {(e.src, e.dst) for e in edges}


# --------------------------------------------------------------------------- #
# parse_uv_lock
# --------------------------------------------------------------------------- #
def test_parse_uv_lock_emits_package_nodes_and_skips_local_root():
    nodes, _edges = parse_uv_lock(CANNED_LOCK)
    by_name = _node_by_name(nodes)

    # The synthetic virtual root project is NOT a distribution node.
    assert "depgraph-resolve-root" not in by_name
    assert set(by_name) == {
        "numpy",
        "opencv-python",
        "pandas",
        "python-dateutil",
        "psycopg2",
        "pillow",
    }

    cv = by_name["opencv-python"]
    assert cv.type is NodeType.PACKAGE
    assert cv.layer is Layer.PIP
    assert cv.discovered_by is DiscoveredBy.RESOLVER
    assert cv.version == "4.9.0.80"
    assert cv.state is State.UNKNOWN
    assert cv.id == "pkg:opencv-python==4.9.0.80"
    assert cv.check_command == "python -m pip show opencv-python"
    assert cv.fix_candidates == ("pip:opencv-python",)
    assert cv.chosen_fix == "pip:opencv-python"


def test_parse_uv_lock_emits_transitive_requires_edges():
    nodes, edges = parse_uv_lock(CANNED_LOCK)
    by_name = _node_by_name(nodes)
    es = _edge_set(edges)

    # opencv-python -> numpy and pandas -> numpy.
    assert (by_name["opencv-python"].id, by_name["numpy"].id) in es
    assert (by_name["pandas"].id, by_name["numpy"].id) in es
    # pandas -> python-dateutil.
    assert (by_name["pandas"].id, by_name["python-dateutil"].id) in es

    for e in edges:
        assert e.relation is EdgeType.REQUIRES
        assert e.origin == "resolver"


def test_parse_uv_lock_carries_dependency_marker_on_edge():
    nodes, edges = parse_uv_lock(CANNED_LOCK)
    by_name = _node_by_name(nodes)
    pandas_id = by_name["pandas"].id
    dateutil_id = by_name["python-dateutil"].id

    marked = [e for e in edges if e.src == pandas_id and e.dst == dateutil_id]
    assert len(marked) == 1
    assert marked[0].marker == "python_version >= '3.8'"

    # An unconditional dep carries no marker.
    numpy_edge = [
        e
        for e in edges
        if e.src == pandas_id and e.dst == by_name["numpy"].id
    ]
    assert numpy_edge[0].marker is None


# --------------------------------------------------------------------------- #
# native_risk_from_lock
# --------------------------------------------------------------------------- #
def test_native_risk_sdist_only_builds_from_source():
    risk = native_risk_from_lock(CANNED_LOCK, LINUX_X86)
    pg = risk["psycopg2"]
    assert pg["build_from_source"] is True
    assert pg["artifact"] == "psycopg2-2.9.9.tar.gz"
    assert pg["hash"] == "sha256:pg-sdist"


def test_native_risk_matching_wheel_no_build():
    risk = native_risk_from_lock(CANNED_LOCK, LINUX_X86)
    np = risk["numpy"]
    assert np["build_from_source"] is False
    # Chosen artifact is the platform-matching wheel, not the sdist.
    assert np["artifact"].endswith(".whl")
    assert "x86_64" in np["artifact"]
    assert np["hash"] == "sha256:np-wheel"


def test_native_risk_wrong_platform_wheel_builds_from_source():
    # pillow ships only a macOS arm64 wheel + an sdist -> must build on linux.
    risk = native_risk_from_lock(CANNED_LOCK, LINUX_X86)
    pil = risk["pillow"]
    assert pil["build_from_source"] is True
    assert pil["artifact"] == "pillow-10.3.0.tar.gz"
    assert pil["hash"] == "sha256:pil-sdist"


def test_native_risk_universal_wheel_matches_any_platform():
    risk = native_risk_from_lock(CANNED_LOCK, LINUX_ARM)
    du = risk["python-dateutil"]
    assert du["build_from_source"] is False
    assert du["artifact"].endswith("-none-any.whl")


def test_native_risk_arch_specific_match_for_aarch64_misses_x86_wheel():
    # On aarch64 the x86_64-only numpy wheel does NOT match -> build from source.
    risk = native_risk_from_lock(CANNED_LOCK, LINUX_ARM)
    assert risk["numpy"]["build_from_source"] is True
    assert risk["numpy"]["artifact"] == "numpy-1.26.4.tar.gz"


# --------------------------------------------------------------------------- #
# Forked lock: one package resolved to TWO versions across python markers.
# Real uv.lock output for `opencv-python + numpy` under requires-python>=3.11:
# numpy forks into 2.4.6 (python<3.12) and 2.5.0 (python>=3.12).  Feeding BOTH
# to `pip install numpy==2.4.6 numpy==2.5.0` errors, so only the version whose
# resolution-markers match the target python must be emitted (container-accurate).
# --------------------------------------------------------------------------- #
FORKED_LOCK = """\
version = 1
requires-python = ">=3.11"
resolution-markers = [
    "python_full_version >= '3.12'",
    "python_full_version < '3.12'",
]

[[package]]
name = "depgraph-resolve-root"
version = "0.0.0"
source = { virtual = "." }
dependencies = [
    { name = "opencv-python" },
]

[[package]]
name = "numpy"
version = "2.4.6"
source = { registry = "https://pypi.org/simple" }
resolution-markers = [
    "python_full_version < '3.12'",
]
wheels = [
    { url = "https://x/numpy-2.4.6-cp311-cp311-manylinux_2_28_aarch64.whl", hash = "sha256:np246" },
]

[[package]]
name = "numpy"
version = "2.5.0"
source = { registry = "https://pypi.org/simple" }
resolution-markers = [
    "python_full_version >= '3.12'",
]
wheels = [
    { url = "https://x/numpy-2.5.0-cp312-cp312-manylinux_2_28_aarch64.whl", hash = "sha256:np250" },
]

[[package]]
name = "opencv-python"
version = "4.13.0.92"
source = { registry = "https://pypi.org/simple" }
dependencies = [
    { name = "numpy", marker = "python_full_version < '3.12'" },
    { name = "numpy", marker = "python_full_version >= '3.12'" },
]
wheels = [
    { url = "https://x/opencv_python-4.13.0.92-cp37-abi3-manylinux_2_28_aarch64.whl", hash = "sha256:cv" },
]
"""


def test_parse_uv_lock_selects_single_version_for_target_python():
    # Target 3.11 -> only numpy 2.4.6 (python<3.12) survives the fork.
    nodes, edges = parse_uv_lock(FORKED_LOCK, target_python="3.11")
    numpys = [n for n in nodes if n.name == "numpy"]
    assert len(numpys) == 1, [n.version for n in numpys]
    assert numpys[0].version == "2.4.6"
    # The opencv->numpy edge points at the kept (2.4.6) node, not a dangling 2.5.0.
    np_id = numpys[0].id
    cv = next(n for n in nodes if n.name == "opencv-python")
    assert any(e.src == cv.id and e.dst == np_id for e in edges)


def test_parse_uv_lock_selects_newer_version_for_py312():
    nodes, _edges = parse_uv_lock(FORKED_LOCK, target_python="3.12")
    numpys = [n for n in nodes if n.name == "numpy"]
    assert len(numpys) == 1
    assert numpys[0].version == "2.5.0"


def test_parse_uv_lock_without_target_python_keeps_all_versions():
    # Backward-compatible default: no python target -> no fork resolution.
    nodes, _edges = parse_uv_lock(FORKED_LOCK)
    numpys = [n for n in nodes if n.name == "numpy"]
    assert len(numpys) == 2


# A package included ONLY via a marker-gated edge (the real markitdown case:
# `audioop-lts`, a Python-3.13 stdlib backport pulled by SpeechRecognition under
# `python_full_version >= '3.13'`). On a 3.11 target it is NOT part of the
# environment and has no installable distribution there — keeping it as a node
# made the whole install collapse. It must be pruned for 3.11, kept for 3.13.
CONDITIONAL_LOCK = """\
version = 1
requires-python = ">=3.11"

[[package]]
name = "depgraph-resolve-root"
version = "0.0.0"
source = { virtual = "." }
dependencies = [
    { name = "speechrecognition" },
]

[[package]]
name = "speechrecognition"
version = "3.17.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [
    { name = "typing-extensions" },
    { name = "audioop-lts", marker = "python_full_version >= '3.13'" },
]
wheels = [
    { url = "https://x/SpeechRecognition-3.17.0-py3-none-any.whl", hash = "sha256:sr" },
]

[[package]]
name = "typing-extensions"
version = "4.12.2"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "https://x/typing_extensions-4.12.2-py3-none-any.whl", hash = "sha256:te" },
]

[[package]]
name = "audioop-lts"
version = "0.2.2"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "https://x/audioop_lts-0.2.2-cp313-cp313-manylinux_2_28_x86_64.whl", hash = "sha256:al" },
]
"""


def test_parse_uv_lock_prunes_conditional_dep_below_marker():
    # Target 3.11: audioop-lts (only reachable via `python>='3.13'`) is pruned;
    # the unconditional deps stay.
    nodes, _edges = parse_uv_lock(CONDITIONAL_LOCK, target_python="3.11")
    names = {n.name for n in nodes}
    assert "audioop-lts" not in names
    assert {"speechrecognition", "typing-extensions"} <= names


def test_parse_uv_lock_keeps_conditional_dep_when_marker_holds():
    # Target 3.13: the marker holds, so audioop-lts IS part of the environment.
    nodes, _edges = parse_uv_lock(CONDITIONAL_LOCK, target_python="3.13")
    assert "audioop-lts" in {n.name for n in nodes}


def test_native_risk_forked_lock_uses_target_python_version():
    # Target 3.11 -> risk keyed to numpy reflects the 2.4.6 wheel, not 2.5.0.
    risk = native_risk_from_lock(FORKED_LOCK, LINUX_ARM, target_python="3.11")
    assert (
        risk["numpy"]["artifact"]
        == "numpy-2.4.6-cp311-cp311-manylinux_2_28_aarch64.whl"
    )
    assert risk["numpy"]["hash"] == "sha256:np246"


# --------------------------------------------------------------------------- #
# Task 7: marker evaluation must honor the TARGET, never the HOST running the
# resolve (``resolve_lock._marker_env`` / ``TargetEnv``).
# --------------------------------------------------------------------------- #
def test_x86_gated_dep_kept_when_target_is_x86_even_on_arm_host():
    from python_deps.depgraph.resolve_lock import _marker_applies, _marker_env
    from python_deps.depgraph.target_env import TargetEnv

    target = TargetEnv(
        python_full="3.11.0",
        python_version="3.11",
        platform_machine="x86_64",
        sys_platform="linux",
        os_name="posix",
        platform_system="Linux",
        python_platform_tag="x86_64-manylinux_2_28",
    )
    assert _marker_applies("platform_machine == 'x86_64'", _marker_env(target)) is True
    assert _marker_applies("sys_platform == 'win32'", _marker_env(target)) is False


# A package forked across ``platform_machine`` (not just python version) — the
# real leak vector: with only the two python keys, ``packaging`` fills
# ``platform_machine`` from the HOST's ``default_environment()``, so the
# outcome used to depend on which machine ran the resolve. Deterministic here
# regardless of host proves the leak is closed.
PLATFORM_FORKED_LOCK = """\
version = 1
requires-python = ">=3.11"

[[package]]
name = "depgraph-resolve-root"
version = "0.0.0"
source = { virtual = "." }
dependencies = [
    { name = "onnxruntime" },
]

[[package]]
name = "onnxruntime"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [
    { name = "arm-only-dep", marker = "platform_machine == 'aarch64'" },
]
wheels = [
    { url = "https://x/onnxruntime-1.0.0-py3-none-any.whl", hash = "sha256:ort" },
]

[[package]]
name = "arm-only-dep"
version = "2.0.0"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "https://x/arm_only_dep-2.0.0-py3-none-any.whl", hash = "sha256:arm" },
]
"""


def test_parse_uv_lock_prunes_platform_gated_dep_for_x86_target():
    nodes, _edges = parse_uv_lock(
        PLATFORM_FORKED_LOCK, target_python="3.11", target_platform=LINUX_X86
    )
    names = {n.name for n in nodes}
    assert "arm-only-dep" not in names
    assert "onnxruntime" in names


def test_parse_uv_lock_keeps_platform_gated_dep_for_arm_target():
    nodes, _edges = parse_uv_lock(
        PLATFORM_FORKED_LOCK, target_python="3.11", target_platform=LINUX_ARM
    )
    assert "arm-only-dep" in {n.name for n in nodes}


# A dep gated on the RAW, non-canonical machine alias Docker Desktop's Apple
# Silicon containers actually report (``platform.machine() == "arm64"``), never
# the canonical ``"aarch64"`` wheel-tag arch. Real uv.lock markers only ever
# reference canonical arches, but any PEP 508 ``platform_machine`` marker is
# evaluated against whatever the container's ``platform.machine()`` literally
# printed -- this proves that raw string, not a normalized stand-in, is what
# reaches evaluation.
RAW_MACHINE_FORKED_LOCK = """\
version = 1
requires-python = ">=3.11"

[[package]]
name = "depgraph-resolve-root"
version = "0.0.0"
source = { virtual = "." }
dependencies = [
    { name = "onnxruntime" },
]

[[package]]
name = "onnxruntime"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [
    { name = "arm64-only-dep", marker = "platform_machine == 'arm64'" },
]
wheels = [
    { url = "https://x/onnxruntime-1.0.0-py3-none-any.whl", hash = "sha256:ort" },
]

[[package]]
name = "arm64-only-dep"
version = "2.0.0"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "https://x/arm64_only_dep-2.0.0-py3-none-any.whl", hash = "sha256:arm64" },
]
"""


def test_resolve_closure_keeps_raw_machine_gated_dep_for_arm64_container(tmp_path):
    """END-TO-END regression (review Critical, Task 7): the wiring from
    ``build.py`` through ``resolve_closure`` into marker evaluation must carry
    the container's RAW ``platform_machine`` (e.g. Docker Desktop's Apple
    Silicon containers report ``platform.machine() == "arm64"``), never the
    NORMALIZED ``--python-platform`` wheel tag (``"aarch64-manylinux_2_28"``).

    Before the fix, ``resolve_closure`` only accepted the two decomposed
    strings (``target_python`` / the NORMALIZED ``target_platform`` tag) and
    ``resolve_lock._target_env_for`` rebuilt ``platform_machine`` by splitting
    that tag -- which can only ever recover the canonical arch (``"aarch64"``),
    never the raw alias (``"arm64"``) a real container reports. A dep gated on
    ``platform_machine == 'arm64'`` was therefore wrongly PRUNED even when the
    real target machine literally was ``"arm64"``. Now the actual
    :class:`TargetEnv` (carrying the RAW machine) is threaded straight through,
    so the dep is correctly KEPT.
    """
    (tmp_path / "uv.lock").write_text(RAW_MACHINE_FORKED_LOCK)
    ex = _lock_ok_executor()

    target = _target_env(LINUX_ARM, machine="arm64")
    assert target.platform_machine == "arm64"  # RAW, non-canonical
    assert target.python_platform_tag == LINUX_ARM  # NORMALIZED -- deliberately differs

    nodes, _edges = resolve_closure(
        [(None, "onnxruntime")],
        ex,
        target_env=target,
        project_dir=str(tmp_path),
    )

    names = {n.name for n in nodes}
    assert "onnxruntime" in names
    assert "arm64-only-dep" in names  # KEPT: marker evaluated against the RAW machine


# --------------------------------------------------------------------------- #
# parse_resolver_error
# --------------------------------------------------------------------------- #
REGISTRY_MISS_STDERR = (
    "  x No solution found when resolving dependencies:\n"
    "  `-> Because nonexistent-pkg was not found in the package registry and you "
    "require nonexistent-pkg, we can conclude that your requirements are "
    "unsatisfiable.\n"
)

NO_VERSION_STDERR = (
    "  `-> Because there is no version of badpkg==9.9.9 and you require "
    "badpkg==9.9.9, we can conclude that your requirements are unsatisfiable.\n"
)

CONFLICT_STDERR = (
    "  `-> Because package-b depends on package-a>=2.0 and you require "
    "package-a<2.0, we can conclude that your requirements are unsatisfiable.\n"
)

PYTHON_INCOMPAT_STDERR = (
    "  `-> Because the current Python version (3.10.12) does not satisfy "
    "Python>=3.11 and you require shiny-lib, we can conclude that your "
    "requirements are unsatisfiable.\n"
)


def test_parse_error_registry_miss():
    diag = parse_resolver_error(REGISTRY_MISS_STDERR)
    assert [m.name for m in diag.missing] == ["nonexistent-pkg"]
    assert diag.missing[0].version is None
    assert "not found in the" in diag.missing[0].evidence


def test_parse_error_no_version():
    diag = parse_resolver_error(NO_VERSION_STDERR)
    names = {m.name: m.version for m in diag.missing}
    assert names == {"badpkg": "9.9.9"}


def test_parse_error_version_conflict_with_bounds():
    diag = parse_resolver_error(CONFLICT_STDERR)
    assert len(diag.conflicts) == 1
    conflict = diag.conflicts[0]
    assert conflict.package == "package-a"
    bounds = {conflict.left.specifier, conflict.right.specifier}
    assert bounds == {">=2.0", "<2.0"}
    # The transitive constraint is attributed to package-b.
    imposers = {conflict.left.imposed_by, conflict.right.imposed_by}
    assert "package-b" in imposers
    assert None in imposers  # the root `you require` side.


# Real `uv lock` (uv 0.10.4) output for a direct shared-dep conflict: the project
# pins urllib3<1.21 while requests==2.32.3 needs urllib3>=1.21.1,<3.  uv attributes
# the root pin to "your project", which must NOT leak in as a package node.
UV_0_10_CONFLICT_STDERR = (
    "  × No solution found when resolving dependencies:\n"
    "  ╰─▶ Because requests==2.32.3 depends on urllib3>=1.21.1,<3 and your project\n"
    "      depends on requests==2.32.3, we can conclude that your project depends\n"
    "      on urllib3>=1.21.1,<3.\n"
    "      And because your project depends on urllib3<1.21, we can conclude that\n"
    "      your project's requirements are unsatisfiable.\n"
)


def test_parse_error_real_uv_0_10_conflict_maps_root_to_shared_pkg():
    """Real uv attributes the root pin to 'your project'; the conflict edge must
    join the real packages (requests <-> urllib3), not the synthetic root, and the
    shared package with no satisfiable version must be MISSING with evidence."""
    diag = parse_resolver_error(UV_0_10_CONFLICT_STDERR)
    assert len(diag.conflicts) == 1

    nodes, edges = _diagnosis_to_graph(diag)
    conflict_edges = [e for e in edges if e.relation is EdgeType.CONFLICTS_WITH]
    assert len(conflict_edges) == 1
    edge = conflict_edges[0]

    by_id = {n.id: n for n in nodes}
    endpoint_names = {by_id[edge.src].name, by_id[edge.dst].name}
    assert endpoint_names == {"requests", "urllib3"}
    assert {edge.data["src_bound"], edge.data["dst_bound"]} == {">=1.21.1", "<1.21"}
    assert edge.data["package"] == "urllib3"

    # The synthetic resolve root never leaks in as a package node.
    assert "project" not in {n.name for n in nodes}

    # The shared package (no satisfiable version) is MISSING with evidence; the
    # imposing distribution stays UNKNOWN (it resolves fine on its own).
    urllib3 = next(n for n in nodes if n.name == "urllib3")
    requests = next(n for n in nodes if n.name == "requests")
    assert urllib3.state is State.MISSING
    assert urllib3.evidence
    assert requests.state is State.UNKNOWN


# Real `uv lock` (uv 0.10.4) output for a missing root: uv wraps the message, so
# "... not\n      found in the package registry" spans a newline + indent.
UV_0_10_WRAPPED_MISSING_STDERR = (
    "  × No solution found when resolving dependencies:\n"
    "  ╰─▶ Because this-package-surely-does-not-exist-zzz999 was not\n"
    "      found in the package registry and your project depends on\n"
    "      this-package-surely-does-not-exist-zzz999, we can conclude that your\n"
    "      project's requirements are unsatisfiable.\n"
)


def test_parse_error_registry_miss_tolerates_line_wrapping():
    """uv wraps long lines; the registry-miss match must span the wrap."""
    diag = parse_resolver_error(UV_0_10_WRAPPED_MISSING_STDERR)
    assert [m.name for m in diag.missing] == [
        "this-package-surely-does-not-exist-zzz999"
    ]


def test_parse_error_python_incompat():
    diag = parse_resolver_error(PYTHON_INCOMPAT_STDERR)
    assert diag.python_incompat is not None
    assert diag.python_incompat.floor == ">=3.11"


def test_parse_error_clean_stderr_is_empty_diagnosis():
    diag = parse_resolver_error("nothing interesting here")
    assert diag.missing == ()
    assert diag.conflicts == ()
    assert diag.python_incompat is None


# Real `uv lock` (uv 0.10.4) output when a root's sdist metadata build fails (an
# old Py2-only setup.py: `factory==1.2` raises NameError on Python 3).  This is
# NOT a version conflict or registry miss, so without explicit handling the
# diagnosis is empty and the offending root can never be attributed/dropped — the
# whole closure collapses (P0).  uv prints the failing dist on the `Failed to
# build` header and (wrapped) on the `help:` line.
BUILD_FAILURE_STDERR = (
    "Using CPython 3.11.14\n"
    "  × Failed to build `factory==1.2`\n"
    "  ├─▶ The build backend returned an error\n"
    "  ╰─▶ Call to `setuptools.build_meta:__legacy__.build_wheel` failed (exit\n"
    "      status: 1)\n"
    "\n"
    "      [stderr]\n"
    "      Traceback (most recent call last):\n"
    "      NameError: name 'file' is not defined. Did you mean: 'filter'?\n"
    "\n"
    "      hint: This usually indicates a problem with the package or the build\n"
    "      environment.\n"
    "  help: `factory` (v1.2) was included because `depgraph-resolve-root` (v0.0.0)\n"
    "        depends on `factory`\n"
)


def test_parse_error_build_failure_is_attributed_as_missing():
    """A root whose sdist fails to BUILD must be surfaced (so the drop-retry can
    attribute and drop it), not silently ignored. P0 regression."""
    diag = parse_resolver_error(BUILD_FAILURE_STDERR)
    names = {m.name: m.version for m in diag.missing}
    assert names == {"factory": "1.2"}
    assert "Failed to build" in diag.missing[0].evidence
    # and it must be reported as an offending root so the loop drops it.
    assert "factory" in _offending_root_names(diag, set())


def test_parse_error_build_failure_emits_missing_node():
    """The build-failed dist becomes a MISSING package node carrying the build
    error as evidence (so the graph records WHY it was dropped)."""
    diag = parse_resolver_error(BUILD_FAILURE_STDERR)
    nodes, _edges = _diagnosis_to_graph(diag)
    factory = next(n for n in nodes if n.name == "factory")
    assert factory.state is State.MISSING
    assert "Failed to build" in factory.evidence


# Real `uv lock` output (RATBench: sooperset/mcp-atlassian) when a root resolves
# only to a YANKED, empty release: uv concludes "all versions of X cannot be
# used". Not a conflict/registry-miss/build-fail -> previously unattributable.
YANKED_STDERR = (
    "Using CPython 3.11.14\n"
    "  × No solution found when resolving dependencies:\n"
    "  ╰─▶ Because atlassian==0.0.0 was yanked (reason: empty release) and only\n"
    "      atlassian==0.0.0 is available, we can conclude that all versions of\n"
    "      atlassian cannot be used.\n"
    "      And because your project depends on atlassian, we can conclude that your\n"
    "      project's requirements are unsatisfiable.\n"
)

# Real `uv lock` output (RATBench: docling) when a root requires a Python the
# target lacks: "X==V cannot be used" — and uv WRAPS "cannot\n      be used"
# across a line, so the matcher must tolerate whitespace runs inside the phrase.
REQUIRES_PYTHON_UNUSABLE_STDERR = (
    "Using CPython 3.11.14\n"
    "  × No solution found when resolving dependencies for split (markers:\n"
    "  │ python_full_version == '3.11.*'):\n"
    "  ╰─▶ Because the requested Python version (>=3.11) does not\n"
    "      satisfy Python>=3.12,<3.13 and nemotron-ocr==2.0.0 depends on\n"
    "      Python>=3.12,<3.13, we can conclude that nemotron-ocr==2.0.0 cannot\n"
    "      be used.\n"
    "      And because only nemotron-ocr<=2.0.0 is available and your project\n"
    "      depends on nemotron-ocr>=2.0.0, we can conclude that your project's\n"
    "      requirements are unsatisfiable.\n"
)


def test_parse_error_yanked_root_cannot_be_used():
    """A root with only a yanked release ("all versions of X cannot be used") is
    attributed so the drop-retry can drop it (RATBench mcp-atlassian)."""
    diag = parse_resolver_error(YANKED_STDERR)
    assert "atlassian" in {m.name for m in diag.missing}
    assert "atlassian" in _offending_root_names(diag, set())


def test_parse_error_requires_python_unusable_root_wrapped():
    """A root requiring an incompatible Python ("X==V cannot be used") is
    attributed even when uv wraps "cannot\\n be used" across a line (docling)."""
    diag = parse_resolver_error(REQUIRES_PYTHON_UNUSABLE_STDERR)
    assert "nemotron-ocr" in {m.name for m in diag.missing}
    assert "nemotron-ocr" in _offending_root_names(diag, set())


def test_parse_error_version_conflict_not_misread_as_unusable():
    """Regression: a genuine version tug-of-war says "unsatisfiable" but never
    "cannot be used", so the new matcher must NOT manufacture a spurious missing
    (which would wrongly drop a conflict root instead of recording the edge)."""
    diag = parse_resolver_error(UV_0_10_CONFLICT_STDERR)
    # urllib3 is the no-satisfiable-version shared package (a real missing), but
    # neither root (requests/your project) is attributed as "cannot be used".
    assert "requests" not in {m.name for m in diag.missing}


def test_conflict_drops_shared_package_not_imposers():
    """A conflict drop-retry must drop only the shared/conflicted package when it
    is itself a current root (a direct pin), never the imposing roots. Dropping
    an imposer collapses its whole subtree to a diagnostic stub instead of
    letting uv pull a consistent version transitively and recording the
    conflict as an advisory edge."""
    # project pins a<2.0 ; package-b requires a>=2.0  -> shared package = "a"
    conflict = SimpleNamespace(
        package="a",
        left=SimpleNamespace(imposed_by="project"),
        right=SimpleNamespace(imposed_by="package-b"),
    )
    diag = SimpleNamespace(missing=[], conflicts=[conflict])
    # "a" is itself a current root (a direct pin), so it is the one dropped.
    names = _offending_root_names(diag, {"a", "package-b"})
    assert "a" in names                 # the pin/shared root is dropped and retried
    assert "package-b" not in names     # the imposing root must be KEPT


def test_transitive_conflict_drops_one_imposer_not_shared():
    """When the conflicted package is purely TRANSITIVE (nobody's root), dropping
    its own name would not shrink the root set at all — the retry loop would see
    `remaining == current` and break, collapsing the whole closure to the
    degraded fallback (review P1 regression). Instead exactly ONE imposing root
    must be dropped so the retry can make progress, while the other imposer's
    subtree survives."""
    # package-b requires a>=2.0 ; package-c requires a<2.0 -> shared package "a"
    # is nobody's root (purely transitive).
    conflict = SimpleNamespace(
        package="a",
        left=SimpleNamespace(imposed_by="package-b"),
        right=SimpleNamespace(imposed_by="package-c"),
    )
    diag = SimpleNamespace(missing=[], conflicts=[conflict])
    names = _offending_root_names(diag, {"package-b", "package-c"})
    assert "a" not in names  # dropping it wouldn't shrink the root set anyway
    # exactly one imposer is dropped so the retry still makes progress, and the
    # other imposer's subtree survives.
    assert len(names & {"package-b", "package-c"}) == 1


def test_transitive_conflict_prefers_root_imposer():
    """Review P1 fix: when only ONE of the two immediate imposers is itself a
    current root, that root must be the one dropped — dropping the non-root
    immediate imposer can't shrink the root set, so the retry would stall
    (`remaining == current`) and collapse to the degraded fallback.

    foo -> b -> a<2 ; bar -> a>=2.  parse_resolver_error reports the immediate
    imposers "b" (not a root; "foo" is) and "bar" (a root). The fix must pick
    "bar", never "b"."""
    conflict = SimpleNamespace(
        package="a",
        left=SimpleNamespace(imposed_by="b"),
        right=SimpleNamespace(imposed_by="bar"),
    )
    diag = SimpleNamespace(missing=[], conflicts=[conflict])
    names = _offending_root_names(diag, {"foo", "bar"})
    assert "bar" in names
    assert "b" not in names
    assert "a" not in names


def test_deep_transitive_conflict_adds_no_nonroot():
    """When NEITHER immediate imposer is a current root, the immediate-imposer
    diagnosis can't name a droppable root — adding a non-root imposer would not
    shrink the root set (false progress), so nothing should be added for this
    conflict."""
    conflict = SimpleNamespace(
        package="a",
        left=SimpleNamespace(imposed_by="b"),
        right=SimpleNamespace(imposed_by="c"),
    )
    diag = SimpleNamespace(missing=[], conflicts=[conflict])
    names = _offending_root_names(diag, {"foo", "baz"})
    assert names & {"foo", "baz", "a", "b", "c"} == set()


# --------------------------------------------------------------------------- #
# resolve_closure — success path (read the temp-project uv.lock).
# --------------------------------------------------------------------------- #
ROOTS = [
    ("import:cv2", "opencv-python"),
    ("import:PIL", "pillow"),
    (None, "pandas"),  # manifest-declared root (no Import node).
]


def _lock_ok_executor():
    from conftest import FakeExecutor  # type: ignore

    return FakeExecutor(
        responses={
            "uv lock": CommandResult("uv lock", 0, "", ""),
        }
    )


def test_resolve_closure_reads_lock_and_builds_graph(tmp_path):
    # Pre-write the uv.lock the orchestrator will read from the temp project dir.
    (tmp_path / "uv.lock").write_text(CANNED_LOCK)
    ex = _lock_ok_executor()

    nodes, edges = resolve_closure(
        ROOTS,
        ex,
        target_env=_target_env(LINUX_X86),
        project_dir=str(tmp_path),
    )

    by_name = _node_by_name(nodes)
    assert "opencv-python" in by_name
    assert "numpy" in by_name
    assert "pandas" in by_name

    # The lock command ran (host-side resolve), and a pyproject was written.
    assert any("uv lock" in c for c in ex.calls)
    assert UV_BIN in ex.calls[0]
    assert (tmp_path / "pyproject.toml").exists()

    es = _edge_set(edges)
    # Import->Package for the scanned roots.
    assert ("import:cv2", by_name["opencv-python"].id) in es
    assert ("import:PIL", by_name["pillow"].id) in es
    # Manifest root (import_id=None) has no Import->Package edge.
    assert not any(e.src is None for e in edges)
    # Package->Package transitive.
    assert (by_name["opencv-python"].id, by_name["numpy"].id) in es


def test_resolve_closure_stamps_targeting_and_native_risk(tmp_path):
    (tmp_path / "uv.lock").write_text(CANNED_LOCK)
    ex = _lock_ok_executor()

    nodes, _edges = resolve_closure(
        ROOTS,
        ex,
        target_env=_target_env(LINUX_X86),
        project_dir=str(tmp_path),
    )
    by_name = _node_by_name(nodes)

    np = by_name["numpy"]
    assert np.resolved_python == "3.11"
    assert np.resolved_platform == LINUX_X86
    assert np.build_from_source is False
    assert np.artifact.endswith(".whl")
    assert np.hash == "sha256:np-wheel"

    pg = by_name["psycopg2"]
    assert pg.build_from_source is True
    assert pg.artifact == "psycopg2-2.9.9.tar.gz"


def test_resolve_closure_default_platform_when_none(tmp_path):
    (tmp_path / "uv.lock").write_text(CANNED_LOCK)
    ex = _lock_ok_executor()

    nodes, _edges = resolve_closure(
        ROOTS, ex, target_env=_target_env(), project_dir=str(tmp_path)
    )
    np = _node_by_name(nodes)["numpy"]
    assert np.resolved_platform == DEFAULT_TARGET_PLATFORM


# --------------------------------------------------------------------------- #
# Task 8 — targeted extras: resolve_closure(extras=...) writes scope into the
# temp pyproject, and a roots list that includes the needed group's members
# (as roots.select_roots would produce) yields their transitive deps.
# --------------------------------------------------------------------------- #
def test_resolve_closure_with_extras_includes_group_transitive_deps(tmp_path):
    (tmp_path / "uv.lock").write_text(CANNED_LOCK_WITH_TEST_EXTRA)
    ex = _lock_ok_executor()
    # roots as select_roots(..., needed_extras={"test"}) would produce: the
    # runtime dep plus the "test" group's own member.
    roots = [(None, "requests"), (None, "pytest")]

    nodes, _edges = resolve_closure(
        roots,
        ex,
        target_env=_target_env(),
        extras=frozenset({"test"}),
        project_dir=str(tmp_path),
    )
    names = {n.name for n in nodes}
    assert "pytest" in names
    assert "iniconfig" in names  # pytest's transitive dep -- would vanish if dropped

    pyproject = (tmp_path / "pyproject.toml").read_text()
    assert "[project.optional-dependencies]" in pyproject
    assert "test = []" in pyproject


def test_resolve_closure_without_extras_excludes_group(tmp_path):
    (tmp_path / "uv.lock").write_text(CANNED_LOCK_NO_EXTRA)
    ex = _lock_ok_executor()
    roots = [(None, "requests")]  # no "test" group member (needed_extras=frozenset())

    nodes, _edges = resolve_closure(
        roots, ex, target_env=_target_env(), project_dir=str(tmp_path)
    )
    names = {n.name for n in nodes}
    assert "pytest" not in names
    assert "iniconfig" not in names

    pyproject = (tmp_path / "pyproject.toml").read_text()
    assert "[project.optional-dependencies]" not in pyproject


def test_resolve_closure_empty_roots_returns_empty():
    ex = _lock_ok_executor()
    assert resolve_closure([], ex, target_env=_target_env()) == ([], [])


# --------------------------------------------------------------------------- #
# resolve_closure — failure -> diagnosis graph.
# --------------------------------------------------------------------------- #
def test_resolve_closure_emits_conflict_edge_on_lock_failure(tmp_path):
    from conftest import FakeExecutor  # type: ignore

    ex = FakeExecutor(
        responses={
            "uv lock": CommandResult("uv lock", 1, "", CONFLICT_STDERR),
            # Fallback also fails so we keep just the diagnosis graph.
            "uv pip compile": CommandResult("uv pip compile", 1, "", "no solution"),
        }
    )
    # Roots not implicated by the conflict -> no drop/retry, straight to diagnosis.
    roots = [("import:flask", "flask")]
    nodes, edges = resolve_closure(
        roots, ex, target_env=_target_env(), project_dir=str(tmp_path)
    )

    conflict_edges = [e for e in edges if e.relation is EdgeType.CONFLICTS_WITH]
    assert len(conflict_edges) == 1
    ce = conflict_edges[0]
    assert ce.data["package"] == "package-a"
    assert {ce.data["src_bound"], ce.data["dst_bound"]} == {">=2.0", "<2.0"}
    assert "package-a" in ce.data["evidence"]

    by_name = _node_by_name(nodes)
    assert "package-a" in by_name
    assert "package-b" in by_name


def test_resolve_closure_emits_missing_node_on_registry_miss(tmp_path):
    from conftest import FakeExecutor  # type: ignore

    ex = FakeExecutor(
        responses={
            "uv lock": CommandResult("uv lock", 1, "", REGISTRY_MISS_STDERR),
            "uv pip compile": CommandResult("uv pip compile", 1, "", "fail"),
        }
    )
    roots = [(None, "nonexistent-pkg")]
    nodes, _edges = resolve_closure(
        roots, ex, target_env=_target_env(), project_dir=str(tmp_path)
    )
    by_name = _node_by_name(nodes)
    assert "nonexistent-pkg" in by_name
    assert by_name["nonexistent-pkg"].state is State.MISSING
    assert "not found in the" in by_name["nonexistent-pkg"].evidence


def test_resolve_closure_emits_interpreter_node_on_python_incompat(tmp_path):
    from conftest import FakeExecutor  # type: ignore

    ex = FakeExecutor(
        responses={
            "uv lock": CommandResult("uv lock", 1, "", PYTHON_INCOMPAT_STDERR),
            "uv pip compile": CommandResult("uv pip compile", 1, "", "fail"),
        }
    )
    nodes, edges = resolve_closure(
        [(None, "shiny-lib")], ex, target_env=_target_env(), project_dir=str(tmp_path)
    )
    by_name = _node_by_name(nodes)
    py = by_name.get("python")
    assert py is not None
    assert py.layer is Layer.INTERPRETER
    assert py.version == ">=3.11"
    assert py.state is State.MISSING

    # spec §"Conflict/failure → graph": python-version incompat emits a
    # conflicts_with edge from the imposing package to the interpreter need.
    assert "shiny-lib" in by_name
    conflict_edges = [
        e
        for e in edges
        if e.relation is EdgeType.CONFLICTS_WITH and e.dst == py.id
    ]
    assert len(conflict_edges) == 1
    ce = conflict_edges[0]
    assert ce.src == by_name["shiny-lib"].id
    assert ce.data["package"] == "python"
    assert ce.data["floor"] == ">=3.11"


# --------------------------------------------------------------------------- #
# resolve_closure — per-root resilience.
# --------------------------------------------------------------------------- #
GOOD_LOCK = """\
version = 1
requires-python = ">=3.11"

[[package]]
name = "depgraph-resolve-root"
version = "0.0.0"
source = { virtual = "." }
dependencies = [
    { name = "opencv-python" },
]

[[package]]
name = "numpy"
version = "1.26.4"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "https://x/numpy-1.26.4-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", hash = "sha256:np" },
]

[[package]]
name = "opencv-python"
version = "4.9.0.80"
source = { registry = "https://pypi.org/simple" }
dependencies = [
    { name = "numpy" },
]
wheels = [
    { url = "https://x/opencv_python-4.9.0.80-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", hash = "sha256:cv" },
]
"""


class _ResilientStub:
    """Lock fails the first time (bad root), then succeeds once it's dropped."""

    def __init__(self, workdir, good_lock):
        self.workdir = workdir
        self.good_lock = good_lock
        self.calls = []
        self._lock_calls = 0

    def run(self, command, *, timeout=300):
        self.calls.append(command)
        if "uv lock" in command:
            self._lock_calls += 1
            if self._lock_calls == 1:
                return CommandResult(
                    command,
                    1,
                    "",
                    "  `-> Because badpkg was not found in the package registry "
                    "and you require badpkg, we can conclude ...\n",
                )
            with open(
                os.path.join(self.workdir, "uv.lock"), "w", encoding="utf-8"
            ) as fh:
                fh.write(self.good_lock)
            return CommandResult(command, 0, "", "")
        return CommandResult(command, 127, "", "no fake")


def test_resolve_closure_per_root_resilience_drops_bad_root(tmp_path):
    stub = _ResilientStub(str(tmp_path), GOOD_LOCK)
    roots = [("import:cv2", "opencv-python"), (None, "badpkg")]

    nodes, edges = resolve_closure(
        roots,
        stub,
        target_env=_target_env(LINUX_X86),
        project_dir=str(tmp_path),
    )
    by_name = _node_by_name(nodes)

    # Good root still produced a real closure ...
    assert "opencv-python" in by_name
    assert "numpy" in by_name
    assert by_name["opencv-python"].state is State.UNKNOWN
    # ... and the dropped root is surfaced as a MISSING node with evidence.
    assert "badpkg" in by_name
    assert by_name["badpkg"].state is State.MISSING
    assert "badpkg" in by_name["badpkg"].evidence
    # Import->Package for the surviving root.
    es = _edge_set(edges)
    assert ("import:cv2", by_name["opencv-python"].id) in es
    # Two lock attempts: full set (fail) then good-root-only (succeed).
    assert stub._lock_calls == 2


class _BuildFailStub:
    """Lock fails the first time with an sdist BUILD failure (not a conflict),
    then succeeds once the build-failing root is dropped."""

    def __init__(self, workdir, good_lock, build_fail_stderr):
        self.workdir = workdir
        self.good_lock = good_lock
        self.build_fail_stderr = build_fail_stderr
        self.calls = []
        self._lock_calls = 0

    def run(self, command, *, timeout=300):
        self.calls.append(command)
        if "uv lock" in command:
            self._lock_calls += 1
            if self._lock_calls == 1:
                return CommandResult(command, 1, "", self.build_fail_stderr)
            with open(
                os.path.join(self.workdir, "uv.lock"), "w", encoding="utf-8"
            ) as fh:
                fh.write(self.good_lock)
            return CommandResult(command, 0, "", "")
        return CommandResult(command, 127, "", "no fake")


def test_resolve_closure_drops_build_failing_root_not_collapse(tmp_path):
    """P0: a root whose sdist FAILS TO BUILD must be dropped so the rest of the
    closure still resolves — not collapse the whole graph to empty.

    Mirrors the real Wagtail/pydantic/fastapi failure: `factory`/`pyodide`/
    `strawberry` fail to build, and previously the empty diagnosis made the loop
    give up and return zero packages."""
    stub = _BuildFailStub(str(tmp_path), GOOD_LOCK, BUILD_FAILURE_STDERR)
    roots = [("import:cv2", "opencv-python"), (None, "factory")]

    nodes, _edges = resolve_closure(
        roots,
        stub,
        target_env=_target_env(LINUX_X86),
        project_dir=str(tmp_path),
    )
    by_name = _node_by_name(nodes)

    # The real closure still resolves (NOT a collapse) ...
    assert "opencv-python" in by_name
    assert "numpy" in by_name
    # ... and the build-failing root is surfaced as MISSING with the build error.
    assert "factory" in by_name
    assert by_name["factory"].state is State.MISSING
    assert "Failed to build" in by_name["factory"].evidence
    # Two lock attempts: full set (build-fail) then survivor-only (succeed).
    assert stub._lock_calls == 2


# --------------------------------------------------------------------------- #
# resolve_closure — degraded `uv pip compile` fallback.
# --------------------------------------------------------------------------- #
FALLBACK_CLOSURE = """\
numpy==1.26.4
    # via opencv-python
opencv-python==4.9.0.80
    # via -r -
"""


def test_resolve_closure_falls_back_to_pip_compile_when_no_lock(tmp_path):
    from conftest import FakeExecutor  # type: ignore

    # `uv lock` "succeeds" (rc0) but writes no uv.lock -> lock unavailable ->
    # degraded `uv pip compile` parse takes over.
    ex = FakeExecutor(
        responses={
            "uv lock": CommandResult("uv lock", 0, "", ""),
            "uv pip compile": CommandResult("uv pip compile", 0, FALLBACK_CLOSURE, ""),
        }
    )
    roots = [("import:cv2", "opencv-python")]
    nodes, edges = resolve_closure(
        roots, ex, target_env=_target_env(), project_dir=str(tmp_path)
    )

    by_name = _node_by_name(nodes)
    assert set(by_name) == {"numpy", "opencv-python"}
    # Fallback nodes are flagged by provenance.
    assert by_name["numpy"].provenance == "uv pip compile"
    es = _edge_set(edges)
    assert ("import:cv2", "pkg:opencv-python==4.9.0.80") in es
    # Package->Package from the `# via` annotation.
    assert ("pkg:opencv-python==4.9.0.80", "pkg:numpy==1.26.4") in es


def test_compile_command_feeds_roots_via_heredoc_stdin():
    from python_deps.depgraph.resolve import _compile_command

    cmd = _compile_command(["opencv-python", "pillow"], "3.11")
    assert "<<'DEPGRAPH_REQS'" in cmd
    body = cmd.split("<<'DEPGRAPH_REQS'\n", 1)[1]
    assert "opencv-python\npillow" in body
    assert body.rstrip().endswith("DEPGRAPH_REQS")
    assert " - " in cmd


# --------------------------------------------------------------------------- #
# parse_uv_lock — local-source skipping, canonicalization, dedup.
# --------------------------------------------------------------------------- #
LOCAL_SOURCES_LOCK = """\
version = 1

[[package]]
name = "depgraph-resolve-root"
version = "0.0.0"
source = { virtual = "." }
dependencies = [
    { name = "real-dep" },
]

[[package]]
name = "editable-pkg"
version = "0.1.0"
source = { editable = "." }

[[package]]
name = "directory-pkg"
version = "0.2.0"
source = { directory = "vendor/x" }

[[package]]
name = "real-dep"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "https://x/real_dep-1.0.0-py3-none-any.whl", hash = "sha256:rd" },
]
"""


def test_parse_uv_lock_skips_editable_and_directory_sources():
    nodes, _edges = parse_uv_lock(LOCAL_SOURCES_LOCK)
    by_name = _node_by_name(nodes)
    assert "editable-pkg" not in by_name
    assert "directory-pkg" not in by_name
    assert set(by_name) == {"real-dep"}


def test_parse_uv_lock_canonicalizes_dependency_name_on_edge():
    lock = """\
version = 1

[[package]]
name = "pandas"
version = "2.2.2"
source = { registry = "https://pypi.org/simple" }
dependencies = [
    { name = "Python_DateUtil" },
]

[[package]]
name = "python-dateutil"
version = "2.9.0"
source = { registry = "https://pypi.org/simple" }
"""
    nodes, edges = parse_uv_lock(lock)
    by_name = _node_by_name(nodes)
    # The differently-cased/separated dep name still resolves to the node.
    assert (by_name["pandas"].id, by_name["python-dateutil"].id) in _edge_set(edges)


def test_parse_uv_lock_skips_dep_not_in_lock():
    lock = """\
version = 1

[[package]]
name = "pkg-a"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [
    { name = "ghost-dep" },
]
"""
    _nodes, edges = parse_uv_lock(lock)
    # A dep with no corresponding [[package]] node produces no dangling edge.
    assert edges == []


def test_parse_uv_lock_dedups_repeated_dependency_edge():
    lock = """\
version = 1

[[package]]
name = "pkg-a"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [
    { name = "pkg-b" },
    { name = "pkg-b" },
]

[[package]]
name = "pkg-b"
version = "2.0"
source = { registry = "https://pypi.org/simple" }
"""
    _nodes, edges = parse_uv_lock(lock)
    assert len(edges) == 1


# --------------------------------------------------------------------------- #
# native_risk_from_lock — remaining artifact branches.
# --------------------------------------------------------------------------- #
def test_native_risk_wheel_only_wrong_platform_no_sdist_no_build():
    lock = """\
version = 1

[[package]]
name = "macwheel"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "https://x/macwheel-1.0-cp311-cp311-macosx_11_0_arm64.whl", hash = "sha256:mw" },
]
"""
    risk = native_risk_from_lock(lock, LINUX_X86)["macwheel"]
    # No sdist -> cannot build; the (non-matching) wheel is the only artifact.
    assert risk["build_from_source"] is False
    assert risk["artifact"] == "macwheel-1.0-cp311-cp311-macosx_11_0_arm64.whl"
    assert risk["hash"] == "sha256:mw"


def test_native_risk_no_artifacts_yields_none():
    lock = """\
version = 1

[[package]]
name = "bare"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
"""
    risk = native_risk_from_lock(lock, LINUX_X86)["bare"]
    assert risk == {"build_from_source": False, "artifact": None, "hash": None}


def test_native_risk_uses_explicit_filename_field():
    lock = """\
version = 1

[[package]]
name = "foo"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
sdist = { filename = "foo-1.0.tar.gz", hash = "sha256:f" }
"""
    risk = native_risk_from_lock(lock, LINUX_X86)["foo"]
    assert risk["artifact"] == "foo-1.0.tar.gz"
    assert risk["build_from_source"] is True


# --------------------------------------------------------------------------- #
# parse_resolver_error — remaining branches.
# --------------------------------------------------------------------------- #
def test_parse_error_no_version_plain():
    diag = parse_resolver_error(
        "there is no version of badpkg and you require badpkg, we conclude ..."
    )
    assert diag.missing[0].name == "badpkg"
    assert diag.missing[0].version is None


def test_parse_error_python_incompat_requires_form():
    diag = parse_resolver_error(
        "Because shiny-lib==1.0 requires Python>=3.12 and you require shiny-lib, "
        "we can conclude that your requirements are unsatisfiable."
    )
    assert diag.python_incompat is not None
    assert diag.python_incompat.floor == ">=3.12"
    assert diag.python_incompat.imposer == "shiny-lib"


def test_parse_error_multiple_distinct_conflicts():
    stderr = (
        "Because package-b depends on package-a>=2.0 and you require package-a<2.0, "
        "and package-d depends on package-c>=3.0 and you require package-c<3.0, "
        "we can conclude that your requirements are unsatisfiable."
    )
    diag = parse_resolver_error(stderr)
    assert len(diag.conflicts) == 2
    by_pkg = {c.package: c for c in diag.conflicts}
    assert {by_pkg["package-a"].left.specifier, by_pkg["package-a"].right.specifier} == {
        ">=2.0",
        "<2.0",
    }
    assert {by_pkg["package-c"].left.specifier, by_pkg["package-c"].right.specifier} == {
        ">=3.0",
        "<3.0",
    }


def test_parse_error_exposes_constraints_tuple():
    diag = parse_resolver_error(CONFLICT_STDERR)
    pairs = {(c.package, c.specifier, c.imposed_by) for c in diag.constraints}
    assert ("package-a", ">=2.0", "package-b") in pairs
    assert ("package-a", "<2.0", None) in pairs


# --------------------------------------------------------------------------- #
# resolve_closure — resilience edge cases + corrupt-lock resilience.
# --------------------------------------------------------------------------- #
def test_resolve_closure_drops_all_roots_emits_only_diagnosis(tmp_path):
    from conftest import FakeExecutor  # type: ignore

    stderr = (
        "Because bada was not found in the package registry and you require bada, "
        "and badb was not found in the package registry and you require badb, "
        "we can conclude that your requirements are unsatisfiable."
    )
    ex = FakeExecutor(
        responses={
            "uv lock": CommandResult("uv lock", 1, "", stderr),
            "uv pip compile": CommandResult("uv pip compile", 1, "", "fail"),
        }
    )
    nodes, edges = resolve_closure(
        [(None, "bada"), (None, "badb")],
        ex,
        target_env=_target_env(),
        project_dir=str(tmp_path),
    )
    by_name = _node_by_name(nodes)
    # Only the diagnosis remains: both dropped roots are MISSING, no real closure.
    assert by_name["bada"].state is State.MISSING
    assert by_name["badb"].state is State.MISSING
    assert not any(n.state is State.UNKNOWN for n in nodes)


class _MultiBadStub:
    """Lock fails twice (one bad root per attempt) then succeeds on the survivors."""

    def __init__(self, workdir, good_lock):
        self.workdir = workdir
        self.good_lock = good_lock
        self.calls = []
        self._lock_calls = 0

    def run(self, command, *, timeout=300):
        self.calls.append(command)
        if "uv lock" in command:
            self._lock_calls += 1
            if self._lock_calls == 1:
                return CommandResult(
                    command, 1, "",
                    "Because bada was not found in the package registry and you "
                    "require bada, we conclude ...\n",
                )
            if self._lock_calls == 2:
                return CommandResult(
                    command, 1, "",
                    "Because badb was not found in the package registry and you "
                    "require badb, we conclude ...\n",
                )
            with open(
                os.path.join(self.workdir, "uv.lock"), "w", encoding="utf-8"
            ) as fh:
                fh.write(self.good_lock)
            return CommandResult(command, 0, "", "")
        return CommandResult(command, 127, "", "no fake")


def test_resolve_closure_drops_multiple_bad_roots_over_iterations(tmp_path):
    stub = _MultiBadStub(str(tmp_path), GOOD_LOCK)
    roots = [("import:cv2", "opencv-python"), (None, "bada"), (None, "badb")]
    nodes, _edges = resolve_closure(
        roots,
        stub,
        target_env=_target_env(LINUX_X86),
        project_dir=str(tmp_path),
    )
    by_name = _node_by_name(nodes)
    assert "opencv-python" in by_name  # survivors still produced a closure
    assert by_name["bada"].state is State.MISSING
    assert by_name["badb"].state is State.MISSING
    assert stub._lock_calls == 3  # full, drop-bada, drop-badb(success)


def test_resolve_closure_self_conflict_emits_no_conflict_edge(tmp_path):
    from conftest import FakeExecutor  # type: ignore

    # Both bounds are root `you require` on the same package -> no distinct
    # endpoints -> the conflicts_with edge must be skipped.
    stderr = (
        "Because you require package_a<2.0 and you require package-a>=2.0, we can "
        "conclude that your requirements are unsatisfiable."
    )
    ex = FakeExecutor(
        responses={
            "uv lock": CommandResult("uv lock", 1, "", stderr),
            "uv pip compile": CommandResult("uv pip compile", 1, "", "fail"),
        }
    )
    _nodes, edges = resolve_closure(
        [(None, "flask")], ex, target_env=_target_env(), project_dir=str(tmp_path)
    )
    assert [e for e in edges if e.relation is EdgeType.CONFLICTS_WITH] == []


def test_resolve_closure_survives_corrupt_lock(tmp_path):
    from conftest import FakeExecutor  # type: ignore

    # uv exits 0 but writes a truncated/corrupt lock (disk-full/NFS hiccup): the
    # parse must not crash the pipeline; it falls through to the fallback.
    (tmp_path / "uv.lock").write_text("this is = = not [[[ valid toml")
    ex = FakeExecutor(
        responses={"uv lock": CommandResult("uv lock", 0, "", "")}
    )
    nodes, edges = resolve_closure(
        [(None, "flask")], ex, target_env=_target_env(), project_dir=str(tmp_path)
    )
    assert (nodes, edges) == ([], [])  # no exception; degraded-empty result


# --------------------------------------------------------------------------- #
# Provenance: exclude_newer is recorded on the Package node (spec §2).
# --------------------------------------------------------------------------- #
def test_resolve_closure_stamps_exclude_newer(tmp_path):
    (tmp_path / "uv.lock").write_text(CANNED_LOCK)
    ex = _lock_ok_executor()
    nodes, _edges = resolve_closure(
        ROOTS,
        ex,
        target_env=_target_env(LINUX_X86),
        exclude_newer="2024-01-01",
        project_dir=str(tmp_path),
    )
    assert _node_by_name(nodes)["numpy"].exclude_newer == "2024-01-01"


# --------------------------------------------------------------------------- #
# Shell-injection hardening (review): quoting + name/version validation.
# --------------------------------------------------------------------------- #
def test_lock_command_quotes_caller_supplied_args():
    from python_deps.depgraph.resolve import _lock_command

    cmd = _lock_command("/tmp/wd", "3.11", "2024-01-01")
    assert "--python 3.11" in cmd
    assert "--exclude-newer 2024-01-01" in cmd


def test_lock_command_omits_unsupported_python_platform_flag():
    # `uv lock` (unlike `uv pip compile` / `uv export`) has no
    # `--python-platform` flag -- passing it makes uv reject the whole
    # command (`error: unexpected argument '--python-platform' found`),
    # silently zeroing out every resolve. `uv.lock` is a universal,
    # cross-platform lock; platform targeting happens downstream at PARSE
    # time (`parse_uv_lock`/`native_risk_from_lock`), never via a `uv lock`
    # CLI flag. `--python` is still the correct/supported interpreter target.
    from python_deps.depgraph.resolve import _lock_command

    cmd = _lock_command("/tmp/wd", "3.11", None)
    assert "--python-platform" not in cmd
    assert "--python 3.11" in cmd


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("uv") is None, reason="requires a real `uv` binary on PATH")
def test_lock_command_succeeds_against_real_uv(tmp_path):
    """Regression guard for the uv 0.10.4 API break (`uv lock` rejecting
    `--python-platform`, which a mocked-only ``FakeExecutor`` suite can never
    catch since it "succeeds" regardless of the command's real validity).

    Builds a tiny real project (one real dependency) and runs the EXACT
    command ``_lock_command`` produces through a real subprocess. Skipped
    entirely without a real ``uv`` on PATH, and skipped gracefully on an
    apparent network failure rather than failing CI on flaky connectivity.
    """
    from python_deps.depgraph.executor import LocalSubprocessExecutor
    from python_deps.depgraph.resolve import _lock_command, _write_pyproject

    workdir = str(tmp_path)
    _write_pyproject(workdir, ["shellingham"], "3.11")
    command = _lock_command(workdir, "3.11", None)

    result = LocalSubprocessExecutor().run(command, timeout=60)

    stderr_lower = (result.stderr or "").lower()
    if result.returncode != 0 and any(
        marker in stderr_lower
        for marker in ("could not connect", "network", "timed out", "temporary failure")
    ):
        pytest.skip(f"uv lock failed, looks network-related:\n{result.stderr}")

    assert result.returncode == 0, (
        f"uv lock failed unexpectedly:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert os.path.exists(os.path.join(workdir, "uv.lock"))


def test_write_pyproject_rejects_bad_python_version(tmp_path):
    from python_deps.depgraph.resolve import _write_pyproject

    import pytest

    with pytest.raises(ValueError):
        _write_pyproject(str(tmp_path), ["flask"], "3.11; rm -rf /")


def test_write_pyproject_includes_optional_dependencies_section_for_chosen_extras(tmp_path):
    from python_deps.depgraph.resolve import _write_pyproject

    _write_pyproject(str(tmp_path), ["flask", "pytest"], "3.11", extras=frozenset({"test"}))
    content = (tmp_path / "pyproject.toml").read_text()
    assert "[project.optional-dependencies]" in content
    assert "test = []" in content


def test_write_pyproject_omits_optional_dependencies_section_when_no_extras(tmp_path):
    from python_deps.depgraph.resolve import _write_pyproject

    _write_pyproject(str(tmp_path), ["flask"], "3.11")
    content = (tmp_path / "pyproject.toml").read_text()
    assert "[project.optional-dependencies]" not in content


def test_write_pyproject_drops_unsafe_extras_group_name(tmp_path):
    from python_deps.depgraph.resolve import _write_pyproject

    # An injectable-looking group name must never reach the TOML table key.
    _write_pyproject(
        str(tmp_path), ["flask"], "3.11", extras=frozenset({"test", "]\ninjected = true"})
    )
    content = (tmp_path / "pyproject.toml").read_text()
    assert "test = []" in content
    assert "injected" not in content


def test_compile_command_drops_injectable_dist_name():
    from python_deps.depgraph.resolve import _compile_command

    cmd = _compile_command(["flask", "evil\nDEPGRAPH_REQS\nrm -rf /"], "3.11")
    body = cmd.split("<<'DEPGRAPH_REQS'\n", 1)[1]
    assert "flask" in body
    assert "rm -rf" not in body  # the injectable name was filtered out


def test_safe_dist_names_keeps_version_specifiers():
    from python_deps.depgraph.resolve import _safe_dist_names

    # Constrained requirement tokens are kept (needed for conflict detection) ...
    kept = _safe_dist_names(["urllib3<1.21", "numpy>=2,<3", "requests==2.32.3"])
    assert kept == ["urllib3<1.21", "numpy>=2,<3", "requests==2.32.3"]
    # ... while injectable / marker'd / spaced tokens are dropped.
    dropped = _safe_dist_names(
        ["flask; os.system('x')", "evil name", "a\nb", 'q"uote']
    )
    assert dropped == []


def test_req_name_strips_specifier_for_matching():
    from python_deps.depgraph.resolve import _req_name

    assert _req_name("urllib3<1.21") == "urllib3"
    assert _req_name("numpy>=2,<3") == "numpy"
    assert _req_name("requests[security]==2.32.3") == "requests"
    assert _req_name("flask") == "flask"


def test_link_imports_to_packages_reconciles_manifest_sourced_packages():
    """Regression: manifest-declared deps (root import_id=None) must still link
    their scanned Import node to the resolved Package (the orphaned-import bug)."""
    from python_deps.depgraph.resolve import link_imports_to_packages
    from python_deps.depgraph.schema import (
        DepGraph,
        DiscoveredBy,
        EdgeType,
        Layer,
        Node,
        NodeType,
    )

    imp = Node(
        id="import:certifi", type=NodeType.IMPORT, name="certifi",
        layer=Layer.NAMING, discovered_by=DiscoveredBy.STATIC_SCAN,
    )
    # underscore import vs hyphen distribution must match via canonicalization
    imp2 = Node(
        id="import:charset_normalizer", type=NodeType.IMPORT,
        name="charset_normalizer", layer=Layer.NAMING,
        discovered_by=DiscoveredBy.STATIC_SCAN,
    )
    pkg = Node(
        id="pkg:certifi==2026.1.1", type=NodeType.PACKAGE, name="certifi",
        version="2026.1.1", layer=Layer.PIP, discovered_by=DiscoveredBy.RESOLVER,
    )
    pkg2 = Node(
        id="pkg:charset-normalizer==3.4.7", type=NodeType.PACKAGE,
        name="charset-normalizer", version="3.4.7", layer=Layer.PIP,
        discovered_by=DiscoveredBy.RESOLVER,
    )
    graph = DepGraph().with_node(imp).with_node(imp2).with_node(pkg).with_node(pkg2)
    assert not [e for e in graph.edges if e.relation is EdgeType.REQUIRES]

    out = link_imports_to_packages(graph)
    req = {(e.src, e.dst) for e in out.edges if e.relation is EdgeType.REQUIRES}
    assert ("import:certifi", "pkg:certifi==2026.1.1") in req
    assert ("import:charset_normalizer", "pkg:charset-normalizer==3.4.7") in req

    # idempotent: a second pass adds nothing
    assert len(link_imports_to_packages(out).edges) == len(out.edges)


def test_link_imports_skips_unresolved_mapping(monkeypatch):
    """Guard (Task 3): an UNRESOLVED mapping must be skipped, not fed into
    ``_canon`` as ``None`` (previously: TypeError, since ``_canon`` runs
    ``re.sub`` on its argument)."""
    import python_deps.depgraph.resolve_link as resolve_link
    from python_deps.depgraph.resolve import link_imports_to_packages
    from python_deps.depgraph.schema import DepGraph, DiscoveredBy, Layer, Node, NodeType
    from python_deps.import_mapping import unresolved_result

    monkeypatch.setattr(
        resolve_link, "map_import_to_package",
        lambda name, *a, **k: unresolved_result(name),
    )

    imp = Node(
        id="import:mystery", type=NodeType.IMPORT, name="mystery",
        layer=Layer.NAMING, discovered_by=DiscoveredBy.STATIC_SCAN,
    )
    graph = DepGraph().with_node(imp)

    # Must not raise (previously: _canon(None) -> re.sub on None -> TypeError).
    out = link_imports_to_packages(graph)
    assert not [e for e in out.edges if e.relation is EdgeType.REQUIRES]


def test_link_imports_reconciles_unresolved_by_own_name(monkeypatch):
    """An UNRESOLVED import must still link to a Package that ALREADY EXISTS under the
    import's own canonical name — reconciliation against existing graph state, never
    fabrication. (Regression guard for the identity-fallback deletion.)"""
    import python_deps.depgraph.resolve_link as resolve_link
    from python_deps.depgraph.resolve import link_imports_to_packages
    from python_deps.depgraph.schema import (
        DepGraph, DiscoveredBy, EdgeType, Layer, Node, NodeType,
    )
    from python_deps.import_mapping import unresolved_result

    # Force the mapper to unresolved for every import (simulates post-flip behavior).
    monkeypatch.setattr(
        resolve_link, "map_import_to_package",
        lambda name, *a, **k: unresolved_result(name),
    )

    imp = Node(
        id="import:certifi", type=NodeType.IMPORT, name="certifi",
        layer=Layer.NAMING, discovered_by=DiscoveredBy.STATIC_SCAN,
    )
    pkg = Node(
        id="pkg:certifi==2026.1.1", type=NodeType.PACKAGE, name="certifi",
        version="2026.1.1", layer=Layer.PIP, discovered_by=DiscoveredBy.RESOLVER,
    )
    graph = DepGraph().with_node(imp).with_node(pkg)

    out = link_imports_to_packages(graph)
    req = {(e.src, e.dst) for e in out.edges if e.relation is EdgeType.REQUIRES}
    assert ("import:certifi", "pkg:certifi==2026.1.1") in req


def test_resolved_package_node_keeps_pip_fix():
    from python_deps.depgraph.resolve import _package_node

    n = _package_node("python-dateutil", "2.9.0.post0")
    assert n.fix_candidates == ("pip:python-dateutil",)
    assert n.chosen_fix == "pip:python-dateutil"


def test_unresolved_placeholder_has_no_confident_fix():
    # A resolver-MISSING placeholder (an identity-fallback root uv could not
    # resolve) must NOT prescribe ``pip:<name>`` -- that name is exactly what
    # failed to resolve, so the fix would fail (or install a squatter).
    from python_deps.depgraph.resolve import _missing_package_node
    from python_deps.depgraph.schema import State

    n = _missing_package_node("dateutil", None, "WARNING: Package(s) not found: dateutil\n")
    assert n.state is State.MISSING
    assert n.chosen_fix is None
    assert n.fix_candidates == ()
    assert "not found" in (n.evidence or "")


def test_conflict_placeholder_has_no_confident_fix():
    from python_deps.depgraph.resolve import _conflict_package_node

    n = _conflict_package_node("foo", "conflict evidence")
    assert n.chosen_fix is None
    assert n.fix_candidates == ()
