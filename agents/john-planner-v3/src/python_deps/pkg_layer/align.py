# src/python_deps/pkg_layer/align.py
"""Alignment plane: typed plane diffs — Task 5.

This is the payoff of the four-plane design: pure set-differences across
Contract / Closure / Usage / Environment, expressed as one typed
``Alignment`` result. No I/O, no Docker, no LLM -- ``align`` takes a fully
materialized ``PackageLayer`` and only computes membership diffs over it.

``under_declared`` is the honest completeness needle, and it is
CONTEXT-SCOPED: only ``ImportContext.RUNTIME`` imports are ever eligible.
Optional (guarded), typing-only, test-only, and dynamic-literal imports never
contribute -- a repo can have plenty of those without ever blocking a real
run, so flagging them would just be crying wolf. A RUNTIME import is
under-declared only when NONE of the following cover it: an existing
``ProvidedEdge`` (certified), its own canon name being a declared/closure
dist, or its curated alias (``import_mapping.map_import_to_package``, e.g.
``cv2`` -> ``opencv-python``) resolving to a declared/closure dist.

Built SEPARATE from ``python_deps.depgraph`` (per plan) so the two designs
can be A/B-evaluated; this module does not modify anything under
``depgraph/``.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from python_deps.import_mapping import is_unresolved, map_import_to_package
from python_deps.pkg_layer.planes import ImportContext, PackageLayer, _canon

# Contexts whose usage of an import name counts as a real, RUNTIME-adjacent
# consumer for `installed_unused` purposes -- guarded/typing/dynamic uses do
# not "spend" a provided dist.
_CONSUMING_CONTEXTS = frozenset({ImportContext.RUNTIME, ImportContext.TEST})


@dataclass(frozen=True)
class Alignment:
    under_declared: tuple[str, ...]
    resolution_anomaly: tuple[str, ...]
    transitive_only: tuple[str, ...]
    installed_unused: tuple[str, ...]


def align(layer: PackageLayer) -> Alignment:
    closure_canons = frozenset(_canon(pkg.name) for pkg in layer.closure)
    declared_or_closure = layer.declared_names() | closure_canons

    return Alignment(
        under_declared=_under_declared(layer, declared_or_closure),
        resolution_anomaly=_resolution_anomaly(layer, closure_canons),
        transitive_only=_transitive_only(layer),
        installed_unused=_installed_unused(layer),
    )


def _under_declared(
    layer: PackageLayer, declared_or_closure: frozenset[str]
) -> tuple[str, ...]:
    provided = layer.provided_imports()
    flagged: set[str] = set()
    for node in layer.runtime_imports():
        canon = _canon(node.name)
        if canon in provided:
            continue  # a ProvidedEdge already certifies this import
        if canon in declared_or_closure:
            continue  # the import's own name is already a declared/closure dist
        mapping = map_import_to_package(node.name)
        if not is_unresolved(mapping) and _canon(mapping.package_name) in declared_or_closure:
            continue  # curated alias (e.g. cv2 -> opencv-python) covers it
        flagged.add(canon)
    return tuple(sorted(flagged))


def _resolution_anomaly(
    layer: PackageLayer, closure_canons: frozenset[str]
) -> tuple[str, ...]:
    return tuple(sorted(layer.declared_names() - closure_canons))


def _transitive_only(layer: PackageLayer) -> tuple[str, ...]:
    return tuple(sorted({_canon(pkg.name) for pkg in layer.transitive_packages()}))


def _installed_unused(layer: PackageLayer) -> tuple[str, ...]:
    consuming = frozenset(
        _canon(node.name) for node in layer.imports if node.context in _CONSUMING_CONTEXTS
    )
    dist_import_canons: dict[str, set[str]] = defaultdict(set)
    for edge in layer.provided:
        dist_import_canons[_canon(edge.dist)].add(_canon(edge.import_name))
    return tuple(
        sorted(
            dist
            for dist, import_canons in dist_import_canons.items()
            if import_canons.isdisjoint(consuming)
        )
    )
