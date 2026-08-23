"""The isolated slice of our pipeline under test (design §6).

Grades the PRE-INSTALL TOOL prediction only: the branch oracle
(resolve_artifact_map) + the build-essential toolchain prior
(seed_wheel_oracle_prior) + the -dev build-dep prior (seed_build_deps),
mirroring production build.py's pre-install seed order. The branch oracle
now runs with a detected target_env (enabling platform/pypi fallback tiers),
and an unclassified package is seeded conservatively (build_from_source=None).
It does NOT include the host-side wheel_preflight_probe (runtime-SystemLib
prediction from a wheel's DT_NEEDED) or the post-install ldd_probe/import-probe
stages, so a natural-mode WHEEL that needs an un-bundled runtime lib is out
of this seam's scope. Thin on purpose: grades the detection modules directly,
unit-testable with a fake executor."""
from __future__ import annotations

from dataclasses import dataclass

from python_deps.depgraph.artifact_map import resolve_artifact_map
from python_deps.depgraph.build_deps import seed_build_deps
from python_deps.depgraph.ids import package_id
from python_deps.depgraph.schema import DepGraph, DiscoveredBy, Layer, Node, NodeType
from python_deps.depgraph.seed import seed_wheel_oracle_prior
from python_deps.depgraph.target_env import detect_target_env
from python_deps.import_mapping import normalize_package_name

_APT = "apt:"


@dataclass(frozen=True)
class PredictResult:
    apt: frozenset[str]
    branch: str  # "wheel" | "sdist" | "unknown"


def _apt_of(graph: DepGraph) -> frozenset[str]:
    return frozenset(
        n.chosen_fix[len(_APT):]
        for n in graph.nodes
        if n.type is NodeType.TOOL and n.chosen_fix and n.chosen_fix.startswith(_APT)
    )


def predict_apt_deps(name: str, version: str, mode: str, executor) -> PredictResult:
    """Predict the apt set P (+ branch) our detection produces for one package."""
    if mode == "forced_sdist":
        branch, build_from_source = "sdist", True
    else:
        cls = resolve_artifact_map(
            [f"{name}=={version}"], executor, target_env=detect_target_env(executor)
        )
        val = cls.get(normalize_package_name(name))
        if val == "wheel":
            branch, build_from_source = "wheel", False
        elif val == "sdist":
            branch, build_from_source = "sdist", True
        else:
            branch, build_from_source = "unknown", None

    pkg = Node(
        id=package_id(name, version),
        type=NodeType.PACKAGE,
        name=name,
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.RESOLVER,
        version=version,
        build_from_source=build_from_source,
    )
    graph = DepGraph().with_node(pkg)
    graph = seed_wheel_oracle_prior(graph)  # mirror build.py:408 — build-essential for from-source pkgs
    seeded = seed_build_deps(graph, executor)
    return PredictResult(apt=_apt_of(seeded), branch=branch)
