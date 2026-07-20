#!/usr/bin/env python3
"""Offline RAT benchmark runner — plugs DockerAgentModel into the RAT eval harness
without requiring Weights & Biases or Hugging Face credentials.

Usage:
    # Sequential (original behaviour):
    python run_rat_benchmark.py [--repos-json PATH] [--root-path DIR] [--limit N]
        [--offset N] [--timeout SECS] [--llm MODEL] [--num-turn N]
        [--tier all|smoke|extended] [--category CAT] [--model dockeragent|rat|repo2run]

    # Worker mode — run exactly one repo and exit (called by scheduler):
    python run_rat_benchmark.py --only owner/repo [--root-path DIR] [--llm MODEL]
        [--timeout SECS] [--num-turn N] [--model dockeragent|rat|repo2run]

    # Parallel scheduler mode:
    python run_rat_benchmark.py --concurrency N [--repos-json PATH] [--root-path DIR]
        [--tier all|smoke|extended] [--category CAT] [--limit N] [--offset N]
        [--timeout SECS] [--llm MODEL] [--num-turn N] [--model dockeragent|rat|repo2run]

    # Aggregate-only (glob existing rows → rat_results.json + report):
    python run_rat_benchmark.py --aggregate-only [--root-path DIR]

    # Optional post-run cleanup of our own images:
    python run_rat_benchmark.py --prune [--root-path DIR]
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
from glob import glob
from typing import Optional

# ── Set RAT/agent roots BEFORE importing dockeragent_model (it reads these at import time) ──
# Portable defaults: derive from THIS file's location so a fresh clone runs on any machine
# (Linux / macOS / WSL2) without editing source. Override either path via env var or, for the
# repos file, the --repos-json flag.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
def _resolve_runner_commit() -> str:
    """Runner code version for traceability. Prefer git (dev box); fall back to the
    .deployed_commit file written by deploy.sh (the VM has no git checkout); else unknown."""
    try:
        out = subprocess.run(
            ["git", "-C", _THIS_DIR, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if out:
            return out
    except Exception:
        pass
    try:
        with open(os.path.join(_THIS_DIR, ".deployed_commit")) as _f:
            v = _f.read().strip()
            if v:
                return v
    except Exception:
        pass
    return "unknown"


_RUNNER_COMMIT = _resolve_runner_commit()


def _default_rat_root(repo_dir: str) -> str:
    """Best-effort default location of the RAT harness (the unzipped 'runanything/src').

    Candidates are checked in order; the first that exists wins. If none exist we
    return the repo-local path so a fresh machine has one obvious place to unzip the
    RAT code into. Set RAT_ROOT explicitly to point anywhere else.
    """
    candidates = [
        os.path.join(repo_dir, "runanything", "src"),                   # <repo>/runanything/src
        os.path.join(os.path.dirname(repo_dir), "runanything", "src"),  # sibling of the repo
        "/tmp/runanything/src",                                         # legacy/dev default
    ]
    for cand in candidates:
        if os.path.isdir(cand):
            return cand
    return candidates[0]


os.environ.setdefault("DOCKERAGENT_ROOT", _THIS_DIR)
os.environ.setdefault("RAT_ROOT", _default_rat_root(_THIS_DIR))

# ── Load .env so API keys are available for all model paths ──────────────────
from dotenv import load_dotenv; load_dotenv()

sys.path[:0] = [os.environ["RAT_ROOT"]]               # RAT repo: scorers + the model file
from eval.common.scorers import success_scorer, pytest_pass_rate_scorer, pytest_collect_scorer
from eval.models.dockeragent_model import DockerAgentModel   # reuse the SAME predict()

PY = sys.executable  # same interpreter for child subprocesses


# ─────────────────────────────────────────────────────────────────────────────
# Model factory
# ─────────────────────────────────────────────────────────────────────────────

def _make_model(model_name: str, root_path: str, timeout: int, llm: str, num_turn: int):
    """Return the correct model instance for *model_name*.

    dockeragent  → DockerAgentModel (imported at module level; tests monkeypatch this)
    rat          → RATModel         (lazy import to avoid circular deps when not needed)
    repo2run     → Repo2RunModel    (lazy import)
    """
    if model_name == "dockeragent":
        return DockerAgentModel(root_path=root_path, timeout=timeout, llm=llm, num_turn=num_turn)
    elif model_name == "rat":
        from eval.models.rat_model import RATModel
        return RATModel(root_path=root_path, timeout=timeout, llm=llm, num_turn=num_turn,
                        save_mode="none")
    elif model_name == "repo2run":
        from eval.models.repo2run_model import Repo2RunModel
        return Repo2RunModel(root_path=root_path, timeout=timeout, llm=llm, num_turn=num_turn)
    else:
        raise ValueError(f"Unknown model name: {model_name!r}. "
                         "Choose one of: dockeragent, rat, repo2run")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_repos(path: str) -> list:
    """Handle bare list OR our subset {"repos":[...]}."""
    d = json.load(open(path))
    return d["repos"] if isinstance(d, dict) else d


def _free_disk_gb() -> float:
    """Return free disk space in GB for the filesystem hosting the CWD."""
    try:
        usage = shutil.disk_usage(".")
        return round(usage.free / (1024 ** 3), 2)
    except Exception:
        return 999.0  # unknown → don't gate


def _collect_meta(
    out: dict,
    start_ts: float,
    end_ts: float,
    pid: Optional[int] = None,
) -> dict:
    """Build the _meta.json payload from a predict() result dict."""
    return {
        "pid": pid or os.getpid(),
        "start_ts": start_ts,
        "end_ts": end_ts,
        "duration_s": round(end_ts - start_ts, 3),
        "failure_reason": out.get("failure_reason"),
        "requested_model": out.get("requested_model"),
        "base_image": out.get("base_image"),
        "head_sha": out.get("head_sha", ""),
        "free_disk_gb": _free_disk_gb(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# §10.3  Per-repo worker helper
# ─────────────────────────────────────────────────────────────────────────────

def _run_one(
    full_name: str,
    model: "DockerAgentModel",
    root_path: str,
    category: str,
    repair_mode: str = "selfverify",
    repair_rounds: int = 2,
) -> dict:
    """Run a single repo through predict(), score it, and write per-repo JSON files.

    CONTRACT
    --------
    Writes to  {root_path}/output/{full_name}/:
      _result_row.json  — merged row: predict_out + _category + three scorer fields
      _meta.json        — pid, start/end ts, duration, failure_reason, requested_model,
                          base_image, head_sha, free_disk_gb

    Returns the merged result row dict.
    Resume-skips if run_pytest_results.json already exists.
    """
    out_dir = os.path.join(root_path, "output", full_name)
    os.makedirs(out_dir, exist_ok=True)

    done_marker = os.path.join(out_dir, "run_pytest_results.json")
    row_path = os.path.join(out_dir, "_result_row.json")
    meta_path = os.path.join(out_dir, "_meta.json")

    start_ts = time.time()

    # ── Resume skip ──────────────────────────────────────────────────────────
    if os.path.exists(done_marker):
        # Reconstruct a minimal out dict for scoring from what's on disk.
        if os.path.exists(row_path):
            try:
                out = json.load(open(row_path))
                print(f"[resume] {full_name} — skipping (row already exists)", flush=True)
                return out
            except Exception:
                pass
        # Fallback: synthesise a success stub so scorers can run.
        out = {"status": "success", "root_path": root_path, "full_name": full_name}
        print(f"[resume] {full_name} — skipping (run_pytest_results.json exists)", flush=True)
    else:
        # ── Run predict() ────────────────────────────────────────────────────
        print(f"[start ] {full_name}", flush=True)
        try:
            out = model.predict(full_name)
        except Exception as exc:
            out = {
                "status": "error",
                "failure_reason": "repo_error",
                "root_path": root_path,
                "full_name": full_name,
                "_exc": str(exc),
            }
        print(f"[done  ] {full_name}  status={out.get('status')}", flush=True)

    # ── Runner-side repair loop ──────────────────────────────────────────────
    # Run whenever an eval Dockerfile exists — INCLUDING status=error repos whose
    # build failed but kept eval_build/Dockerfile (15/50 in the first run). The loop
    # can rebuild + repair those. _repair_and_rescore is defensive: it no-ops on
    # already-passing or absent-Dockerfile (e.g. no_dockerfile errors), so gating on
    # the artifact's existence is strictly broader-and-safe vs the old status!=error.
    _eval_dockerfile = os.path.join(root_path, "output", full_name, "eval_build", "Dockerfile")
    if repair_mode in ("runner", "both") and os.path.exists(_eval_dockerfile):
        from repo2run_repair_port import _repair_and_rescore  # lazy import; module is optional
        try:
            out = _repair_and_rescore(
                out=out,
                root_path=root_path,
                full_name=full_name,
                llm=model.llm if hasattr(model, "llm") else "",
                max_rounds=repair_rounds,
            )
        except Exception as _repair_exc:
            print(f"[repair] {full_name} — runner repair failed (non-fatal): {_repair_exc}",
                  flush=True)

    end_ts = time.time()

    # ── Build merged row ─────────────────────────────────────────────────────
    row = {
        **out,
        "_category": category,
        **success_scorer(out),
        **pytest_collect_scorer(out),
        **pytest_pass_rate_scorer(out),
    }

    # ── Write _result_row.json ────────────────────────────────────────────────
    try:
        json.dump(row, open(row_path, "w"), indent=2)
    except Exception as exc:
        print(f"[warn  ] {full_name} — could not write _result_row.json: {exc}", flush=True)

    # ── Write _meta.json (best-effort) ────────────────────────────────────────
    try:
        meta = _collect_meta(out, start_ts, end_ts)
        json.dump(meta, open(meta_path, "w"), indent=2)
    except Exception as exc:
        print(f"[warn  ] {full_name} — could not write _meta.json: {exc}", flush=True)

    return row


# ─────────────────────────────────────────────────────────────────────────────
# §10.4  Aggregator
# ─────────────────────────────────────────────────────────────────────────────

def _print_paper_faithful_essr(root_path: str) -> None:
    """Deterministic, paper-faithful ESSR recompute (wraps scripts/compute_essr.py).

    The official headline ``essr()`` divides by *executed* repos; the paper
    (arXiv:2604.23190) defines ESSR = Npass/Nverified with setup-failures counted as 0.
    This prints the paper-faithful ÷all metric + coverage alongside, and cross-checks the
    from-raw recompute against the stored scorer rows. Never raises — degrades to a note.
    """
    scripts_dir = os.path.join(_THIS_DIR, "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        from compute_essr import score_agent
    except Exception as exc:  # pragma: no cover - optional helper
        print(f"\n[essr] paper-faithful recompute unavailable: {exc}")
        return
    try:
        r = score_agent(root_path)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"\n[essr] paper-faithful recompute failed: {exc}")
        return

    print("\nPaper-faithful ESSR (deterministic recompute; setup-failures count as 0):")
    print(f"  coverage (exec/n)                  = {r['n_exec']}/{r['n']} = {r['coverage']}")
    print(f"  ESSR ÷all  (PAPER, Npass/Nverified) = {r['pass_rate_over_all']}   <- report this")
    print(f"  ESSR ÷exec (official headline)      = {r['ESSR_avg_pass_rate_official']}")
    print(f"  micro pooled (test-weighted)        = {r['micro_pooled']}")
    print(f"  full-pass repos (>=0.999)           = {r['full_pass_repos']}")
    if r["mismatches"]:
        print(f"  [warn] {len(r['mismatches'])} stored-row mismatch(es) vs from-raw recompute:")
        for fn, s_pr, c_pr, s_ex, c_ex in r["mismatches"][:10]:
            print(f"      {fn}: stored_pr={s_pr} raw_pr={c_pr} stored_exec={s_ex} raw_exec={c_ex}")
    else:
        print("  cross-check: all rows match stored scorer ✓")


def aggregate(root_path: str) -> list:
    """Glob all _result_row.json files, compute metrics, print report, write rat_results.json."""
    pattern = os.path.join(root_path, "output", "**", "_result_row.json")
    row_files = sorted(glob(pattern, recursive=True))

    if not row_files:
        print(f"[aggregate] No _result_row.json files found under {root_path}/output/")
        return []

    rows = []
    for path in row_files:
        try:
            rows.append(json.load(open(path)))
        except Exception as exc:
            print(f"[warn] Could not read {path}: {exc}")

    if not rows:
        print("[aggregate] All row files were unreadable.")
        return []

    n = len(rows)
    n_exec = sum(1 for x in rows if x.get("pytest_executed"))

    def mean(k: str) -> float:
        """Coverage-penalized mean over ALL rows (not-executed repos count as 0)."""
        return round(sum(x.get(k, 0) for x in rows) / n, 4)

    def essr(k: str) -> float:
        """Paper ESSR: macro mean over pytest_executed repos only.

        Mirrors generate_latex_report.py:282-284,326 (sum of per-repo rate divided
        by pytest_executed_count). Dividing by all rows (``mean``) is NOT the paper's
        metric and understates the headline by counting not-executed repos as 0.
        """
        if not n_exec:
            return 0.0
        return round(sum(x.get(k, 0) for x in rows if x.get("pytest_executed")) / n_exec, 4)

    print(f"\nn={n}  n_executed={n_exec}  build_success={mean('success')}  collect_success={mean('pytest_collect_success')}")
    print(f"ESSR (paper macro, /n_executed): pass_rate={essr('pytest_pass_rate')}  excl_code={essr('pass_rate_exclude_code_issues')}")
    print(f"coverage-penalized (/n_total)  : pass_rate={mean('pytest_pass_rate')}  excl_code={mean('pass_rate_exclude_code_issues')}")

    # Per-category breakdown
    cat_rows: dict = defaultdict(list)
    for row in rows:
        cat_rows[row.get("_category", "?")].append(row)

    print("\nPer-category breakdown:")
    print(f"  {'category':<40}  {'n':>5}  {'build_ok':>8}  {'collect_ok':>10}  {'pass_rate':>9}")
    for cat in sorted(cat_rows):
        cr = cat_rows[cat]
        cn = len(cr)

        def cmean(k: str) -> float:
            return round(sum(x.get(k, 0) for x in cr) / cn, 4)

        print(f"  {cat:<40}  {cn:>5}  {cmean('success'):>8}  {cmean('pytest_collect_success'):>10}  {cmean('pytest_pass_rate'):>9}")

    out_path = os.path.join(root_path, "rat_results.json")
    json.dump({"runner_commit": _RUNNER_COMMIT, "rows": rows}, open(out_path, "w"), indent=2)
    print(f"\n[aggregate] Wrote {out_path}  ({n} rows)")
    _print_paper_faithful_essr(root_path)
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# §9.3  Scheduler — fan-out via subprocesses
# ─────────────────────────────────────────────────────────────────────────────

def _child_cmd(full_name: str, root_path: str, llm: str, timeout: int, num_turn: int,
               repos_json: str, model: str = "dockeragent",
               repair_mode: str = "selfverify", repair_rounds: int = 2) -> list:
    """Build the argv for a worker child process."""
    # Forward the arm explicitly. The scheduler parent resolved --arm into
    # DOCKERAGENT_ENABLE_V1 before fanning out, but a child re-parses args (--arm
    # defaults to "arm0") and RE-SETS DOCKERAGENT_ENABLE_V1="0" in its own main() —
    # silently downgrading every worker to the legacy ReAct agent. Pass --arm so the
    # child keeps the parent's choice (v1 stays v1).
    if os.environ.get("DOCKERAGENT_ENABLE_GRAPH_SCHEDULER") == "1":
        arm = "v3"
    elif os.environ.get("DOCKERAGENT_ENABLE_V1") == "1":
        arm = "v1"
    else:
        arm = "arm0"
    return [
        PY, __file__,
        "--only", full_name,
        "--root-path", root_path,
        "--llm", llm,
        "--timeout", str(timeout),
        "--num-turn", str(num_turn),
        "--repos-json", repos_json,   # child needs it only if it ever falls into main(); harmless
        "--model", model,
        "--repair-mode", repair_mode,
        "--repair-rounds", str(repair_rounds),
        "--arm", arm,
    ]


def _synthesize_timeout_row(full_name: str, root_path: str, category: str) -> dict:
    """Write and return a synthetic status=timeout row for a hard-killed child."""
    out_dir = os.path.join(root_path, "output", full_name)
    os.makedirs(out_dir, exist_ok=True)
    stub: dict = {
        "status": "timeout",
        "failure_reason": "harness_timeout",
        "root_path": root_path,
        "full_name": full_name,
        "_category": category,
    }
    row = {
        **stub,
        **success_scorer(stub),
        **pytest_collect_scorer(stub),
        **pytest_pass_rate_scorer(stub),
    }
    try:
        json.dump(row, open(os.path.join(out_dir, "_result_row.json"), "w"), indent=2)
    except Exception:
        pass
    return row


def _synthesize_error_row(full_name: str, root_path: str, category: str) -> dict:
    """Write and return a synthetic status=error row for a child that exited non-zero
    without leaving a _result_row.json."""
    out_dir = os.path.join(root_path, "output", full_name)
    os.makedirs(out_dir, exist_ok=True)
    stub: dict = {
        "status": "error",
        "failure_reason": "repo_error",
        "root_path": root_path,
        "full_name": full_name,
        "_category": category,
    }
    row = {
        **stub,
        **success_scorer(stub),
        **pytest_collect_scorer(stub),
        **pytest_pass_rate_scorer(stub),
    }
    try:
        json.dump(row, open(os.path.join(out_dir, "_result_row.json"), "w"), indent=2)
    except Exception:
        pass
    return row


def _run_child(
    full_name: str,
    category: str,
    root_path: str,
    llm: str,
    timeout: int,
    num_turn: int,
    repos_json: str,
    hard_wall: int,
    model_name: str = "dockeragent",
    repair_mode: str = "selfverify",
    repair_rounds: int = 2,
) -> dict:
    """Run one child subprocess; return the result row.

    Called from inside a ThreadPoolExecutor thread — one thread per in-flight child.
    Crash-isolated: exceptions here never abort the pool.
    """
    out_dir = os.path.join(root_path, "output", full_name)
    os.makedirs(out_dir, exist_ok=True)
    log_path = os.path.join(out_dir, "run.log")
    row_path = os.path.join(out_dir, "_result_row.json")

    # Resume: if result row already written, skip.
    if os.path.exists(row_path):
        try:
            row = json.load(open(row_path))
            print(f"[scheduler/resume] {full_name} — skipping", flush=True)
            return row
        except Exception:
            pass

    cmd = _child_cmd(full_name, root_path, llm, timeout, num_turn, repos_json, model_name,
                     repair_mode, repair_rounds)
    print(f"[scheduler/start ] {full_name}  log={log_path}", flush=True)

    try:
        with open(log_path, "wb") as log_fh:
            proc = subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # own process group → clean kill
            )
        try:
            proc.wait(timeout=hard_wall)
        except subprocess.TimeoutExpired:
            # Hard kill: terminate the entire process group.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
            proc.wait()
            print(f"[scheduler/TIMEOUT] {full_name} (>{hard_wall}s) — synthesising timeout row",
                  flush=True)
            return _synthesize_timeout_row(full_name, root_path, category)
    except Exception as exc:
        print(f"[scheduler/ERROR ] {full_name} — Popen/wait failed: {exc}", flush=True)
        return _synthesize_error_row(full_name, root_path, category)

    # Child exited.  Read the row it wrote, or synthesise an error row.
    if os.path.exists(row_path):
        try:
            row = json.load(open(row_path))
            status = row.get("status", "?")
            print(f"[scheduler/done  ] {full_name}  status={status}", flush=True)
            return row
        except Exception as exc:
            print(f"[scheduler/warn  ] {full_name} — bad _result_row.json: {exc}", flush=True)

    # No row written or unreadable.
    rc = proc.returncode
    print(f"[scheduler/miss  ] {full_name}  rc={rc} — synthesising error row", flush=True)
    return _synthesize_error_row(full_name, root_path, category)


def scheduler(
    repos: list,
    root_path: str,
    llm: str,
    timeout: int,
    num_turn: int,
    repos_json: str,
    concurrency: int,
    disk_low_gb: float = 15.0,
    poll_interval: float = 30.0,
    model_name: str = "dockeragent",
    repair_mode: str = "selfverify",
    repair_rounds: int = 2,
) -> None:
    """Fan-out repos as independent child subprocesses, at most `concurrency` in flight.

    Uses ThreadPoolExecutor where each thread blocks on subprocess.run-equivalent
    (subprocess.Popen + wait).  One child dying never aborts the batch.
    """
    hard_wall = timeout + 600  # §9.1: per-child wall clock
    futures_map: dict = {}  # future → full_name

    print(f"[scheduler] {len(repos)} repos  concurrency={concurrency}  hard_wall={hard_wall}s",
          flush=True)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        pending = list(repos)  # copy so we can pop

        def _submit_next() -> bool:
            """Submit the next pending repo if disk is healthy. Returns True if submitted."""
            if not pending:
                return False
            free = _free_disk_gb()
            if free < disk_low_gb:
                print(
                    f"[scheduler/disk  ] free={free:.1f} GB < {disk_low_gb} GB — "
                    "pausing new launches until space recovered",
                    flush=True,
                )
                return False
            repo = pending.pop(0)
            fn = repo["full_name"]
            cat = repo.get("_category", "?")
            fut = pool.submit(
                _run_child,
                fn, cat, root_path, llm, timeout, num_turn, repos_json, hard_wall, model_name,
                repair_mode, repair_rounds,
            )
            futures_map[fut] = fn
            return True

        # Fill initial slots.
        launched = 0
        while launched < concurrency and pending:
            if _submit_next():
                launched += 1

        # Drain completed futures; refill slots.
        while futures_map:
            # wait() returns (done, not_done) and does NOT raise when the window
            # elapses with futures still in flight. as_completed(timeout=) raises
            # concurrent.futures.TimeoutError in that case, which previously killed
            # the whole run the moment a wave took longer than the poll window.
            done_futures, _ = wait(
                list(futures_map), timeout=poll_interval, return_when=FIRST_COMPLETED,
            )
            for fut in done_futures:
                full_name = futures_map.pop(fut)
                try:
                    fut.result()
                except Exception as exc:
                    print(f"[scheduler/except] {full_name} — {exc}", flush=True)
                # Try to submit a new job now that a slot freed.
                if pending:
                    # Retry disk gate up to 10 times with a short pause.
                    for _ in range(10):
                        if _submit_next():
                            break
                        time.sleep(30)

    print("[scheduler] All children finished.", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# §10.1  Optional scoped prune
# ─────────────────────────────────────────────────────────────────────────────

def prune_our_images() -> None:
    """Remove only Docker images whose tag starts with 'dockeragent-eval-'."""
    try:
        result = subprocess.run(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
            capture_output=True, text=True, timeout=30,
        )
        images = [
            line for line in result.stdout.splitlines()
            if line.startswith("dockeragent-eval-")
        ]
        if not images:
            print("[prune] No dockeragent-eval-* images found.")
            return
        for img in images:
            subprocess.run(["docker", "rmi", "-f", img], timeout=60)
            print(f"[prune] Removed {img}")
    except Exception as exc:
        print(f"[prune] Error during scoped prune: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Main entry points
# ─────────────────────────────────────────────────────────────────────────────

def _select_repos(repos_json: str, tier: str, category: Optional[str],
                  offset: int, limit: Optional[int]) -> list:
    """Load + filter + slice the repo list."""
    repos = load_repos(repos_json)
    if tier != "all":
        repos = [r for r in repos if r.get("_tier") == tier]
    if category is not None:
        repos = [r for r in repos if r.get("_category") == category]
    repos = repos[offset: offset + limit if limit else None]
    return repos


def worker_main(full_name: str, root_path: str, llm: str, timeout: int, num_turn: int,
                repos_json: str, model_name: str = "dockeragent",
                repair_mode: str = "selfverify", repair_rounds: int = 2) -> None:
    """--only <full_name>: run exactly one repo and exit.  Does NOT write rat_results.json."""
    os.makedirs(root_path, exist_ok=True)

    # Resolve category from the repos JSON (best-effort; "?" if not found).
    category = "?"
    try:
        for r in load_repos(repos_json):
            if r.get("full_name") == full_name:
                category = r.get("_category", "?")
                break
    except Exception:
        pass

    model = _make_model(model_name, root_path, timeout, llm, num_turn)
    _run_one(full_name, model, root_path, category,
             repair_mode=repair_mode, repair_rounds=repair_rounds)


def sequential_main(repos_json: str, root_path: str, limit: Optional[int], offset: int,
                    timeout: int, llm: str, num_turn: int, tier: str,
                    category: Optional[str], model_name: str = "dockeragent",
                    repair_mode: str = "selfverify", repair_rounds: int = 2) -> None:
    """Original sequential loop — backward compatible."""
    os.makedirs(root_path, exist_ok=True)
    try:
        json.dump({"commit": _RUNNER_COMMIT},
                  open(os.path.join(root_path, "_runner_version.json"), "w"), indent=2)
    except Exception:
        pass
    repos = _select_repos(repos_json, tier, category, offset, limit)

    if not repos:
        print(
            f"No repos match the selection "
            f"(tier={tier}, category={category}, offset={offset}, limit={limit}). Nothing to do."
        )
        return

    model = _make_model(model_name, root_path, timeout, llm, num_turn)
    for r in repos:
        _run_one(r["full_name"], model, root_path, r.get("_category", "?"),
                 repair_mode=repair_mode, repair_rounds=repair_rounds)

    aggregate(root_path)


def parallel_main(repos_json: str, root_path: str, limit: Optional[int], offset: int,
                  timeout: int, llm: str, num_turn: int, tier: str,
                  category: Optional[str], concurrency: int,
                  model_name: str = "dockeragent",
                  repair_mode: str = "selfverify", repair_rounds: int = 2) -> None:
    """Scheduler mode: fan-out to N independent child processes."""
    os.makedirs(root_path, exist_ok=True)
    try:
        json.dump({"commit": _RUNNER_COMMIT},
                  open(os.path.join(root_path, "_runner_version.json"), "w"), indent=2)
    except Exception:
        pass
    repos = _select_repos(repos_json, tier, category, offset, limit)

    if not repos:
        print(
            f"No repos match the selection "
            f"(tier={tier}, category={category}, offset={offset}, limit={limit}). Nothing to do."
        )
        return

    scheduler(
        repos=repos,
        root_path=root_path,
        llm=llm,
        timeout=timeout,
        num_turn=num_turn,
        repos_json=repos_json,
        concurrency=concurrency,
        model_name=model_name,
        repair_mode=repair_mode,
        repair_rounds=repair_rounds,
    )
    aggregate(root_path)


# ─────────────────────────────────────────────────────────────────────────────
# Arm environment helper
# ─────────────────────────────────────────────────────────────────────────────

def _apply_arm_env(arm: str) -> None:
    """Set DOCKERAGENT_ENABLE_* env vars for *arm*.

    Two supported arms plus the deferred legacy default:
      arm0 — legacy v0 ReAct (all flags off; removed when v0 is deleted)
      v1   — three-role Planner/BuildAgent/Maintainer loop
      v3   — graph-scheduled agent (v1 + dep-graph/emit/runtime-feedback/
             scheduler/runtime-pin/service-provision)
    """
    is_v1 = arm in ("v1", "v3")
    is_v3 = arm == "v3"
    os.environ["DOCKERAGENT_ENABLE_V1"] = "1" if is_v1 else "0"
    os.environ["DOCKERAGENT_ENABLE_CONTRACT_GRAPH"] = "0"
    os.environ["DOCKERAGENT_ENABLE_DEP_GRAPH"] = "1" if is_v3 else "0"
    os.environ["DOCKERAGENT_ENABLE_DEP_EMIT"] = "1" if is_v3 else "0"
    os.environ["DOCKERAGENT_ENABLE_RUNTIME_FEEDBACK"] = "1" if is_v3 else "0"
    os.environ["DOCKERAGENT_ENABLE_GRAPH_SCHEDULER"] = "1" if is_v3 else "0"
    os.environ["DOCKERAGENT_ENABLE_RUNTIME_PIN"] = "1" if is_v3 else "0"
    os.environ["DOCKERAGENT_ENABLE_SERVICE_PROVISION"] = "1" if is_v3 else "0"


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run RAT benchmark offline with DockerAgentModel.")
    parser.add_argument("--repos-json",
                        default=os.path.join(_THIS_DIR, "datasets", "rat_python_hard_subset.json"),
                        help="Path to repos JSON (bare list or {\"repos\":[...]} dict). "
                             "Defaults to the dataset shipped in this repo.")
    parser.add_argument("--root-path", default="./rat_run",
                        help="Root directory for outputs and rat_results.json.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Maximum number of repos to evaluate (after offset and filters).")
    parser.add_argument("--offset", type=int, default=0,
                        help="Skip this many repos before starting (after tier/category filters).")
    parser.add_argument("--timeout", type=int, default=7200,
                        help="Per-repo timeout in seconds (default 7200, matching the paper).")
    parser.add_argument("--llm", default="deepseek-chat",
                        help="LLM model name passed to the DockerAgent.")
    parser.add_argument("--num-turn", type=int, default=30,
                        help="Maximum agent turns per repo.")
    parser.add_argument("--tier", choices=["all", "smoke", "extended"], default="all",
                        help="Filter repos by _tier field (default: all).")
    parser.add_argument("--category", default=None,
                        help="Filter repos by _category field (default: no filter).")

    # §10.3  Worker mode
    parser.add_argument("--only", default=None, metavar="FULL_NAME",
                        help="WORKER mode: run exactly this repo and exit. "
                             "Does NOT write rat_results.json.")

    # §9.3  Scheduler mode
    parser.add_argument("--concurrency", type=int, default=None,
                        help="SCHEDULER mode: run up to N repos in parallel as subprocesses.")

    # §10.4  Aggregate-only mode
    parser.add_argument("--aggregate-only", action="store_true",
                        help="Glob existing _result_row.json files, print report, "
                             "write rat_results.json. Does not run any repos.")

    # §10.1  Optional scoped prune
    parser.add_argument("--prune", action="store_true",
                        help="Remove Docker images matching 'dockeragent-eval-*' and exit.")

    # Model selection
    parser.add_argument("--model", choices=["dockeragent", "rat", "repo2run"],
                        default="dockeragent",
                        help="Which eval model to use (default: dockeragent).")
    parser.add_argument("--arm", choices=["arm0", "v1", "v3"], default="arm0",
                        help="DockerAgent variant: 'arm0' = legacy v0 ReAct loop "
                             "(default; deferred for removal); "
                             "'v1' = three-role Planner/BuildAgent/Maintainer loop "
                             "(sets DOCKERAGENT_ENABLE_V1=1); "
                             "'v3' = graph-scheduled agent — graph DECIDEs, agent EXECUTEs, "
                             "host CERTIFIEs; v1 + dep-graph/emit/runtime-feedback/scheduler/"
                             "runtime-pin/service-provision.")

    # Repair-loop controls
    parser.add_argument("--repair-mode",
                        choices=["runner", "selfverify", "both", "off"],
                        default="selfverify",
                        help=(
                            "Repair strategy. "
                            "'runner': runner-side verbatim Repo2Run loop ON, agent self-verify OFF. "
                            "'selfverify': agent self-verify ON, runner loop OFF (current default). "
                            "'both': both ON (debug-compare only). "
                            "'off': both OFF (clean baseline). "
                            "Default: selfverify."
                        ))
    parser.add_argument("--repair-rounds", type=int, default=2,
                        help=(
                            "Maximum LLM Dockerfile repair rounds for the runner-side loop. "
                            "0 disables LLM repair (deterministic only). Default: 2."
                        ))

    args = parser.parse_args()

    # Set DOCKERAGENT_REPAIR_MODE so the adapter reads the correct value in this process
    # and in any subprocess that inherits the environment (belt-and-suspenders with --repair-mode CLI).
    os.environ["DOCKERAGENT_REPAIR_MODE"] = args.repair_mode
    # Set DOCKERAGENT_ENABLE_* so the adapter constructs DockerAgent with the correct
    # feature flags in this process and any subprocess that inherits the environment.
    _apply_arm_env(args.arm)

    # ── --prune ───────────────────────────────────────────────────────────────
    if args.prune:
        prune_our_images()
        sys.exit(0)

    # ── --aggregate-only ──────────────────────────────────────────────────────
    if args.aggregate_only:
        aggregate(args.root_path)
        sys.exit(0)

    # ── --only (worker mode) ──────────────────────────────────────────────────
    if args.only is not None:
        worker_main(
            full_name=args.only,
            root_path=args.root_path,
            llm=args.llm,
            timeout=args.timeout,
            num_turn=args.num_turn,
            repos_json=args.repos_json,
            model_name=args.model,
            repair_mode=args.repair_mode,
            repair_rounds=args.repair_rounds,
        )
        sys.exit(0)

    # ── --concurrency (scheduler mode) ───────────────────────────────────────
    if args.concurrency is not None:
        parallel_main(
            repos_json=args.repos_json,
            root_path=args.root_path,
            limit=args.limit,
            offset=args.offset,
            timeout=args.timeout,
            llm=args.llm,
            num_turn=args.num_turn,
            tier=args.tier,
            category=args.category,
            concurrency=args.concurrency,
            model_name=args.model,
            repair_mode=args.repair_mode,
            repair_rounds=args.repair_rounds,
        )
        sys.exit(0)

    # ── Sequential mode (original behaviour) ─────────────────────────────────
    sequential_main(
        repos_json=args.repos_json,
        root_path=args.root_path,
        limit=args.limit,
        offset=args.offset,
        timeout=args.timeout,
        llm=args.llm,
        num_turn=args.num_turn,
        tier=args.tier,
        category=args.category,
        model_name=args.model,
        repair_mode=args.repair_mode,
        repair_rounds=args.repair_rounds,
    )
