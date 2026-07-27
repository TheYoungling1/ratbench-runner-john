# tests/bench/test_metrics_py50_reference.py
"""Regression pin against a COMPLETED 50-repo Python benchmark run.

`data/py50_rows.json` holds the 50 real row.json records from that run, distilled: the fields
compute_metrics reads are verbatim, `collected_node_ids` is kept as its LENGTH (the only thing
scored) and re-expanded on load, and the byte-heavy diagnostics that no metric reads
(build_log_tail, collect_errors, the passed/failed/error node-id lists) are dropped. The distilled
set was checked to produce an aggregate dict byte-identical to the one the untouched rows produce.

The rows are materialised back into the <agent>/<owner>/<repo>/row.json layout and read through
unified_bench.aggregate, so the real load path (MeasureRow construction, list->tuple coercion,
legacy-status defaulting) is exercised, not just compute_metrics.
"""
import json
import pathlib

from bench.unified_bench import aggregate

_DATA = pathlib.Path(__file__).resolve().parent / "data" / "py50_rows.json"
_AGENT = "claudecode-dockerfile"

# The completed run's headline numbers.
_REFERENCE = {"n": 45, "n_ebsr": 22, "EBSR": 0.4889, "n_exec": 35, "ESSR": 0.7368,
              "ESSR_all": 0.5731, "real_success": 0.5111, "full_pass_repos": 8}


def _rows() -> list:
    return json.loads(_DATA.read_text())


def _materialise(root: pathlib.Path) -> pathlib.Path:
    for r in _rows():
        d = dict(r)
        d["collected_node_ids"] = [""] * d["collected_node_ids"]
        p = root.joinpath(d["agent"], *d["repo"].split("/"), "row.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d))
    return root


def test_py50_row_set_is_intact():
    rows = _rows()
    assert len(rows) == 50 and all(r["agent"] == _AGENT for r in rows)


def test_py50_aggregate_is_unchanged(tmp_path):
    m = aggregate(str(_materialise(tmp_path)))[_AGENT]
    assert {k: m[k] for k in _REFERENCE} == _REFERENCE


def test_no_recorded_row_flips_executed_under_the_element_based_predicate(tmp_path):
    """The empty-<testsuites/> fix must be inert on this reference run.

    The old predicate credited execution when `total > 0` OR the raw XML merely CONTAINED the
    substring "testsuite". is_executed keeps the first clause verbatim, so any row that was
    credited through it is unaffected; only a row credited through the substring alone — i.e.
    executed=True with total == 0 — could flip. There are none.

    The reverse direction cannot flip either: a row recorded executed=False had no "testsuite"
    substring anywhere in its report, and every real JUnit document containing a <testcase>
    element wraps it in a <testsuite>/<testsuites> root, so the new element count is 0 there too.
    """
    credited_by_substring_alone = [r["repo"] for r in _rows()
                                   if r["executed"] and not (r["total"] or 0) > 0]
    assert credited_by_substring_alone == []
