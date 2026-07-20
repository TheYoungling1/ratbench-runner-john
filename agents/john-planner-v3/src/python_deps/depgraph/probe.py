"""Stage 4 — probing: discover ``SystemLib`` / ``Tool`` nodes.

This realizes design section 4.4 (probe native and system needs).  Two functions,
both pure with respect to their inputs (every "mutation" returns a NEW
``DepGraph``; the originals are never changed — repo immutability rule):

* :func:`install_closure` runs ONE ``pip install`` of the resolved closure and
  parses stderr for *build-time* gaps (compiler / ``*_config`` / headers).  Each
  recognised gap becomes a ``Tool`` node (``layer=TOOLCHAIN``,
  ``discovered_by=PROBE``, ``state=MISSING``) with a ``requires`` edge from the
  owning ``Package``.
* :func:`import_probe` runs ``python -c "import X"`` for every ``Import`` node and
  every native-risk ``Package`` whose name is a valid module name, parsing stderr
  for *run-time* gaps (missing shared libraries).  Each gap becomes a
  ``SystemLib`` node (``layer=SYSTEM``) with a ``requires`` edge from the owning
  ``Package``.

This is discovery only: nodes are surfaced ``MISSING`` with evidence and an
``Attempt`` record.  The host certifies the fix later (Task 8); the apply /
re-probe remediation loop is the agent loop, out of scope here.

The certification invariant (design 3.1) holds: nothing here flips a node to
``SATISFIED``.  A discovered need is ``MISSING`` until a host-run check passes.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import replace

from python_deps.depgraph.executor import Executor
from python_deps.depgraph.failure_signatures import extract_needs
from python_deps.depgraph.ids import syslib_id, TEST_NODE_ID
from python_deps.depgraph.os_resolver import (
    ObservedNeed,
    capability_id,
    check_command_for,
    resolve,
)
from python_deps.depgraph.relink import flag_runtime_import_failure
from python_deps.depgraph.schema import (
    Attempt,
    DepGraph,
    DiscoveredBy,
    Edge,
    EdgeType,
    Layer,
    Node,
    NodeType,
    State,
)
from python_deps.depgraph.tables import NATIVE_RISK_PACKAGES
from python_deps.import_mapping import normalize_package_name

logger = logging.getLogger(__name__)

# Timeout (seconds) for the one bulk closure install. A cold install of a large
# closure (downloads + any from-source build) routinely exceeds the executor's
# 300s default; a false timeout would mark the install failed and cascade the
# whole graph to MISSING at certification, so give it generous headroom.
INSTALL_TIMEOUT = 900

# Bounded rounds of "drop the build-failing packages, reinstall the survivors" so a
# single un-buildable package cannot starve the whole closure (and the probe/relink).
MAX_INSTALL_ROUNDS = 3

# A wheel-build failure prints the distribution being built; used to attribute a
# build-time toolchain gap to the package that triggered it.
_WHEEL_FOR_RE = re.compile(r"[Bb]uilding wheel for ([A-Za-z0-9_.][A-Za-z0-9_.-]*)")
# A legal Python module name (so we never shell ``import opencv-python``).
_MODULE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Stable pip "this distribution's wheel build failed" summaries (pip 21+). Used to
# drop only the un-buildable packages and reinstall the rest.
_FAILED_WHEEL_RE = re.compile(r"Failed building wheel for ([A-Za-z0-9_.][A-Za-z0-9_.-]*)")
_COULD_NOT_BUILD_RE = re.compile(r"Could not build wheels for ([A-Za-z0-9_.,\s-]+?),?\s+which")
_FAILED_TO_BUILD_RE = re.compile(
    r"Failed to build installable wheels for some pyproject\.toml based projects "
    r"\(([A-Za-z0-9_.,\s-]+)\)"
)


def install_closure(graph: DepGraph, executor: Executor) -> DepGraph:
    """Install the resolved closure once; surface build-time toolchain gaps.

    Returns a new graph with a ``Tool`` node + ``requires`` edge for every
    recognised build-time gap, and an install ``Attempt`` recorded on every
    installed ``Package`` node.

    Resolver-diagnosed ``MISSING`` packages (unresolvable / conflict placeholders
    with no real version) are excluded from the bulk install: adding one makes the
    single ``pip install`` fail and poisons the whole closure (every good package
    would then certify ``MISSING``), defeating per-root resilience.
    """
    packages = [
        n
        for n in graph.nodes
        if n.type is NodeType.PACKAGE and n.state is not State.MISSING
    ]
    if not packages:
        return graph

    command = "python -m pip install " + " ".join(_spec(p) for p in _sorted(packages))
    result = executor.run(command, timeout=INSTALL_TIMEOUT)
    outcome = "succeeded" if result.ok else "failed"

    new = graph
    install_attempt = Attempt(command=command, outcome=outcome)
    for pkg in packages:
        node = new.get(pkg.id)
        new = new.with_node(node.with_attempt(install_attempt))

    if result.ok:
        return new

    stderr = result.stderr or ""
    owners = _build_owners(packages, stderr)
    for need in extract_needs(stderr, context_hint="build"):
        new, node_id = _ingest_need(
            new, need, stderr=stderr, command=command, executor=executor
        )
        for src in owners:
            new = new.with_edge(
                Edge(src=src, dst=node_id, relation=EdgeType.REQUIRES, origin="probe")
            )

    # Salvage the survivors: a single un-buildable package (e.g. an off-platform
    # optional dep) aborts the all-or-nothing bulk install and leaves the whole
    # closure uninstalled, starving the import probe and the relink. Drop the
    # build-failing packages and reinstall the rest.
    new = _reinstall_survivors(new, packages, stderr, executor)
    return new


def _failed_build_packages(stderr: str) -> set[str]:
    """Canonical (PEP 503) names of distributions whose wheel build FAILED.

    Parses the stable pip failure summaries so the un-buildable packages can be
    dropped and the rest reinstalled. Returns an empty set when no build failure
    can be attributed (the caller then changes nothing — never worse than today).
    """
    names: set[str] = set()
    for match in _FAILED_WHEEL_RE.finditer(stderr):
        names.add(match.group(1))
    for pattern in (_COULD_NOT_BUILD_RE, _FAILED_TO_BUILD_RE):
        for match in pattern.finditer(stderr):
            names.update(part.strip() for part in match.group(1).split(",") if part.strip())
    return {normalize_package_name(name) for name in names}


def _requirers_of_failed(
    graph: DepGraph, packages: list[Node], failed: set[str]
) -> set[str]:
    """Package node ids that TRANSITIVELY require any failed-build package.

    Reinstalling such a package re-pulls the un-buildable dependency (its own
    install_requires), so the survivor salvage must drop them too — otherwise
    each round re-introduces the failure and the loop never converges. BFS over
    package->package ``requires`` edges, reversed (dst failed -> drop its srcs).
    """
    pkg_ids = {p.id for p in packages}
    by_name = {normalize_package_name(p.name): p.id for p in packages}
    failed_ids = {by_name[name] for name in failed if name in by_name}

    requirers: dict[str, set[str]] = defaultdict(set)
    for edge in graph.edges:
        if (edge.relation is EdgeType.REQUIRES
                and edge.src in pkg_ids and edge.dst in pkg_ids):
            requirers[edge.dst].add(edge.src)

    drop: set[str] = set()
    frontier = set(failed_ids)
    while frontier:
        nxt: set[str] = set()
        for fid in frontier:
            for src in requirers.get(fid, ()):
                if src not in drop and src not in failed_ids:
                    drop.add(src)
                    nxt.add(src)
        frontier = nxt
    return drop


def _reinstall_survivors(
    graph: DepGraph, packages: list[Node], stderr: str, executor: Executor
) -> DepGraph:
    """Reinstall the closure minus packages whose build failed AND anything that
    transitively requires them (a requirer re-pulls the un-buildable dep). Bounded
    rounds. Returns a NEW graph; a no-op when nothing is attributable/droppable.
    """
    failed = _failed_build_packages(stderr)
    drop_ids = _requirers_of_failed(graph, packages, failed)
    survivors = [
        p for p in packages
        if normalize_package_name(p.name) not in failed and p.id not in drop_ids
    ]
    if not failed or not survivors or len(survivors) == len(packages):
        return graph

    new = graph
    for _round in range(MAX_INSTALL_ROUNDS):
        command = "python -m pip install " + " ".join(_spec(p) for p in _sorted(survivors))
        result = executor.run(command, timeout=INSTALL_TIMEOUT)
        attempt = Attempt(command=command, outcome="succeeded" if result.ok else "failed")
        for pkg in survivors:
            new = new.with_node(new.get(pkg.id).with_attempt(attempt))
        if result.ok:
            break
        more_failed = _failed_build_packages(result.stderr or "")
        more_drop = _requirers_of_failed(graph, packages, more_failed)
        next_survivors = [
            p for p in survivors
            if normalize_package_name(p.name) not in more_failed and p.id not in more_drop
        ]
        if not more_failed or not next_survivors or len(next_survivors) == len(survivors):
            break  # no further progress
        survivors = next_survivors
    return new


def import_probe(graph: DepGraph, executor: Executor) -> DepGraph:
    """Import-probe every Import / native-risk Package; surface run-time gaps.

    For each probe target, runs ``python -c "import X"`` once, records an
    ``Attempt`` on the affected node(s), and — on failure — surfaces every need
    ``extract_needs`` recognises in stderr (soname, header, binary, pkgconfig),
    each as a node with a ``requires`` edge from the owning ``Package`` (or the
    ``Import`` itself when no package is linked). Not soname-only: a C-extension
    import failing on a missing binary/header/pkgconfig dep is discoverable too.

    2-phase preservation (HEAD): when ``extract_needs`` recognises NOTHING in a
    failed probe's stderr, the failure is a metadata-PRESENT non-native import
    error — a dist supplies the import name (there is an outgoing REQUIRES edge),
    yet ``import X`` still raises for a Python-level reason. Rather than silently
    dropping it (the third failure class), the owning Import node is honestly
    flagged via ``flag_runtime_import_failure``. That flag only touches PROVIDED
    Import nodes, so metadata-ABSENT imports (already flagged ``unresolved``,
    P0.3) are left alone.
    """
    targets = _probe_targets(graph)

    new = graph
    for name, target in targets.items():
        command = f'python -c "import {name}"'
        result = executor.run(command)
        outcome = "succeeded" if result.ok else "failed"
        attempt = Attempt(command=command, outcome=outcome)
        for node_id in target["attempt_nodes"]:
            node = new.get(node_id)
            if node is not None:
                new = new.with_node(node.with_attempt(attempt))

        if result.ok:
            continue
        stderr = result.stderr or ""
        needs = extract_needs(stderr, context_hint="runtime")
        if needs:
            for need in needs:
                new, node_id = _ingest_need(
                    new, need, stderr=stderr, command=command, executor=executor
                )
                for src in _edge_sources(target):
                    new = new.with_edge(
                        Edge(src=src, dst=node_id, relation=EdgeType.REQUIRES, origin="probe")
                    )
        else:
            # Non-native import failure with NO recognised need. A metadata-PRESENT
            # import (a dist provides it, yet ``import X`` still raises) is the third
            # failure class: honestly flag the owning Import node instead of silently
            # dropping it. Never fabricate a SystemLib here (there is no missing
            # soname). Metadata-ABSENT imports are already flagged ``unresolved``
            # (P0.3) and are left alone by flag_runtime_import_failure (it only
            # touches provided Import nodes).
            reason = _short_import_error(stderr) or f"import {name} failed"
            for node_id in target["attempt_nodes"]:
                new = flag_runtime_import_failure(new, node_id, reason=reason)
    return new


def test_gate_probe(
    graph: DepGraph,
    executor: "Executor | None",
    stderr: str,
    *,
    command: str = "pytest",
    owner_id: str | None = TEST_NODE_ID,
) -> DepGraph:
    """Ingest runtime SystemLib sonames surfaced by the repo's own test run.

    See the design spec §3 (the test run is the dlopen-tail oracle). ``ldd``
    (DT_NEEDED) and ``import`` (eager module-init dlopen) are a PARTIAL backstop;
    a feature-gated ``dlopen`` (onnxruntime session-create, Qt ``QApplication``,
    GDAL reprojection) only fires when a test exercises the feature, so the
    testability gate's stderr is the oracle for that tail. Each ``soname`` need
    is resolved to apt and reconciled onto ``syslib:<soname>`` through the SAME
    ``_ingest_need`` path as ``import_probe`` (so it becomes renderable). Pure.
    """
    new = graph
    for need in extract_needs(stderr, context_hint="runtime"):
        if need.kind != "soname":
            continue  # only a runtime shared object is actionable at test time
        new, node_id = _ingest_need(
            new, need, stderr=stderr, command=command, executor=executor
        )
        node = new.get(node_id)
        logger.info(
            "test_gate: dlopen-tail soname=%s fix=%s (missed by ldd+import)",
            need.name, node.chosen_fix if node else None,
        )
        if owner_id is not None and new.get(owner_id) is not None:
            new = new.with_edge(
                Edge(src=owner_id, dst=node_id, relation=EdgeType.REQUIRES, origin="test-gate")
            )
    return new


# Its name matches pytest's default ``test_*`` collection pattern; mark it
# not-a-test so importing it into tests/depgraph/test_test_gate_probe.py does
# not make pytest try to call it as a test function (missing-fixture error).
test_gate_probe.__test__ = False


# --------------------------------------------------------------------------- #
# Prediction reconciliation                                                    #
# --------------------------------------------------------------------------- #
def reconcile_predicted(
    graph: DepGraph,
    predicted_id: str,
    *,
    check: str,
    evidence: str,
    command: str,
    chosen_fix: str | None = None,
    fix_candidates: tuple[str, ...] = (),
) -> Node | None:
    """Reconcile an observed gap with a resolver *prediction* of the same id.

    ``predicted_id`` is the CANONICAL id for the observed gap: capability-keyed
    (``capability_id``) for a build-time ``Tool`` (``binary:``/``header:``),
    soname-keyed (``syslib_id``) for a run-time ``SystemLib`` (Task 9 — the
    soname is the SystemLib's identity, not the apt name, so callers must pass
    ``syslib_id(soname)`` here, never ``syslib_id(apt)``).  When the resolver
    pre-emitted a predicted node (seed
    stage) at that same id, return a NEW node that keeps the predicted node's
    id + discovery origin (``discovered_by`` stays RESOLVER per the spec) but
    adopts the real observed ``check_command`` / ``evidence`` and records the
    failing probe attempt.  ``state`` is left for the host certifier to flip —
    discovery never certifies (design 3.1).

    ``chosen_fix`` / ``fix_candidates`` carry what the OBSERVE-path resolve()
    just learned. When the prediction resolved no provider (``chosen_fix`` is
    falsy) but the observation did, the observed fix is adopted so the node
    becomes renderable — the spec promises "observation fills a chosen_fix
    that prediction left None." A prediction that already has a chosen_fix is
    never overridden by a (possibly different) observed one.

    Returns ``None`` when there is no matching prediction (caller then creates a
    fresh probe-discovered node), so existing observed-only behavior is preserved.
    """
    predicted = graph.get(predicted_id)
    if predicted is None or predicted.discovered_by is not DiscoveredBy.RESOLVER:
        return None
    changes: dict = {"check_command": check, "evidence": evidence}
    if not predicted.chosen_fix and chosen_fix:
        data = dict(predicted.data)
        data["resolution_status"] = "resolved"
        changes.update(
            chosen_fix=chosen_fix,
            fix_candidates=fix_candidates or (chosen_fix,),
            data=data,
        )
    return replace(predicted, **changes).with_attempt(
        Attempt(command=command, outcome="failed", check=check)
    )


# --------------------------------------------------------------------------- #
# Node builders                                                                #
# --------------------------------------------------------------------------- #
def _ingest_need(
    graph: DepGraph,
    need: ObservedNeed,
    *,
    stderr: str,
    command: str,
    executor: Executor,
) -> tuple[DepGraph, str]:
    """Resolve one observed need; reconcile with a prediction of the same
    capability id, else create a fresh probe node. Returns (new_graph, node_id).

    ``capability_id`` and ``check_command_for`` already dispatch by kind, so the
    only kind-specific step is the fresh-node builder: ``soname`` -> SystemLib,
    every other kind -> Tool.
    """
    check = check_command_for(need)
    evidence = need.evidence or _first_line_with(stderr, need.name)
    predicted_id = capability_id(need)
    cands = resolve(need, executor)
    fix = f"apt:{cands[0].package}" if cands else None
    fixc = tuple(f"apt:{c.package}" for c in cands)
    reconciled = reconcile_predicted(
        graph,
        predicted_id,
        check=check,
        evidence=evidence,
        command=command,
        chosen_fix=fix,
        fix_candidates=fixc,
    )
    if reconciled is not None:
        return graph.with_node(reconciled), reconciled.id
    if need.kind == "soname":
        node = _make_syslib_node(
            need.name, stderr, command, apt=cands[0].package if cands else None
        )
    else:
        node = _make_capability_node(need, stderr, command, executor)
    return graph.with_node(node), node.id


def _make_capability_node(
    need: ObservedNeed, stderr: str, command: str, executor: Executor
) -> Node:
    cands = resolve(need, executor)
    top = cands[0] if cands else None
    fix = f"apt:{top.package}" if top else None
    check = top.check_command if top else check_command_for(need)
    node = Node(
        id=capability_id(need),
        type=NodeType.TOOL,
        name=need.name,
        layer=Layer.TOOLCHAIN,
        discovered_by=DiscoveredBy.PROBE,
        state=State.MISSING,
        check_command=check,
        evidence=need.evidence or _first_line_with(stderr, need.name),
        fix_candidates=(fix,) if fix else (),
        chosen_fix=fix,
        provenance="probe (observed)",
        data={
            "kind": need.kind,
            "context": need.context,
            "observation_strength": "strong",
            "resolution_status": "resolved" if fix else "unresolved",
        },
    )
    return node.with_attempt(
        Attempt(command=command, outcome="failed", check=check)
    )


def _make_syslib_node(
    soname: str, stderr: str, command: str, apt: str | None = None
) -> Node:
    check = f"ldconfig -p | grep {soname}"
    node = Node(
        id=syslib_id(soname),
        type=NodeType.SYSTEM_LIB,
        name=soname,
        layer=Layer.SYSTEM,
        discovered_by=DiscoveredBy.PROBE,
        state=State.MISSING,
        check_command=check,
        evidence=_first_line_with(stderr, soname),
        fix_candidates=(f"apt:{apt}",) if apt else (),
        chosen_fix=f"apt:{apt}" if apt else None,
        provenance="probe (observed)",
    )
    return node.with_attempt(
        Attempt(command=command, outcome="failed", check=check)
    )


# --------------------------------------------------------------------------- #
# Target selection / attribution                                              #
# --------------------------------------------------------------------------- #
def _probe_targets(graph: DepGraph) -> dict[str, dict]:
    """import-name -> {owners, attempt_nodes, fallback_src}.

    Aggregates each ``Import`` (owner = the ``Package`` it requires) and each
    native-risk ``Package`` whose name is a valid module name (owner = itself).
    Deduped by import name so each name is probed exactly once.
    """
    targets: dict[str, dict] = {}

    for imp in (n for n in graph.nodes if n.type is NodeType.IMPORT):
        entry = targets.setdefault(
            imp.name, {"owners": set(), "attempt_nodes": set(), "fallback_src": None}
        )
        entry["attempt_nodes"].add(imp.id)
        entry["fallback_src"] = imp.id
        entry["owners"].update(
            p.id for p in graph.requires_of(imp.id) if p.type is NodeType.PACKAGE
        )

    # NATIVE_RISK_PACKAGES now scopes the dlopen BACKSTOP only: DT_NEEDED run-time
    # gaps are covered authoritatively by Stage 4.5 (ldd_probe); this list just
    # picks packages worth import-probing for dlopen'd libs not in their NEEDED list.
    for pkg in (n for n in graph.nodes if n.type is NodeType.PACKAGE):
        if pkg.name not in NATIVE_RISK_PACKAGES:
            continue
        if not _MODULE_NAME_RE.match(pkg.name):
            continue
        entry = targets.setdefault(
            pkg.name, {"owners": set(), "attempt_nodes": set(), "fallback_src": None}
        )
        entry["owners"].add(pkg.id)
        entry["attempt_nodes"].add(pkg.id)

    return targets


def _edge_sources(target: dict) -> set[str]:
    """Owning packages for a discovered SystemLib, else the Import node itself."""
    owners = target["owners"]
    if owners:
        return owners
    fallback = target["fallback_src"]
    return {fallback} if fallback else set()


def _build_owners(packages: list[Node], stderr: str) -> set[str]:
    """Packages a build-time gap is attributable to.

    Prefer the distribution named in a "Building wheel for X" line; otherwise
    fall back to the native-risk packages present in the closure (the gap came
    from *some* compiled build, and those are the ones that compile).
    """
    by_name = {p.name: p.id for p in packages}
    owners = {
        by_name[m.group(1)]
        for m in _WHEEL_FOR_RE.finditer(stderr)
        if m.group(1) in by_name
    }
    if owners:
        return owners
    return {p.id for p in packages if p.name in NATIVE_RISK_PACKAGES}


# --------------------------------------------------------------------------- #
# Small pure helpers                                                          #
# --------------------------------------------------------------------------- #
def _spec(pkg: Node) -> str:
    return f"{pkg.name}=={pkg.version}" if pkg.version else pkg.name


def _sorted(packages: list[Node]) -> list[Node]:
    return sorted(packages, key=lambda n: n.name)


def _short_import_error(stderr: str, max_chars: int = 200) -> str:
    """The exception line of an import failure — the LAST non-empty stderr line
    (a Python traceback ends with ``ExcType: message``), trimmed. Keeps the honest
    ``import_error`` flag short instead of dumping the whole traceback."""
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    return lines[-1][:max_chars] if lines else ""


def _first_line_with(text: str, needle: str, max_chars: int = 500) -> str:
    for line in (text or "").splitlines():
        if needle in line:
            return line.strip()[:max_chars]
    return (text or "").strip()[:max_chars]
