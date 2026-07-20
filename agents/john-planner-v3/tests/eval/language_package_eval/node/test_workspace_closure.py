"""Workspace / monorepo closure — the root-cause lock for the npm-workspaces
defect: seeding only from the root ``""`` entry drops every workspace member and
its deps (root deps is EMPTY in a workspace; members live in package.json's
``workspaces``, not the lock's dep edges). ``npm ci`` installs the union of the
root's + every member's deps, and every member is itself installed (as a
``node_modules/<name>`` symlink whose ``link`` entry is the authoritative name).

Each fixture's EXPECTED set is the REAL ``npm ci`` install tree, captured by a
``node_modules/<name>`` boundary walk inside ``node:20-slim --platform
linux/amd64`` (Debian glibc) — the closure must reproduce it exactly.
"""
from __future__ import annotations

from pathlib import Path

from src.eval.language_package_eval.node.lockfile import parse_lockfile_graph
from src.eval.language_package_eval.node.platform_filter import (
    DEFAULT_TARGET,
    installed_closure,
)
from src.eval.language_package_eval.node.run_ours_node import ours_for_repo

_FIX = Path(__file__).parent / "fixtures"

# --- docker-verified `npm ci` install sets (node:20-slim, linux/amd64) ---------
# workspace_scoped: @scope/app depends on member @scope/core (workspace->workspace)
_SCOPED = {"@scope/app", "@scope/core", "is-number", "left-pad"}
# workspace_nested: packages/group/* members; member entries OMIT `name` (so the
# link entry is the only name source); sub2 depends on member sub1 (w->w).
_NESTED = {"ansi-styles", "color-convert", "color-name", "debug", "ms", "sub1", "sub2"}
# workspace_native: a member depends on esbuild -> 23 platform-optional @esbuild/*
# binaries collapse to the ONE for linux/x64/glibc, threaded through the member.
_NATIVE = {
    "@esbuild/linux-x64", "ansi-styles", "chalk", "color-convert", "color-name",
    "deep-member", "esbuild", "has-flag", "ms", "supports-color", "util-member",
}


def _closure_names(fixture: str) -> set[str]:
    pkgs, seeds = parse_lockfile_graph(_FIX / fixture / "package-lock.json")
    return {p.name for p in installed_closure(pkgs, seeds, DEFAULT_TARGET)}


def test_scoped_workspace_closure_equals_npm_ci():
    assert _closure_names("workspace_scoped") == _SCOPED


def test_nested_workspace_closure_equals_npm_ci():
    assert _closure_names("workspace_nested") == _NESTED


def test_native_workspace_closure_equals_npm_ci():
    # the headline repro: npm installs 11, and the closure now matches (was 0).
    assert _closure_names("workspace_native") == _NATIVE


def test_root_deps_are_empty_but_members_are_seeded():
    # the defect's origin: the root "" entry declares NO deps in a workspace, so
    # root-only seeding reaches nothing. Members must be seeded on their own.
    pkgs, seeds = parse_lockfile_graph(_FIX / "workspace_scoped" / "package-lock.json")
    assert "@scope/app" in seeds and "@scope/core" in seeds     # members ARE seeds


def test_workspace_to_workspace_edge_is_followed():
    # @scope/app -> @scope/core is a member->member edge that resolves through the
    # node_modules/@scope/core symlink; both members appear in the closure.
    names = _closure_names("workspace_scoped")
    assert {"@scope/app", "@scope/core"} <= names


def test_name_omitted_member_named_from_link_entry():
    # packages/group/sub1|sub2 carry NO `name` field; the folded node must take its
    # name from the node_modules/<name> link entry (not the dir path).
    pkgs, _ = parse_lockfile_graph(_FIX / "workspace_nested" / "package-lock.json")
    by_path = {p.path: p for p in pkgs}
    assert by_path["node_modules/sub1"].name == "sub1"          # not "packages/group/sub1"
    assert by_path["node_modules/sub2"].name == "sub2"


def test_ours_for_repo_recall_is_one_on_native_workspace():
    # end-to-end through the runner: the filtered closure == the 11-package npm-ci set.
    rec = ours_for_repo(_FIX / "workspace_native")
    assert set(rec["packages"]) == _NATIVE
    assert rec["package_count"] == 11


def test_members_not_mislabeled_platform_dropped():
    # a workspace member (or a member's dep) is never platform-dropped; only the
    # genuine other-arch @esbuild/* binaries are (their own os/cpu/libc rejects x64).
    rec = ours_for_repo(_FIX / "workspace_native")
    dropped = set(rec["platform_dropped"])
    assert "deep-member" not in dropped and "util-member" not in dropped
    assert "chalk" not in dropped and "esbuild" not in dropped
    # the dropped set is exactly the non-linux-x64 esbuild binaries (22 of 23).
    assert dropped and all(d.startswith("@esbuild/") for d in dropped)
    assert "@esbuild/linux-x64" not in dropped                 # the kept one
