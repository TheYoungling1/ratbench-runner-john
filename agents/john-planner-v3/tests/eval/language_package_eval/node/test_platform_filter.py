"""The platform filter — the design's headline regression lock (§7): the real
esbuild+rollup lockfile has 43 entries; the {linux,x64,glibc} filter reproduces
`npm ci`'s selection EXACTLY (5), and libc is a REQUIRED third axis (the
gnu/musl split)."""
from __future__ import annotations

from pathlib import Path

from src.eval.language_package_eval.node.lockfile import (
    LockPackage,
    parse_lockfile,
    parse_lockfile_graph,
)
from src.eval.language_package_eval.node.platform_filter import (
    DEFAULT_TARGET,
    _match,
    filter_for_target,
    installed_closure,
    keep,
)

_LOCK = Path(__file__).parent / "fixtures" / "esbuild_rollup" / "package-lock.json"

# The 5 packages `npm ci` installs on node:20-slim linux/amd64 (design §7).
_EXPECTED_KEPT = {
    "esbuild",
    "rollup",
    "@types/estree",
    "@esbuild/linux-x64",
    "@rollup/rollup-linux-x64-gnu",
}


def test_golden_43_to_5_matches_npm_ci():
    pkgs = parse_lockfile(_LOCK)
    assert len(pkgs) == 43
    kept = filter_for_target(pkgs, DEFAULT_TARGET)
    assert {p.name for p in kept} == _EXPECTED_KEPT
    assert len(kept) == 5                        # 43 -> 5, precision 0.116 -> 1.0


def test_libc_is_a_required_third_axis():
    # gnu and musl differ ONLY in libc; os+cpu alone would keep both (6). The
    # glibc target must drop the musl variant.
    kept = {p.name for p in filter_for_target(parse_lockfile(_LOCK), DEFAULT_TARGET)}
    assert "@rollup/rollup-linux-x64-gnu" in kept
    assert "@rollup/rollup-linux-x64-musl" not in kept


def test_darwin_only_dropped_on_linux():
    kept = {p.name for p in filter_for_target(parse_lockfile(_LOCK), DEFAULT_TARGET)}
    assert "fsevents" not in kept                # os:["darwin"]


def test_match_semantics():
    assert _match((), "linux") is True                       # empty = all
    assert _match(("linux",), "linux") is True
    assert _match(("linux",), "darwin") is False
    assert _match(("!win32",), "linux") is True              # only-negation => allow
    assert _match(("!win32",), "win32") is False             # negation denies
    assert _match(("linux", "darwin"), "darwin") is True     # allow-list membership


def test_keep_requires_all_three_axes():
    musl = LockPackage("x", "1", "node_modules/x", os=("linux",), cpu=("x64",), libc=("musl",))
    gnu = LockPackage("y", "1", "node_modules/y", os=("linux",), cpu=("x64",), libc=("glibc",))
    assert keep(gnu, DEFAULT_TARGET) is True
    assert keep(musl, DEFAULT_TARGET) is False               # libc mismatch alone excludes


# ---- reachability closure (the root-cause fix: graph reachability, not per-node) ----

def test_closure_reproduces_golden_five():
    # the platform binaries are all leaves with their own constraint, so the
    # reachability closure == the per-node filter here: still exactly the npm-ci 5.
    pkgs, root = parse_lockfile_graph(_LOCK)
    kept = {p.name for p in installed_closure(pkgs, root, DEFAULT_TARGET)}
    assert kept == _EXPECTED_KEPT


def test_closure_drops_unconstrained_child_of_pruned_optional():
    # The real-corpus case the per-node filter gets WRONG: @emnapi/core has NO
    # os/cpu/libc, but its ONLY parent is a cpu:wasm32 optional binding that is
    # pruned on x64 — so npm ci omits @emnapi/core too. Reachability captures this;
    # the per-node filter wrongly keeps it.
    parent = LockPackage("parent", "1", "node_modules/parent",
                         opt_deps=("@rolldown/binding-wasm32-wasi",
                                   "@rolldown/binding-linux-x64-gnu"))
    wasm = LockPackage("@rolldown/binding-wasm32-wasi", "1",
                       "node_modules/@rolldown/binding-wasm32-wasi",
                       cpu=("wasm32",), optional=True, deps=("@emnapi/core",))
    native = LockPackage("@rolldown/binding-linux-x64-gnu", "1",
                         "node_modules/@rolldown/binding-linux-x64-gnu",
                         os=("linux",), cpu=("x64",), libc=("glibc",), optional=True)
    emnapi = LockPackage("@emnapi/core", "1", "node_modules/@emnapi/core")  # no constraint
    pkgs = [parent, wasm, native, emnapi]

    closure = {p.name for p in installed_closure(pkgs, ("parent",), DEFAULT_TARGET)}
    assert closure == {"parent", "@rolldown/binding-linux-x64-gnu"}
    assert "@emnapi/core" not in closure                     # <- the fix
    assert "@rolldown/binding-wasm32-wasi" not in closure    # cpu wasm32 pruned

    # the OLD per-node filter demonstrably gets this wrong:
    per_node = {p.name for p in filter_for_target(pkgs, DEFAULT_TARGET)}
    assert "@emnapi/core" in per_node                        # unconstrained -> wrongly kept


def test_closure_unreachable_package_omitted():
    # a package present in the lock but not reachable from root deps is not installed
    orphan = LockPackage("orphan", "1", "node_modules/orphan")
    used = LockPackage("used", "1", "node_modules/used")
    closure = {p.name for p in installed_closure([orphan, used], ("used",), DEFAULT_TARGET)}
    assert closure == {"used"}
