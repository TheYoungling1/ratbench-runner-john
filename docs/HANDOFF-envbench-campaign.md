# Handoff — envbench benchmark campaign

Written 2026-09-12. Read this before running or scoring anything.

---

> ## ⚠️ NOT ALL ROWS IN THE PAPER TABLE ARE SCORED THE SAME WAY
>
> **`Graph2Env`, `Repo2Run` and `RAT` are NOT yet computed the way every other row is.
> Reconcile this before submission.** As of 2026-09-15 they were supplied as per-dataset
> numbers and combined by hand; they have not been through `bench/bench/metrics.py`.
>
> | | scored by | ESSR denominator | cross-dataset | tokens |
> |---|---|---|---|---|
> | pipreqs, SetupX, SWE-agent, Claude Code | `metrics.py` via `unified_bench --agent-dir` | certified gold manifest | **pooled** | median per repo |
> | **Graph2Env, Repo2Run, RAT** | **supplied externally** | **unknown — reported as "MTPR"** | **weighted `(x+2y)/3`** | **mean per repo** |
>
> Three concrete mismatches, each of which changes a published number:
>
> 1. **"MTPR" is not ESSR.** It is a pass-rate metric with an unverified denominator — very
>    likely the agent's own pytest collection, i.e. the ÷exec family DELETED from `metrics.py`
>    for exactly this reason. Putting MTPR in an ESSR column would claim Graph2Env beats Claude
>    Code by 13 points using two metrics that do not measure the same thing. **Do not merge the
>    columns until these arms are scored against the gold manifests.**
> 2. **Weighted, not pooled.** Their unified figures use `(rat50×50 + env100×100)/150`; every
>    other row uses the pooled per-repo mean. The two differ by ~0.0006 on ESSR, so this one is
>    cosmetic — but it is a second convention in one table.
> 3. **Mean vs median tokens.** Theirs are means, the rest are medians. Means run high: RAT's
>    2,100K mean against Claude Code's 1,156K median is not a like-for-like comparison, and the
>    caption currently says "medians".
>
> The fix is one command per arm once their run dirs are on this machine:
> `python -m bench.unified_bench --agent-dir runs/<arm> --gold datasets/gold_manifests_combined.json`.
>
> Weighted figures as supplied, for reference only — EBSR / MTPR / turns / mean tokens:
> Graph2Env 89.3% / 65.22% / 27.80 / 890,183 · Repo2Run 52.7% / 33.10% / 38.81 / 1,167,801 ·
> RAT 44.7% / 36.15% / 61.13 / 2,100,294.

---

**Updated 2026-09-15 (later)** — **the metric definitions moved into the code.** `ESSR` is now
gold-anchored and nothing else: the ÷exec and ÷all variants were DELETED from
`bench/bench/metrics.py`, so there is exactly one number called ESSR in the pipeline. `EBSR` now
denies credit to untrusted repos (`pypa/distutils`). A fifth arm, `pipreqs`, was added as the
zero-LLM static floor and is the first arm ever run on `envbench_python100.json` directly. See
"Metric definitions — the code is the authority" and "Edge cases" below, both of which supersede
the earlier "Gold-anchored scoring" text.

**Updated 2026-09-15** — the gold-anchored scoring section below is revised: a larger, consolidated
gold manifest superseded the old gt100/human_manual_counts split (98/100 envbench repos scored now,
up from 92/100), the no-gold-repo policy was refined from a blanket exclude into three distinct
cases, EBSR was recomputed after finding this session had briefly re-made the exact `row['ebsr']`
mistake documented below, and a unified rat50+envbench100 metric was added.

## What this campaign is

Comparing environment-construction agents on two datasets, scored with EBSR (does the env build
and collect cleanly) and ESSR (what fraction of tests pass). Three arms have run; a fourth is
built but unrun.

| arm | variety | what it is |
|---|---|---|
| setupx | `setupx` | SetupX + XPU experience retrieval, replays `setup.history` |
| ccdf | `claudecode-dockerfile` | Claude Code CLI emitting a Dockerfile |
| modern | `sweagent_repo2run_modern` | SWE-agent 1.1.0's own harness (`function_calling`) |
| baseline | `sweagent_repo2run` | SWE-agent under the Repo2Run paper's scaffold (`thought_action`) |
| pipreqs | `pipreqs` | static import scanner, NO LLM — the zero-cost EBSR/ESSR floor |

`modern` vs `baseline` is a deliberate single-variable contrast: same model, temperature, budget,
timeouts and task prompt; only the scaffold differs (parser, edit tool, history processor).

## THE DATASET TRAP — read this first

`envbench_python70` and `envbench_python100` **do not nest**.

```
envbench_python70              70 repos
envbench_python100            100 repos   (shares 61 with the 70; 39 are new)
envbench_python100_minus70     39 repos   (exactly the 39 not in the 70)
70 ∪ 100                      109 repos   <-- NOT 100
```

So any metric "over envbench100" must filter to those 100 `full_name`s. An unfiltered mean over
the run dirs silently includes 9 extra repos and is not a number about envbench100 at all.

