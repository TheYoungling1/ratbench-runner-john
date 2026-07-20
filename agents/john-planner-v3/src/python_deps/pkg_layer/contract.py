# src/python_deps/pkg_layer/contract.py
"""Contract plane + contract-only root selection — Task 3.

The "verifier" root selection: roots come from the declared CONTRACT ONLY.
Imports are never consulted, which is what makes it structurally impossible
to reproduce the ``depgraph.roots``/``package_roots`` bug where an optional
dependency excluded from ``needed_extras`` gets silently re-added because the
code happens to import it (manifest-first, scan-gap-fill design). There is no
import input here at all -- only ``(contract, needed_extras)`` in.

Built SEPARATE from ``python_deps.depgraph`` (per plan) so the two designs can
be A/B-evaluated; this module does not modify anything under ``depgraph/``.
"""
from __future__ import annotations

from python_deps.depgraph.roots import _requirement_group
from python_deps.evidence import collect_python_dependency_evidence
from python_deps.pkg_layer.planes import DeclaredDep, _canon


def read_contract(repo_path: str) -> tuple[DeclaredDep, ...]:
    """Declared dependencies from every manifest source, as ``DeclaredDep``s.

    Delegates evidence collection to
    :func:`python_deps.evidence.collect_python_dependency_evidence` (parses
    pyproject.toml / setup.cfg / setup.py / requirements*.txt) and maps each
    resulting ``PythonRequirement`` 1:1. The extras GROUP for a
    ``kind=="optional_dependency"`` requirement is derived from its
    ``.source`` string using the same ``_requirement_group`` helper
    ``depgraph.roots.select_roots`` uses, so the two designs agree on what an
    "extra" is; runtime (non-optional) deps always get ``group=None``.
    """
    evidence = collect_python_dependency_evidence(repo_path)
    return tuple(
        DeclaredDep(
            name=requirement.name,
            kind=requirement.kind,
            specifier=requirement.specifier or None,
            marker=requirement.marker or None,
            group=(
                # _requirement_group returns "" on no match; DeclaredDep.group
                # is the real extra name or None, so map falsy -> None.
                (_requirement_group(requirement.source) or None)
                if requirement.kind == "optional_dependency"
                else None
            ),
        )
        for requirement in evidence.declared_dependencies
    )


def in_scope_deps(
    contract: tuple[DeclaredDep, ...], needed_extras: frozenset[str]
) -> tuple[DeclaredDep, ...]:
    """The ROW-LEVEL in-scope subset of ``contract`` -- the single source of
    truth for what "in scope" means for a given ``needed_extras``.

    Returns exactly: every ``kind=="dependency"`` row, plus every
    ``kind=="optional_dependency"`` row whose ``group`` is a member of
    ``needed_extras``. Nothing else -- in particular, no import/usage
    evidence is consulted here either.

    ``select_roots`` derives its canon-deduped name list from this same set.
    Callers that need the full ``DeclaredDep`` rows for the in-scope subset
    (e.g. ``pkg_layer.construct.build_package_layer``) must use this function
    directly rather than re-deriving scope via name membership against
    ``select_roots``'s output: a distribution declared BOTH as a base
    ``kind=="dependency"`` AND inside an EXCLUDED optional group shares its
    name with an in-scope row, so a name-membership filter would incorrectly
    let the excluded optional-group row back in. Row identity, not name
    identity, is what "in scope" means.
    """
    return tuple(
        dep
        for dep in contract
        if dep.kind == "dependency"
        or (dep.kind == "optional_dependency" and dep.group in needed_extras)
    )


def select_roots(
    contract: tuple[DeclaredDep, ...], needed_extras: frozenset[str]
) -> tuple[str, ...]:
    """Canon-deduped distribution-name roots, from the CONTRACT ONLY.

    Every ``kind=="dependency"`` dep is included unconditionally. A
    ``kind=="optional_dependency"`` dep is included only when its ``group``
    is a member of ``needed_extras``. Nothing else -- in particular, no
    import/usage evidence is consulted, so an optional dep excluded from
    ``needed_extras`` cannot be re-added by the code happening to import it
    (the bug this design structurally rules out).

    Derived from :func:`in_scope_deps`, canon-deduped to names -- this
    function's observable behaviour is unchanged by that refactor.
    """
    seen: set[str] = set()
    roots: list[str] = []
    for dep in in_scope_deps(contract, needed_extras):
        canon = _canon(dep.name)
        if canon in seen:
            continue
        seen.add(canon)
        # Return the CANON (PEP 503) token, not the manifest's original
        # spelling: the A/B compares dist-name membership, and a canon token
        # is the stable, case/separator-insensitive identity.
        roots.append(canon)
    return tuple(roots)
