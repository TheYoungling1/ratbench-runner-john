# src/python_deps/depgraph/project_native_deps.py
"""Project-native-build-obligations construction stage (R1b).

Gives the repo-under-test's OWN ``NodeType.PROJECT`` node the same
build-dep-prior treatment ``seed_build_deps``/``seed_wheel_oracle_prior``
already give ``NodeType.PACKAGE`` nodes. Design:
``docs/superpowers/research/R1-native-build-requirements.md`` §2.1-2.5 — the
Project node is categorically excluded from every existing build-dep-prior
stage (they all filter on ``NodeType.PACKAGE`` + a real ``version``, neither
of which the Project node ever has), so a repo's OWN native build
requirements (pygraphviz's ``libraries=["cdt","cgraph","gvc"]``, lxml's
Debian ``Build-Depends``) never reach the graph even when every OTHER
sdist's do.

Assembles from four independent, additive sources (§2.2-§2.5), each seeding
capability/apt nodes + a ``REQUIRES`` edge FROM THE PROJECT NODE ID (never
floating):

* §2.4 (primary) — ``project_native_scan.scan_native_build_surface`` reads
  the repo's OWN ``setup.py``/``pyproject.toml`` for declared
  ``Extension(libraries=[...])`` link targets, funneled through the ALREADY
  ecosystem-neutral ``os_resolver.resolve()`` (closes pygraphviz: Debian
  never packaged it, so §2.3 structurally misses it).
* §2.3 — Debian ``Build-Depends`` + the curated table, keyed by the
  PROJECT'S OWN distribution name rather than a dependency's (closes lxml:
  the mechanism was always sound, just never invoked on the project).
* §2.2 — PEP 725 ``[external]`` read directly off the local
  ``pyproject.toml`` (near-zero recall today; future-proofing, zero cost).
* §2.5 — the unconditional ``build-essential`` floor (reusing ``seed.py``'s
  singleton node/id, deduped exactly as ``seed_build_deps`` dedupes
  ``pkg-config``) whenever ANY native-build signal was detected, even one
  whose specific libraries couldn't be statically extracted.

All new nodes are additive, ``DiscoveredBy.RESOLVER``/``State.UNKNOWN``
(matching ``seed_build_deps``'s seeding convention) — this stage never marks
anything SATISFIED; only a host-run ``check_command`` certifies later.  A
repo with no native-build signal at all is a NO-OP: the Project node gets
nothing.  Every source degrades independently to "contributes nothing" on
failure (bad AST, no Debian source, no network, no ``[external]`` table) —
never a wrong guess, never an aborted stage.

Pure: every "mutation" returns a NEW ``DepGraph`` (repo immutability rule).
"""

from __future__ import annotations

import logging
from pathlib import Path

from python_deps.depgraph.build_deps import (
    PACKAGE_TO_BUILD_NEEDS,
    _apt_build_node,
    _apt_installable,
    _capability_node,
)
from python_deps.depgraph.debian_builddeps import debian_build_deps
from python_deps.depgraph.executor import Executor
from python_deps.depgraph.ids import apt_build_id
from python_deps.depgraph.os_resolver import capability_id
from python_deps.depgraph.pep725 import needs_from_pyproject
from python_deps.depgraph.project_native_scan import (
    has_native_build_signal,
    scan_native_build_surface,
)
from python_deps.depgraph.schema import DepGraph, Edge, EdgeType, NodeType
from python_deps.depgraph.seed import _BUILD_ESSENTIAL_ID, _build_essential_node
from python_deps.import_mapping import normalize_package_name

logger = logging.getLogger(__name__)


def _project_edge(proj_id: str, node_id: str) -> Edge:
    return Edge(src=proj_id, dst=node_id, relation=EdgeType.REQUIRES, origin="resolver")