No arm has ever run on `envbench_python100.json` directly. Every envbench100 figure is **pooled**
from that arm's envbench70 run (58 of the scored repos) + its minus70 run (34). The two are
disjoint (`overlap = 0`, verified), per-repo rows are independent, so pooling is sound — but say
so when reporting.

## Completed runs

Local under `runs/` (gitignored). `input/` was excluded when pulling — it is regenerable clones.

| arm | dataset | run dir | EBSR | ESSR (÷exec) |
|---|---|---|---|---|
| setupx | envbench70 | `setupx/envbench70-xpu-on-20260908-133913` | 0.686 | 0.778 |
| setupx | minus70 | `setupx/minus70-xpu-on-20260909-042630` | 0.462 | 0.808 |
| ccdf | envbench70 | `claudecode-dockerfile/envbench70-20260908-203504` | 0.743 | 0.692 |
| ccdf | minus70 | `claudecode-dockerfile/minus70-20260909-083859` | 0.744 | 0.814 |
| modern | envbench70 | `sweagent_repo2run_modern/envbench70-modern-20260909-020413` | 0.400 | 0.864 |
| modern | minus70 | `sweagent_repo2run_modern/minus70-modern-20260909-103702` | 0.487 | 0.664 |
| setupx | rat50 | `setupx/run-20260904-173735` | 0.360 | 0.540 |
| ccdf | rat50 | `claudecode-dockerfile/run-20260904-134239` | 0.620 | 0.707 |
| baseline | rat50 | `sweagent_repo2run/run-20260903-161432` (measure only) | 0.180 | 0.642 |
| pipreqs | rat50 | `pipreqs/rat50-20260915-043144` | 0.120 | — |
| pipreqs | **envbench100 (direct)** | `pipreqs/envbench100-20260915-055253` | 0.120 | — |

`pipreqs` is the ONLY arm run against `envbench_python100.json` as ONE job — 100 repos, one run
dir, no envbench70+minus70 pooling and none of the 9-stray-repo filtering the others need. Its
÷exec ESSR is left blank above because that metric no longer exists (see below). Both runs are
also on the VM under `/opt/ratbench/runs/pipreqs/`, still named `run-20260915-043144` / `run-20260915-055253` there (renamed locally only); `input/` was dropped from both.

Every other `runs/` dir matching these datasets is an n=1 to n=4 smoke. Ignore them.

For rat50 sweagent there are TWO full runs. Use **161432** (harness `9c34439`, `exit_format 1`).
`152004` predates the DSML fixes and lost 25 of 50 repos to parser deaths, which inflates its
÷exec ESSR (dead repos drop out) while depressing EBSR.

## Metric definitions — the code is the authority

As of 2026-09-15 the definitions live in `bench/bench/metrics.py` and are computed automatically
by every run. Do NOT hand-derive either metric from `row.json`; call the code.

```
EBSR_r = (status not in _DISQUALIFIED) and collect_clean      and repo not untrusted
ESSR_r = min(passed_r / D_r, 1.0)                             D_r = certified manifest size
EBSR   = mean(EBSR_r) over ALL measured repos                 ESSR = mean(ESSR_r), MACRO
```

**Cross-dataset: POOLED, and nothing else.** One mean over one pile of repos — the rows from both
datasets together, the gold corpora merged, `compute_metrics` called once:

```
EBSR = credited repos / 150          every repo has an EBSR
ESSR = sum(ESSR_r) / 146             only the repos with a certified manifest
```

`bench/bench/campaign.py::pool_metrics`, surfaced by
`unified_bench --agent-dir runs/<arm> --gold <manifest>`. It is legitimate because the two
datasets are DISJOINT — 50 + 100 = 150 unique repos, overlap 0, gold 48 + 98 = 146 with no key
collisions (all verified).

Why pooled and not the macro/weighted pair that used to live here:

* The paper's own §4.2 defines ESSR as "the macro average **over repositories**". Pooled is that
  sentence applied to all 150 at once. Macro-of-macros adds a second averaging step the method
  section never defines.
* "Macro" already means per-repo INSIDE one dataset, so reusing it for a mean-over-datasets gave
  one word two meanings in one paper.
* Weighted multiplied a 48-repo mean by 50, silently imputing the gold-less repos at their
  dataset's mean. Pooled removes the question instead of answering it.
* Macro was not rank-safe: it flattered `modern` by +2.1 points and `pipreqs` by +1.7, because
  both score better on rat50 and macro weighted a rat50 repo **2.04x** an envbench100 one.

**The two denominators differ on purpose — EBSR over 150, ESSR over 146 — and both must be stated
wherever this is published.** An unlabelled 146-vs-150 reads as an error. `metrics.json` carries
both as `n` and `n_gold_scored`; the `--agent-dir` report prints the note automatically.

**Always report the per-dataset columns next to the pooled row.** Pooling mixes two sampling
frames (rat50 is 50 of RATBench's 500, envbench100 is 100 of EnvBench's 329), and that is only
defensible while the breakdown is visible. It also carries the real finding: envbench100 is harder
for every arm.

