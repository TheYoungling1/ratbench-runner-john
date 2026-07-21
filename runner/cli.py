# runner/cli.py
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

from .registry import load_registry, resolve_variety
from .provision import provision_agent, git_commit
from . import manifest

RUNNER_ROOT = os.environ.get("RUNNER_ROOT", "/opt/runner")
REPO_ROOT = os.environ.get("REPO_ROOT") or os.path.dirname(RUNNER_ROOT)
RAT_ROOT = os.environ.get("RAT_ROOT", "/opt/rat_root")
AGENTS_ROOT = os.environ.get("AGENTS_ROOT", "/opt/agents")
RUNS_ROOT = os.environ.get("RUNS_ROOT", "/opt/runs")
BENCH_ROOT = os.environ.get("BENCH_ROOT", "/opt/bench")


def output_dir(variety: str, run_name: str, now: float) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    label = run_name or "run"
    return os.path.join(RUNS_ROOT, variety, f"{label}-{stamp}")


def build_env(spec, agent_root: str, harness_commit: str, agent_commit: str) -> dict:
    env = dict(os.environ)                       # creds (OPENAI_API_KEY, ...) flow through here
    env["RAT_ROOT"] = RAT_ROOT
    env["DOCKERAGENT_ROOT"] = RUNNER_ROOT if spec.is_baseline else agent_root
    env["HARNESS_COMMIT"] = harness_commit
    env["AGENT_COMMIT"] = agent_commit
    env["RUN_VARIETY"] = spec.name
    env["RUN_AGENT_BRANCH"] = spec.branch or ""
    if spec.venv:
        env["PATH"] = os.path.join(spec.venv, "bin") + os.pathsep + env["PATH"]
    return env


def _is_native_lane(model: str, declared_measure) -> bool:
    """True => the EFFECTIVE method has no rebuildable artifact (skip harvest, write live_scores).
    Source of truth is the producer's `measurable` flag (design §4); falls back to the variety's
    `measure` tag ('none') only when `model` has no registered producer. Best-effort: any import
    failure degrades to the declared-measure check so the hook never crashes the run."""
    try:
        _root = os.environ.get("PRODUCERS_ROOT") or os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))  # runner/cli.py -> <repo> (env-bench) / /opt (VM)
        if _root not in sys.path:
            sys.path.insert(0, _root)
        import producers
        prod = producers.PRODUCERS.get(model)
        if prod is not None:
            return not prod.measurable
    except Exception:
        pass
    return declared_measure == "none"


