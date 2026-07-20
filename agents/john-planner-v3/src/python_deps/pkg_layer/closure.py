# src/python_deps/pkg_layer/closure.py
"""Closure plane: resolved packages -> DIRECT/TRANSITIVE ``ClosurePkg``s — Task 4.

``build_closure`` is pure and knows nothing about ``uv`` or Docker: it maps
whatever rows a ``ResolveSource`` hands back into ``ClosurePkg`` nodes,
canon-deduped. The resolve SOURCE itself is the pluggable seam -- swapping
"fresh uv lock" for "adopt a committed lock" (or a canned fixture in tests)
is just injecting a different ``ResolveSource``, never touching
``build_closure``.

Built SEPARATE from ``python_deps.depgraph`` (per plan) so the two designs
can be A/B-evaluated; this module does not modify anything under
``depgraph/``. ``UvResolveSource`` is the one exception that reaches into
``depgraph.resolve.resolve_closure`` (the real, tested uv-lock orchestrator)
purely as an ADAPTER -- it is the ONLY place in this module that shells out
to a live ``uv`` binary, and it is deliberately NOT exercised by the unit
tests in ``tests/pkg_layer/test_closure.py`` (those drive a fake
``ResolveSource`` instead).
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from python_deps.depgraph.executor import Executor
from python_deps.depgraph.resolve import resolve_closure
from python_deps.depgraph.schema import NodeType
from python_deps.depgraph.target_env import TargetEnv
from python_deps.pkg_layer.planes import ClosurePkg, Tier, _canon

# ``resolve_closure`` returns real resolved Package nodes with this
# provenance (see resolve_lock._package_node's default / resolve._pip_
# compile_fallback's fallback default) alongside DIAGNOSTIC placeholder
# Package nodes for roots it had to drop (provenance "uv lock (unresolved)"
# / "uv lock (conflict)", see resolve_errors._missing_package_node /
# _conflict_package_node). Only the former are real closure members.
_RESOLVED_PROVENANCE = frozenset({"uv.lock", "uv pip compile"})


@runtime_checkable
class ResolveSource(Protocol):
    """A pluggable source of ``(name, version, is_direct)`` resolve rows."""

    def resolve(
        self, roots: tuple[str, ...]
    ) -> tuple[tuple[str, str | None, bool], ...]: ...


def build_closure(
    roots: tuple[str, ...], source: ResolveSource
) -> tuple[ClosurePkg, ...]:
    """Resolve ``roots`` via ``source`` into canon-deduped ``ClosurePkg``s.

    Each row is ``(name, version, is_direct)``; ``is_direct`` is taken from
    the source AS-IS (this function does not recompute directness against
    ``roots`` itself -- that is the source's job, e.g. ``UvResolveSource``
    below derives it from canon membership in ``roots``). A row's tier is
    ``Tier.DIRECT`` when ``is_direct`` else ``Tier.TRANSITIVE``.

    Dedup key is the PEP 503 canon name; on a duplicate the FIRST-seen row
    wins (matching ``pkg_layer.contract.select_roots``'s dedup convention) --
    a later duplicate can neither change the kept row's version nor upgrade
    its tier.
    """
    seen: set[str] = set()
    packages: list[ClosurePkg] = []
    for name, version, is_direct in source.resolve(roots):
        canon = _canon(name)
        if canon in seen:
            continue
        seen.add(canon)
        packages.append(
            ClosurePkg(
                name=name,
                version=version,
                tier=Tier.DIRECT if is_direct else Tier.TRANSITIVE,
            )
        )
    return tuple(packages)


class UvResolveSource:
    """Adapts ``depgraph.resolve.resolve_closure`` (real ``uv.lock`` resolve)
    to the ``ResolveSource`` protocol.

    NOT exercised by unit tests: calling ``.resolve`` shells out to the host
    ``uv`` binary through the injected ``Executor`` (``resolve_closure`` runs
    ``uv lock`` in a throwaway project dir). Construct with the same
    ``host_executor`` / ``target_env`` (and any other ``resolve_closure``
    keyword args, e.g. ``exclude_newer``/``project_dir``/``extras``) the
    caller would otherwise pass directly to ``resolve_closure``.

    FIDELITY CAVEAT (untested here, flagged for the report): ``resolve_
    closure`` returns ``(nodes, edges)`` where ``nodes`` can mix real
    resolved ``Package`` nodes with DIAGNOSTIC placeholder ``Package`` nodes
    for roots it had to drop after a failed lock (missing/conflict,
    ``state=State.MISSING`` or provenance ``"uv lock (unresolved)"`` /
    ``"uv lock (conflict)"``). This adapter filters to nodes whose
    provenance is exactly ``"uv.lock"`` or ``"uv pip compile"`` (the two
    provenance values a REAL resolved package carries) to exclude those
    placeholders -- that filter is inferred by reading resolve.py/
    resolve_lock.py/resolve_errors.py, not verified against a live uv run,
    since exercising it would require Docker + a real uv binary.
    """

    def __init__(
        self,
        host_executor: Executor,
        target_env: TargetEnv,
        **resolve_kwargs: object,
    ) -> None:
        self._host_executor = host_executor
        self._target_env = target_env
        self._resolve_kwargs = resolve_kwargs

    def resolve(
        self, roots: tuple[str, ...]
    ) -> tuple[tuple[str, str | None, bool], ...]:
        # resolve_closure's own `roots` shape is `list[tuple[import_id |
        # None, dist_name]]`; a plain name-only root here has no Import node
        # to attach, so it maps to `(None, name)` (the same shape roots.py
        # uses for a manifest-declared root with no import site).
        resolve_roots = [(None, name) for name in roots]
        nodes, _edges = resolve_closure(
            resolve_roots,
            self._host_executor,
            target_env=self._target_env,
            **self._resolve_kwargs,
        )
        root_canons = {_canon(name) for name in roots}
        return tuple(
            (node.name, node.version, _canon(node.name) in root_canons)
            for node in nodes
            if node.type is NodeType.PACKAGE
            and node.provenance in _RESOLVED_PROVENANCE
        )