**There is exactly one ESSR now.** `ESSR` ÷exec and `ESSR_all` ÷all were **DELETED** from
`metrics.py`. Both divided by the agent's OWN pytest collection, so an environment that collected
12 tests and passed 11 scored 0.92 — identical to one that collected 1400 and passed 1288. Three
differently-denominated numbers all called "ESSR" is how the paper ended up quoting one while its
method section defined another. `metrics.json["ESSR"]` is now the gold-anchored one, full stop.

* `ESSR` is `None`, never `0.0`, when no gold manifest is supplied — "not scored" must stay
  distinguishable from "scored zero".
* `runner/cli.py` now passes `--gold` automatically, picking the corpus from the dataset name
  (`rat_python50*` → `rat_python50`, anything `envbench*` → `envbench_python100`, since
  envbench70/minus70 are subsets of that corpus). Override with `$RATBENCH_GOLD`.
* Gold source in-repo: `datasets/gold_manifests_combined.json`. `tools/essr_gold.py` still reads
  the OLD external split (`$RATBENCH_GT_DIR/gt100/corpus_results.jsonl` +
  `human_manual_counts.json`, and `claude_gt_py_50/rat_python50_gold.json`). **Verified identical**
  — same 98 and 48 repos, zero denominator disagreements — but the external paths do not exist on
  the VM or any other machine, so prefer the in-repo mirror.

**`tools/essr_gold.py` is NOT obsolete.** `metrics.py` scores ONE run dir; `essr_gold.py` scores a
campaign — pooling envbench70+minus70 per arm, filtering to a canonical dataset, the multi-arm
comparison table, `check_shas` (gold sha vs the DATASET's pin, a different check from the row-sha
one in `metrics.py`), and dataset-scoped economy medians. It should eventually call into
`metrics.py` instead of duplicating the formula; it does not yet.

### Edge cases — how each is handled, and why

Two distinct situations, routinely conflated. **146 of the 150 repos hit neither.**

**1. No gold manifest entry** — `neurogym/neurogym` (envbench), `Checkmk/checkmk`,
`mlflow/mlflow` (rat50). All three were rejected during certification.

| metric | treatment | why |
|---|---|---|
| EBSR | **counted normally** | EBSR asks whether the env built and collected cleanly; that is fully measurable without gold. Dropping it would shrink the denominator for a reason unrelated to the metric. |
| ESSR | **excluded from the mean** | There is no denominator. Scoring 0 would punish an arm for OUR missing certification. Excluding is arithmetically identical to imputing at the mean of the others. |
| tokens | **counted** | The spend was real and measured. |

**2. Untrusted repo** — `pypa/distutils` only, via `metrics.UNTRUSTED_REPOS` (overridable).

Its certified commit is unreachable upstream, so no arm can legitimately build the commit under
test — yet `sweagent_repo2run_modern` scored `status=executed, collect_clean=True` on it, which can
only mean it built a DIFFERENT, reachable commit. That result is not evidence about the pinned
repo for any arm.

| metric | treatment | why |
|---|---|---|
| EBSR | **forced 0, STAYS in the denominator** | Uniform penalty, so `n` is constant across arms and the column stays comparable. |
| ESSR | **excluded, not zeroed** | "ESSR = the mean of the other repos". A 0 would assert a failure rather than an absence of evidence. |
| `real_success` | **denied** | It is EBSR credit plus a high pass rate; if the build is not trusted, neither half is. |
| tokens | counted in absolute spend | The money was spent. Per-success ratios self-correct via the reduced `n_ebsr`. |

**Consequence you must state wherever this is published: EBSR is over 150 repos, ESSR over 146.**
Different populations, same table. Correct, but it reads as an error if unlabelled. `metrics.json`
carries both — `n` and `n_gold_scored`.

**Effect of the untrusted rule on real numbers** (envbench100): only one arm moves.

| arm | EBSR before | EBSR after |
|---|---|---|
| claudecode-dockerfile | 0.7300 | 0.7300 |
| setupx | 0.5800 | 0.5800 |
| **sweagent_repo2run_modern** | 0.4300 | **0.4200** |
| pipreqs | 0.1200 | 0.1200 |

**Unresolved: the paper says something different.** `Graph2Env-Paper/experiments.tex` §4.1 states
distutils is dropped, "leaving 149 repositories". The code scores it 0 over 150. Both are
defensible; the sentence and the code must be made to agree before submission.

### The older gold text (still accurate on sources and tiers)

Anchors the ESSR denominator to independently certified test counts instead of the agent's own
floating pytest collection.

```
ESSR_r = min(passed_r, D_r) / D_r        ESSR_gold = mean(ESSR_r)   # macro, not micro
```

