"""Curated build-time system-dependency prior — capability-keyed nodes.

For a source-built (sdist) package that is a KNOWN native package, predict the
low-level capability (``binary:``/``header:``/``pkgconfig:``) its build
reports before install, instead of the generic ``build-essential`` that
``seed.py`` predicts. ``build_dep_prior``/``seed_build_deps`` assemble this
from multiple sources (curated table, flavor overrides, Debian
Build-Depends, PEP 725 ``[external]``) and ``resolve(need, executor=None)``
maps each capability onto an apt provider via ``PROVIDER_TABLE`` first,
falling back to the container's ``executor`` (e.g. apt-file) when passed one;
a resolution miss seeds an honest unresolved node (``chosen_fix`` is None)
rather than fabricating a package.

Capability-keyed (``capability_id``) so the post-install observed gap
reconciles onto the SAME node (see ``probe.py``) — predict and observe
collapse to ONE node instead of two.

Complementary to ``seed.py``, not a replacement: a table package still gets a
``build-essential`` edge (it needs a compiler) AND the specific capability
edge(s).

Pure: every "mutation" returns a NEW ``DepGraph`` (repo immutability rule).
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass, replace

from python_deps.depgraph.debian_builddeps import debian_build_deps
from python_deps.depgraph.executor import Executor
from python_deps.depgraph.ids import apt_build_id
from python_deps.depgraph.os_resolver import ObservedNeed, capability_id, resolve
from python_deps.depgraph.pep725 import pep725_external
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
from python_deps.import_mapping import normalize_package_name

logger = logging.getLogger(__name__)

# Canonical package name -> the capability need(s) its from-source build reports.
# Confident, PoC/well-known-verified subset (see plan Scope note). resolve()
# maps each capability -> apt via PROVIDER_TABLE at construction (table-only).
PACKAGE_TO_BUILD_NEEDS: dict[str, tuple[ObservedNeed, ...]] = {
    "psycopg2": (ObservedNeed("binary", "pg_config", context="build", strength="curated"),),
    "mysqlclient": (ObservedNeed("binary", "mysql_config", context="build", strength="curated"),),
    "pycairo": (ObservedNeed("pkgconfig", "cairo", context="build", strength="curated"),),
    "pyaudio": (ObservedNeed("header", "portaudio.h", context="build", strength="curated"),),
    "pyodbc": (ObservedNeed("header", "sql.h", context="build", strength="curated"),),
    "python-ldap": (
        ObservedNeed("header", "ldap.h", context="build", strength="curated"),
        ObservedNeed("header", "sasl/sasl.h", context="build", strength="curated"),
    ),
    "dbus-python": (
        ObservedNeed("pkgconfig", "dbus-1", context="build", strength="curated"),
        ObservedNeed("pkgconfig", "glib-2.0", context="build", strength="curated"),
    ),
    "python-snappy": (ObservedNeed("header", "snappy.h", context="build", strength="curated"),),
}
# pygobject is deliberately EXCLUDED — its girepository dev pkg + pkgconfig
# module differ by Debian release (bookworm: libgirepository1.0-dev /
# gobject-introspection-1.0; trixie: libgirepository-2.0-dev /
# girepository-2.0) and by pygobject version, so a single curated entry would
# be wrong on one base. Left to observe-time apt-file + apt_verify.

@dataclass(frozen=True)
class FlavorOverride:
    """Curated correction for a multi-backend native lib.

    ``needs`` are forced, highest-priority CORE capability needs that pin the
    backend the sdist actually expects (overriding the distro's default — e.g.
    Debian builds pycurl against gnutls but the openssl backend is what most
    stacks want). ``apt_directives`` are forced apt names for corrections that are
    best expressed as an apt package rather than a capability (empty for pycurl;
    available for future libs). ``build_env`` are build-time env vars stamped onto
    the owner package node's ``data["build_env"]`` (rendering the ``export`` before
    the pip build is a populate.py follow-up, out of Part 3 scope). ``suppress_apt``
    are Debian apt names this flavor's backend pin makes conflicting/unwanted (e.g.
    pycurl's Debian Build-Depends hard-lists the gnutls curl backend even though the
    flavor forces openssl; both landing in one ``apt-get install`` breaks it since
    they are mutually ``Conflicts:``) — dropped from the Debian directives.
    """
    needs: tuple[ObservedNeed, ...]
    build_env: tuple[tuple[str, str], ...] = ()
    apt_directives: tuple[str, ...] = ()
    suppress_apt: tuple[str, ...] = ()


FLAVOR_OVERRIDES: dict[str, FlavorOverride] = {
    # pycurl links whatever curl-config points at; Debian's default curl is gnutls,
    # but the openssl backend is the common expectation. Force the openssl -dev pair
    # (curl-config -> libcurl4-openssl-dev, openssl/ssl.h -> libssl-dev via
    # PROVIDER_TABLE) and pin the backend with PYCURL_SSL_LIBRARY=openssl.
    "pycurl": FlavorOverride(
        needs=(
            ObservedNeed("binary", "curl-config", context="build", strength="curated"),
            ObservedNeed("header", "openssl/ssl.h", context="build", strength="curated"),
        ),
        build_env=(("PYCURL_SSL_LIBRARY", "openssl"),),
        # Debian's real Build-Depends hard-lists the gnutls curl backend; suppress
        # it (+ the gnutls lib it pulls) so it doesn't land alongside the
        # flavor-forced libcurl4-openssl-dev (mutually Conflicts: at apt-install
        # time). libssh2-1-dev is a legitimate non-conflicting build dep — kept.
        suppress_apt=("libcurl4-gnutls-dev", "libgnutls28-dev"),
    ),
}


def build_env_for(pkg_name: str) -> dict[str, str]:
    """Build-time env vars a package's flavor override pins (``{}`` when none)."""
    flavor = FLAVOR_OVERRIDES.get(normalize_package_name(pkg_name))
    return dict(flavor.build_env) if flavor and flavor.build_env else {}