def _write_live_scores(out: str, spec, model: str) -> None:
    """Native-lane methods have no rebuildable artifact. Capture the method's native inline score
    (the same numbers the runner prints) to live_scores.json instead of harvesting a non-existent
    Dockerfile into a false EBSR-0. Non-fatal: any failure => score=None + a note. `model` is the
    EFFECTIVE method (spec.model, or --model override) — recorded so the marker names what ran."""
    summary = None
    try:
        if BENCH_ROOT not in sys.path:
            sys.path.insert(0, BENCH_ROOT)
        from bench.inline_score import score_agent
        r = score_agent(out)
        keys = ("n", "n_exec", "coverage", "n_ebsr", "EBSR_build_execute", "n_agent_goal",
                "agent_goal_rate", "ESSR_avg_pass_rate_official", "pass_rate_over_all",
                "n_collect_success", "collect_success_all")
        summary = {k: r.get(k) for k in keys}
    except Exception as exc:                       # noqa: BLE001 — non-fatal capture
        print(f"[bench] live-score capture failed (non-fatal): {exc}", flush=True)
    payload = {"variety": spec.name, "method": model, "measure": "none",
               "source": "inline", "note": "native-lane method; no rebuildable artifact; "
               "excluded from fresh-container EBSR/ESSR", "score": summary}
    path = os.path.join(out, "live_scores.json")
    tmp = None
    try:
        # Atomic write: a truncated live_scores.json is a false marker, so write a temp sibling
        # then os.replace (all-or-nothing).
        fd, tmp = tempfile.mkstemp(dir=out, prefix=".live_scores-", suffix=".part")
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, path)
        tmp = None
        print(f"[bench] measure=none -> wrote live_scores.json (inline score preserved); "
              f"skipped fresh-container harvest for {spec.name}", flush=True)
    except Exception as exc:                       # noqa: BLE001 — non-fatal
        print(f"[bench] could not write live_scores.json (non-fatal): {exc}", flush=True)
    finally:
        if tmp and os.path.exists(tmp):            # os.replace didn't run — clean up the temp
            try:
                os.remove(tmp)
            except OSError:
                pass


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="bench")
    p.add_argument("variety")
    p.add_argument("--tier", default="smoke")
    p.add_argument("--model", default=None)
    p.add_argument("--concurrency", type=int, default=None)
    p.add_argument("--num-turn", type=int, default=None)
    p.add_argument("--run-name", default=None)
    p.add_argument("--limit", type=int, default=None,
                   help="cap the number of repos (forwarded to the runner)")
    p.add_argument("--only", default=None, metavar="FULL_NAME",
                   help="run a single repo by full_name (forwarded to the runner)")
    p.add_argument("--llm", default=None,
                   help="LLM model id; overrides the variety's pinned llm "
                        "(e.g. deepseek/deepseek-v4-flash)")
    p.add_argument("--offset", type=int, default=None,
                   help="skip the first N repos (forwarded to the runner)")
    p.add_argument("--repos-json", default=None,
                   help="custom dataset path (forwarded to the runner)")
    p.add_argument("--repair-mode", default=None,
                   choices=["runner", "selfverify", "both", "off"],
                   help="forwarded to the runner; unset = runner's default (runner)")
    args = p.parse_args(argv)

    registry = load_registry(os.path.join(REPO_ROOT, "varieties.toml"))
    spec = resolve_variety(registry, args.variety)
    model = args.model or spec.model
    # Measure lane is derived from the EFFECTIVE model's producer (--model overrides spec.model),
    # so `bench radical --model rat` correctly skips the harvest. Falls back to the variety's
    # `measure` tag only when the model has no registered producer (design §4 / FIX 1).
    native = _is_native_lane(model, spec.measure)
    # LLM precedence: explicit --llm > variety's pinned llm > (None => runner's own default).
    # Resolve by None-check (not truthiness) so an explicit value always wins, then treat a
    # blank value from either source as unset so we never forward an empty model id.
    llm = args.llm if args.llm is not None else spec.llm
    if llm is not None:
        llm = llm.strip() or None

    harness_commit = git_commit(REPO_ROOT)
    if spec.is_baseline:
        agent_root, agent_commit = RUNNER_ROOT, harness_commit
    else:
        agent_root = os.path.join(AGENTS_ROOT, spec.name)
        agent_commit = provision_agent(agent_root, spec.branch)

    # Provenance vars (consumed by build_env and the manifest helper).
    os.environ["RUN_VARIETY"] = spec.name
    os.environ["RUN_AGENT_BRANCH"] = spec.branch or ""
    os.environ["AGENT_COMMIT"] = agent_commit
    os.environ["HARNESS_COMMIT"] = harness_commit

    out = output_dir(spec.name, args.run_name, time.time())
    env = build_env(spec, agent_root, harness_commit, agent_commit)

    # bench owns the run-level manifest: written here at run START so it exists
    # regardless of which runner exit path executes (--only worker mode, sequential,
    # or the --concurrency scheduler). Status updated to done/failed after the run.
    manifest.write_manifest(out, **manifest.manifest_fields_from_env(
        model=model, tier=args.tier, num_turn=args.num_turn,
        concurrency=args.concurrency, status="running"))

    cmd = [sys.executable, os.path.join(RUNNER_ROOT, "benchmark.py"),
           "--model", model, "--tier", args.tier, "--root-path", out]
    if args.concurrency is not None:
        cmd += ["--concurrency", str(args.concurrency)]
    if args.num_turn is not None:
        cmd += ["--num-turn", str(args.num_turn)]
    if args.limit is not None:
        cmd += ["--limit", str(args.limit)]
    if args.only is not None:
        cmd += ["--only", args.only]
    if llm is not None:
        cmd += ["--llm", llm]
    if args.offset is not None:
        cmd += ["--offset", str(args.offset)]
    if args.repos_json is not None:
        cmd += ["--repos-json", args.repos_json]
    if args.repair_mode is not None:
        cmd += ["--repair-mode", args.repair_mode]
    rc = subprocess.run(cmd, env=env).returncode
    # Measure stage: rebuild each emitted Dockerfile from a clean context and re-run
    # pytest for reproducible EBSR/ESSR, distinct from the runner's live inline scoring.
    # Non-fatal: a measure failure never changes the run's own exit status.
    if rc == 0 and not os.environ.get("BENCH_SKIP_MEASURE"):
        if native:
            # Native-lane method (rat/sweagent/live-claude): no rebuildable Dockerfile. Skip the
            # fresh-container harvest (it would emit a shadowing EBSR-0 that buries the real score)
            # and record the method's OWN inline score to live_scores.json. (design §3 methods 5-6.)
            _write_live_scores(out, spec, model)
        else:
            try:
                subprocess.run(
                    [sys.executable, "-m", "bench.unified_bench",
                     "--harvest", f"{spec.name}={os.path.join(out, 'output')}",
                     "--out", os.path.join(out, "measure"),
                     "--concurrency", str(args.concurrency or 4)],
                    cwd=BENCH_ROOT, env=env, check=False)
            except Exception as exc:
                print(f"[bench] measure stage failed (non-fatal): {exc}", flush=True)
    manifest.update_status(out, "done" if rc == 0 else "failed")
    return rc


if __name__ == "__main__":
    import sys
    sys.exit(main())
