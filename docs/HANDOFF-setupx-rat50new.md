# Handoff — SetupX rat50new data and pooled results (2026-09-26)

## Pre-fix evaluator — the numbers in the paper's Table 1

SetupX pooled over 200 repos: **EBSR 52.5%, ESSR 47.1%** (matches Table 1).

| Group | Run (all under `runs/setupx/`) | Rows | EBSR | ESSR |
|---|---|---|---|---|
| rat50 | `ratbench50_run-20260904-173735` | 50 | 36.0 | 36.1 |
| rat50new | `setupx-rat50new-xpu-on-c3-20260916-20260916-142151` | 50 | 58.0 | 53.1 |
| envbench100 | `envbench100-merged-20260915` | 100 | 58.0 | 49.8 |

**Where the rat50new rows live**
- **Source of truth (VM):** `ssh -p 443 root@128.140.63.122`,
  `/opt/ratbench-runner/runs/setupx/setupx-rat50new-xpu-on-c3-20260916-20260916-142151/measure/`
  (50 rows + `metrics.json`, `errors.json`). The sibling `measure.prebashfix-20260919/` (14 rows) is
  an older, superseded pass — do not use it.
- **Local copy:** the same `measure/` was copied (JSON only) to
  `runs/setupx/setupx-rat50new-xpu-on-c3-20260916-20260916-142151/measure/` on 2026-09-26. Before
  that, the local run had only `measure.prebashfix-archive/`, which is why local scoring showed
  SetupX over 150 repos.

**Reproduce**
```bash
cd bench && python -m bench.unified_bench --agent-dir ../runs/setupx \
  --gold ../datasets/gold_manifests_combined.json
```

## Fixed-evaluator re-measure (NOT in the paper)

Re-measure of 2026-09-26 with the collection fix (repo `testpaths` respected, pristine pytest config
restored, `PYTEST_ADDOPTS`/`PYTEST_PLUGINS` cleared). Evaluator changes are uncommitted in
`bench/bench/languages/python.py`, `bench/bench/measure.py`, `bench/bench/metrics.py`,
`bench/bench/schema.py`, `bench/bench/unified_bench.py` (+ tests). Do not mix these rows with
pre-fix rows in one table.

- **Merged local result:** `runs_results/setupx/` (copies of the three runs with re-measured rows
  overlaid). SetupX rat50new here has 44/50 rows; the 6 missing repos (headroom, devopness, go-api,
  dl_techniques, dataflow, deleterr) have no Dockerfile and score 0.
- **Score it:** gold `datasets/gold_manifests_combined.testpaths.json` (gold node IDs trimmed to
  each repo's declared `testpaths`; the original gold file is unchanged).
- **Raw rows on the VMs** (`out/setupx__setupx-rat50new-xpu-on-c3-20260916-20260916-142151/`):

| VM | Directories |
|---|---|
| 49.13.125.88 | `/opt/remeasure_b/out/`, `/opt/remeasure_r/out/` |
| 2.28.97.71 | `/opt/remeasure_b/out/`, `/opt/remeasure_r/out/` |
| 2.28.101.185 | `/opt/remeasure_b/out/`, `/opt/remeasure_r3/out/` |
| 167.233.64.96 | `/opt/remeasure_b/out/`, `/opt/remeasure_r2/out/` |
| 128.140.63.122 (`ssh -p 443`) | `/opt/remeasure_b/out/` |

  Retry directories (`remeasure_r*`) supersede `remeasure_b` for the same repo.
