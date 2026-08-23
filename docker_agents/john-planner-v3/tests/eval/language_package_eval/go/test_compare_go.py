from __future__ import annotations

from src.eval.language_package_eval.go.compare_go import score_repo


def _ours(packages, replace_local=None, resolve_required=False):
    return {
        "packages": packages,
        "replace_local": replace_local or [],
        "resolve_required": resolve_required,
    }


def _oracle(installed):
    return {"installed": installed}


def test_perfect_match_is_recall_precision_one():
    s = score_repo(
        _ours({"github.com/x/y": "v1.0.0", "github.com/a/b": "v2.0.0"}),
        _oracle({"github.com/x/y": "v1.0.0", "github.com/a/b": "v2.0.0"}),
    )
    assert s["recall_buildlist"] == 1.0 and s["precision"] == 1.0
    assert s["missing"] == [] and s["extra"] == []
    assert s["vexact"] == 2


def test_missing_and_extra_buckets():
    s = score_repo(
        _ours({"github.com/x/y": "v1.0.0", "github.com/only/ours": "v9"}),
        _oracle({"github.com/x/y": "v1.0.0", "github.com/only/oracle": "v3"}),
    )
    assert s["recall_buildlist"] == 0.5 and s["precision"] == 0.5
    assert s["missing"] == ["github.com/only/oracle"]
    assert s["extra"] == ["github.com/only/ours"]


def test_local_replace_removed_from_both_sides():
    # locally-replaced module is emitted by `go list -m all` but dropped by OURS;
    # it must be excluded from both denominators (spec §6).
    s = score_repo(
        _ours({"github.com/x/y": "v1.0.0"}, replace_local=["github.com/local/m"]),
        _oracle({"github.com/x/y": "v1.0.0", "github.com/local/m": "v0.0.0"}),
    )
    assert s["recall_buildlist"] == 1.0 and s["precision"] == 1.0
    assert s["replace_local"] == ["github.com/local/m"]


def test_resolve_required_relabels_whole_oracle():
    s = score_repo(
        _ours({}, resolve_required=True),
        _oracle({"github.com/x/y": "v1.0.0"}),
    )
    assert s["recall_buildlist"] is None and s["precision"] is None
    assert s["resolve_required"] is True
    assert s["missing"] == []  # NOT counted as recall misses
    assert s["resolve_required_missing"] == ["github.com/x/y"]


def test_empty_both_sides_no_zero_division():
    s = score_repo(_ours({}), _oracle({}))
    assert s["recall_buildlist"] == 1.0 and s["precision"] == 1.0
    assert s["recall_loadset"] is None  # no load-set oracle supplied


def test_loadset_splits_pruned_superset_from_recall_defect():
    # OURS misses TWO build-list modules. One (`needed`) provides a package the
    # main module loads -> a real recall DEFECT. The other (`sibling`) is a
    # dep-of-dep the main module never imports -> expected PRUNED SUPERSET.
    ours = _ours({"github.com/x/y": "v1.0.0"})
    build = _oracle(
        {
            "github.com/x/y": "v1.0.0",
            "github.com/needed": "v1.0.0",
            "github.com/sibling": "v1.0.0",
        }
    )
    load = _oracle({"github.com/x/y": "v1.0.0", "github.com/needed": "v1.0.0"})
    s = score_repo(ours, build, oracle_loadset=load)
    assert s["recall_buildlist"] == 1 / 3  # only x/y of 3 matched
    assert s["recall_loadset"] == 1 / 2  # x/y matched, needed missed
    assert s["recall_defect"] == ["github.com/needed"]
    assert s["pruned_superset"] == ["github.com/sibling"]


def test_no_loadset_leaves_split_none():
    s = score_repo(
        _ours({"github.com/x/y": "v1.0.0"}),
        _oracle({"github.com/x/y": "v1.0.0", "github.com/z/w": "v1.0.0"}),
    )
    assert s["recall_loadset"] is None
    assert s["pruned_superset"] is None and s["recall_defect"] is None
    assert s["missing"] == ["github.com/z/w"]  # undifferentiated without load-set
