"""Task 3.4 — render/emit smoke: the ported native nodes reach ``setup.sh``.

The ``package_installability`` eval validates DETECTION by calling ``apt-get
install <P>`` directly; it never calls ``render_build_script``. This module
covers the complementary EMIT path: the capability-keyed ``TOOL``/``SYSTEM_LIB``
nodes that the wired ``seed_build_deps`` (Task 3.2) and ``wheel_preflight``
(Task 2.2) now produce must actually render into the system/apt tier of the
compiled ``setup.sh``, ordered before the ``pip install … --no-deps`` line.

Docker-free: the two pipeline tests drive ``_python_package_obligations`` with a
``SequencedFakeExecutor`` (a real universal ``uv.lock`` classifies the target as
sdist vs wheel) and monkeypatch ``wheel_preflight`` download/inspect — exactly
like ``test_build_native_prepass.py``. The third test is a pure ``emit`` unit
that pins the ``build_from_source`` gate (``emit._toolchain_ready``) that decides
whether a source build waits on its build tool.
"""

from __future__ import annotations

from python_deps.depgraph import wheel_preflight
from python_deps.depgraph.build import _python_package_obligations
from python_deps.depgraph.build_script import render_build_script
from python_deps.depgraph.emit import next_deterministic_wave
from python_deps.depgraph.executor import CommandResult
from python_deps.depgraph.ids import package_id, tool_id
from python_deps.depgraph.schema import (
    DepGraph,
    DiscoveredBy,
    Edge,
    EdgeType,
    Layer,
    Node,
    NodeType,
    State,
)


def _r(returncode: int = 0, stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(command="", returncode=returncode, stdout=stdout, stderr=stderr)


# psycopg2 ships ONLY an sdist -> native_risk_from_lock stamps
# build_from_source=True, the source-built classification seed_build_deps seeds a
# SPECIFIC -dev prior for (Task 3.2).
_SDIST_LOCK = """\
version = 1
requires-python = ">=3.11"

[[package]]
name = "sdist-emit-root"
version = "0.0.0"
source = { virtual = "." }
dependencies = [
    { name = "psycopg2" },
]

[[package]]
name = "psycopg2"
version = "2.9.9"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://files.pythonhosted.org/x/psycopg2-2.9.9.tar.gz", hash = "sha256:psql-sdist" }
"""

# opencv-python ships a manylinux x86_64 wheel and no sdist -> build_from_source
# is False (a known wheel). wheel_preflight seeds its bundled DT_NEEDED soname as
# a SystemLib prior (via make_syslib_node); NO build tool is seeded for a wheel.
_WHEEL_LOCK = """\
version = 1
requires-python = ">=3.11"

[[package]]
name = "wheel-emit-root"
version = "0.0.0"
source = { virtual = "." }
dependencies = [
    { name = "opencv-python" },
]

[[package]]
name = "opencv-python"
version = "4.9.0.80"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "https://files.pythonhosted.org/x/opencv_python-4.9.0.80-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", hash = "sha256:cv-wheel" },
]
"""


def _repo(tmp_path, import_name: str, dist: str) -> str:
    (tmp_path / "app.py").write_text(f"import {import_name}\n")
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname="fx"\nversion="0"\ndependencies=["{dist}"]\n'
    )
    return str(tmp_path)


def _obligations(tmp_path, ex, provider):
    graph, _roots, _env, _newer = _python_package_obligations(
        str(tmp_path),
        ex,
        host_executor=ex,
        target_python="3.11",
        target_platform="x86_64-manylinux_2_28",
        record_provider=provider,
    )
    return graph


def test_source_built_syslib_nodes_render_into_apt_tier(tmp_path, monkeypatch):
    """A source-built package's ported build-dep nodes render into the apt tier of
    setup.sh, BEFORE its pip install. Drives the real wired pipeline: seed_build_deps
    contributes ``binary:pg_config`` -> ``apt:libpq-dev`` (curated) and the B3
    ``binary:pkg-config`` baseline; seed_wheel_oracle_prior contributes the
    ``tool:build-essential`` floor. All three must reach the compiled artifact."""
    from conftest import SequencedFakeExecutor  # type: ignore

    # An sdist closure never triggers a wheel download; if it did, this fake path
    # would surface — guard that no soname sneaks in from the wheel pre-pass.
    monkeypatch.setattr(wheel_preflight, "download_target_wheel", lambda *a, **k: "/tmp/nope.whl")
    monkeypatch.setattr(wheel_preflight, "inspect_wheel_sonames", lambda p: set())

    ex = SequencedFakeExecutor(responses={"uv lock": [_r(0, stdout=_SDIST_LOCK)]}, default=_r(0))
    provider = lambda dist: {"psycopg2"} if "psycopg2" in dist.lower() else None  # noqa: E731
    graph = _obligations(_repo(tmp_path, "psycopg2", "psycopg2"), ex, provider)

    out = render_build_script(graph)

    # The ported capability/floor nodes each render as a real apt install line.
    assert "apt-get install -y --no-install-recommends libpq-dev" in out  # binary:pg_config
    assert "apt-get install -y --no-install-recommends build-essential" in out  # floor
    assert "apt-get install -y --no-install-recommends pkgconf" in out  # B3 binary:pkg-config
    # Provenance is annotated so render_fidelity can attribute the apt line.
    assert "provider=apt:libpq-dev" in out

    # The apt/toolchain tier is ordered BEFORE the pip tier (native deps first).
    assert (out.index("# ==================== TOOLCHAIN ====================")
            < out.index("# ==================== PIP ===================="))
    # And each apt install line precedes the package's pip install line (topo).
    pip_line = "python3 -m pip install --break-system-packages --no-deps psycopg2==2.9.9"
    assert pip_line in out
    assert out.index("libpq-dev\n") < out.index(pip_line)
    assert out.index("build-essential\n") < out.index(pip_line)


