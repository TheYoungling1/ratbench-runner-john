"""Diagnosis router (design 2026-07-01 diagnosis-first loop).

Pure module — no src.envstate imports. Unit-testable with plain strings.

Wraps the pure runtime classifier with repository context so the loop can
DISTINGUISH an environment requirement from a repo-internal reference, a
residual bug, or a disproven attempt — and so a repo-local import is never
mis-added as a PyPI package (the design's single highest-value guard).
"""
from __future__ import annotations

import enum
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from python_deps.depgraph.runtime_classify import Discovery, classify_observation
from python_deps.depgraph.schema import NodeType
from python_deps.failure_classifier import classify_dependency_failure


class Mode(enum.Enum):
    ENVIRONMENT = "environment"                    # real env requirement -> ingest + repair
    REPO_INTERNAL_REF = "repo_internal_reference"  # local import/path -> out of scope
    RESIDUAL = "residual"                          # assertion/logic bug -> non-env give-up
    INVALID_ATTEMPT = "invalid_attempt"            # pip disproved this name -> do not retry
    AMBIGUOUS = "ambiguous"                        # unclear -> probe then reclassify


@dataclass(frozen=True)
class RepoContext:
    local_names: frozenset[str] = field(default_factory=frozenset)
    # PEP-503-normalized (see ``_norm``) disproven package names — callers must
    # normalize before constructing (``diagnose`` compares both sides via
    # ``_norm`` so ``Frobnicate_9000`` and ``frobnicate-9000`` are the same
    # entry). Kept separate from ``repair_loop.known_invalid``, which is a
    # heterogeneous key space of raw failed commands + node/block ids —
    # mixing normalized package names into it would corrupt equality lookups.
    invalid_names: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Diagnosis:
    mode: Mode
    discovery: Discovery | None   # populated only when mode is ENVIRONMENT
    reason: str


def is_local_import(import_name: str, local_names: frozenset[str]) -> bool:
    """True when ``import_name`` (or its top-level package) is defined in the repo.

    ``local_names`` are basenames from ``scan.local_module_names`` (dirs with
    ``__init__.py`` and top-level ``*.py`` stems), so compare the first dotted
    segment: ``docs_src.helpers`` is local iff ``docs_src`` is local.
    """
    if not import_name:
        return False
    return import_name.split(".", 1)[0] in local_names


def _norm(name: str) -> str:
    """PEP-503-ish normalization so ``Frobnicate_9000`` == ``frobnicate-9000``."""
    return (name or "").strip().lower().replace("_", "-")


def _previously_disproven(disc, import_name: str, invalid_names: frozenset[str]) -> bool:
    """True when this failing import was already pip-disproven and must not be retried.

    Matches the resolved distribution name when the runtime classifier produced one
    AND the raw import name. The strict runtime classifier deliberately withholds a
    package name for an unmapped import (``name=None``, "never guessed as itself"),
    but the disproven distribution normalizes straight from the import that triggered
    it (``frobnicate_9000`` -> ``frobnicate-9000``), so retry-suppression must not
    depend on a package name the classifier refuses to guess.
    """
    if disc is not None and disc.name is not None and _norm(disc.name) in invalid_names:
        return True
    return bool(import_name) and _norm(import_name) in invalid_names


# An assertion / logic failure is a residual (non-environment) bug: the graph
# cannot close it by adding a node. Conservative — anything else stays AMBIGUOUS.
_RESIDUAL_RE = re.compile(r"\bAssertionError\b")


