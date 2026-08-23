from __future__ import annotations

import os
import shutil

import pytest

from src.eval.language_package_eval.go.oracle import oracle_closure, parse_go_list_json


def test_parse_go_list_json_skips_main_and_handles_replace():
    # `go list -m -json all` emits a STREAM of JSON objects (not an array). `Main:true`
    # is the workspace's main module(s) -> skipped; a `Replace` keys the ORIGINAL Path
    # with the replacement Version (build-list identity). Robust vs pseudo-versions.
    out = (
        '{"Path":"github.com/acme/app","Main":true}\n'
        '{"Path":"github.com/spf13/cobra","Version":"v1.8.0"}\n'
        '{"Path":"github.com/old/mod","Version":"v1.0.0",'
        '"Replace":{"Path":"github.com/new/mod","Version":"v1.2.0"}}\n'
    )
    assert parse_go_list_json(out) == {
        "github.com/spf13/cobra": "v1.8.0",
        "github.com/old/mod": "v1.2.0",
    }


def test_parse_go_list_json_ignores_versionless_main_in_workspace():
    # a second Main:true (workspace member) with no Version must not appear.
    out = (
        '{"Path":"example.com/a","Main":true}\n'
        '{"Path":"example.com/b","Main":true}\n'
        '{"Path":"github.com/x/y","Version":"v1.0.0"}\n'
    )
    assert parse_go_list_json(out) == {"github.com/x/y": "v1.0.0"}


@pytest.mark.skipif(
    os.environ.get("GO_ORACLE_DOCKER") != "1" or shutil.which("docker") is None,
    reason="integration oracle: set GO_ORACLE_DOCKER=1 and have Docker + network",
)
def test_oracle_buildlist_anchor_is_superset_of_ours():
    # Corrected expectation (spec §0.1): OURS (require-block) is a SUBSET of the
    # build list, NOT equal to it. So assert NO extras and recall in (0, 1] — never
    # assert Delta=0. Requires the anchor manifest lifted by Task 7.
    from src.eval.language_package_eval.go.compare_go import score_repo
    from src.eval.language_package_eval.go.run_ours_go import SMOKE, ours_for_repo

    anchor = SMOKE / "viper"
    if not (anchor / "go.mod").is_file():
        pytest.skip("anchor corpus not lifted (run Task 7)")
    ours = ours_for_repo(anchor)
    oracle = {"installed": oracle_closure(anchor)}
    s = score_repo(ours, oracle)
    assert s["extra"] == []  # OURS ⊆ build list (pruning)
    assert 0.0 < s["recall_buildlist"] <= 1.0  # measured gap, not asserted == 1
