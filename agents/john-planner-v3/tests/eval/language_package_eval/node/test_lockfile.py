"""Parser tests for the Node package-fidelity eval — the os/cpu/libc extraction
the platform filter depends on, verified against the REAL esbuild@0.21.5 +
rollup@4.18.0 lockfile (design §7)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.eval.language_package_eval.node.lockfile import parse_lockfile, parse_lockfile_graph

_FIX = Path(__file__).parent / "fixtures" / "esbuild_rollup"
_LOCK = _FIX / "package-lock.json"


def _by_name(pkgs):
    return {p.name: p for p in pkgs}


def test_parses_all_non_root_entries():
    pkgs = parse_lockfile(_LOCK)
    # §7: 43 non-root entries (the root "" is excluded)
    assert len(pkgs) == 43


def test_plain_dep_has_no_platform_constraint():
    p = _by_name(parse_lockfile(_LOCK))
    assert p["esbuild"].version == "0.21.5"
    assert p["esbuild"].os == () and p["esbuild"].cpu == () and p["esbuild"].libc == ()


def test_dev_flag_extracted():
    p = _by_name(parse_lockfile(_LOCK))
    assert p["rollup"].dev is True          # rollup is a devDependency
    assert p["esbuild"].dev is False        # esbuild is a prod dependency


def test_os_cpu_extracted_for_platform_binary():
    p = _by_name(parse_lockfile(_LOCK))
    e = p["@esbuild/linux-x64"]
    assert e.os == ("linux",) and e.cpu == ("x64",)
    assert e.optional is True


def test_libc_axis_distinguishes_gnu_from_musl():
    p = _by_name(parse_lockfile(_LOCK))
    assert p["@rollup/rollup-linux-x64-gnu"].libc == ("glibc",)
    assert p["@rollup/rollup-linux-x64-musl"].libc == ("musl",)


def test_scoped_name_keeps_scope_prefix():
    names = {p.name for p in parse_lockfile(_LOCK)}
    assert "@esbuild/linux-x64" in names       # @scope preserved, not path-mangled
    assert "@types/estree" in names


def test_edges_and_root_deps_parsed():
    pkgs, root = parse_lockfile_graph(_LOCK)
    p = _by_name(pkgs)
    # root seeds = deps + devDeps (npm-ci dev-inclusive)
    assert "esbuild" in root and "rollup" in root
    # esbuild -> its platform binaries via optionalDependencies
    assert "@esbuild/linux-x64" in p["esbuild"].opt_deps
    assert p["esbuild"].deps == ()
    # rollup -> @types/estree via (non-optional) dependencies + platform bins via optional
    assert "@types/estree" in p["rollup"].deps
    assert "@rollup/rollup-linux-x64-gnu" in p["rollup"].opt_deps
    # a leaf binary has no outgoing edges
    assert p["@esbuild/linux-x64"].deps == () and p["@esbuild/linux-x64"].opt_deps == ()


def test_missing_packages_map_raises(tmp_path):
    bad = tmp_path / "package-lock.json"
    bad.write_text(json.dumps({"lockfileVersion": 1, "dependencies": {}}))
    with pytest.raises(ValueError, match="packages"):
        parse_lockfile(bad)
