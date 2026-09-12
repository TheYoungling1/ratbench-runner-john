# Handoff — envbench benchmark campaign

Written 2026-09-12. Read this before running or scoring anything.

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

Every other `runs/` dir matching these datasets is an n=1 to n=4 smoke. Ignore them.

For rat50 sweagent there are TWO full runs. Use **161432** (harness `9c34439`, `exit_format 1`).
`152004` predates the DSML fixes and lost 25 of 50 repos to parser deaths, which inflates its
÷exec ESSR (dead repos drop out) while depressing EBSR.

## Gold-anchored scoring — `tools/essr_gold.py` (UNCOMMITTED)

Anchors the ESSR denominator to independently certified test counts instead of the agent's own
floating pytest collection.

```
P_r = min(passed_r / gold_r, 1.0)        ESSR_gold = mean(P_r)      # macro, not micro
python3 tools/essr_gold.py                                          # envbench100
python3 tools/essr_gold.py --dataset datasets/rat_python50.json     # rat50
```

Operator decisions baked in: cap at 1.0; repos with no gold are EXCLUDED (not zeroed); repos that
failed to build score 0.0 and STAY IN the average.

Gold sources:
* envbench100 — `/Users/john/john-v3-multi-lang/artifacts/gt100/corpus_results.jsonl`
  (85 CERTIFIED `manifest_size`) + `artifacts/human_manual_counts.json` (7 hand-verified). 92 of
  100 scored; 8 have no count and are dropped.
* rat50 — `/Users/john/john-v3-multi-lang/claude_gt_py_50/rat_python50_gold.json`
  (48 of 50 CERTIFIED, full node-id lists, 0 SHA drift vs the dataset pins). This is the pinned
  gold JSON whose absence caused `bench/gold.py` to be deprecated (`metrics.py:91`).

### Results

| arm | rat50 ESSR_gold | envbench100 ESSR_gold | rat50 EBSR | envbench100 EBSR |
|---|---|---|---|---|
| ccdf | 0.477 | 0.556 | 0.620 | 0.730 |
| setupx | 0.361 | 0.517 | 0.360 | 0.580 |
| sweagent | 0.131 (baseline) | 0.326 (modern) | 0.180 | 0.430 |

Ranking is identical on both datasets. The sweagent row is NOT same-arm across columns.

## Metric traps

**`row['ebsr']` is NOT the EBSR metric.** It is a looser stored flag. The metric
(`bench/bench/metrics.py:50`) is `status not in _DISQUALIFIED AND collect_clean`. On the rat50
setupx run the flag is True for 38 of 50 while the published EBSR is 0.36 (18/50). `essr_gold.py`
replicates the real definition; anything new must too.

**ESSR ÷exec is the misleading headline.** It drops repos that never executed, so an arm that
fails often looks good. modern scored 0.864 ÷exec on envbench70 and 0.326 gold-anchored. Compare
against `ESSR_all` or gold, not `ESSR`.

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
| `/opt/ratbench` | `ba0eca2` | setupx, both sweagent arms |
| `/opt/ratbench-cc` | `d7d7fb3` (`claudecode-deepseek-turncap`) | ccdf |
| `/root/agents/SetupX` | `85de355` | the SetupX agent, patched in place |
| `/opt/swe-agent` | `3ea751c0` | SWE-agent 1.1.0, both sweagent arms |

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

VM idle, nothing running, 58G free (down from 175G).

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
?? tools/essr_gold.py                        the gold-anchored scorer (works, unreviewed)
?? datasets/envbench_python100.json          100 repos, sha dfea1373f96310ff
?? datasets/envbench_python100_minus70.json   39 repos, sha 4b74583fde2d7d4a
?? datasets/ablation30.json                  not mine, unexamined
```

The two envbench datasets should be committed: four completed runs pin them by sha in their
manifests, so leaving them untracked makes those runs unreproducible. `envbench_python70.json` is
already committed for exactly this reason (`82e2726`).

## Next steps, in priority order

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