`passed_r` is the raw `passed` field from `measure/<producer>/<org>/<repo>/row.json` (the real
replay/rebuild stage) — **not** `output/<org>/<repo>/_result_row.json` (the producer's own
in-sandbox self-report, which is frequently `executed: false, passed: 0` even when the real replay
shows the repo built and tested fine). `passed_r` is a raw count, not node-ID-matched against gold
— deliberately: gold `node_ids` is itself an intersection of two baseline collection runs (built to
reject flaky tests), so a given run can legitimately pass real tests outside that intersection.
Matching would be stricter but wasn't judged worth the extra complexity here.

**Gold source (single file now):** `gold_manifests_combined.json`, canonical copy at
`/Users/john/john-v3-multi-lang/artifacts/`, mirrored in-repo at
`datasets/gold_manifests_combined.json`. This supersedes the old split sources below — it's a
later, larger consolidation (generated 2026-09-14) with **98/100 envbench100 repos scored**, up
from the 92/100 the old gt100 + human_manual_counts split gave. The old rat50 source
(`claude_gt_py_50/rat_python50_gold.json`, 48/50 CERTIFIED) is unchanged and matches what's in the
combined file — only envbench100's coverage grew.
* ~~envbench100 — `artifacts/gt100/corpus_results.jsonl` + `artifacts/human_manual_counts.json`~~
  (92/100 scored) — superseded.
* ~~rat50 — `claude_gt_py_50/rat_python50_gold.json`~~ — same 48/50 CERTIFIED, now folded into the
  combined file.

**`D_r` (the denominator) per repo, by gold `status` — the "repos with no gold are EXCLUDED" rule
from the 9-12 version of this doc is now split into three cases, since a blanket exclude was
hiding a real distinction:**
1. **`CERTIFIED`** — `D_r = manifest_size` (`== len(node_ids)`).
2. **`HUMAN_COUNTED`** (envbench100 only, 7 repos) — `D_r = manifest_size` too, despite the gold
   file's own per-repo note calling this "NOT usable as an ESSR denominator" — used anyway, by
   deliberate choice, same as CERTIFIED.
3. **In-corpus but not certified or human-counted** (`Checkmk/checkmk`, `mlflow/mlflow` on rat50;
   `neurogym/neurogym` on envbench100 — the repo unaccounted for in `91 certified + 7 human_counted`
   vs. 99 viable) — **fallback**, `D_r = max(passed_r, collected_r)` from that repo's own run data
   (`collected_r = len(row.json["collected_node_ids"])`), so it still gets a real `[0,1]` score
   instead of being dropped.
4. **`pypa/distutils`** (envbench100 only; gold note: "commit unreachable upstream") — **forced
   `ESSR_r = 0` for every arm**, not the fallback formula — it's genuinely unbuildable at the
   certified commit, not just short a gold count. See "Metric traps" below: this repo scores
   EBSR=True for 2 of 3 arms under the CORRECT metric anyway, which is why it can't be trusted with
   a fallback score computed from that arm's own (likely wrong-commit) run data.

**Separately, 9 repos must be excluded from envbench100 entirely** (not scored, not zeroed): repos
present in `envbench_python70.json` that were never part of the canonical `envbench_python100.json`
100 — a dataset-sampling mistake, not a certification outcome. Filter to
`full_name ∈ envbench_python100.json` before scoring any `envbench70-*`/`minus70-*` run pair. (This
is the same 70/100/minus70 trap from the top of this doc, now hitting the gold-scoring step too.)

Populations: rat50 = 48 CERTIFIED + 2 fallback = 50. envbench100 = 91 CERTIFIED + 7 HUMAN_COUNTED +
1 fallback (`neurogym`) + 1 forced-zero (`pypa/distutils`) = 100.

**Merged run dirs** — `runs/<arm>/envbench100-merged-20260915/` now exist for all three arms:
`output/`+`measure/` hardlinked (not copied) from that arm's `envbench70-*` + `minus70-*` runs,
filtered to the 100 canonical repos, with `per_repo_table.json` / `in_sandbox_score.json` /
`case_studies_summary.json` / `rat_results.json` / `measure/metrics.json` all regenerated via the
existing tooling (`bench.report.run_tables`, `bench.report.case_study`,
`runner/benchmark.py --aggregate-only`, `bench.unified_bench --aggregate-only`) — no Dockerfile or
agent was re-run. Source `envbench70-*`/`minus70-*` dirs are untouched; pre-merge upper-level files
backed up to `runs/_pre_merge_backups/20260915/`. `setupx`'s two source runs used **different
harness commits** (`943870b` vs `fb47717`) — recorded explicitly in the merged dir's
`_run_manifest.json`, not hidden by the merge. Use these merged dirs going forward instead of
manually pooling two run dirs per arm.

### Results (2026-09-15, corrected — see "Metric traps" for why EBSR moved)

All figures below are produced by `unified_bench --agent-dir runs/<arm> --gold ...` — no
hand-picked run dirs, no hand arithmetic. Reproduce any row with that one command.