@dataclass(frozen=True)
class BuildDepPlan:
    """The assembled build-time prior for one sdist package.

    ``capability_needs`` — precise, capability-keyed needs (curated/flavor/PEP 725),
      ALWAYS seeded as capability nodes (they reconcile with the observe path).
    ``apt_directives`` — Debian (+ forced-flavor) apt package names to seed as
      apt-keyed ``aptdep:`` nodes that pre-satisfy the build.
    """
    capability_needs: list[ObservedNeed]
    apt_directives: list[str]


def _resolved_apt(need: ObservedNeed, executor: Executor) -> str | None:
    """The apt package a capability need resolves to: table first (no container),
    else the container's apt-file. ``None`` when unresolved (then it covers no
    Debian directive). Used only for de-duplicating the Debian apt list."""
    cands = resolve(need, executor=None)
    if not cands and executor is not None:
        cands = resolve(need, executor)
    return cands[0].package if cands else None


def _apt_installable(pkgs: list[str], executor: Executor) -> bool:
    """True iff ``apt-get install -s`` (SIMULATE — no download/compile) resolves the
    set cleanly. The apt=0 guard against a Debian source whose full Build-Depends
    conflict or don't exist (uWSGI's per-plugin -devs). Empty set is installable."""
    if not pkgs:
        return True
    quoted = " ".join(shlex.quote(p) for p in pkgs)
    return executor.run(f"apt-get install -s {quoted}").returncode == 0


