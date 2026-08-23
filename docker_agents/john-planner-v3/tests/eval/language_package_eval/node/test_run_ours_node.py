"""OURS runner — end-to-end on the golden fixture repo: the filtered closure is
the 5-package npm-ci set, with the 38 platform-optionals emitted as a diagnostic."""
from __future__ import annotations

from pathlib import Path

from src.eval.language_package_eval.node.run_ours_node import ours_for_repo

_REPO = Path(__file__).parent / "fixtures" / "esbuild_rollup"


def test_filtered_closure_is_the_npm_ci_five():
    rec = ours_for_repo(_REPO)
    assert set(rec["packages"]) == {
        "esbuild", "rollup", "@types/estree",
        "@esbuild/linux-x64", "@rollup/rollup-linux-x64-gnu",
    }
    assert rec["packages"]["esbuild"] == "0.21.5"
    assert rec["packages"]["rollup"] == "4.18.0"
    assert rec["package_count"] == 5


def test_unfiltered_and_dropped_diagnostics():
    rec = ours_for_repo(_REPO)
    assert rec["unfiltered_count"] == 43
    assert rec["platform_dropped_count"] == 38          # 43 - 5
    # the avoided precision hit, as a number the report can cite
    assert set(rec["packages"]).isdisjoint(rec["platform_dropped"])


def test_target_override_changes_selection():
    # a darwin/arm64 target keeps the mac binaries and drops the linux ones
    rec = ours_for_repo(_REPO, {"os": "darwin", "cpu": "arm64", "libc": "glibc"})
    assert "@esbuild/darwin-arm64" in rec["packages"]
    assert "@esbuild/linux-x64" not in rec["packages"]
    assert "fsevents" in rec["packages"]                # darwin-only now applies


def test_constraints_emitted_for_platform_packages():
    rec = ours_for_repo(_REPO)
    # non-platform deps carry no constraint entry; platform binaries do
    assert "esbuild" not in rec["constraints"]
    assert rec["constraints"]["@rollup/rollup-linux-x64-musl"]["libc"] == ["musl"]