| arm | rat50 ESSR | env100 ESSR | rat50 EBSR | env100 EBSR | **POOLED ESSR** | **POOLED EBSR** |
|---|---|---|---|---|---|---|
| ccdf | 0.4767 | 0.5404 | 0.6200 | 0.7300 | **0.5194** | **0.6933** |
| setupx | 0.3605 | 0.4982 | 0.3600 | 0.5800 | **0.4529** | **0.5067** |
| modern (sweagent_repo2run_modern) | 0.4400 | 0.3163 | 0.6000 | 0.4200 | **0.3570** | **0.4800** |
| **pipreqs** (static floor) | 0.2004 | 0.1039 | 0.1200 | 0.1200 | **0.1356** | **0.1200** |

POOLED = one mean over one pile of repos. ESSR over the **146** repos with a certified manifest,
EBSR over all **150**. The denominators differ on purpose and both must be stated.

**macro and weighted-150 are GONE** (removed from the code 2026-09-15). Two reasons. "Macro"
already means per-repo *inside* one dataset — the paper's §4.2 defines ESSR as "the macro average
over repositories" — so reusing the word for a mean-over-datasets gave one term two meanings in
one paper. And weighted-150 multiplied a 48-repo mean by 50, silently imputing the gold-less repos
at their dataset's mean. Pooled is §4.2's own sentence applied to all 150 repos at once, and needs
no imputation. For reference, the superseded values were ccdf 0.499/0.513, setupx 0.432/0.454,
modern 0.369/0.352, pipreqs 0.152/0.136 (macro / weighted ESSR); pooled EBSR happens to equal
weighted EBSR exactly, because every repo has an EBSR so the reweighting was lossless there.

**Three rat50 ESSRs moved** when recomputed from the run dirs rather than transcribed: ccdf
0.458 → 0.4767, setupx 0.366 → 0.3605, modern 0.422 → 0.4400. The ccdf case was checked two ways
(the campaign code and `essr_gold.py`'s dataset-scoped method both give 0.4767 from
`ratbench50_run-20260904-134239`) and 0.458 is not reproducible from anything on this machine. The
earlier values may come from run dirs that no longer exist here; treat the recomputed ones as
authoritative.

envbench100 ESSR recomputed 2026-09-15 via `metrics.py` (ccdf 0.5404, setupx 0.4982, modern
0.3163, pipreqs 0.1039); differences from the earlier row are rounding. modern's envbench100 EBSR
is **0.420 not 0.430** under the untrusted rule — that was the one number the rule moved. rat50
ESSR/EBSR for ccdf/setupx/modern are carried over from the 2026-09-15 pass and were NOT
recomputed: those run dirs are not on this machine (they ARE on the VM at
`/opt/ratbench/runs/setupx/run-20260904-173735` and
`/opt/ratbench-cc/runs/claudecode-dockerfile/run-20260904-134239`).

pipreqs is the floor and behaves like one: below every agent arm on both datasets under both
conventions, at **$0.00 and zero tokens**. The gap is the number worth reporting — ccdf buys
+0.384 ESSR and +0.573 EBSR over static import scanning (pooled).

The cross-benchmark weighting question that used to sit here is closed: there IS no literature
convention (checked against SWE-bench/SWE-agent/Repo2Run/HAL), so rather than pick one, the code
now pools per repo and the question does not arise. Report the per-dataset columns alongside the
pooled row — they carry the real finding that envbench100 is harder for every arm, and pooling
across two sampling frames is only defensible when the breakdown is visible.

Ranking is identical on ESSR and EBSR, both datasets and pooled: ccdf > setupx > modern > pipreqs.
Note it was NOT invariant to the choice of convention — macro flattered modern by +2.1 points and
pipreqs by +1.7, because both score better on rat50 and macro weighted a rat50 repo 2.04x an
envbench100 one. That sensitivity is the other reason for dropping it.

## Metric traps

