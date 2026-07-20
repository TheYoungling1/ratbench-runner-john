"""TDD tests for the pkg_layer Alignment plane (Task 5).

``align`` computes typed set-differences across the four planes on a
hand-built ``PackageLayer`` -- no I/O, no Docker. The headline property is
CONTEXT SCOPING on ``under_declared``: only ``ImportContext.RUNTIME`` imports
can ever appear there, so optional/typing/test/dynamic imports never cry
wolf about a completeness gap that doesn't actually block a real run.
"""
from __future__ import annotations

from python_deps.pkg_layer.align import Alignment, align
from python_deps.pkg_layer.planes import (
    ClosurePkg,
    DeclaredDep,
    ImportContext,
    ImportNode,
    PackageLayer,
    ProvidedEdge,
    Tier,
)


def _layer(
    *,
    contract: tuple[DeclaredDep, ...] = (),
    closure: tuple[ClosurePkg, ...] = (),
    imports: tuple[ImportNode, ...] = (),
    provided: tuple[ProvidedEdge, ...] = (),
) -> PackageLayer:
    return PackageLayer(contract=contract, closure=closure, imports=imports, provided=provided)


def _import(name: str, context: ImportContext) -> ImportNode:
    return ImportNode(name=name, context=context, provenance=("app.py",))


# --------------------------------------------------------------------------- #
# under_declared: context scoping is the headline property
# --------------------------------------------------------------------------- #

def test_runtime_import_no_provider_no_dist_is_under_declared():
    layer = _layer(imports=(_import("requests", ImportContext.RUNTIME),))

    result = align(layer)

    assert result.under_declared == ("requests",)


def test_optional_import_not_under_declared():
    # SAME import name as the RUNTIME case above, but tagged OPTIONAL
    # (guarded `try/except ImportError`) -- must NEVER flag.
    layer = _layer(imports=(_import("requests", ImportContext.OPTIONAL),))

    result = align(layer)

    assert result.under_declared == ()


def test_typing_import_not_under_declared():
    # SAME import name again, tagged TYPING (TYPE_CHECKING-guarded) -- also
    # must never flag. Context scoping proven on both suppressing contexts.
    layer = _layer(imports=(_import("requests", ImportContext.TYPING),))

    result = align(layer)

    assert result.under_declared == ()


def test_test_import_not_under_declared():
    layer = _layer(imports=(_import("requests", ImportContext.TEST),))

    result = align(layer)

    assert result.under_declared == ()


def test_dynamic_import_not_under_declared():
    layer = _layer(imports=(_import("requests", ImportContext.DYNAMIC),))

    result = align(layer)

    assert result.under_declared == ()


def test_runtime_import_with_provided_edge_not_under_declared():
    layer = _layer(
        imports=(_import("requests", ImportContext.RUNTIME),),
        provided=(ProvidedEdge(import_name="requests", dist="requests", origin="certified"),),
    )

    result = align(layer)

    assert result.under_declared == ()


def test_runtime_import_matching_declared_name_not_under_declared():
    # No ProvidedEdge yet, but the import's own canon name is already a
    # declared dist -- self-evidently covered, not under-declared.
    layer = _layer(
        contract=(DeclaredDep(name="requests", kind="dependency", specifier=None,
                               marker=None, group=None),),
        imports=(_import("requests", ImportContext.RUNTIME),),
    )

    result = align(layer)

    assert result.under_declared == ()


def test_runtime_import_matching_closure_name_not_under_declared():
    # Declared elsewhere (e.g. transitively resolved) so it shows up in the
    # closure even without its own DeclaredDep row.
    layer = _layer(
        closure=(ClosurePkg(name="requests", version="2.31.0", tier=Tier.TRANSITIVE),),
        imports=(_import("requests", ImportContext.RUNTIME),),
    )

    result = align(layer)

    assert result.under_declared == ()


def test_runtime_import_curated_mapping_resolved_in_closure_not_under_declared():
    # cv2 -> opencv-python is a CURATED_IMPORT_TO_PACKAGE entry; when the
    # curated dist is present in the closure, the import is genuinely
    # covered even though "cv2" itself is never declared and has no
    # ProvidedEdge yet.
    layer = _layer(
        closure=(ClosurePkg(name="opencv-python", version="4.9.0", tier=Tier.DIRECT),),
        imports=(_import("cv2", ImportContext.RUNTIME),),
    )

    result = align(layer)

    assert result.under_declared == ()


def test_runtime_import_curated_mapping_resolved_but_dist_absent_still_under_declared():
    # cv2 curated-maps to opencv-python, but opencv-python is NOT declared or
    # in the closure here -- the curated mapping existing is not itself
    # enough; the mapped dist must actually be present.
    layer = _layer(imports=(_import("cv2", ImportContext.RUNTIME),))

    result = align(layer)

    # under_declared reports the CANON IMPORT NAME (not the resolved dist).
    assert result.under_declared == ("cv2",)


def test_under_declared_canon_names_sorted_and_deduped():
    layer = _layer(
        imports=(
            _import("Zeta-Lib", ImportContext.RUNTIME),
            _import("alpha_lib", ImportContext.RUNTIME),
        ),
    )

    result = align(layer)

    assert result.under_declared == ("alpha-lib", "zeta-lib")


# --------------------------------------------------------------------------- #
# resolution_anomaly: declared but absent from the resolved closure
# --------------------------------------------------------------------------- #

