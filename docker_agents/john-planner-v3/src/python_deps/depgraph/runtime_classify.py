"""Runtime-feedback classifier (design 2026-06-26 §5, §6, §10).

Pure module — no src.envstate imports. Unit-testable with plain strings.

``classify_observation(command, output) -> Discovery | None``

Tries four sub-classifiers in priority order (spec §6) and returns the first
non-None hit.  Returns None for ignored observations (build-time install
failures, assertion errors, and anything not requirement-bearing).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from python_deps.depgraph.schema import Layer, NodeType
from python_deps.failure_classifier import classify_dependency_failure
from python_deps.import_mapping import is_unresolved, map_import_to_package


# ---------------------------------------------------------------------------
# Data structures (spec §10)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Observation:
    command: str
    output: str   # combined stdout/stderr text from the ledger event


@dataclass(frozen=True)
class Discovery:
    node_type: NodeType           # PACKAGE | SYSTEM_LIB | TOOL | CONFIG | SERVICE
    name: str                     # dist / soname / tool / VAR / service-kind
    layer: Layer
    evidence: str                 # failure excerpt that revealed the requirement
    check_command: str | None     # None only for SERVICE (advisory)
    confidence: str = "runtime-deterministic"
    data: dict = field(default_factory=dict)
    requires_of: str | None = None   # owner node id this is a dependency OF (spec §7)


# ---------------------------------------------------------------------------
# Ignored failure_type values from classify_dependency_failure (spec §6).
#
# These are genuine build-time / unrelated DEP failures owned elsewhere.
# CRITICAL: "not_dependency_related" is deliberately NOT in this set.
# classify_dependency_failure returns "not_dependency_related" for ANY non-dep
# failure — including the very connection-refused / KeyError / command-not-found
# shapes the Service/Config/Tool classifiers handle. If we short-circuited on it,
# priorities 2/3/4 would never run and 3 of the 5 classes would be silently
# dropped. It therefore FALLS THROUGH to the service→config→tool chain; the
# dispatcher returns None only at the very end, when nothing matched.
# ---------------------------------------------------------------------------
_IGNORED_FAILURE_TYPES: frozenset[str] = frozenset({
    "no_matching_distribution",
    "dependency_conflict",
    "syntax_requires_newer_python",
})


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------

def classify_observation(command: str, output: str) -> Discovery | None:
    """Classify one (command, output) observation.  Returns Discovery or None.

    Priority order (spec §6):
      1. classify_dependency_failure  — Package (module/import) or SystemLib
      2. classify_service_error       — Service
      3. classify_config_error        — Config
      4. classify_tool_error          — Tool
    """
    text = output or ""

    # ── Priority 1: python import / native-lib failures ──────────────────
    dep = classify_dependency_failure(command, text)
    if dep.failure_type == "module_not_found":
        import_name = dep.import_name or ""
        result = map_import_to_package(import_name)
        pkg_name = None if is_unresolved(result) else result.package_name
        return Discovery(
            node_type=NodeType.PACKAGE,
            name=pkg_name,
            layer=Layer.PIP,
            evidence=dep.message[:500],
            check_command=f'python3 -c "import {import_name}"',
            data={"import_name": import_name},
        )
    if dep.failure_type == "import_name_error":
        import_name = dep.import_name or ""
        result = map_import_to_package(import_name)
        pkg_name = None if is_unresolved(result) else result.package_name
        return Discovery(
            node_type=NodeType.PACKAGE,
            name=pkg_name,
            layer=Layer.PIP,
            evidence=dep.message[:500],
            check_command=f'python3 -c "import {import_name}"',
            data={"import_name": import_name},
        )
    if dep.failure_type == "native_library_missing":
        soname = dep.details.get("library", "")
        return Discovery(
            node_type=NodeType.SYSTEM_LIB,
            name=soname,
            layer=Layer.SYSTEM,
            evidence=dep.message[:500],
            check_command=f"ldconfig -p | grep -q {soname}",
        )
    if dep.failure_type in _IGNORED_FAILURE_TYPES:
        return None

    # dep.failure_type == "not_dependency_related" is NOT ignored — it only means
    # the dep classifier did not match, so we FALL THROUGH to the service→config→
    # tool classifiers below. Returning None here would silently drop 3 of 5 classes.

    # ── Priority 2: service connection failures ───────────────────────────
    from python_deps.depgraph.service_scan import classify_service_error
    svc_kind = classify_service_error(text)
    if svc_kind is not None:
        return Discovery(
            node_type=NodeType.SERVICE,
            name=svc_kind,
            layer=Layer.SERVICES,
            evidence=text[:500],
            check_command=None,   # services are advisory; certify skip-guards them
        )

    # ── Priority 3: missing config / env-var ─────────────────────────────
    from python_deps.failure_classifier import classify_config_error
    var_name = classify_config_error(command, text)
    if var_name is not None:
        return Discovery(
            node_type=NodeType.CONFIG,
            name=var_name,
            layer=Layer.CONFIG,
            evidence=text[:500],
            check_command=f"printenv {var_name}",
        )

    # ── Priority 4: missing tool / executable ────────────────────────────
    from python_deps.failure_classifier import classify_tool_error
    tool_name = classify_tool_error(command, text)
    if tool_name is not None:
        return Discovery(
            node_type=NodeType.TOOL,
            name=tool_name,
            layer=Layer.TOOLCHAIN,
            evidence=text[:500],
            check_command=f"command -v {tool_name}",
        )

    return None
