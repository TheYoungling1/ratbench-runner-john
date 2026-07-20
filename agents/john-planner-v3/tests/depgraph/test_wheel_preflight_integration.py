"""Task 2.2 — integration: proactive wheel soname prior + ldd reconcile.

Two levels of proof that a wheel's bundled ``DT_NEEDED`` soname becomes a
``RESOLVER``/``UNKNOWN`` ``SystemLib`` prior pre-install and that the reactive
``ldd_probe`` (Phase 2) reconciles onto the SAME ``syslib_id`` node rather than
minting a duplicate:

* ``test_wheel_preflight_seeds_soname_from_real_fixture_wheel`` — DOCKER-FREE.
  Builds a real ``.whl`` around the committed extension-module ELF fixture
  (``mod.cpython-311-x86_64-linux-gnu.so``, which links ``libGL.so.1``), stubs
  ONLY the network download, and drives ``wheel_preflight_probe`` through the
  REAL ``inspect_wheel_sonames`` reader. Proves the seed half end-to-end.

* ``test_wheel_preflight_seeds_runtime_soname_reconciled_by_ldd`` —
  ``@pytest.mark.docker``. pyodbc's wheel bundles a ``DT_NEEDED`` on
  ``libodbc.so.2``; the full pipeline must seed ONE ``syslib:libodbc.so.2``
  prior (RESOLVER/UNKNOWN) pre-install, and ``ldd_probe`` must reconcile onto
  the SAME node (``discovered_by`` stays RESOLVER, a failed ldd Attempt is
  appended — not a second PROBE node). Skips cleanly when Docker is absent; the
  seed half is still covered by the fixture test above. The 70-row eval credit
  for this runtime-dlopen case is DEFERRED (optional Task 3.5).
"""

from __future__ import annotations

import shutil
import sys
import zipfile
from pathlib import Path

import pytest

# sys.path shim (mirrors tests/depgraph/conftest.py) so the module imports
# without an editable install when run in isolation.
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from python_deps.depgraph import wheel_preflight  # noqa: E402
from python_deps.depgraph.ids import package_id, syslib_id  # noqa: E402
from python_deps.depgraph.schema import (  # noqa: E402
    DepGraph,
    DiscoveredBy,
    Layer,
    Node,
    NodeType,
    State,
)
from python_deps.depgraph.target_env import TargetEnv  # noqa: E402
from python_deps.depgraph.wheel_preflight import wheel_preflight_probe  # noqa: E402

FIXTURE_SO = (
    Path(__file__).parent / "fixtures" / "mod.cpython-311-x86_64-linux-gnu.so"
)


def _target_env() -> TargetEnv:
    return TargetEnv(
        python_full="3.11.0",
        python_version="3.11",
        platform_machine="x86_64",
        sys_platform="linux",
        os_name="posix",
        platform_system="Linux",
        python_platform_tag="x86_64-manylinux_2_28",
    )


def _wheel_pkg(name: str, version: str) -> Node:
    return Node(
        id=package_id(name, version),
        type=NodeType.PACKAGE,
        name=name,
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.RESOLVER,
        version=version,
        build_from_source=False,  # map-classified WHEEL -> inspected by the pre-pass
    )


