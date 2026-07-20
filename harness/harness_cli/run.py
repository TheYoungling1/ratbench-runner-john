# harness_cli/run.py
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

from .registry import load_registry, resolve_variety
from .provision import provision_agent, git_commit, symlink_glue
from . import manifest

HARNESS_ROOT = os.environ.get("HARNESS_ROOT", "/opt/harness")
RAT_ROOT = os.environ.get("RAT_ROOT", "/opt/rat_root")
AGENTS_ROOT = os.environ.get("AGENTS_ROOT", "/opt/agents")
RUNS_ROOT = os.environ.get("RUNS_ROOT", "/opt/runs")
BENCH_ROOT = os.environ.get("BENCH_ROOT", "/opt/bench")
GLUE_FILES = ["dockeragent_model.py", "rat_model.py", "repo2run_model.py",
              "claudecode_model.py", "sweagent_subprocess_model.py",
              "claudecode_dockerfile_model.py", "_claudecode_dockerfile_helpers.py"]


def output_dir(variety: str, run_name: str, now: float) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    label = run_name or "run"
    return os.path.join(RUNS_ROOT, variety, f"{label}-{stamp}")


def build_env(spec, agent_root: str, harness_commit: str, agent_commit: str) -> dict:
    env = dict(os.environ)                       # creds (OPENAI_API_KEY, ...) flow through here
    env["RAT_ROOT"] = RAT_ROOT
    env["DOCKERAGENT_ROOT"] = HARNESS_ROOT if spec.is_baseline else agent_root
    env["HARNESS_COMMIT"] = harness_commit
    env["AGENT_COMMIT"] = agent_commit
    env["RUN_VARIETY"] = spec.name
    env["RUN_AGENT_BRANCH"] = spec.branch or ""
    if spec.venv:
        env["PATH"] = os.path.join(spec.venv, "bin") + os.pathsep + env["PATH"]
    return env


def preflight() -> None:
    models = os.path.join(RAT_ROOT, "eval", "models")
    for name in GLUE_FILES:
        p = os.path.join(models, name)
        if not os.path.exists(p):     # follows symlink; catches a broken/missing link
            raise FileNotFoundError(f"glue not provisioned in rat_root: {p}")


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

    registry = load_registry(os.path.join(HARNESS_ROOT, "varieties.toml"))
    spec = resolve_variety(registry, args.variety)
    model = args.model or spec.model
    # LLM precedence: explicit --llm > variety's pinned llm > (None => runner's own default).
    # Resolve by None-check (not truthiness) so an explicit value always wins, then treat a
    # blank value from either source as unset so we never forward an empty model id.
    llm = args.llm if args.llm is not None else spec.llm
    if llm is not None:
        llm = llm.strip() or None

    symlink_glue(os.path.join(HARNESS_ROOT, "eval", "models"),
                 os.path.join(RAT_ROOT, "eval", "models"), GLUE_FILES)
    preflight()

    harness_commit = git_commit(HARNESS_ROOT)
    if spec.is_baseline:
        agent_root, agent_commit = HARNESS_ROOT, harness_commit
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

    cmd = [sys.executable, os.path.join(HARNESS_ROOT, "run_rat_benchmark.py"),
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
