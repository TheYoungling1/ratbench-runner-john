"""Stage orchestrator — repo path in, host-certified ``DepGraph`` out.

Wires the pipeline of
``docs/DESIGN-static-probe-certified-dependency-graph.md`` /
``docs/superpowers/specs/2026-06-23-uv-enriched-depgraph.md`` as TWO phases with
TWO distinct oracles (P2.1):

    scan/map   static import scan + declared-ONLY roots -> Import/Test  (cycle 1)

    Phase A -- "is it PROVIDED?"  oracle = RECORD-union coverage.
       A bounded resolve (HOST uv) -> install (CONTAINER) -> look -> repair
       FIXPOINT (:func:`_phase_a_fixpoint`). Each round audits the runtime imports
       against the RESOLVED closure's RECORD-union coverage
       (:func:`coverage.resolved_record_coverage`) and repairs an under-declared
       import by grounding + adding an AUDIT root, re-resolving until coverage is
       stable. The loop condition is the RECORD-union oracle, NEVER a per-round
       ``packages_distributions()`` probe — so a resolved-but-failed-to-build dist
       counts PROVIDED here (its wheel RECORDs the module) and its native gap is a
       Phase-B concern, not a Phase-A under-declaration.                (cycle 2)

    Phase B -- "does it LOAD / who PROVIDES it?"  oracle = the live CONVERGED
    container. A single tier descent over the converged closure, "look then
    derive":
       relink   certified Import->Package edges + honest ``unresolved`` flags
                (packages_distributions, CONTAINER) -- Phase B's LOOK           (cycle 3)
       ldd      ldd ext .so ``=> not found`` -> run-time SystemLib (DT_NEEDED)   (cycle 3)
       probe    import backstop -> dlopen'd SystemLib not in NEEDED             (cycle 3)
       apt      reconcile predicted apt names against the target image
       certify  host check_commands (CONTAINER) -> node ``state``              (cycle 4)

**Executor split (spec "Architecture change"):** resolution is HOST-side — ``uv``
cross-platform resolves the container target without a container interpreter — so
it runs through ``host_executor``.  Install/probe/certify must observe the real
target environment, so they run through ``container_executor``.  Both default-safe
for unit tests (a single ``FakeExecutor`` can be injected for both).

Phase B's discovery order and the later execution order differ (design 3.3 /
10.10): the descent LOOKS (relink) then DERIVES system deps (ldd/probe) on the
converged closure, but certification then runs in execution layer order (system
before pip).  Every stage returns a NEW immutable graph; this function only ever
rebinds ``graph``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import replace

try:  # tomllib is stdlib on 3.11+; fall back to the tomli backport on 3.10.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python < 3.11
    import tomli as tomllib

from python_deps.depgraph.apt_verify import reconcile_apt_names
from python_deps.depgraph.build_deps import seed_build_deps
from python_deps.depgraph.certify import certify_all
from python_deps.depgraph.coverage import (
    composite_record_provider,
    default_record_provider,
    pypi_record_provider,
    resolved_record_coverage,
)
from python_deps.depgraph.executor import Executor, LocalSubprocessExecutor
from python_deps.depgraph.ids import TEST_NODE_ID, project_id
from python_deps.depgraph.ldd_probe import ldd_probe
from python_deps.depgraph.pins import compute_exclude_newer
from python_deps.depgraph.probe import import_probe, install_closure
from python_deps.depgraph.project_native_deps import project_native_obligations
from python_deps.depgraph.relink import certified_import_links
from python_deps.depgraph.repair import (
    RecordProvider,
    Verdict,
    choose_provider,
    generate_candidates,
)
from python_deps.depgraph.resolve import (
    _req_name,
    resolve_closure,
)
from python_deps.depgraph.roots import select_roots
from python_deps.depgraph.scan import scan_to_nodes
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
from python_deps.depgraph.seed import seed_wheel_oracle_prior
from python_deps.depgraph.subprocess_scan import add_subprocess_tool_nodes
from python_deps.depgraph.target_env import detect_target_env
from python_deps.depgraph.wheel_preflight import wheel_preflight_probe
from python_deps.evidence import collect_python_dependency_evidence
from python_deps.import_mapping import normalize_package_name, top_level_import_name

logger = logging.getLogger(__name__)

# Numeric backstop on Phase-A repair rounds (Correction 2b): the attempted-set is
# the honest terminator; this caps pathological non-convergence.
_MAX_REPAIR_ROUNDS = 5

# discovered_cycle stamps, one per discovery stage (design 5.2 example uses 3 for
# probe-discovered SystemLibs).
_SCAN_CYCLE = 1
_RESOLVER_CYCLE = 2
_PROBE_CYCLE = 3
_CERTIFY_CYCLE = 4


def _restamp(graph: DepGraph, node_ids: set[str], cycle: int) -> DepGraph:
    """Return a new graph with ``discovered_cycle = cycle`` on the named nodes."""
    new = graph
    for node_id in node_ids:
        node = new.get(node_id)
        if node is not None:
            new = new.with_node(replace(node, discovered_cycle=cycle))
    return new


def _canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _project_name(repo_path: str) -> str:
    """Project name from ``[project].name`` in pyproject.toml, else dir basename."""
    pyproject = os.path.join(repo_path, "pyproject.toml")
    try:
        with open(pyproject, "rb") as fh:
            data = tomllib.load(fh)
        name = (data.get("project") or {}).get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    except (OSError, tomllib.TOMLDecodeError):
        pass
    return os.path.basename(repo_path.rstrip("/\\")) or "project"


def _project_build_manifest(repo_path: str) -> str | None:
    """The build manifest that makes the repo editable-installable, or None.

    A ``pip install -e .`` needs DECLARED packaging intent, not merely a file
    named ``pyproject.toml``: many repos ship a ``pyproject.toml`` purely for tool
    config (``[tool.black]``, ``[tool.ruff]``, …) alongside a flat multi-module
    layout that ``pip install -e .`` cannot build (setuptools aborts with
    "Multiple top-level modules discovered in a flat-layout"). Under the rendered
    script's ``set -Eeuo pipefail`` that one line would abort the whole setup.sh —
    a NEW first-pass failure on a repo that never needed the editable install.

    So a ``pyproject.toml`` counts only when it declares ``[project]`` (PEP 621
    metadata) or ``[build-system]`` (PEP 517 backend); a bare ``setup.py`` is the
    legacy installable signal. A tool-config-only pyproject with no ``setup.py``,
    or an unparseable pyproject we cannot confirm, yields None — the renderer then
    emits no editable install (never a command we cannot stand behind).
    """
    pyproject = os.path.join(repo_path, "pyproject.toml")
    if os.path.isfile(pyproject):
        try:
            with open(pyproject, "rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
        if "project" in data or "build-system" in data:
            return pyproject
    setup_py = os.path.join(repo_path, "setup.py")
    if os.path.isfile(setup_py):
        return setup_py
    return None


def _add_project_node(graph: DepGraph, repo_path: str) -> DepGraph:
    """Add a Project hub node and connect declared direct deps to it.

    The repo under test is otherwise only reachable through the Test->Import
    chain, so its declared direct dependencies have no shared parent (e.g.
    ``certifi`` had no incoming Package->Package edge).  This node makes "what
    does the project directly require" a single explorable subtree:

    * ``Test --requires--> Project``
    * ``Project --requires--> <runtime declared dep Package>``  (kind=dependency)
    * ``Test --requires--> <test/optional declared dep Package>`` (kind=optional)

    Runtime vs test classification reuses ``evidence`` (kind ``dependency`` vs
    ``optional_dependency``); no new parsing.  Transitive deps still hang off
    their parents, and Import->Package reconciliation is unchanged.
    """
    name = _project_name(repo_path)
    proj_id = project_id(name)
    manifest = _project_build_manifest(repo_path)
    graph = graph.with_node(
        Node(
            id=proj_id,
            type=NodeType.PROJECT,
            name=name,
            layer=Layer.PIP,
            discovered_by=DiscoveredBy.STATIC_SCAN,
            state=State.UNKNOWN,
            provenance=manifest or repo_path,
            # installable => the renderer emits `pip install -e .` as the final,
            # post-dependency step (populate/build_script read this flag).
            data={"installable": manifest is not None},
        )
    )
    graph = graph.with_edge(
        Edge(src=TEST_NODE_ID, dst=proj_id, relation=EdgeType.REQUIRES, origin="project")
    )

    canon_to_pkg = {
        _canon(n.name): n.id for n in graph.nodes if n.type is NodeType.PACKAGE
    }
    evidence = collect_python_dependency_evidence(repo_path)
    for req in evidence.declared_dependencies:
        if getattr(req, "kind", "dependency") == "constraint":
            continue
        pkg_id = canon_to_pkg.get(_canon(normalize_package_name(req.name)))
        if pkg_id is None:
            continue
        # runtime deps hang off the Project; test/optional deps off the Test goal.
        src = (
            TEST_NODE_ID
            if getattr(req, "kind", "dependency") == "optional_dependency"
            else proj_id
        )
        graph = graph.with_edge(
            Edge(src=src, dst=pkg_id, relation=EdgeType.REQUIRES, origin="project")
        )
    return graph


def _pad_python_full(target_python: str) -> str:
    """``"3.13"`` -> ``"3.13.0"`` (padding for a caller-supplied override).

    Mirrors the padding ``resolve_lock._target_env_for`` applies so an
    overridden ``target_python`` still produces a valid ``python_full_version``
    for marker evaluation (``python_full_version < '3.12'`` style forks).
    """
    parts = [p for p in target_python.split(".") if p]
    return ".".join((parts + ["0", "0"])[:3]) if parts else target_python


# Minor-version token in a ``Python 3.13.14`` banner.
_PY_VER_RE = re.compile(r"(\d+\.\d+)")
# Last-resort interpreter version when the container probe yields nothing.
_DEFAULT_TARGET_PYTHON = "3.11"


def _detect_target_python(
    container_executor: Executor, default: str = _DEFAULT_TARGET_PYTHON
) -> str:
    """Probe the container's interpreter minor version (e.g. ``"3.13"``).

    The resolve MUST target the python the container actually runs, or it pins
    versions that have no wheel for that interpreter (observed: a
    3.11-resolved ``pyarrow==2.0.0`` cannot build on a 3.13 container). Tries
    ``python3`` then ``python``, reading both streams (``--version``
    historically printed to stderr). Falls back to ``default`` when nothing
    parses, so a fake/empty executor preserves the legacy 3.11 target.

    Superseded in :func:`build_dep_graph` by :func:`target_env.detect_target_env`
    (Task 7, one combined probe covering python + platform); kept standalone
    (directly unit-tested) as it captures a slightly different signal (a
    ``--version`` banner rather than ``sys.version``) that some callers may
    still want in isolation.
    """
    for cmd in ("python3 --version", "python --version"):
        result = container_executor.run(cmd)
        if not result.ok:
            continue
        m = _PY_VER_RE.search((result.stdout or "") + " " + (result.stderr or ""))
        if m:
            return m.group(1)
    return default


def reconcile_packages(
    graph: DepGraph,
    pkg_nodes: list[Node],
    pkg_edges: list[Edge],
    prev_pkg_ids: set[str],
) -> DepGraph:
    """Merge a fresh resolve round's Package nodes/edges, dropping the prior
    round's stale ones (Phase-A Correction 2c).

    Package ids bake the version (``pkg:name==version``) and ``DepGraph`` is
    upsert-only, so a version shift between rounds would otherwise leave the old
    ``pkg:name==v_old`` node (and its edges) orphaned. Before merging the new
    nodes/edges, remove every Package node the PRIOR round produced
    (``prev_pkg_ids``) that the NEW resolve no longer emits (``without_node`` also
    drops that node's dangling edges), and every prior Package->Package
    ``requires`` edge among still-surviving nodes the new resolve no longer emits
    (``without_edge``). Conflict advisory edges are left untouched. Returns a NEW
    graph; net effect: no stale Package orphan survives a version change.
    """
    new_ids = {n.id for n in pkg_nodes}
    new_edge_keys = {e.key() for e in pkg_edges}
    new = graph
    for stale_id in prev_pkg_ids - new_ids:
        new = new.without_node(stale_id)
    for edge in list(new.edges):
        if (
            edge.relation is EdgeType.REQUIRES
            and edge.src in prev_pkg_ids
            and edge.dst in prev_pkg_ids
            and edge.key() not in new_edge_keys
        ):
            new = new.without_edge(edge)
    for node in pkg_nodes:
        new = new.with_node(node)
    for edge in pkg_edges:
        new = new.with_edge(edge)
    return new


def _stamp_audit(graph: DepGraph, repaired: set[str]) -> DepGraph:
    """Stamp ``discovered_by=AUDIT`` on Package nodes whose canon dist was repaired.

    Each resolve round re-emits a repaired dist's Package as ``RESOLVER`` (fresh
    from the lock), so this runs every round after the merge to keep the AUDIT
    provenance; declared/transitive packages keep ``RESOLVER``.
    """
    new = graph
    for node in graph.nodes:
        if (
            node.type is NodeType.PACKAGE
            and _canon(node.name) in repaired
            and node.discovered_by is not DiscoveredBy.AUDIT
        ):
            new = new.with_node(replace(node, discovered_by=DiscoveredBy.AUDIT))
    return new


def _phase_a_fixpoint(
    graph: DepGraph,
    roots: list[tuple[str | None, str]],
    host_executor: Executor,
    container_executor: Executor,
    record_provider: RecordProvider,
    *,
    target_env,
    exclude_newer: str | None,
    needed_extras: frozenset[str],
) -> DepGraph:
    """Bounded resolve -> install -> look -> repair fixpoint (Phase A).

    Each round resolves the current roots, reconciles the Package layer (dropping
    stale nodes/edges — Correction 2c), install-probes the closure, then audits
    the FULL runtime import set against the resolved closure's RECORD-union
    coverage (Correction 3 — never ``packages_distributions``). A non-optional
    import that no resolved dist provides is repaired by grounding candidate dists
    (deterministic normalize -> curated, RECORD-grounded via ``record_provider``;
    NEVER an LLM) and, on an unambiguous ACCEPT, adding the dist as an AUDIT root
    (``audit_root_names`` threads the repaired set into the resolve retry so a
    declared root is never evicted — Correction 2a) and re-resolving.

    Terminates when coverage is complete, when no new ``(import, candidate)`` pair
    can be proposed (attempted-set / fixpoint — Correction 2b), or at the numeric
    bound ``min(initial_missing, 5)``; residue is left for P0.3 to flag
    unresolved downstream — construction is NEVER aborted. Every round returns a
    NEW graph; the orchestrator only rebinds ``graph``.
    """
    root_dists = {_canon(_req_name(dist)) for _imp, dist in roots}
    repaired: set[str] = set()
    attempted: set[tuple[str, str]] = set()
    prev_pkg_ids: set[str] = set()
    bound: int | None = None
    iteration = 0

    while True:
        pkg_nodes, pkg_edges = resolve_closure(
            roots,
            host_executor,
            target_env=target_env,
            exclude_newer=exclude_newer,
            extras=needed_extras,
            audit_root_names=frozenset(repaired),
        )
        graph = reconcile_packages(graph, pkg_nodes, pkg_edges, prev_pkg_ids)
        graph = _stamp_audit(graph, repaired)
        prev_pkg_ids = {n.id for n in pkg_nodes}
        graph = install_closure(graph, container_executor)

        # Correction 3: the coverage oracle is RECORD-union over the RESOLVED
        # closure, NOT a post-install packages_distributions() snapshot.
        provided = resolved_record_coverage(pkg_nodes, record_provider)
        missing = [
            n
            for n in graph.nodes
            if n.type is NodeType.IMPORT
            and n.data.get("optional") is not True
            and top_level_import_name(n.name).lower() not in provided
        ]
        if bound is None:
            bound = min(len(missing), _MAX_REPAIR_ROUNDS)
        if not missing:
            break

        new_pair = False
        for imp in missing:
            # LLM stays OUT of the deterministic core (never pass an llm here).
            candidates = generate_candidates(imp.name, llm=None)
            decision = choose_provider(imp.name, candidates, record_provider)
            if (
                decision.verdict is Verdict.ACCEPT
                and decision.dist is not None
                and (imp.name, decision.dist) not in attempted
                and _canon(decision.dist) not in root_dists
            ):
                roots = roots + [(None, decision.dist)]
                repaired.add(_canon(decision.dist))
                root_dists.add(_canon(decision.dist))
                new_pair = True
            # Remember every candidate tried for this import (Correction 2b), so a
            # re-proposal of an already-attempted pair cannot re-add / oscillate.
            attempted |= {(imp.name, candidate.dist) for candidate in candidates}
        if not new_pair:
            logger.warning(
                "phase-A stopped: no new repair candidate; residue left unresolved "
                "(fixpoint/oscillation): %s",
                sorted(n.name for n in missing),
            )
            break
        iteration += 1
        if iteration > bound:
            logger.warning(
                "phase-A hit bound=%d; residue left unresolved (honest), not aborting",
                bound,
            )
            break
    return graph


def _python_package_obligations(
    repo_path: str,
    container_executor: Executor,
    *,
    host_executor: Executor | None = None,
    target_python: str | None = None,
    target_platform: str | None = None,
    exclude_newer: str | None = None,
    needed_extras: frozenset[str] = frozenset(),
    record_provider: RecordProvider | None = None,
) -> tuple[DepGraph, list, object, str | None]:
    """Python PHASE 1 — VERBATIM move of build_dep_graph body lines 488-608.

    Scan -> target-env -> declared roots -> era-anchor (ONCE, INV-1) -> Runtime
    node -> composite record-provider default (constructed HERE at the old
    569-571 site, INV-8) -> Phase-A repair fixpoint -> aux-once (project/tools/
    seed) -> resolver restamp (INV-7). Returns (graph, roots, target_env,
    exclude_newer); only ``graph`` flows onward — the other three are provider-
    composition / test-visibility surface (never read again after the fixpoint).
    """
    host_executor = host_executor or LocalSubprocessExecutor()

    # Stage 1 — static import scan -> Import + Test nodes.
    graph = scan_to_nodes(repo_path)
    graph = _restamp(graph, {n.id for n in graph.nodes}, _SCAN_CYCLE)

    # Stage 1.5 — detect the TARGET container's env (Task 7) BEFORE root
    # selection (moved ahead of Stage 2, review fix: Stage 2's environment-
    # marker filter needs a real TargetEnv to evaluate against). ONE detected
    # TargetEnv replaces the previous two independent probes; explicit
    # target_python/target_platform (if given) patch the detected env rather
    # than skipping detection, so every other target-honest field (used by
    # marker evaluation in resolve_lock.py and roots.py) still reflects the
    # real container. The resulting `target_env` OBJECT (never decomposed into
    # separate strings) is what gets passed to select_roots below and
    # resolve_closure further down, so its RAW `platform_machine` (e.g. a
    # container reporting "arm64") reaches PEP 508 marker evaluation instead
    # of being lost to a normalized wheel-tag split.
    target_env = detect_target_env(container_executor)
    if target_python:
        target_env = replace(
            target_env,
            python_version=target_python,
            python_full=_pad_python_full(target_python),
        )
    if target_platform:
        target_env = replace(
            target_env,
            platform_machine=target_platform.split("-", 1)[0] or target_env.platform_machine,
            python_platform_tag=target_platform,
        )
    target_python = target_env.python_version

    # Stage 2 — manifest-declared-only, filtered resolver roots (imports never
    # generate roots; graph is passed but not consulted for root selection).
    # needed_extras gates which optional-dependency groups become roots at all
    # (Task 8) -- logged here since it silently determines closure membership.
    # target_env (Task 8 review fix) additionally drops a manifest dep whose
    # PEP 508 environment marker evaluates False for the TARGET (e.g. `foo ;
    # sys_platform == 'win32'` on a Linux target); see
    # roots._env_marker_excludes for the conservative keep-unless-certain rule
    # (extra-gated markers are left untouched -- that's needed_extras' job).
    logger.info("build_dep_graph: needed_extras=%s", sorted(needed_extras))
    roots = select_roots(
        repo_path, graph, needed_extras=needed_extras, target_env=target_env
    )

    # Stage 2a — anchor the resolve cutoff to the project's pinned era (HOST,
    # PyPI). A pinned old root (opencv-python==4.9.0.80) otherwise lets uv pull an
    # ABI-incompatible latest transitive dep (numpy 2.x); resolving as-of the pin
    # era keeps the closure compatible. Unset/unpinned -> None -> resolve latest.
    if exclude_newer is None:
        exclude_newer = compute_exclude_newer(roots)

    # Runtime-tier obligation: the container must run the targeted python minor.
    # Certified later by a host check (rc 0 iff sys.version_info matches); discovery
    # here never implies SATISFIED.
    from python_deps.depgraph.ids import runtime_id as _runtime_id
    _maj, _min = target_python.split(".")[:2]
    _rt_check = f'python3 -c "import sys; sys.exit(0 if sys.version_info[:2]==({_maj},{_min}) else 1)"'
    graph = graph.with_node(
        Node(
            id=_runtime_id(target_python),
            type=NodeType.RUNTIME,
            name=f"python {target_python}",
            layer=Layer.RUNTIME,
            discovered_by=DiscoveredBy.STATIC_SCAN,
            state=State.UNKNOWN,
            version=target_python,
            check_command=_rt_check,
            resolved_python=target_python,
        )
    )
    # RECORD-union coverage oracle (Correction 3). Injected in tests (fake, no
    # network). The production DEFAULT (P1.5) is the composite: the cheap
    # post-install container reader for already-installed closure members, falling
    # through to the PRE-install PyPI wheel read for not-yet-installed repair
    # CANDIDATES (and resolved-but-failed-to-build dists) — so choose_provider can
    # confirm a candidate and repair is actually functional, not inert. The PyPI
    # read is behind an injected fetch seam (coverage._default_wheel_top_levels);
    # already-installed deps never trigger a PyPI call (composite short-circuit).
    record_provider = record_provider or composite_record_provider(
        default_record_provider(container_executor), pypi_record_provider()
    )

    # === Phase A — repair FIXPOINT: resolve -> install -> audit the runtime ===
    # imports against the resolved closure's RECORD-union coverage -> repair
    # under-declarations by adding AUDIT roots -> re-resolve until stable (P1.4).
    # Install stays inside the loop (re-install each round). The loop only rebinds
    # ``graph``; every round returns a new immutable graph.
    pre_resolve_ids = {n.id for n in graph.nodes}
    graph = _phase_a_fixpoint(
        graph,
        roots,
        host_executor,
        container_executor,
        record_provider,
        target_env=target_env,
        exclude_newer=exclude_newer,
        needed_extras=needed_extras,
    )

    # Stage 3a'/3a''/3b — auxiliary node stages run ONCE after convergence (they
    # don't affect the missing-set): add the Project hub + subprocess CLI tools,
    # and seed the wheel-oracle build-essential prior. (The provisional Stage 3a
    # Import->Package heuristic is retired — Stage 4a's certified relink below is
    # now the sole Import->Package source.) Then stamp the RESOLVER discovery
    # cycle onto every node added since the resolve began that is NOT probe-
    # discovered — the install ran inside the loop and already surfaced its probe
    # Tool/SystemLib nodes, which keep the _PROBE_CYCLE stamp below (never
    # restamped back, and AUDIT provenance is set on discovered_by, not touched by
    # the cycle restamp).
    graph = _add_project_node(graph, repo_path)
    graph = add_subprocess_tool_nodes(graph, repo_path)
    graph = seed_wheel_oracle_prior(graph)
    # Stage 3b' — PROACTIVE wheel-soname priors: for each package the Phase-A
    # native_risk_from_lock stamp classified as a WHEEL (build_from_source is
    # False), read its target wheel's DT_NEEDED sonames (host, no install) and
    # seed them as RESOLVER/UNKNOWN SystemLib priors. Additive: non-native /
    # non-wheel closures (build_from_source None/True) add nothing -> byte-
    # identical. Phase-B's ldd_probe reconciles its observations onto these same
    # syslib_id nodes (reconcile_predicted), so no ordering change is needed.
    graph = wheel_preflight_probe(graph, host_executor, target_env)
    # Stage 3b'' — sdist build-dep prior: for each source-built Package
    # (build_from_source not False, incl. None/unclassified), seed the SPECIFIC
    # -dev capability priors (Bucket-B/B.1 curated + Debian Build-Depends +
    # PEP 725) PLUS the unconditional baseline binary:pkg-config (B3), UNIONING
    # with seed_wheel_oracle_prior's generic build-essential FLOOR at :568 (both
    # kept — distinct node ids never erase each other). Runs on the CONTAINER
    # executor: the Bucket-B.1 apt-installability guard shells out via
    # container_executor. RESOLVER/UNKNOWN nodes (never SATISFIED-at-seed), so
    # they fall inside the resolver_ids restamp just below.
    graph = seed_build_deps(graph, container_executor)
    # Stage 3b''' — R1b project-native-build-obligations: the repo-under-test's
    # OWN Project node gets the SAME build-dep-prior treatment as a source-built
    # Package (setup.py Extension.libraries + Debian Build-Depends keyed by the
    # project's OWN name + PEP 725 [external] read locally + the build-essential
    # floor), since it is otherwise categorically excluded from every prior
    # stage above (NodeType.PROJECT, no version -- see
    # docs/superpowers/research/R1-native-build-requirements.md). Pure-additive,
    # no-op for a repo with no native-build signal; RESOLVER/UNKNOWN nodes, so
    # they fall inside the resolver_ids restamp just below. No render-ordering
    # change needed (build_script.py's layer-then-capstone walk already renders
    # Layer.TOOLCHAIN before the Project capstone, edge-independently).
    graph = project_native_obligations(graph, repo_path, host_executor, container_executor)
    resolver_ids = {
        n.id
        for n in graph.nodes
        if n.id not in pre_resolve_ids and n.discovered_by is not DiscoveredBy.PROBE
    }
    graph = _restamp(graph, resolver_ids, _RESOLVER_CYCLE)
    return graph, roots, target_env, exclude_newer


def _python_native_obligations(graph: DepGraph, container_executor: Executor) -> DepGraph:
    """Python PHASE 2 — "look then derive" on the CONVERGED closure.

    relink (certified Import->Package + honest ``unresolved`` flags) -> ldd
    (DT_NEEDED SystemLibs) -> import_probe (dlopen backstop) -> probe restamp
    (INV-9 order; relink FIRST). Self-contained WITHOUT a snapshot: the probe
    restamp stamps every ``discovered_by=PROBE`` node — no ``pre_resolve_ids``/
    ``pre_probe_ids`` exclusion is needed, because that clause is vacuous for the
    PROBE branch AND a Phase-B-entry snapshot would wrongly drop the PROBE Tool/
    SystemLib nodes ``install_closure`` already created during Phase A (FIX-1; see
    Task 4 proof). The verbatim build.py:610-634 inline stage comments carry over
    below — this docstring does NOT replace them.
    """
    # === Phase B — tier descent on the CONVERGED closure, "look then derive". ===
    # Stage 4a — certified Import->Package relink FIRST: this is Phase B's LOOK,
    # and the SOLE Import->Package source in construction.
    # ``packages_distributions()`` (CONTAINER) certifies Import->Package edges on
    # the converged closure and flags every still-unprovided non-optional import
    # ``unresolved`` (P0.3). It adds certified EDGES + honest data flags to EXISTING
    # Import nodes — it never adds a PROBE node — so it leaves the resolver/probe
    # cycle bookkeeping (below) untouched.
    graph = certified_import_links(graph, container_executor)
    # Stage 4.5 — AUTHORITATIVE run-time native-lib discovery: ldd each installed
    # package's extension .so files and surface ``=> not found`` sonames as
    # SystemLib nodes (DT_NEEDED ground truth). Derives system deps from the SAME
    # converged closure the relink just certified (needs the built .so — runs after
    # the loop, and after the relink LOOK).
    graph = ldd_probe(graph, container_executor)
    # import_probe is the dlopen BACKSTOP only: DT_NEEDED gaps are covered by
    # Stage 4.5 (ldd_probe); this catches libs loaded at run time via dlopen that
    # never appear in the binary's NEEDED list.
    graph = import_probe(graph, container_executor)
    probe_ids = {
        n.id
        for n in graph.nodes
        if n.discovered_by is DiscoveredBy.PROBE
    }
    graph = _restamp(graph, probe_ids, _PROBE_CYCLE)
    return graph


def build_dep_graph(
    repo_path: str,
    container_executor: Executor,
    *,
    host_executor: Executor | None = None,
    target_python: str | None = None,
    target_platform: str | None = None,
    exclude_newer: str | None = None,
    needed_extras: frozenset[str] = frozenset(),
    record_provider: RecordProvider | None = None,
) -> DepGraph:
    """Build a host-certified dependency graph for ``repo_path``.

    ``container_executor`` runs install/probe/certify inside the target container;
    ``host_executor`` (default :class:`LocalSubprocessExecutor`) runs the
    host-side ``uv`` resolve.  A single :class:`TargetEnv` (Task 7) is detected
    from the container (``detect_target_env`` — one probe covering interpreter
    version, ``sys_platform``/``os_name``/``platform_machine``, and a glibc/musl
    guess for the wheel/uv platform tag used at PARSE time -- ``uv lock`` is
    universal and takes no platform flag of its own) so the resolve — and every
    PEP 508 marker it evaluates — targets the CONTAINER, never the host running
    this function.  ``target_python`` / ``target_platform`` remain accepted as
    caller overrides that patch the detected env (a hardcoded python would pin
    wheels for the wrong interpreter; an unset default would leak the dev host's
    own platform into the resolve).  The detected/patched ``TargetEnv`` OBJECT is
    passed straight into :func:`resolve_closure` (never decomposed into two
    strings first) so its RAW ``platform_machine`` — not a normalized wheel-tag
    stand-in — is what every marker evaluation downstream actually sees.  See
    the module docstring for the staged pipeline.  Returns the final immutable
    ``DepGraph``; certificates produced here are provisional (scratch-container
    scope) per design section 4.6.

    ``needed_extras`` (Task 8, targeted extras) is the set of
    ``[project.optional-dependencies]`` / ``extras_require`` group names this
    build actually needs (e.g. ``{"test"}`` when the goal is running the test
    suite). It is threaded, unchanged, into both :func:`select_roots` (which
    gates which optional groups become roots at all — fixing the prior
    "union every group" bug) and :func:`resolve_closure` (which records the
    chosen groups' scope in the resolver's temp pyproject). The default is
    deliberately runtime-only (``frozenset()``), NOT a union of every declared
    group. **Seam, not policy**: this function does not itself discover which
    extras a repo's CI/tox/Makefile actually invokes (e.g. `pip install -e
    .[test]`) — that discovery is separate future enrichment (cluster-1); a
    caller that already knows the needed groups passes them here.

    ``record_provider`` (P1.4/P1.5) is the RECORD-union coverage oracle the
    Phase-A repair fixpoint audits imports against: ``dist name -> {top-level
    modules}`` or ``None`` (no wheel to read). Injected in tests (a fake, no
    network); when omitted the production DEFAULT is
    :func:`coverage.composite_record_provider` over the cheap post-install
    container reader (:func:`coverage.default_record_provider`) and the PRE-install
    PyPI wheel reader (:func:`coverage.pypi_record_provider`) — so a not-yet-
    installed repair candidate is grounded from PyPI (P1.5, making production
    repair functional) while already-installed closure members stay network-free.
    """
    # Function-local import breaks the build<->provider cycle: by the time this
    # runs, build.py is fully loaded, so ecosystems.python.provider (which imports
    # build helpers) resolves cleanly.
    from ecosystems.registry import PROVIDERS, select_provider

    # default=PROVIDERS[0] (the PythonProvider) preserves "build_dep_graph never
    # rejects a repo": if NO provider clears the detect threshold (degenerate /
    # manifest-less / *.py-less repo), dispatch STILL routes to Python instead of
    # raising LookupError — zero-impact vs the pre-seam unconditional-accept path.
    provider = select_provider(repo_path, PROVIDERS, default=PROVIDERS[0])  # dispatch
    graph, roots, target_env, exclude_newer = provider.package_obligations(
        repo_path,
        container_executor,
        host_executor=host_executor,
        target_python=target_python,
        target_platform=target_platform,
        exclude_newer=exclude_newer,
        needed_extras=needed_extras,
        record_provider=record_provider,
    )
    # NOTE: only `graph` flows onward; roots/target_env/exclude_newer are
    # provider-composition / test-visibility surface (spec extraction boundary).

    graph = provider.native_obligations(graph, container_executor)

    # Stage 4b — release-aware apt-name reconciliation against the TARGET image:
    # remap stale predicted/table names (e.g. libglib2.0-0 -> libglib2.0-0t64)
    # so the fix-candidate is correct for the actual base image.
    graph = reconcile_apt_names(graph, container_executor)

    # Stage 5 — host certification in the container (layer-ordered; flips state).
    graph = certify_all(graph, container_executor, cycle=_CERTIFY_CYCLE)

    return graph