**The paper's own table is currently built on this trap.** `Graph2Env-Paper/Tables + Figures/
main_result.tex` quotes SetupX 71.3% / SWE-agent 46.7% / Claude Code 75.3% for EBSR. Recomputing
over the canonical 100 gives **58.0 / 43.0 / 73.0** under the correct gate and **70.0 / 43.0 /
77.0** under the loose `row['ebsr']` flag — the table tracks the loose one. Its ESSR column, by
contrast, matches the then-current weighted-150 gold figure to the digit (45.2 / 34.7 / 51.2;
those are now superseded by the pooled numbers). So the two
columns come from different scorers, and §4.2 defines EBSR as the gate the numbers do not use.
The residual gap needs the rat50 halves to close exactly, so this is evidence-backed inference,
not proof. **Fixing it is an `--aggregate-only` re-score, not a re-run.**

**`row['ebsr']` is NOT the EBSR metric — and this bit a fresh session again on 2026-09-15.**
`bench/bench/measure.py:380` sets `ebsr=executed` on the success path — the stored field is just an
alias for "pytest ran," with no gate on build/collection cleanliness. The real metric
(`bench/bench/metrics.py:50`) is `status not in _DISQUALIFIED AND collect_clean`. On the rat50
setupx run the loose flag is True for 38 of 50 while the real gate passes only 18 of 50 (0.36).
Confirmed again independently on 2026-09-15 using the current data (same 38-vs-18 split) before
being caught and fixed — `essr_gold.py` replicates the real definition; **anything new computing
EBSR from `row.json` must recompute `status not in _DISQUALIFIED and collect_clean` itself, never
read the stored `ebsr` key.** The other two arms move less on this fix (ccdf rat50: unaffected in
this instance; modern rat50: 0.56→0.60) — the size of the gap is architecture-specific, not a fixed
correction factor, so don't assume it's small just because it was small once.

**`pypa/distutils` (envbench100, gold-dropped for "commit unreachable upstream") still shows
`ebsr=True` under the CORRECT gate for 2 of 3 arms** (`sweagent_repo2run_modern`: status=executed,
collect_clean=True; `setupx`: status=collect_error, collect_clean=False;
`claudecode-dockerfile`: status=build_fail, collect_clean=False). A repo whose gold-certified commit
is unreachable upstream shouldn't be buildable at all — two arms building it successfully means
they most likely built against a different (reachable) commit than the one certification tried and
failed on, not that the "unreachable" note is wrong. This is why both ESSR and EBSR force this repo
to `0` for every arm rather than trusting whatever that arm's own run produced.

**ESSR ÷exec was the misleading headline — now DELETED.** It dropped repos that never executed, so
an arm that failed often looked good: modern scored 0.864 ÷exec on envbench70 and 0.326
gold-anchored. Both ÷exec and ÷all are gone from `metrics.py`; `metrics.json["ESSR"]` is the
gold-anchored one. `bench/bench/inline_score.py` still computes its own ÷exec
`ESSR_avg_pass_rate_official` for the native/live lane (rat, sweagent, live-claude — arms with no
rebuildable artifact), and `report/run_tables.py` reads it. That is a SEPARATE scorer for a
separate lane; do not quote it next to a `metrics.json` ESSR.

**A subset run used to poison the gold ESSR.** `gold_essr` scores a population; with none given it
now defaults to the repos the run actually attempted. Before that fix a one-repo `--only` smoke
scored the other 47 gold repos 0.0 and reported ESSR 0.0208 for a repo that passed everything —
and `metrics.py` runs automatically on every run, smokes included. Pass `population=<dataset>`
only when you deliberately want an arm charged for repos it never attempted.

**Hollow EBSR passes.** `collect_clean` accepts pytest exit code 5 ("no tests collected") as
success, so a repo collecting 0 of 1642 tests earns EBSR credit. Gold now quantifies this:
`collection_coverage = collected/gold`. On envbench100, 12 ccdf / 7 setupx / 5 modern credited
repos collect under half the certified suite, many exactly zero.

**Gold denominators are imperfect.** The gt100 harness runs `pytest --collect-only` over the whole
tree, overriding `testpaths`, so it counts dev scripts maintainers exclude. Biases every arm LOW,
unevenly. `capped = 0` everywhere, so nothing broke, but absolute values are not "fraction of the
real suite passed".

**The 100-call cap binds and dominates.** `num_turn = 100` is the real budget on every arm — cost
never approaches the $2/repo ceiling ($0.05 typical). modern: 57/70 repos capped on envbench70,
only 7 finished voluntarily. Any score is "at a 100-call budget", not "what the agent can do".

## VM — Hetzner `Graph2Env`

```
ssh -p 443 root@167.233.64.96      # ALWAYS 443; port 22 is unreachable from Claude Code's sandbox
```

Four checkouts, deliberately separate. A pull in one never affects another:

| path | commit | used by |
|---|---|---|
| `/opt/ratbench` | `78c797a` (branch `setupx-baseline`) | setupx, both sweagent arms, pipreqs |
| `/opt/ratbench-cc` | `d7d7fb3` (`claudecode-deepseek-turncap`) | ccdf |
| `/root/agents/SetupX` | `85de355` | the SetupX agent, patched in place |
| `/opt/swe-agent` | `3ea751c0` | SWE-agent 1.1.0, both sweagent arms |
| `/root/pp314` | Python 3.14.4 venv | pipreqs only — `$PIPREQS_PYTHON`; see below |

**Why pipreqs needs its own 3.14 interpreter.** pipreqs `ast.parse`s every `.py` under the
interpreter RUNNING it, and aborts the whole scan on the first file it cannot parse. Under the
3.11 venv, any repo using PEP 695 (`type X = ...`, 3.12+) died: PostHog went 0 → 191 packages once
moved to 3.14. `producers/pipreqs_baseline.py` therefore invokes it as a MODULE under
`$PIPREQS_PYTHON` (falling back to `sys.executable`) and passes `--encoding utf-8-sig`, which
fixes a second class — one file with a UTF-8 BOM also aborted the scan (Azure/azure-cli, 0 → 51
packages). What remains is genuine: 3 repos on rat50 and 7 on envbench100 contain a `.py` that is
not valid Python 3 at all — a Jinja template, a Python 2 file, an IPython transcript — and pipreqs
fails closed on them. That is the baseline's headline weakness, not a harness bug.

**NEVER `git pull` a checkout while a benchmark runs there.** The scheduler spawns a fresh child
per repo that imports from disk, so repos split across two code versions while the manifest still
records the starting commit. Already happened once — see the `PROVENANCE.md` in
`runs/setupx/run-20260904-173735`.

**`/root/agents/SetupX` is patched in place and is NOT affected by git operations in
`/opt/ratbench`.** A checkout there can revert `tools/setupx-bench.patch` (the recorded
fingerprint) without changing the agent's actual behaviour. Check both.

Three janitors run in loops: `mirror_janitor.sh` (setupx mirrors), `container_janitor_loop.sh`
(orphaned containers, 180-min floor — MUST exceed the 130-min hard wall), `image_janitor_loop.sh`
(dangling images, because the sweagent lanes set `remove_images: false` and leak ~1GB/repo).

## Shell traps that cost real time this campaign

* **`pgrep -f "<pattern>"` matches the shell running it.** An `until ! pgrep -f "runner.cli setupx"`
  loop never terminates, and three such orphans nearly blocked a queued job forever. Put wait logic
  in a FILE (cmdline is then just the script path) and use a bracket pattern: `runner[.]cli setupx`.
* **`pkill -f X` kills your own SSH session** when the command line contains X.
* **The harness shell is zsh**, which does not word-split unquoted `$var`. `set -- $out` yields ONE
  field and every parsed value is empty — this fabricated two false alarms. Run watcher scripts
  with `bash` explicitly and parse with `read -r a b c <<< "$out"`.
* **Long idle SSH dies and exits 0**, indistinguishable from success. Use `ServerAliveInterval=30`
  plus periodic heartbeat output.
* **Agent stdout is not a clean log.** It contains repo source the agent printed. Grepping for
  `429`/`402` matched token counts and `401 tests collected`; `AuthenticationError` matched a
  repo's own imports. Match only fully-qualified names (`litellm.AuthenticationError`).

## State right now

VM idle, nothing running, **97G free** (was 37G before the 2026-09-15 cleanup reclaimed ~60G of
run `input/` dirs). `/opt/ratbench` is on branch `setupx-baseline` — NOT `main`, as an earlier
version of this doc claimed — and carries its own uncommitted edits, so a pull needs a stash.
`pipreqs==0.4.13` and `pytest` are installed in its 3.11 venv, plus a **3.14** venv at
`/root/pp314` that the pipreqs arm needs (`PIPREQS_PYTHON=/root/pp314/bin/python`).

**Concurrency works and is the default worth using.** `--concurrency 8` ran 100 repos end to end
in **21 minutes**, against 48 minutes for 50 repos sequentially — ~9x throughput. The produce
stage had been fully sequential because `--concurrency` was never passed; the measure stage was
already running at 4 (`runner/cli.py`: `args.concurrency or 4`). One flag drives both stages.

Disk is run `input/` dirs, not a leak: 30G modern-envbench70, 29G ccdf-envbench70, 8.1G + 7.9G
sweagent rat50, 7.9G ccdf rat50. All regenerable from the pinned commits, all already pulled
locally minus `input/`. 39GB of Docker images are reclaimable but TAGGED, so `image_janitor`
(dangling-only, correctly) will not touch them.

To reclaim ~80G before the next run:
```bash
ssh -p 443 root@167.233.64.96 'rm -rf /opt/ratbench/runs/*/*/input /opt/ratbench-cc/runs/*/*/input'
```
Destructive — confirm with the operator first.

## Uncommitted

```
 M bench/bench/metrics.py                    ESSR now gold-anchored only; UNTRUSTED_REPOS; the
                                              population fix. ÷exec and ÷all DELETED.
 M bench/bench/unified_bench.py              --gold / --gold-corpus now honoured (was forced None)
 M runner/cli.py                             passes --gold automatically; _gold_for() corpus pick
 M bench/tests/bench/test_metrics_gates.py           ) four EXISTING test files edited to drop
 M bench/tests/bench/test_metrics_gating.py          ) assertions on the removed keys. Every
 M bench/tests/bench/test_orchestrator.py            ) other pinned number in the py50 reference
 M bench/tests/bench/test_metrics_py50_reference.py  ) still holds — that is the evidence the
                                                       removal touched nothing else.