# --------------------------------------------------------------------------- #
# Docker-free: real fixture wheel exercises the actual ELF reader.
# --------------------------------------------------------------------------- #
def test_wheel_preflight_seeds_soname_from_real_fixture_wheel(tmp_path, monkeypatch):
    """The pre-pass seeds a RESOLVER/UNKNOWN prior from a REAL wheel's DT_NEEDED
    (only the network download is stubbed; the soname read is the real reader)."""
    whl = tmp_path / "fixturepkg-1.0-cp311-cp311-manylinux_2_28_x86_64.whl"
    with zipfile.ZipFile(whl, "w") as zf:
        zf.write(FIXTURE_SO, "fixturepkg/mod.cpython-311-x86_64-linux-gnu.so")

    # Only the download is faked (no network); inspect_wheel_sonames runs for real.
    monkeypatch.setattr(
        wheel_preflight, "download_target_wheel", lambda *a, **k: str(whl)
    )

    graph = DepGraph().with_node(_wheel_pkg("fixturepkg", "1.0"))
    out = wheel_preflight_probe(graph, object(), _target_env())

    node = out.get(syslib_id("libGL.so.1"))
    assert node is not None, "pre-pass did not seed the soname read from the wheel"
    assert node.type is NodeType.SYSTEM_LIB
    assert node.discovered_by is DiscoveredBy.RESOLVER
    assert node.state is State.UNKNOWN
    assert node.provenance == "wheel:fixturepkg"
    # base-image sonames (libc.so.6) are filtered by the reader -> only the real need.
    syslibs = [n for n in out.nodes if n.type is NodeType.SYSTEM_LIB]
    assert [n.id for n in syslibs] == [syslib_id("libGL.so.1")]


# --------------------------------------------------------------------------- #
# Docker: full pipeline — seed pre-install, ldd reconcile onto the same node.
# --------------------------------------------------------------------------- #
_BASE_IMAGE = "python:3.11-slim"

pytestmark_docker = pytest.mark.docker


def _docker_available() -> bool:
    return shutil.which("docker") is not None


@pytest.mark.docker
def test_wheel_preflight_seeds_runtime_soname_reconciled_by_ldd(tmp_path):
    """pyodbc's wheel bundles DT_NEEDED libodbc.so.2. The Phase-B pre-pass seeds
    it as a RESOLVER/UNKNOWN prior BEFORE install; the container ldd_probe then
    observes ``libodbc.so.2 => not found`` (unixodbc absent on -slim) and
    reconciles onto the SAME ``syslib_id`` node — one node, discovered_by stays
    RESOLVER, with a failed ldd attempt appended (not a duplicate PROBE node)."""
    if not _docker_available():
        pytest.skip("Docker binary not on PATH — skipping Docker integration test")

    from python_deps.depgraph.build import build_dep_graph
    from python_deps.depgraph.executor import (
        DockerExecutor,
        LocalSubprocessExecutor,
    )

    (tmp_path / "pyproject.toml").write_text(
        '[project]\n'
        'name = "pyodbc-preflight-test"\n'
        'version = "0.1.0"\n'
        'dependencies = ["pyodbc"]\n'
        'requires-python = ">=3.11"\n'
    )
    (tmp_path / "main.py").write_text("import pyodbc\n")

    host_executor = LocalSubprocessExecutor()
    with DockerExecutor(_BASE_IMAGE) as container_executor:
        graph = build_dep_graph(
            str(tmp_path),
            container_executor,
            host_executor=host_executor,
            target_python="3.11",
        )

    sid = syslib_id("libodbc.so.2")
    matches = [n for n in graph.nodes if n.id == sid]
    assert len(matches) == 1, (
        "expected exactly ONE syslib:libodbc.so.2 node (seed + ldd must reconcile, "
        "not duplicate); got: "
        + str([(n.id, n.discovered_by.value) for n in graph.nodes if n.type is NodeType.SYSTEM_LIB])
    )
    node = matches[0]
    # RESOLVER origin survives reconciliation -> the pre-install seed owns the node.
    assert node.discovered_by is DiscoveredBy.RESOLVER
    # A failed ldd attempt on the node is the reconcile fingerprint (the bare seed
    # carries no attempts; reconcile_predicted appends the failing ldd probe).
    assert any(a.outcome == "failed" for a in node.attempts), (
        "no failed ldd attempt on the node — ldd_probe did not reconcile onto the "
        "RESOLVER seed"
    )
    print(
        f"\n[wheel-preflight-docker] one node id={node.id!r} "
        f"discovered_by={node.discovered_by.value} state={node.state.value} "
        f"attempts={len(node.attempts)}"
    )
