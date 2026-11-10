# Overnight 200-repo Claude Code run: launch and monitoring handoff

Written 2026-11-10 for a fresh agent session. Read this whole file before acting. It builds on
`docs/overnight-ccdf-vm-preparation.md` (VM cleanup and the staged dataset) — read that too.

**Goal:** generate and measure Dockerfiles for all 200 repos with the `claudecode-dockerfile` arm on
DeepSeek V4 Pro through LinkAPI, at **concurrency 3**, and keep it healthy overnight with as little
of John's time as possible.

## 1. Readiness (verified 2026-11-10 ~12:30 UTC)

| Check | State |
|---|---|
| VM | `root@157.180.47.81`, SSH port 22, 8 vCPU / 15 GB RAM, 257 GB free, no containers |
| Code | `/opt/ratbench` = rsync of the local tree at `f68469a` + this doc + `tools/ccdf_watch.py` (not a git clone; `.git` is present so manifests record the commit) |
| Dataset | `datasets/full200_claudecode_v4pro.json`, 200 repos, sha256 `9553196c…a0548d` (rat_python50 + rat_python50_new + envbench_python100). Measure merges all gold corpora for this filename. |
| Model route | LinkAPI Anthropic endpoint, model slug **`[SP]deepseek-v4-pro`** (responses report `deepseek/deepseek-v4-pro-0813`). The plain `deepseek-v4-pro` slug fails on the Anthropic endpoint — do not use it. |
| Proxy | tmux `proxy`: `tools/anthropic_thinking_proxy.py` → `api.linkapi.ai`, forces `thinking: disabled`, no provider pin (that field is OpenRouter-only). Port 8931 is firewalled on eth0. |
| `.env` (VM) | `ANTHROPIC_BASE_URL=http://host.docker.internal:8931`, `ANTHROPIC_AUTH_TOKEN`=LinkAPI key, `PROXY_UPSTREAM_HOST=api.linkapi.ai`. OpenRouter version kept as `.env.openrouter`. |
| Smoke | `bruin-data/ingestr` via LinkAPI on the VM: EBSR 1, 19/19 tests, 11 calls, 0 thinking blocks, 0 DSML leaks |
| Workbench | `claude-runner:latest` built (`sha256:8a4c9caf…`). Do not delete it. |

**Known limits — state them in any report:**
- **Cost:** LinkAPI reports no cost, so `cost_usd` is the CLI's own Anthropic-priced estimate
  (`usage_source: "cli"`) and is wrong. Price from `tokens_in`/`tokens_out` × LinkAPI's rate.
- **Balance unknown:** LinkAPI's billing endpoints show no usable balance. Running out shows up as
  HTTP 402 failures; the watchdog stops the run on a burst of them (section 4).
- **Provider unpinned:** LinkAPI picks the upstream. The DeepSeek-pinned OpenRouter run had no
  DSML leaks in 16 repos; unpinned OpenRouter had 2 in 9. Watch the `dsml` failure count.
- **Duration:** it will not finish overnight. Produce took a mean of ~12 min/repo (median 7, large
  repos 30–40) → **~13–14 h for 200 repos at concurrency 3**. Measure starts only after ALL produce
  finishes, then rebuilds every Dockerfile (build ≤ 60 min, tests ≤ 30 min each) — estimate another
  8–15 h. Overnight covers produce; expect measure to run into the next day.

## 2. Launch (only after John says go)

