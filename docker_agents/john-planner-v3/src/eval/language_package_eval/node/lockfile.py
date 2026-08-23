"""Minimal ``package-lock.json`` (v2/v3) parser for the Node package-fidelity eval.

The lockfile's ``packages`` map keys install PATHS ("" = root,
"node_modules/<name>", "node_modules/a/node_modules/b" nested) to resolved
entries. Each entry carries ``version``, the platform allow-lists
(``os``/``cpu``/``libc``) that gate platform-optional binaries, and its
dependency EDGES (``dependencies`` / ``optionalDependencies`` /
``peerDependencies``). The edges are needed because ``npm ci``'s install set is
graph reachability, not a per-package filter (an unconstrained package is still
omitted when its only parent is a platform-pruned optional). No resolve/install/
network — pure JSON.

Workspaces / monorepos: ``npm ci`` installs the union of the root's deps AND
every workspace MEMBER's deps, and every member is itself installed. The lock
encodes a member three ways: a ``packages/<path>`` entry holding the member's
own deps (and often OMITTING ``name`` when the dir basename already equals the
registry name), plus a ``node_modules/<name>`` entry with ``"link": true`` whose
``resolved`` points back at ``packages/<path>`` — the SYMLINK that is where
``require(name)`` actually binds. The link key is the authoritative registry
name. We fold each member into a synthetic ``node_modules/<name>`` node carrying
the member entry's deps, so the ordinary reachability closure sees npm's true
graph: member-name edges (member A -> member B) resolve through the symlink, and
every member is seeded as a root. Non-workspace locks have no link entries, so
this path is inert and their parse is byte-for-byte unchanged.

This is eval-local; when the Node EcosystemProvider lands, its
``ecosystems/node/lockfile.py`` becomes the source of truth and callers switch.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class LockPackage:
    """One resolved lockfile entry (a non-root ``packages`` node)."""

    name: str                              # registry name, incl. @scope (path-derived)
    version: str
    path: str                              # the lockfile key (install path)
    os: tuple[str, ...] = field(default_factory=tuple)      # npm allow-list, '!'-negation
    cpu: tuple[str, ...] = field(default_factory=tuple)
    libc: tuple[str, ...] = field(default_factory=tuple)
    dev: bool = False
    optional: bool = False
    deps: tuple[str, ...] = field(default_factory=tuple)         # dependencies (always-follow)
    opt_deps: tuple[str, ...] = field(default_factory=tuple)     # optionalDependencies
    peer_deps: tuple[str, ...] = field(default_factory=tuple)    # peerDependencies


def _name_from_path(key: str) -> str:
    """Registry name = the segment after the LAST 'node_modules/' (handles nesting
    and @scope): 'node_modules/@esbuild/linux-x64' -> '@esbuild/linux-x64'."""
    return key.rsplit("node_modules/", 1)[-1]


def _as_tuple(v) -> tuple[str, ...]:
    if not v:
        return ()
    return tuple(v) if isinstance(v, (list, tuple)) else (v,)


def _edge_names(entry: dict, field_name: str) -> tuple[str, ...]:
    """Dependency NAMES declared under ``field_name`` (values are version ranges,
    which reachability does not need — resolution is by install path)."""
    return tuple((entry.get(field_name) or {}).keys())


def _mk(key: str, entry: dict, *, name: str | None = None, path: str | None = None) -> LockPackage:
    """Build a ``LockPackage`` from a lock entry. ``name``/``path`` overrides let a
    workspace member be folded to its install location (``node_modules/<name>``)
    while its edges/version still come from the ``packages/<path>`` entry."""
    return LockPackage(
        name=name or entry.get("name") or _name_from_path(key),
        version=str(entry.get("version")),
        path=path or key,
        os=_as_tuple(entry.get("os")),
        cpu=_as_tuple(entry.get("cpu")),
        libc=_as_tuple(entry.get("libc")),
        dev=bool(entry.get("dev", False)),
        optional=bool(entry.get("optional", False)),
        deps=_edge_names(entry, "dependencies"),
        opt_deps=_edge_names(entry, "optionalDependencies"),
        peer_deps=_edge_names(entry, "peerDependencies"),
    )


def _root_edges(entry: dict) -> tuple[str, ...]:
    """``npm ci`` is dev-inclusive: a project/member seeds the closure with its
    dependencies + devDependencies + optionalDependencies + peerDependencies."""
    return (
        _edge_names(entry, "dependencies")
        + _edge_names(entry, "devDependencies")
        + _edge_names(entry, "optionalDependencies")
        + _edge_names(entry, "peerDependencies")
    )


def parse_lockfile_graph(lock_path: str | Path) -> tuple[list[LockPackage], tuple[str, ...]]:
    """Parse a v2/v3 lockfile into (non-root ``LockPackage`` list, SEED names). The
    seeds are what ``npm ci`` installs unconditionally: the root's dev-inclusive deps
    PLUS — in a workspace — every member's name (members are always installed) and,
    implicitly via each member node's edges, every member's deps. Raises ``ValueError``
    if there is no ``packages`` map.

    Workspace members are folded to synthetic ``node_modules/<name>`` nodes (name taken
    from the authoritative ``link`` entry, edges from the ``packages/<path>`` entry) so
    the reachability closure resolves member-name edges through the symlink. A lock with
    no link entries (a plain single-package project) is parsed exactly as before.
    """
    data = json.loads(Path(lock_path).read_text())
    packages = data.get("packages")
    if packages is None:
        raise ValueError(
            f"{lock_path}: no 'packages' map — needs package-lock.json v2/v3 "
            "(v1/yarn/pnpm out of scope)"
        )
    out: list[LockPackage] = []
    root_names: tuple[str, ...] = ()
    member_entries: dict[str, dict] = {}   # packages/<path> -> entry (workspace member)
    link_name: dict[str, str] = {}         # member path -> installed registry name
    for key, entry in packages.items():
        if not isinstance(entry, dict):
            continue
        if key == "":
            root_names = _root_edges(entry)
            continue
        if entry.get("link"):
            target = entry.get("resolved")   # node_modules/<name> -> packages/<path>
            if target:
                link_name[target] = _name_from_path(key)
            continue
        if "node_modules/" not in key:
            member_entries[key] = entry      # workspace member at a real fs path
            continue
        if entry.get("version") is None:
            continue  # linkless / metadata-only entry
        out.append(_mk(key, entry))

    # Fold each workspace member into its install location. The member is installed as
    # node_modules/<name> (a symlink); require(name) binds there, so that is the path the
    # resolver must find. Seed every member name — members are always installed.
    member_seeds: list[str] = []
    for mpath, entry in member_entries.items():
        name = link_name.get(mpath) or entry.get("name") or _name_from_path(mpath)
        out.append(_mk(mpath, entry, name=name, path=f"node_modules/{name}"))
        member_seeds.append(name)

    return out, root_names + tuple(member_seeds)


def parse_lockfile(lock_path: str | Path) -> list[LockPackage]:
    """Non-root ``LockPackage`` entries (with edges). Thin wrapper over
    ``parse_lockfile_graph`` for callers that don't need the root seeds."""
    return parse_lockfile_graph(lock_path)[0]
