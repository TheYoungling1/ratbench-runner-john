"""pkg_layer.repair — trust-ordered repair ladder for ONE under-declared
RUNTIME import (Task 6).

Resolves a single import that has no declared provider to a distribution by
climbing a TRUST-ORDERED ladder: module_name identity -> curated alias table
-> PyPI-metadata probe -> LLM probe. Every candidate MUST be verified
(install + ``importlib.metadata.packages_distributions``-style proof) before
acceptance -- a candidate merely being *suggested* by a probe is never
sufficient.

Guardrail (from the DeepSeek provider-ambiguity experiment): install
verification proves IMPORTABILITY, not INTENT. It cannot tell ``psycopg2``
from ``psycopg2-binary`` -- both install and both satisfy ``import
psycopg2``. So when a rung's candidates verify to MORE THAN ONE distribution,
this module refuses to pick a variant: it stops and flags the import
ambiguous. Only a declared contract (outside this module's scope) may choose
between such variants. A rung with exactly one verified provider wins
immediately and the ladder stops; a rung with zero verified providers falls
through to the next rung.

Pure logic: all effects (PyPI probe, LLM, install-verify) are injected
Protocols, faked in tests. Built SEPARATE from ``python_deps.depgraph`` and
the rest of ``python_deps.pkg_layer`` (no changes to existing modules).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from python_deps.import_mapping import is_unresolved, map_import_to_package


class ProviderProbe(Protocol):
    """PyPI-metadata probe: candidate distributions for an import name."""

    def candidates(self, import_name: str) -> tuple[str, ...]:
        ...


class LlmProbe(Protocol):
    """LLM-suggested candidate distributions for an import name."""

    def candidates(self, import_name: str) -> tuple[str, ...]:
        ...


class Verifier(Protocol):
    """Install-verification: does ``dist`` actually provide ``import_name``?"""

    def provides(self, dist: str, import_name: str) -> bool:
        ...


@dataclass(frozen=True)
class RepairOutcome:
    import_name: str
    resolved_dist: str | None
    source: str


def _verified(
    candidates: tuple[str, ...], import_name: str, verifier: Verifier
) -> tuple[str, ...]:
    """The subset of ``candidates`` the verifier actually confirms, in order."""
    return tuple(dist for dist in candidates if verifier.provides(dist, import_name))


def _accept_or_flag(
    candidates: tuple[str, ...], import_name: str, verifier: Verifier, source: str
) -> RepairOutcome | None:
    """Apply the single-verified-provider rule to one rung's raw candidates.

    Returns an accept ``RepairOutcome`` when exactly one candidate verifies,
    a ``flagged_ambiguous`` outcome when more than one verifies, or ``None``
    (fall through to the next rung) when none verify.
    """
    verified = _verified(candidates, import_name, verifier)
    if len(verified) == 1:
        return RepairOutcome(import_name=import_name, resolved_dist=verified[0], source=source)
    if len(verified) > 1:
        return RepairOutcome(import_name=import_name, resolved_dist=None, source="flagged_ambiguous")
    return None


def repair_import(
    import_name: str, probe: ProviderProbe, llm: LlmProbe, verifier: Verifier
) -> RepairOutcome:
    """Climb the trust-ordered ladder for ``import_name``; see module docstring."""
    # Rung 1: module_name identity -- single candidate by construction.
    if verifier.provides(import_name, import_name):
        return RepairOutcome(import_name=import_name, resolved_dist=import_name, source="module_name")

    # Rung 2: curated alias table -- single candidate by construction.
    mapping = map_import_to_package(import_name)
    if not is_unresolved(mapping):
        curated_dist = mapping.package_name
        assert curated_dist is not None  # not is_unresolved() guarantees this
        if verifier.provides(curated_dist, import_name):
            return RepairOutcome(import_name=import_name, resolved_dist=curated_dist, source="curated")

    # Rung 3: PyPI-metadata probe -- may yield 0, 1, or >1 verified providers.
    outcome = _accept_or_flag(probe.candidates(import_name), import_name, verifier, "pypi")
    if outcome is not None:
        return outcome

    # Rung 4: LLM probe -- same single-verified-provider rule.
    outcome = _accept_or_flag(llm.candidates(import_name), import_name, verifier, "llm")
    if outcome is not None:
        return outcome

    # No rung produced a verified provider.
    return RepairOutcome(import_name=import_name, resolved_dist=None, source="flagged_missing")