Ask John to confirm the LinkAPI balance is enough before launching: at OpenRouter rates 50 repos
cost ~$30–35, so plan for **~$120–140** for 200 (LinkAPI's rate may differ). Then:

```bash
ssh root@157.180.47.81
cd /opt/ratbench
# preflight: proxy up, nothing running, disk healthy
tmux ls; ss -lnt | grep 8931; docker ps -q | wc -l; df -h / | tail -1
docker builder prune -f --filter until=1h          # start clean (proxy/runner image untouched)

tmux new -d -s ccdf "cd /opt/ratbench && ./run_bench.sh claudecode-dockerfile \
  --llm '[SP]deepseek-v4-pro' --repos-json datasets/full200_claudecode_v4pro.json \
  --tier all --num-turn 100 --concurrency 3 --run-name full200-v4pro-linkapi \
  2>&1 | tee /opt/ratbench/full200_v4pro_linkapi.log; \
  echo EXIT=\${PIPESTATUS[0]} >> /opt/ratbench/full200_v4pro_linkapi.log"
sleep 20
tmux new -d -s watch "cd /opt/ratbench && python3 tools/ccdf_watch.py \
  --run-name full200-v4pro-linkapi --log /opt/ratbench/full200_v4pro_linkapi.log --act"
```

`--tier all` is required (no repo has a tier label; without it zero repos run). Measure inherits
`--concurrency 3`.

## 3. Monitoring loop (the agent)

Start the monitoring session with `/loop` (no interval) so the agent paces itself with
`ScheduleWakeup`, e.g.:

> /loop Monitor the overnight ccdf run per docs/overnight-ccdf-run-handoff.md section 3–6. Each
> tick: read the watchdog status, act per the runbook, log what you did.

Each tick:

```bash
ssh root@157.180.47.81 'cat /opt/ratbench/overnight-v4pro/watch-status.json; \
  tail -3 /opt/ratbench/overnight-v4pro/watch.log; \
  grep -E "scheduler/done" /opt/ratbench/full200_v4pro_linkapi.log | tail -5'
```

Wake-up pacing: **1200 s** during the first 2 h (problems show early), then **1800 s**. Wake at
600 s after any action, to confirm it worked. Mark ticks with nothing new as `noop: true`.

Each tick, check:
1. `watch-status.json` `time_utc` is < 15 min old. If not, the watchdog died → restart it (section 2).
2. `phase` and progress: `produce.done` should rise ~10–15 per hour at concurrency 3.
3. `failures`: infra classes (`credit`, `rate_limit`, `api_5xx`, `connection`) vs result classes
   (`turn_cap`, `dsml`, `timeout`, `no_dockerfile`, `other`).
4. `disk_free_gb`, `build_cache`, `mem_available_gb`, `proxy_up`, `run_tmux_alive`, `actions`.

Append one line per tick to `/opt/ratbench/overnight-v4pro/agent-log.md` (time, progress, what you
did and why).

## 4. What the watchdog does on its own (every 5 min)

`tools/ccdf_watch.py --act` (tests in `tools/tests/test_ccdf_watch.py`):

| Condition | Action |
|---|---|
| free disk < 60 GB | `docker builder prune -f --filter until=3h` (unused cache only) |
| free disk < 35 GB | same with `until=1h`, plus `docker image prune -f` (dangling only) |
| last 3 finished repos all failed on credit (402) | writes `overnight-v4pro/STOPPED_CREDIT`, kills tmux `ccdf`, the `runner/benchmark.py` workers and `claudecode-dockerfile-producer-*` containers |
| nothing listening on 8931 | restarts tmux `proxy` with `PROXY_UPSTREAM_HOST` from `.env` |

It never prunes images in use, volumes, `claude-runner`, or anything under `runs/`. The scheduler's
own gate also stops starting new repos below 15 GB free (produce phase only; measure has none).
Do not enable Docker's automatic builder GC in `daemon.json`: on an earlier VM with the same
containerd image store it deleted freshly built images mid-measure (`measure_error`).

## 5. Runbook

| Symptom | Do |
|---|---|
| `STOPPED_CREDIT` exists | **PushNotification John** ("LinkAPI out of credit, run stopped at N/200"). Do nothing else until he tops up; then follow *Resume* below. |
| `rate_limit`/`api_5xx`/`connection` failures, isolated | Leave them; retry after the run drains (*Retry*). |
| Same infra failures on ≥ 5 consecutive repos | Check the proxy (`tail proxy.log`, a test request from section 1 of the prep doc). Proxy dead → restart. Upstream down → stop the run like the credit stop, push John, resume later. |
| `dsml` failures > 10% of finished repos | Push John with the count; keep running (it is a result of this route, not a bug to hide). |
| Disk < 25 GB after the watchdog's prune | Measure phase: `docker builder prune -f --filter until=30m`; still low → push John. Never delete run output. |
| `mem_available_gb` < 1 repeatedly / OOM kills in `dmesg` | Note it; repos that fail on OOM get re-measured later at concurrency 1. Don't change concurrency mid-run. |
| A repo stuck > 2 h 10 min in produce | The scheduler's hard wall (7,800 s) kills it; only intervene if it is still alive after that. |
| VM unreachable for 2 ticks | Push John. |
| Watchdog or run crashed with a Python traceback | Diagnose (section 6). |

**Resume after a stop** (credit or upstream): in-flight repos were killed and have no result row, so
they rerun automatically. Repos that finished with an infra failure *do* have a row and would be
skipped — move them aside first, outside the run folder:

```bash
R=$(ls -d /opt/ratbench/runs/claudecode-dockerfile/full200-v4pro-linkapi-*/ | tail -1)
A=${R%/}-infra-attempts; mkdir -p $A
for repo in <owner/name ...infra-failed repos from watch-status.json...>; do
  mkdir -p $A/$(dirname $repo); mv $R/output/$repo $A/$repo; done
cd /opt/ratbench && tmux new -d -s ccdf "bash -c 'source env.sh && python runner/benchmark.py \
  --model claudecode-dockerfile --tier all --root-path $R --concurrency 3 --num-turn 100 \
  --llm \"[SP]deepseek-v4-pro\" --repos-json datasets/full200_claudecode_v4pro.json \
  2>&1 | tee -a /opt/ratbench/full200_v4pro_linkapi.log'"
```

Then measure, which the CLI would otherwise have done:

```bash
cd /opt/ratbench/bench && source ../env.sh && python -m bench.unified_bench \
  --harvest claudecode-dockerfile=$R/output --out $R/measure --concurrency 3 \
  --gold /opt/ratbench/datasets/gold_manifests_combined.json
```

After a resume the log gets no `EXIT=` line, so the watchdog never reports `phase: finished`:
judge completion by `produce.done` reaching 200 and no `bench.unified_bench` process, and stop
tmux `watch` yourself once measure is done.

**Retry** infra-failed repos after the main run drains: same move-aside + `benchmark.py` with the
same root, then re-measure. **Never retry result classes** (`turn_cap`, `dsml`, `timeout`,
`no_dockerfile`): retrying only the failures the agent caused would inflate its score.

## 6. Autonomy: what you may fix without asking

John wants the night used well. You may, without asking:
- restart the proxy, the watchdog, or the firewall rule (`iptables -I INPUT -i eth0 -p tcp --dport 8931 -j DROP`, same for `ip6tables`);
- prune unused build cache and dangling images as in sections 4–5;
- stop the run on credit exhaustion or a sustained upstream outage, and resume it once the cause
  is gone (except credit: John must top up first);
- move aside and retry **infra-failed** repos;
- diagnose crashes and write code fixes **locally** in `/Users/john/ratbench-runner-john`:
  failing test first, then the fix, run the affected suites, commit to the current branch with a
  conventional message. **Do not push.**

Rules for code changes:
- **Never edit `/opt/ratbench` while the run is active.** The scheduler starts a fresh child process
  per repo that imports from disk, so repos would split across two code versions while the manifest
  records one. Deploy a fix only when the run is drained or stopped: rsync as in
  `docs/overnight-ccdf-vm-preparation.md` (`--filter=':- .gitignore' --exclude=/runs_results/
  --exclude=/.env --exclude=/.venv/ --exclude=/runs/`), then `chown -R root:root /opt/ratbench`.
- A fix that changes how repos are produced or scored makes earlier rows incomparable: record which
  repos ran before and after it, and tell John.

Ask John first (PushNotification, then wait) before: changing model, provider, concurrency or
budget; deleting anything under `runs/`; touching `/opt/ratbench-remeasure` (another session's
stopped campaign); `docker system prune`, `volume prune`, or removing tagged images; pushing code;
spending beyond the 200-repo run (re-running agent failures, extra datasets).

## 7. Morning report

When John returns (or at `phase: finished`), write `/opt/ratbench/overnight-v4pro/REPORT.md` and
copy it to `docs/overnight-ccdf-run-report.md` locally:
- progress (produced / measured / 200), failure counts by class with repo lists,
- infra retries done and their outcomes, any stops and resumes with times,
- disk and memory low points and every prune performed,
- any code changes (commit hashes, which repos ran on which version),
- EBSR/ESSR from `measure/metrics.json` once measure finishes — note that `cost_usd` is not valid
  for this route (section 1) and give token totals instead.

## Reference

| Thing | Where |
|---|---|
| Run log | `/opt/ratbench/full200_v4pro_linkapi.log` (progress lines: `[scheduler/done  ] <repo>  status=…`) |
| Run folder | `/opt/ratbench/runs/claudecode-dockerfile/full200-v4pro-linkapi-<timestamp>/` |
| Watchdog | tmux `watch`; status `overnight-v4pro/watch-status.json`; history `overnight-v4pro/watch.log` |
| Proxy | tmux `proxy`; log `/opt/ratbench/proxy.log` |
| Per-repo detail | `output/<owner>/<repo>/{_meta.json,claude_stream.jsonl,claude_actions.log,run.log}` |
| Earlier v4-pro runs (do not cite) | `runs/claudecode-dockerfile/rat50-v4pro-20261109-233848` (unpinned, stopped), `rat50-v4pro-deepseek-20261110-000659` (34/50 lost to OpenRouter credit) |
