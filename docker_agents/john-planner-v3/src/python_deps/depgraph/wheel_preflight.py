"""Stage 3b' — host-side pre-install wheel inspection.

For each resolved ``Package`` the artifact map classified as a WHEEL
(``build_from_source is False``), download its target wheel (host, no install),
read the sonames its extension modules link against, and seed them as
RESOLVER-origin ``SystemLib`` priors (``state=UNKNOWN``) keyed by canonical
soname. The wheel-vs-sdist branch is decided upstream by ``resolve_artifact_map``
(§1) and stamped onto ``build_from_source`` before this stage runs — a failed
per-package wheel download here is a soname-read miss, never the sdist signal it
once was. Runs BEFORE ``install_closure`` so the priors exist even if the batch
install later fails — this is what neutralizes the batch-install-poisoning gap
for wheel packages. The post-install ``ldd_probe`` reconciles its observations
onto these same soname ids automatically (``reconcile_predicted``), so no new
reconciliation logic is needed here.

Resolution is TABLE-ONLY (``os_resolver.resolve`` called with ``executor=None``):
the host has no ``apt-file``, so misses stay ``UNKNOWN`` with empty ``fix_candidates``
and are filled later by ``ldd_probe`` / ``reconcile_apt_names`` in the
container. Complementary to ``seed.py`` (sdist packages fail the download and
still fall through to its generic ``build-essential``). Degrades to a
graph-unchanged no-op on any failure.
"""

from __future__ import annotations

import logging
import tempfile

from python_deps.depgraph.executor import Executor
from python_deps.depgraph.ids import syslib_id
from python_deps.depgraph.os_resolver import ObservedNeed, resolve
from python_deps.depgraph.schema import (
    DepGraph,
    DiscoveredBy,
    Edge,
    EdgeType,
    NodeType,
    State,
)
from python_deps.depgraph.syslib import make_syslib_node
from python_deps.depgraph.target_env import TargetEnv, pip_wheel_platform_tag
from python_deps.depgraph.wheel_inspect import (
    download_target_wheel,
    inspect_wheel_sonames,
)

logger = logging.getLogger(__name__)


def wheel_preflight_probe(
    graph: DepGraph, host_executor: Executor, target_env: TargetEnv
) -> DepGraph:
    """Seed RESOLVER/UNKNOWN SystemLib priors from each package's target wheel."""
    platform_tag = pip_wheel_platform_tag(target_env)
    py_version = target_env.python_version
    abi = "cp" + py_version.replace(".", "")

    new = graph
    # §1: inspect ONLY packages the artifact map classified as wheels
    # (build_from_source is False, stamped in build.py before this stage). A
    # failed download below is then a soname-read miss, NOT an sdist signal —
    # sdist/unknown packages are skipped (their -dev build deps come from
    # seed_build_deps; their run-time libs from ldd_probe post-install).
    for pkg in [
        n
        for n in graph.nodes
        if n.type is NodeType.PACKAGE and n.version and n.build_from_source is False
    ]:
        try:
            with tempfile.TemporaryDirectory() as dest:
                wheel = download_target_wheel(
                    pkg.name,
                    pkg.version,
                    platform_tag=platform_tag,
                    py_version=py_version,
                    abi=abi,
                    dest=dest,
                    executor=host_executor,
                )
                if wheel is None:
                    continue
                sonames = inspect_wheel_sonames(wheel)
        except Exception:
            continue

        for soname in sorted(sonames):  # sorted -> deterministic node/edge order
            sid = syslib_id(soname)
            if new.get(sid) is None:
                cands = resolve(ObservedNeed("soname", soname, context="runtime"), None)
                apt = cands[0].package if cands else None
                new = new.with_node(
                    make_syslib_node(
                        soname,
                        discovered_by=DiscoveredBy.RESOLVER,
                        state=State.UNKNOWN,
                        apt=apt,
                        provenance=f"wheel:{pkg.name}",
                    )
                )
            new = new.with_edge(
                Edge(src=pkg.id, dst=sid, relation=EdgeType.REQUIRES, origin="resolver")
            )
    versioned = [n for n in graph.nodes if n.type is NodeType.PACKAGE and n.version]
    skipped_sdist = sum(1 for n in versioned if n.build_from_source is True)
    skipped_unknown = sum(1 for n in versioned if n.build_from_source is None)
    inspected = sum(1 for n in versioned if n.build_from_source is False)
    logger.info(
        "wheel_preflight: inspected=%d skipped_sdist=%d skipped_unknown=%d",
        inspected, skipped_sdist, skipped_unknown,
    )
    return new
