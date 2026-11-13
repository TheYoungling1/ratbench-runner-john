# Failing-test error labels

Every failing test in every built environment, labelled by error type and failure category, for
Graph2Env, Claude Code, Repo2Run and RAT over the 200 benchmark repositories. SetupX, SWE-agent
and pipreqs are absent: their failure messages were never saved.

## Files

`<arm>_repos.csv`: one row per repository (RAT has 193; 7 produced no measurement).

| Column | Meaning |
|---|---|
| `status`, `built_ebsr` | measured status from the paper row in `runs_results/<arm>/<run>/measure/`; `built_ebsr` = counted in EBSR |
| `gold_tests`, `passed`, `failing_tests` | gold test count (`datasets/gold_manifests_combined_test_steps.json`), passed tests, failed + errored tests |
| `labels` | `labelled`, or why not: `n/a: not built`, `n/a: no failing tests`, `unavailable: ...`, `excluded: ...` |
| `label_source` | where the failure messages came from (see below) |
| `matched` | failing tests matched to a message |
| category columns | failing tests per category (paper classifier) |
| `OperationalError`, `RuntimeError` | failing tests with that exception type |

`<arm>_failing_tests.csv`: one row per failing test in a `labelled` repository: `test_id`,
`error_type` (exception class), `category`, and the first 300 characters of the failure message.

## How the labels were made

- Failing tests are the paper row's `failed_node_ids` + `error_node_ids`: tests that ran and
  failed in an environment that built, i.e. tests lost from ESSR.
- Each is matched by test ID to a JUnit report:
  - **method's own test run**: Graph2Env rat50/envbench and all Repo2Run/RAT repositories with
    one, from each system's `test_pass_rate_evaluation` output;
  - **rerun**: the saved Dockerfile rebuilt in November 2026 with the paper-era evaluator
    (`d7d7fb3` for Claude Code; a Sep 24 reconstruction for Graph2Env rat50new and the 13
    Repo2Run/RAT gaps), via `tools/paper_eval_measure.py`. A rerun is used only when its failing
    tests contain every failing test of the paper row.
- A repository is `excluded` when more than 5% of its failing tests have no matching message.
- `error_type` and `category` come from `error_type()` and `error_category()` in
  `Graph2Env/scripts/analysis/essr_breakdown.py`, the classifier behind the paper's Figure 3.
  Categories: Local service unavailable, Missing configuration or data, External network access,
  Missing package/tool/library, Incompatible package version, Test assertion, Other.
