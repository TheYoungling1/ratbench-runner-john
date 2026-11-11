# Provenance of `runs_results/`

Every `<arm>/<run>/measure/` folder is a complete, unmodified copy of the original measurement
output that produced the paper's tables. It holds one `row.json` per repo plus that run's
`metrics.json` and `errors.json`, copied on 2026-11-12 from the run archive (an APFS disk image,
`ratbench-archive`, path `runs/<variety>/<run>/measure/`). `SHA256SUMS` lists all 1,456 files.

## Check it

```sh
python3 tools/reproduce_paper_table.py
```

This verifies every file against `SHA256SUMS` and recomputes Table 1 of the submitted paper from
these rows and `datasets/gold_manifests_combined_test_steps.json` (sha256 `99f5d447…0c45ae`). It
exits non-zero on any mismatch. All 14 cells reproduce; SWE-agent's ESSR is 35.65 against the
printed 35.6. Each arm is averaged over its own rows: 200, except RAT with 193, whose seven
missing rat50new repos produced no measurement.

## Where each run came from

Measured dates are the original row timestamps. Git does not keep timestamps, so they are recorded
here.

| Arm | Run | Archive source | Rows measured |
|---|---|---|---|
| claudecode | `ratbench50_run-20260904-134239` | `runs/claudecode-dockerfile/…/measure` | 2026-09-05 |
| claudecode | `envbench100-merged-20260915` | `runs/claudecode-dockerfile/…/measure` | 2026-09-09 |
| claudecode | `rat50new-full-openrouter-20260918-035416` | `runs/claudecode-dockerfile/…/measure` | 2026-09-23 |
| graph2env | `rat_python50` | `runs/g2e-rat50/rat_python50/measure` | 2026-09-24 |
| graph2env | `rat_python50_new` | `runs/g2e-rat50new/rat_python50_new/measure` | 2026-09-24 |
| graph2env | `envbench_python100` | `runs/g2e-envbench100/envbench_python100/measure` | 2026-09-24 |
| sweagent | `ratbench50-modern-20260914-014042` | `runs/sweagent_repo2run_modern/…/measure` | 2026-09-14 |
| sweagent | `rat50new-modern-20260916-20260916-110539` | `runs/sweagent_repo2run_modern/…/measure` | 2026-09-19 to 09-23 |
| sweagent | `envbench100-merged-20260915` | `runs/sweagent_repo2run_modern/…/measure` | 2026-09-09 |
| setupx | `ratbench50_run-20260904-173735` | `runs/setupx/…/measure` | 2026-09-05 |
| setupx | `setupx-rat50new-xpu-on-c3-20260916-20260916-142151` | `runs/setupx/…/measure` | 2026-09-19 to 09-23 |
| setupx | `envbench100-merged-20260915` | `runs/setupx/…/measure` | 2026-09-09 |
| rat | `rat50-pjy`, `rat50new-pjy`, `envbench100-pjy` | `runs/pjy-rat-dockerfile/…/measure` | 2026-09-23 to 09-24 |
| repo2run | `rat50-pjy`, `rat50new-pjy`, `envbench100-pjy` | `runs/pjy-repo2run/…/measure` | 2026-09-23 |
| pipreqs | `rat50-20260915-043144`, `envbench100-20260915-055253` | `runs/pipreqs/…/measure` | 2026-09-15 |
| pipreqs | `rat50new-20260922-170025` | `runs/pipreqs/…/measure` | 2026-09-23 |

The evaluator changed during September (shell `bash -lc` → `bash -c` on 09-19, a build-context
fix on 09-22, a pytest bootstrap fix on 09-24), so runs measured on different dates used different
evaluator versions. The rows are exactly what the paper scored; they are not a single-version
re-measurement.

## What these replaced

Until 2026-11-12 these folders held a re-measurement made on 2026-09-26, after the paper's tables
were fixed, with a later evaluator (repo `testpaths` honoured, original pytest config restored,
`PYTEST_ADDOPTS` cleared). It rebuilt 28–105 repos per arm from the same Dockerfiles and was not
used in the paper. It is still in git history and in each arm's local `<arm>.zip`, under
`runs_rescore/`. The agent outputs and Dockerfiles in those zips are byte-identical to the
archive's.