def test_wheel_preflight_syslib_renders_and_no_build_tool_leaks(tmp_path, monkeypatch):
    """A KNOWN-wheel package: its wheel_preflight SystemLib prior (via
    make_syslib_node) renders in the apt/system tier before pip, and NO
    build-essential apt line is emitted for it — a wheel is never source-built, so
    seed_wheel_oracle_prior seeds no build tool for it. This is the emit-visible
    contrast to the source-built case above."""
    from conftest import SequencedFakeExecutor  # type: ignore

    monkeypatch.setattr(wheel_preflight, "download_target_wheel", lambda *a, **k: "/tmp/fake.whl")
    monkeypatch.setattr(wheel_preflight, "inspect_wheel_sonames", lambda p: {"libGL.so.1"})

    ex = SequencedFakeExecutor(responses={"uv lock": [_r(0, stdout=_WHEEL_LOCK)]}, default=_r(0))
    provider = lambda dist: {"cv2"} if "opencv" in dist.lower() else None  # noqa: E731
    graph = _obligations(_repo(tmp_path, "cv2", "opencv-python"), ex, provider)

    out = render_build_script(graph)

    # The ported runtime SystemLib prior renders into the system tier.
    assert "apt-get install -y --no-install-recommends libgl1" in out  # syslib:libGL.so.1
    # A wheel gets NO build tool: build-essential must not appear anywhere.
    assert "build-essential" not in out
    # System tier precedes pip; the runtime lib installs before the wheel.
    assert (out.index("# ==================== SYSTEM ====================")
            < out.index("# ==================== PIP ===================="))
    pip_line = "python3 -m pip install --break-system-packages --no-deps opencv-python==4.9.0.80"
    assert pip_line in out
    assert out.index("libgl1\n") < out.index(pip_line)


def _tool(nid: str, name: str, fix: str) -> Node:
    return Node(id=nid, type=NodeType.TOOL, name=name, layer=Layer.TOOLCHAIN,
                discovered_by=DiscoveredBy.RESOLVER, state=State.MISSING, chosen_fix=fix)


def _pkg(nid: str, name: str, version: str, *, build_from_source) -> Node:
    return Node(id=nid, type=NodeType.PACKAGE, name=name, layer=Layer.PIP,
                discovered_by=DiscoveredBy.RESOLVER, state=State.MISSING,
                version=version, build_from_source=build_from_source)


def _graph_pkg_needs_tool(build_from_source) -> DepGraph:
    pkg = _pkg(package_id("foo", "1.0"), "foo", "1.0", build_from_source=build_from_source)
    tool = _tool(tool_id("build-essential"), "build-essential", "apt:build-essential")
    return (DepGraph(nodes=(pkg, tool))
            .with_edge(Edge(src=pkg.id, dst=tool.id, relation=EdgeType.REQUIRES)))


def test_build_from_source_gate_blocks_package_until_tool_certifies():
    """emit._toolchain_ready gate (emit.py:78): a source-built package waits on its
    unsatisfied build TOOL — the emit wave installs the tool but NOT the package.
    A KNOWN wheel (build_from_source is False) does NOT wait: the same graph emits
    the tool AND the package together. Pins the one bit the render path (_is_reciped)
    does not itself gate on."""
    src_wave = next_deterministic_wave(_graph_pkg_needs_tool(True))
    kinds = {s.kind for s in src_wave}
    assert kinds == {"system_install"}, "source build must not emit its pkg before the tool"
    (sys_step,) = src_wave
    assert "build-essential" in sys_step.command
    assert package_id("foo", "1.0") not in sys_step.target_node_ids

    wheel_wave = next_deterministic_wave(_graph_pkg_needs_tool(False))
    kinds = {s.kind for s in wheel_wave}
    assert kinds == {"system_install", "python_install"}, "wheel must not wait on the build tool"
    pip_step = next(s for s in wheel_wave if s.kind == "python_install")
    assert package_id("foo", "1.0") in pip_step.target_node_ids