def test_declared_dep_absent_from_closure_is_resolution_anomaly():
    layer = _layer(
        contract=(DeclaredDep(name="ghost-pkg", kind="dependency", specifier=None,
                               marker=None, group=None),),
    )

    result = align(layer)

    assert result.resolution_anomaly == ("ghost-pkg",)


def test_declared_dep_present_in_closure_not_resolution_anomaly():
    layer = _layer(
        contract=(DeclaredDep(name="requests", kind="dependency", specifier=None,
                               marker=None, group=None),),
        closure=(ClosurePkg(name="requests", version="2.31.0", tier=Tier.DIRECT),),
    )

    result = align(layer)

    assert result.resolution_anomaly == ()


def test_resolution_anomaly_canon_matches_case_and_separator_insensitive():
    layer = _layer(
        contract=(DeclaredDep(name="Typing-Extensions", kind="dependency", specifier=None,
                               marker=None, group=None),),
        closure=(ClosurePkg(name="typing_extensions", version="4.10.0", tier=Tier.DIRECT),),
    )

    result = align(layer)

    assert result.resolution_anomaly == ()


# --------------------------------------------------------------------------- #
# transitive_only: closure packages with tier == TRANSITIVE
# --------------------------------------------------------------------------- #

def test_transitive_closure_pkg_is_transitive_only():
    layer = _layer(
        closure=(ClosurePkg(name="urllib3", version="2.0.0", tier=Tier.TRANSITIVE),),
    )

    result = align(layer)

    assert result.transitive_only == ("urllib3",)


def test_direct_closure_pkg_not_transitive_only():
    layer = _layer(
        closure=(ClosurePkg(name="requests", version="2.31.0", tier=Tier.DIRECT),),
    )

    result = align(layer)

    assert result.transitive_only == ()


def test_mixed_direct_and_transitive_closure_only_transitive_reported():
    layer = _layer(
        closure=(
            ClosurePkg(name="requests", version="2.31.0", tier=Tier.DIRECT),
            ClosurePkg(name="urllib3", version="2.0.0", tier=Tier.TRANSITIVE),
            ClosurePkg(name="certifi", version="2024.2.2", tier=Tier.TRANSITIVE),
        ),
    )

    result = align(layer)

    assert result.transitive_only == ("certifi", "urllib3")


# --------------------------------------------------------------------------- #
# installed_unused: provided dists with no consuming RUNTIME/TEST import
# --------------------------------------------------------------------------- #

def test_provided_dist_with_no_importing_context_is_installed_unused():
    layer = _layer(
        provided=(ProvidedEdge(import_name="unused_lib", dist="unused-dist", origin="certified"),),
    )

    result = align(layer)

    assert result.installed_unused == ("unused-dist",)


def test_provided_dist_with_runtime_import_not_installed_unused():
    layer = _layer(
        imports=(_import("used_lib", ImportContext.RUNTIME),),
        provided=(ProvidedEdge(import_name="used_lib", dist="used-dist", origin="certified"),),
    )

    result = align(layer)

    assert result.installed_unused == ()


def test_provided_dist_with_test_import_not_installed_unused():
    # TEST counts as "used" too -- only truly unreferenced dists are flagged.
    layer = _layer(
        imports=(_import("used_lib", ImportContext.TEST),),
        provided=(ProvidedEdge(import_name="used_lib", dist="used-dist", origin="certified"),),
    )

    result = align(layer)

    assert result.installed_unused == ()


def test_provided_dist_with_only_optional_import_is_still_installed_unused():
    # An OPTIONAL (or TYPING/DYNAMIC) reference does not count as a real
    # RUNTIME/TEST consumer -- the dist is still reported unused.
    layer = _layer(
        imports=(_import("guarded_lib", ImportContext.OPTIONAL),),
        provided=(ProvidedEdge(import_name="guarded_lib", dist="guarded-dist", origin="certified"),),
    )

    result = align(layer)

    assert result.installed_unused == ("guarded-dist",)


def test_installed_unused_dist_used_via_any_matching_edge_not_flagged():
    # Same dist provided under two import names; only one is actually
    # consumed at runtime -- the dist as a whole must NOT be flagged unused.
    layer = _layer(
        imports=(_import("used_alias", ImportContext.RUNTIME),),
        provided=(
            ProvidedEdge(import_name="used_alias", dist="shared-dist", origin="certified"),
            ProvidedEdge(import_name="unused_alias", dist="shared-dist", origin="certified"),
        ),
    )

    result = align(layer)

    assert result.installed_unused == ()


# --------------------------------------------------------------------------- #
# Structural properties
# --------------------------------------------------------------------------- #

def test_empty_layer_returns_empty_alignment():
    result = align(_layer())

    assert result == Alignment(
        under_declared=(),
        resolution_anomaly=(),
        transitive_only=(),
        installed_unused=(),
    )


def test_result_is_alignment_instance_with_tuple_fields():
    result = align(_layer())

    assert isinstance(result, Alignment)
    assert isinstance(result.under_declared, tuple)
    assert isinstance(result.resolution_anomaly, tuple)
    assert isinstance(result.transitive_only, tuple)
    assert isinstance(result.installed_unused, tuple)


def test_alignment_is_frozen():
    result = align(_layer())

    try:
        result.under_declared = ("mutated",)  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        raise AssertionError("Alignment must be frozen (immutable)")
