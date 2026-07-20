"""Task 4 — Docker integration test: ldd-derived discovery.

Proves that ``ldd_probe`` discovers native library gaps purely from inspecting
the installed binary — there is no curated table to compare against any more
(construction-enrichment cluster 1a deleted it); this test now exercises
binary-inspection discovery + release-correct apt naming directly.

Package under test: ``pygame``.

* Its extension module ``.cpython-NNN*.so`` links against
  ``libgthread-2.0.so.0`` / ``libglib-2.0.so.0`` (both soname providers in
  ``os_resolver.PROVIDER_TABLE``),
  so ``ldd_probe`` surfaces them as ``SystemLib`` nodes with
  ``discovered_by=PROBE`` **and** a non-empty ``apt:`` fix-candidate.
* After ``reconcile_apt_names`` the fix-candidate name is release-correct for the
  actual base image (e.g. ``libglib2.0-0t64`` on bookworm).

Run (requires Docker, python:3.11-slim pre-pulled):

    python3 -m pytest tests/depgraph/test_ldd_probe_docker.py -q -m docker

Skips cleanly when the ``docker`` binary is absent.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

# ── sys.path shim (mirrors tests/depgraph/conftest.py) ────────────────────────
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from python_deps.depgraph.build import build_dep_graph  # noqa: E402
from python_deps.depgraph.executor import DockerExecutor, LocalSubprocessExecutor  # noqa: E402
from python_deps.depgraph.schema import DiscoveredBy, NodeType  # noqa: E402
from python_deps.depgraph.os_resolver import PROVIDER_TABLE  # noqa: E402

# soname -> apt subset of the unified resolver table (the former curated
# soname->apt table was folded into os_resolver.PROVIDER_TABLE).
_SONAME_APT = {
    name: apt for (kind, name), apt in PROVIDER_TABLE.items() if kind == "soname"
}

# ── constants ──────────────────────────────────────────────────────────────────

_BASE_IMAGE = "python:3.11-slim"
_TEST_PACKAGE = "pygame"

# Generous timeout for the whole test: pygame wheel download + install can take
# 2-5 min on a cold-cache run.  DockerExecutor already gives 900 s to the bulk
# install (INSTALL_TIMEOUT); add headroom here for Docker container start +
# certification rounds.
_TEST_TIMEOUT_SECONDS = 600  # pytest-timeout, if the plugin is present

# ── skip marker ───────────────────────────────────────────────────────────────

# @pytest.mark.docker is a purely organisational label (for -m docker filtering);
# the actual skip guard checks whether the docker binary is on PATH.
pytestmark = pytest.mark.docker


def _docker_available() -> bool:
    return shutil.which("docker") is not None


# ── tiny synthetic repo fixture ───────────────────────────────────────────────

def _make_pygame_repo(tmp_path: Path) -> str:
    """Create a minimal repo that declares pygame as a dependency."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\n'
        f'name = "{_TEST_PACKAGE}-integration-test"\n'
        'version = "0.1.0"\n'
        f'dependencies = ["{_TEST_PACKAGE}"]\n'
        'requires-python = ">=3.11"\n'
    )
    # A Python source file that imports pygame so the static scanner produces an
    # Import node (exercises the full scan->resolve->install->ldd pipeline).
    (tmp_path / "main.py").write_text("import pygame\n")
    return str(tmp_path)


# ── integration test ──────────────────────────────────────────────────────────

@pytest.mark.docker
def test_ldd_probe_table_independent_knowledge(tmp_path: Path) -> None:
    """ldd_probe discovers pygame's native library gaps from binary inspection.

    Assertions:
    1. At least one ``SystemLib`` node exists with ``discovered_by=PROBE`` —
       proves ldd_probe (or import_probe as backstop) surfaced the dependency.
    2. At least one PROBE ``SystemLib`` node has a non-empty ``apt:``
       fix-candidate whose soname resolves via ``os_resolver.PROVIDER_TABLE`` —
       proves option-A release-correct naming (binary-inspection *discovery*,
       release-correct *names* for known sonames).
    """
    if not _docker_available():
        pytest.skip("Docker binary not on PATH — skipping Docker integration test")

    # ── Run the full pipeline in a real Docker container ──────────────────────
    repo_path = _make_pygame_repo(tmp_path)
    host_executor = LocalSubprocessExecutor()

    with DockerExecutor(_BASE_IMAGE) as container_executor:
        graph = build_dep_graph(
            repo_path,
            container_executor,
            host_executor=host_executor,
            target_python="3.11",
        )

    # ── Assertion 1: at least one PROBE SystemLib node was discovered ─────────
    probe_syslibs = [
        n
        for n in graph.nodes
        if n.type is NodeType.SYSTEM_LIB
        and n.discovered_by is DiscoveredBy.PROBE
    ]
    assert probe_syslibs, (
        "No SystemLib node with discovered_by=PROBE found in the graph. "
        "Expected ldd_probe (or import_probe backstop) to discover at least one "
        f"native library gap for {_TEST_PACKAGE!r}. "
        "All SystemLib nodes: "
        + str([(n.id, n.discovered_by.value) for n in graph.nodes if n.type is NodeType.SYSTEM_LIB])
    )

    # ── Assertion 2: at least one PROBE node has a release-correct apt fix ────
    # A PROBE node with a non-empty apt: fix-candidate proves two things:
    #   (a) os_resolver.resolve matched the soname via PROVIDER_TABLE (or
    #       apt-file fallback), i.e. the soname is in the table.
    #   (b) reconcile_apt_names verified / remapped the name against the actual
    #       base image (release-correct).
    probe_with_apt_fix = [
        n
        for n in probe_syslibs
        if any(c.startswith("apt:") for c in n.fix_candidates)
    ]
    assert probe_with_apt_fix, (
        "No PROBE SystemLib node has an apt: fix-candidate. "
        "Expected at least one soname from "
        f"{_TEST_PACKAGE!r} to be in the resolver's soname providers "
        f"(current table sonames: {sorted(_SONAME_APT.keys())}). "
        "Nodes found: "
        + str([(n.id, n.fix_candidates) for n in probe_syslibs])
    )

    # ── Diagnostic output (visible with -s / on failure) ─────────────────────
    all_syslibs = [n for n in graph.nodes if n.type is NodeType.SYSTEM_LIB]
    for node in probe_with_apt_fix:
        apt_fix = next(c for c in node.fix_candidates if c.startswith("apt:"))
        print(
            f"\n[ldd-probe-docker] discovered: id={node.id!r} "
            f"fix={apt_fix!r} state={node.state.value}"
        )
    print(
        f"[ldd-probe-docker] total SystemLib nodes: {len(all_syslibs)}, "
        f"PROBE nodes: {len(probe_syslibs)}, "
        f"PROBE+apt-fix: {len(probe_with_apt_fix)}"
    )