?? bench/tests/bench/test_metrics_gold_anchored.py   34 tests: the metric, the loader, the edge
                                                       cases, the untrusted rule
?? tools/essr_gold.py                        campaign-level scorer (works, unreviewed). pipreqs
                                              added to RAT50_RUNS and ENVBENCH100_RUNS.
?? datasets/envbench_python100.json          100 repos, sha dfea1373f96310ff
?? datasets/envbench_python100_minus70.json   39 repos, sha 4b74583fde2d7d4a
?? datasets/ablation30.json                  not mine, unexamined
?? datasets/gold_manifests_combined.json     47MB mirror of the canonical gold manifest. NOW A
                                              RUNTIME DEPENDENCY, not just a reporting one —
                                              runner/cli.py reads it on every run and ESSR is null
                                              without it. Decide deliberately whether to commit it
                                              (47MB) or ship it to the VM out of band; it is NOT
                                              on the VM today, so VM runs currently score ESSR null.
```

The two envbench datasets should be committed: four completed runs pin them by sha in their
manifests, so leaving them untracked makes those runs unreproducible. `envbench_python70.json` is
already committed for exactly this reason (`82e2726`).

The pipreqs producer itself IS committed and deployed (`producers/pipreqs_baseline.py`,
`producers/tests/test_pipreqs_baseline.py`, registered in `producers/__init__.py`,
`runner/benchmark.py::_PRODUCE_ABLE`, `varieties.toml`, `requirements.txt`).

## Paper table — what is updated and what is not

`Graph2Env-Paper/Tables + Figures/main_result.tex`, as of 2026-09-15:

| row | EBSR | ESSR | tokens | scored by |
|---|---|---|---|---|
| pipreqs | 12.0% | 13.6% | N/A | `metrics.py`, pooled |
| SetupX | 50.7% | 45.3% | 962.3K median | `metrics.py`, pooled |
| SWE-agent | 48.0% | 35.7% | 2373.2K median | `metrics.py`, pooled |
| Claude Code | 69.3% | 51.9% | 1156.1K median | `metrics.py`, pooled |
| Repo2Run / RAT / Graph2Env | TBD | TBD | TBD | **see the warning at the top** |

Changed in that edit: the `# Successfully Built Environments` column was dropped; a
`Static based` section was added for pipreqs; the caption now states both denominators.

