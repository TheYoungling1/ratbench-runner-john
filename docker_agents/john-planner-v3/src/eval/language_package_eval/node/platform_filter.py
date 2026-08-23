"""The os/cpu/libc platform filter — the Node analogue of Python's PEP 508
environment-marker evaluation, and the one piece of genuinely new logic in the
Node package-fidelity eval (design §4/§6).

npm ``os``/``cpu``/``libc`` are allow-lists with ``'!'``-negation; an empty list
means "all platforms". ``npm ci`` installs a platform-optional package iff all
three axes match the target triple. Filtering OURS (the whole lockfile) by the
target reproduces ``npm ci``'s selection exactly (verified 43->5, precision
0.116->1.0), turning the platform-optional over-inclusion into a measured fix.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from .lockfile import LockPackage

# Target triple matching ``node:20-slim`` on linux/amd64 (Debian glibc).
DEFAULT_TARGET: dict[str, str] = {"os": "linux", "cpu": "x64", "libc": "glibc"}


def _match(field: tuple[str, ...], target: str) -> bool:
    """npm allow-list semantics: empty = all; '!'-prefixed entries deny; a
    non-empty allow-list requires membership (only-negations => allow)."""
    if not field:
        return True
    allow = [x for x in field if not x.startswith("!")]
    deny = [x[1:] for x in field if x.startswith("!")]
    if target in deny:
        return False
    return not allow or target in allow


def keep(pkg: LockPackage, target: Mapping[str, str]) -> bool:
    """True iff ``pkg`` would be installed on ``target`` (all three axes match)."""
    return (
        _match(pkg.os, target["os"])
        and _match(pkg.cpu, target["cpu"])
        and _match(pkg.libc, target["libc"])
    )


def filter_for_target(
    packages: Iterable[LockPackage], target: Mapping[str, str] = DEFAULT_TARGET
) -> list[LockPackage]:
    """Per-node filter: drop packages failing their OWN os/cpu/libc. A first-order
    approximation of ``npm ci`` — correct only when every platform-conditional package
    is a leaf with its own constraint. Prefer ``installed_closure`` (graph-accurate)."""
    return [p for p in packages if keep(p, target)]


def _resolve(keys: set[str], from_path: str, name: str) -> str | None:
    """npm node-resolution: from an install path, find the entry a ``require(name)``
    binds to — the nearest ``node_modules/<name>`` walking up ancestor scopes."""
    prefix = from_path
    while True:
        cand = f"{prefix}/node_modules/{name}" if prefix else f"node_modules/{name}"
        if cand in keys:
            return cand
        if not prefix:
            return None
        idx = prefix.rfind("/node_modules/")
        prefix = prefix[:idx] if idx != -1 else ""


def installed_closure(
    packages: Iterable[LockPackage],
    root_dep_names: Iterable[str],
    target: Mapping[str, str] = DEFAULT_TARGET,
) -> list[LockPackage]:
    """The set ``npm ci`` installs on ``target`` = graph REACHABILITY from the root's
    deps, following dependencies/optionalDependencies/peerDependencies but ENTERING a
    package only when it passes its own os/cpu/libc check. Pruning a platform-mismatched
    optional drops its whole orphaned subtree — so an unconstrained package reachable
    ONLY through a pruned optional is correctly omitted (the @emnapi / @napi-rs
    wasm-runtime case that the per-node filter wrongly keeps). Subsumes ``filter_for_target``.
    """
    by_path = {p.path: p for p in packages}
    keys = set(by_path)
    visited: set[str] = set()
    stack: list[str] = []

    def enter(from_path: str, name: str) -> None:
        t = _resolve(keys, from_path, name)
        if t is not None and t not in visited and keep(by_path[t], target):
            visited.add(t)
            stack.append(t)

    for name in root_dep_names:
        enter("", name)
    while stack:
        p = by_path[stack.pop()]
        for name in (*p.deps, *p.opt_deps, *p.peer_deps):
            enter(p.path, name)
    return [by_path[path] for path in sorted(visited)]
