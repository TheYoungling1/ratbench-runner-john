# src/python_deps/pkg_layer/planes.py
"""Plane data model for the four-plane Python package-requirement layer
(Contract / Closure / Usage / Environment) — Task 1.

Pure data structures: frozen dataclasses + enums, plus a small set of pure
query helpers on ``PackageLayer``. No I/O, no Docker, no LLM, no logic beyond
those queries. Built SEPARATE from ``python_deps.depgraph`` so the two layers
can be A/B-evaluated; do not import from ``depgraph`` here.
"""
from __future__ import annotations

import enum
import re
from dataclasses import dataclass


class ImportContext(enum.Enum):
    RUNTIME = "runtime"
    TEST = "test"
    OPTIONAL = "optional"
    TYPING = "typing"
    DYNAMIC = "dynamic"


class Tier(enum.Enum):
    DIRECT = "direct"
    TRANSITIVE = "transitive"


class Trust(enum.Enum):
    DECLARED = "declared"
    CLOSURE = "closure"
    CERTIFIED = "certified"
    CANDIDATE = "candidate"


def _canon(name: str) -> str:
    """PEP 503 canonicalization: case/separator-insensitive package-name key."""
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


@dataclass(frozen=True)
class DeclaredDep:
    name: str
    kind: str
    specifier: str | None
    marker: str | None
    group: str | None  # optional-dependency extra name, or None


@dataclass(frozen=True)
class ClosurePkg:
    name: str
    version: str | None
    tier: Tier


@dataclass(frozen=True)
class ImportNode:
    name: str
    context: ImportContext
    provenance: tuple[str, ...]


@dataclass(frozen=True)
class ProvidedEdge:
    import_name: str
    dist: str
    origin: str  # e.g. "certified"


@dataclass(frozen=True)
class CandidateEdge:
    import_name: str
    dist: str
    trust: Trust  # always Trust.CANDIDATE here
    verify_required: bool


@dataclass(frozen=True)
class PackageLayer:
    contract: tuple[DeclaredDep, ...]
    closure: tuple[ClosurePkg, ...]
    imports: tuple[ImportNode, ...]
    provided: tuple[ProvidedEdge, ...]

    def runtime_imports(self) -> tuple[ImportNode, ...]:
        return tuple(n for n in self.imports if n.context is ImportContext.RUNTIME)

    def direct_packages(self) -> tuple[ClosurePkg, ...]:
        return tuple(p for p in self.closure if p.tier is Tier.DIRECT)

    def transitive_packages(self) -> tuple[ClosurePkg, ...]:
        return tuple(p for p in self.closure if p.tier is Tier.TRANSITIVE)

    def declared_names(self) -> frozenset[str]:
        return frozenset(_canon(d.name) for d in self.contract)

    def provided_imports(self) -> frozenset[str]:
        return frozenset(_canon(e.import_name) for e in self.provided)