**EBSR moved a lot and the reason matters.** SetupX 71.3% → 50.7% and Claude Code 75.3% → 69.3%
are NOT new data — the old values came from the loose `row['ebsr']` flag, the new ones from the
gate §4.2 actually defines. ESSR barely moved because that column was already gold-anchored.

**The `SWE-agent` row is `sweagent_repo2run_modern`.** The other arm, `sweagent_repo2run` (the
Repo2Run-paper scaffold), has NO envbench run, so it cannot be pooled and cannot appear.

**Token coverage is uneven and the medians hide it**: SetupX carries telemetry on 134 of 150 rows,
SWE-agent 147, Claude Code 150. Rows without telemetry are excluded from the median rather than
counted as zero, so SetupX's 962.3K is a median over 89% of its repos. Footnote it if the claim
depends on it.

## Next steps, in priority order

0. **Get `gold_manifests_combined.json` onto the VM** (47MB, currently local-only). Until then
   every VM run reports `ESSR: null` — it still measures and still reports EBSR, and prints a
   warning naming the path it looked for, but the ESSR column will be empty.
0b. **Re-score the published runs and fix the paper table.** The EBSR column in
   `main_result.tex` is the loose flag (see "Metric traps"); the fix is
   `bench.unified_bench --aggregate-only --gold ... --gold-corpus ...` per run dir, no re-run
   needed. A "Static based" section for pipreqs is also still outstanding.

1. **Run the `sweagent_repo2run` baseline on envbench70's 70 repos.** This is the campaign's
   biggest gap — the modern arm exists to be contrasted against it and that contrast does not yet
   exist on envbench. The VM at `ba0eca2` is correctly configured: both arms carry matched
   600/7200 timeouts, and the DSML translator (live for `thought_action`) is restored.
   ```bash
   ssh -p 443 root@167.233.64.96
   cd /opt/ratbench && ./run_bench.sh sweagent_repo2run \
     --repos-json datasets/envbench_python70.json --concurrency 4 --tier all --run-name envbench70-baseline
   ```
2. Commit `tools/essr_gold.py` + the two datasets.
3. Decide whether `num_turn = 100` is measuring the agent or the cap, before any of this is
   published. It binds on the large majority of repos in every arm.
4. setupx still reports NO cost (`cost_usd` null on every run, both datasets). There is no
   cost-per-score for that arm anywhere.
5. Consider an `EBSR_strict` requiring `collected > 0` (or coverage above a threshold) — gold now
   makes the hollow-pass rate measurable.

## Useful one-liners

```bash
# every run dir with its dataset, n, and metrics
python3 - <<'EOF'
import glob, json, os
for m in sorted(glob.glob("runs/*/*/_run_manifest.json")):
    d = os.path.dirname(m); man = json.load(open(m))
    mp = os.path.join(d, "measure", "metrics.json")
    s = {}
    if os.path.exists(mp):
        j = json.load(open(mp)); s = j[list(j)[0]]
    print(f"{man.get('variety','?'):<24} {os.path.basename(d):<38} "
          f"{(man.get('dataset') or {}).get('path','?').split('/')[-1]:<32} "
          f"n={s.get('n','-')} EBSR={s.get('EBSR','-')} ESSR={s.get('ESSR','-')}")
EOF

# VM status in one line: FREE_GB SETUPX CCDF QUEUE
ssh -p 443 root@167.233.64.96 /root/bench_status.sh
```