def project_native_obligations(
    graph: DepGraph,
    repo_path: str,
    host_executor: Executor,
    container_executor: Executor,
) -> DepGraph:
    """Seed the Project node's OWN native build-dep prior, additively.

    ``host_executor`` is accepted for symmetry with the other aux-once
    stages (``wheel_preflight_probe``) and future host-side sources; every
    source wired today (static scan resolution, Debian ``Build-Depends``)
    reads through ``container_executor`` per R1b's spec. A no-op (returns
    ``graph`` unchanged, minus any no-op source contribution) when there is
    no ``NodeType.PROJECT`` node in the graph at all.
    """
    project_node = next((n for n in graph.nodes if n.type is NodeType.PROJECT), None)
    if project_node is None:
        return graph

    proj_id = project_node.id
    new = graph
    cap_nodes = 0
    aptdep_nodes = 0

    def _seed_capability(need) -> None:
        nonlocal new, cap_nodes
        node_id = capability_id(need)
        if new.get(node_id) is None:
            new = new.with_node(_capability_node(need, container_executor))
            cap_nodes += 1
        new = new.with_edge(_project_edge(proj_id, node_id))

    def _seed_apt(name: str) -> None:
        nonlocal new, aptdep_nodes
        node_id = apt_build_id(name)
        if new.get(node_id) is None:
            new = new.with_node(_apt_build_node(name))
            aptdep_nodes += 1
        new = new.with_edge(_project_edge(proj_id, node_id))

    # §2.4 (primary) — the repo's OWN declared native-build surface
    # (setup.py Extension.libraries / pyproject ext-modules), resolved
    # through the ecosystem-neutral os_resolver.
    try:
        scanned_needs = list(scan_native_build_surface(repo_path))
    except Exception:
        logger.exception(
            "project_native_obligations: scan_native_build_surface failed for %s", repo_path
        )
        scanned_needs = []
    for need in scanned_needs:
        _seed_capability(need)

    # Native-build signal (computed ONCE, reused by §2.3's gate below and
    # §2.5's floor). Degrades to False on any scan failure.
    try:
        native_signal = bool(has_native_build_signal(repo_path))
    except Exception:
        logger.exception(
            "project_native_obligations: has_native_build_signal failed for %s", repo_path
        )
        native_signal = False

    # §2.3 — Debian Build-Depends + the curated table, keyed by the PROJECT'S
    # OWN distribution name (not a dependency's) — GATED on the native-build
    # signal. A pure-Python project (no Extension/.pyx/native backend) needs
    # ZERO system build-deps, so its Debian NAMESAKE must never be consulted:
    # `apt-cache showsrc <name>` can return an UNRELATED same-named Debian
    # source (e.g. Ubuntu's Click package manager for Python "click", or
    # rich's `pybuild-plugin-pyproject`), over-predicting apt for a repo that
    # compiles nothing. Only a project that actually builds native code looks
    # up its own Build-Depends. lxml/pygraphviz (signal True) keep this path.
    if native_signal:
        canonical = normalize_package_name(project_node.name)
        for need in PACKAGE_TO_BUILD_NEEDS.get(canonical, ()):
            _seed_capability(need)
        try:
            debian_names = list(debian_build_deps(canonical, container_executor))
        except Exception:
            logger.exception(
                "project_native_obligations: debian_build_deps failed for %s", canonical
            )
            debian_names = []
        # apt-installability guard (mirrors build_deps.build_dep_prior): a
        # project whose own Debian source has a conflicting/uninstallable JOINT
        # Build-Depends (the uWSGI class) would break the whole apt step at
        # replay. Simulate-install the set (`apt-get install -s`); on failure
        # drop the ENTIRE Debian set, keeping §2.4/§2.5/§2.2.
        if debian_names and not _apt_installable(debian_names, container_executor):
            logger.info(
                "project_native_obligations: %s debian set not apt-installable, "
                "dropping %s", canonical, debian_names,
            )
            debian_names = []
        for name in debian_names:
            _seed_apt(name)

    # §2.2 — PEP 725 [external], read directly off the local checkout (no
    # sdist download: the manifest is already on disk for the repo under
    # test). Near-zero recall today; future-proofing.
    pyproject_path = Path(repo_path) / "pyproject.toml"
    if pyproject_path.is_file():
        try:
            pyproject_text = pyproject_path.read_text(encoding="utf-8")
        except OSError:
            pyproject_text = None
        if pyproject_text is not None:
            try:
                pep_needs = list(
                    needs_from_pyproject(pyproject_text, source=project_node.name)
                )
            except Exception:
                logger.exception(
                    "project_native_obligations: needs_from_pyproject failed for %s",
                    repo_path,
                )
                pep_needs = []
            for need in pep_needs:
                _seed_capability(need)

    # §2.5 — unconditional build-essential floor for ANY detected
    # native-build signal (``native_signal``, computed above), even when no
    # specific library was statically extractable. Reuses seed.py's singleton
    # node/id — deduped, never a second copy.
    if native_signal:
        if new.get(_BUILD_ESSENTIAL_ID) is None:
            new = new.with_node(_build_essential_node())
        new = new.with_edge(_project_edge(proj_id, _BUILD_ESSENTIAL_ID))

    logger.info(
        "project_native_obligations: project=%s cap_nodes=%d aptdep_nodes=%d "
        "native_signal=%s",
        project_node.name, cap_nodes, aptdep_nodes, native_signal,
    )
    return new