def diagnose(command: str, output: str, ctx: RepoContext) -> Diagnosis:
    """Classify one (command, output) failure into a routing Mode.

    Only ``Mode.ENVIRONMENT`` carries a ``Discovery`` (produced by the existing
    ``classify_observation``, which owns import->package mapping). Every other
    mode carries ``discovery=None`` and a human-readable ``reason``.
    """
    text = output or ""
    dep = classify_dependency_failure(command, text)

    # pip already proved this distribution does not exist -> never retry the name.
    if dep.failure_type == "no_matching_distribution":
        name = dep.package_name or ""
        return Diagnosis(Mode.INVALID_ATTEMPT, None,
                         f"pip found no matching distribution for {name!r}")

    # ModuleNotFoundError is strong, unambiguous evidence: the exact top-level
    # name failed to import. Repo-local (out of scope), previously disproven
    # (invalid), or a genuine external package requirement.
    if dep.failure_type == "module_not_found":
        import_name = dep.import_name or ""
        if is_local_import(import_name, ctx.local_names):
            return Diagnosis(Mode.REPO_INTERNAL_REF, None,
                             f"{import_name!r} resolves to a repo-local module")
        disc = classify_observation(command, text)
        if disc is None:
            return Diagnosis(Mode.AMBIGUOUS, None,
                             f"import {import_name!r} had no package mapping")
        if _previously_disproven(disc, import_name, ctx.invalid_names):
            return Diagnosis(Mode.INVALID_ATTEMPT, None,
                             f"import {import_name!r} was previously disproven")
        return Diagnosis(Mode.ENVIRONMENT, disc,
                         f"external import {import_name!r} -> package requirement")

    # ImportError "cannot import name X from Y" is weaker evidence than
    # ModuleNotFoundError: Y may already be installed-but-outdated, mid a
    # circular import, or Y may be a dotted submodule reference rather than a
    # distribution name. classify_observation handles this byte-identically to
    # module_not_found (guesses a package from Y's top-level segment even when
    # Y is a nested submodule) — be conservative here: only trust that guess
    # when the failed "from" target is *itself* the bare top-level name
    # (no dotting occurred), i.e. classify_observation's resolved import_name
    # equals the literal "from" target. Otherwise stay AMBIGUOUS rather than
    # mint a bogus package node the traceback doesn't actually support.
    if dep.failure_type == "import_name_error":
        import_name = dep.import_name or ""
        if is_local_import(import_name, ctx.local_names):
            return Diagnosis(Mode.REPO_INTERNAL_REF, None,
                             f"{import_name!r} resolves to a repo-local module")
        failed_from = dep.details.get("module_name", import_name)
        disc = classify_observation(command, text)
        if disc is None or disc.data.get("import_name") != failed_from:
            return Diagnosis(Mode.AMBIGUOUS, None,
                             f"import_name_error for {failed_from!r} has no confirmed "
                             "top-level package mapping — probe before repair")
        if _previously_disproven(disc, import_name, ctx.invalid_names):
            return Diagnosis(Mode.INVALID_ATTEMPT, None,
                             f"import {import_name!r} was previously disproven")
        return Diagnosis(Mode.ENVIRONMENT, disc,
                         f"external import {failed_from!r} -> package requirement")

    # Native lib / service / config / tool: reuse the classifier verbatim.
    disc = classify_observation(command, text)
    if disc is not None:
        # RESIDUAL takes priority over the WEAK config/tool tiers. A co-reported
        # AssertionError means the test's own logic failed; a missing env-var or
        # missing executable scraped from the same traceback is almost always
        # incidental to that residual (e.g. click's pager tests: an incidental
        # `less`/`PAGER` token alongside a stable `test_echo_via_pager`
        # AssertionError). Installing it cannot make the assertion pass — minting
        # the obligation only starts an unfixable repair churn. The STRONG,
        # anchored tiers (SystemLib soname, Service) still win: they are matched
        # from specific failure signatures, not arbitrary tokens.
        if disc.node_type in (NodeType.TOOL, NodeType.CONFIG) and _RESIDUAL_RE.search(text):
            return Diagnosis(Mode.RESIDUAL, None,
                             f"{disc.node_type.value.lower()} signal co-occurs with an "
                             "AssertionError — treating as residual, not environment")
        return Diagnosis(Mode.ENVIRONMENT, disc,
                         f"{disc.node_type.value.lower()} requirement")

    # Nothing environment-shaped matched. Distinguish residual from ambiguous.
    if _RESIDUAL_RE.search(text):
        return Diagnosis(Mode.RESIDUAL, None, "assertion failure — non-environment residual")
    return Diagnosis(Mode.AMBIGUOUS, None, "unclassified failure — probe before repair")


def diagnose_all(
    observations: tuple[tuple[str, str], ...], ctx: RepoContext
) -> tuple[Diagnosis, ...]:
    """Batch form of :func:`diagnose` (Phase 6 orchestrator routing seam).

    Pure; reads ``mode``/``reason`` without re-running classification per call
    site. ``make_diagnostic_classifier`` remains the ingest-time seam.
    """
    return tuple(diagnose(cmd, out, ctx) for cmd, out in observations)


def make_diagnostic_classifier(ctx: RepoContext) -> Callable[[str, str], Discovery | None]:
    """Adapt :func:`diagnose` to the ``ingest_runtime_failures`` classifiers seam.

    Returns a Discovery only for ``Mode.ENVIRONMENT``; every other mode
    (repo-internal-reference, residual, invalid-attempt, ambiguous) returns
    ``None`` so no node is appended. The router's mode/reason are consumed by
    the orchestrator in Phase 2; Phase 1 uses only the ENVIRONMENT/else split.
    """
    def _classify(command: str, output: str) -> Discovery | None:
        return diagnose(command, output, ctx).discovery
    return _classify