def _dedup(names: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def build_dep_prior(
    pkg_name: str, version: str | None, executor: Executor
) -> BuildDepPlan:
    """Assemble the build-time ``-dev`` prior for one sdist package.

    Capability needs (priority union flavor > PEP 725 > curated, deduped by
    ``capability_id``, all ``context="build"``/``strength="curated"``) are the
    precise, always-seeded part. Debian ``Build-Depends`` are apt install
    directives: any name a capability node already resolves to is dropped
    (dedup at the resolved-apt level); the remaining names are always seeded
    as ``apt_directives`` (no threshold). A flavor's forced apt names are
    always directives. Empty plan when no source contributes.
    """
    canonical = normalize_package_name(pkg_name)
    flavor = FLAVOR_OVERRIDES.get(canonical)
    forced_needs = list(flavor.needs) if flavor else []
    forced_apt = list(flavor.apt_directives) if flavor else []
    pep = list(pep725_external(canonical, version, executor))
    curated = list(PACKAGE_TO_BUILD_NEEDS.get(canonical, ()))

    # Precise capability needs: priority union, deduped by capability_id.
    capability_needs: list[ObservedNeed] = []
    seen_caps: set[str] = set()
    for need in (*forced_needs, *pep, *curated):  # priority order
        cid = capability_id(need)
        if cid in seen_caps:
            continue
        seen_caps.add(cid)
        capability_needs.append(replace(need, context="build", strength="curated"))

    # Apt names already covered by a capability node (+ forced flavor apt).
    covered: set[str] = set(forced_apt)
    if flavor:
        covered.update(flavor.suppress_apt)
    for need in capability_needs:
        apt = _resolved_apt(need, executor)
        if apt:
            covered.add(apt)
    # seed_build_deps always baseline-seeds binary:pkg-config (-> apt:pkgconf),
    # so a Debian source that explicitly lists pkg-config/pkgconf is already
    # covered — drop it here rather than seed a redundant second aptdep node.
    covered.update(("pkg-config", "pkgconf"))

    # Debian directives minus anything already covered by a capability node.
    raw_debian = debian_build_deps(canonical, executor)
    covered_debian = [n for n in raw_debian if n in covered]
    debian = [n for n in raw_debian if n not in covered]
    if debian and not _apt_installable(debian, executor):
        logger.info("build_dep_prior: %s debian set not apt-installable, dropping %s",
                    canonical, debian)
        debian = []
    apt_directives = _dedup(forced_apt + debian)
    logger.info(
        "build_dep_prior: %s caps=%d debian_apt=%d dedup_dropped=%s",
        canonical, len(capability_needs), len(debian), covered_debian,
    )
    return BuildDepPlan(capability_needs, apt_directives)


# build-time capability -> NodeType.TOOL / Layer.TOOLCHAIN (renders before pip).

_PKG_CONFIG_NEED = ObservedNeed("binary", "pkg-config", context="build", strength="curated")


def _capability_node(need: ObservedNeed, executor: Executor) -> Node:
    """A capability-keyed RESOLVER/UNKNOWN node; chosen_fix resolved via table
    then, on a table miss, the container's apt-file (now that seed has an
    executor). Reconciles with the observe path on ``capability_id``."""
    cands = resolve(need, executor)
    top = cands[0] if cands else None
    fix = f"apt:{top.package}" if top else None
    return Node(
        id=capability_id(need),
        type=NodeType.TOOL,
        name=need.name,
        layer=Layer.TOOLCHAIN,
        discovered_by=DiscoveredBy.RESOLVER,
        state=State.UNKNOWN,
        check_command=top.check_command if top else None,
        fix_candidates=(fix,) if fix else (),
        chosen_fix=fix,
        provenance="curated build-dep prediction",
        data={
            "kind": need.kind,
            "context": need.context,
            "observation_strength": need.strength,
            "resolution_status": "resolved" if fix else "unresolved",
        },
    )


def _apt_build_node(name: str) -> Node:
    """An apt-keyed Debian build directive node (separate ``aptdep:`` id space).

    The apt name IS the fix, so ``chosen_fix=apt:<name>`` -> it renders (emit
    installs it, TOOLCHAIN tier before pip) and its ``dpkg`` check certifies
    (installed? SATISFIED : MISSING). Seeded UNKNOWN; certify flips it, and a
    MISSING apt: TOOL is emittable/reciped. Pre-satisfies the build, so unlike a
    capability node it never reconciles with an observation.
    """
    check = (
        f"dpkg-query -W -f='${{Status}}' {name} "
        f"2>/dev/null | grep -q 'install ok installed'"
    )
    return Node(
        id=apt_build_id(name),
        type=NodeType.TOOL,
        name=name,
        layer=Layer.TOOLCHAIN,
        discovered_by=DiscoveredBy.RESOLVER,
        state=State.UNKNOWN,
        check_command=check,
        fix_candidates=(f"apt:{name}",),
        chosen_fix=f"apt:{name}",
        provenance="debian build-dep",
        data={"source": "debian"},
    )


def seed_build_deps(graph: DepGraph, executor: Executor) -> DepGraph:
    """Seed the multi-source build-time prior for source-built packages.

    For each source-built Package (``build_from_source`` not False, real version),
    every such package first gets a baseline ``binary:pkg-config`` capability
    node + edge (Debian omits it from Build-Depends as buildd-assumed, and
    slim images lack it — see B3). ``build_dep_prior`` then assembles a
    ``BuildDepPlan``. Capability needs become capability-keyed nodes
    (``binary:``/``header:``/``pkgconfig:``) with a ``requires`` edge (always
    seeded; reconcile with the observe path). Debian apt directives become
    apt-keyed ``aptdep:`` nodes with a ``requires`` edge (pre-satisfy, render
    before pip). A flavor override's ``build_env`` is stamped on the Package
    node. Pure; a no-op when no target package contributes a prior (though
    the baseline pkg-config seed is unconditional for every source-built
    package). ``executor`` lets Debian lookups and off-table capability
    resolution use the target container.
    """
    new = graph
    pkgs = cap_nodes = aptdep_nodes = 0
    pc_id = capability_id(_PKG_CONFIG_NEED)
    for pkg in graph.nodes:
        if pkg.type is not NodeType.PACKAGE or not pkg.version:
            continue
        if pkg.build_from_source is False:
            continue

        # B3: baseline pkg-config for EVERY source-built package. Debian omits it
        # (buildd-assumed); slim images lack it. Reuses the binary:pkg-config
        # capability node (os_resolver -> apt:pkgconf), deduped once, plan-independent.
        if new.get(pc_id) is None:
            new = new.with_node(_capability_node(_PKG_CONFIG_NEED, executor))
            cap_nodes += 1
        new = new.with_edge(
            Edge(src=pkg.id, dst=pc_id, relation=EdgeType.REQUIRES, origin="resolver")
        )

        plan = build_dep_prior(pkg.name, pkg.version, executor)
        if not (plan.capability_needs or plan.apt_directives):
            continue
        pkgs += 1

        # Stamp flavor build_env on the owner package node.
        updates: dict = {}
        env = build_env_for(pkg.name)
        if env:
            updates["build_env"] = env
        if updates:
            current = new.get(pkg.id)
            new = new.with_node(replace(current, data={**current.data, **updates}))

        # Capability needs -> capability node + edge.
        for need in plan.capability_needs:
            node_id = capability_id(need)
            if new.get(node_id) is None:
                new = new.with_node(_capability_node(need, executor))
                cap_nodes += 1
            new = new.with_edge(
                Edge(src=pkg.id, dst=node_id, relation=EdgeType.REQUIRES, origin="resolver")
            )

        # Debian apt directives -> apt-keyed aptdep: node + edge.
        for name in plan.apt_directives:
            node_id = apt_build_id(name)
            if new.get(node_id) is None:
                new = new.with_node(_apt_build_node(name))
                aptdep_nodes += 1
            new = new.with_edge(
                Edge(src=pkg.id, dst=node_id, relation=EdgeType.REQUIRES, origin="resolver")
            )
    logger.info(
        "seed_build_deps: pkgs=%d cap_nodes=%d aptdep_nodes=%d",
        pkgs, cap_nodes, aptdep_nodes,
    )
    return new
