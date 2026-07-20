#!/usr/bin/env python3
"""Consolidate one RAT/baseline run into a per-task case_study.json holding
EVERYTHING useful for the paper: metrics (ESSR/EBSR/attribution), tokens,
timing/phase-breakdown, the synthesized final Dockerfile + ordered steps,
and provenance. Post-hoc, non-destructive, model-agnostic.

Usage: python3 consolidate_run.py <RUN_DIR> [--model rat] [--llm deepseek/...] [--dataset path.json]
Writes: <RUN>/output/<owner>/<repo>/case_study.json  (one per task)
        <RUN>/case_studies.jsonl                     (one compact line per task)
        <RUN>/case_studies_summary.json              (aggregate table)
"""
import json, os, re, sys, argparse

sys.path.insert(0, "/opt/harness/scripts")
import compute_essr  # noqa

def load(p, default=None):
    try:
        return json.load(open(p))
    except Exception:
        return default

def read_text(p):
    try:
        return open(p, errors="replace").read()
    except Exception:
        return ""

# ---- classify a shell command as env-mutating "recipe" vs exploration ----
RECIPE_RE = re.compile(
    r"\b(pip3?\s+install|python3?\s+-m\s+pip\s+install|uv\s+pip\s+install|poetry\s+(install|add)|"
    r"pipenv\s+install|conda\s+(install|env)|apt(-get)?\s+(install|update)|dpkg\s+-i|"
    r"python3?\s+setup\.py|make\b|cmake\b|\./configure|update-alternatives|ln\s+-s|"
    r"npm\s+(install|ci)|yarn\s+install|wget\b|curl\s+.*-[oO]\b|git\s+clone|bash\s+.*install|"
    r"export\s+\w+=|mkdir\b|pip\s+download|gem\s+install|cargo\s+build)", re.I)
EXPLORE_RE = re.compile(r"^\s*(ls|cat|head|tail|find|pwd|grep|which|tree|wc|file|stat|echo)\b", re.I)

def is_recipe(cmd):
    return bool(RECIPE_RE.search(cmd)) and not (EXPLORE_RE.match(cmd) and not RECIPE_RE.search(cmd))

def _read_image_size(tdir):
    """Per-repo image_size.json sidecar (written by measure_images.py / inject_image_sizes.py).
    Durable: survives re-consolidation. Returns {} when absent."""
    p = os.path.join(tdir, "image_size.json")
    if os.path.isfile(p):
        try:
            return json.load(open(p))
        except Exception:
            return {}
    return {}


def synth_dockerfile(base_image, inner_cmds):
    lines = [f"FROM {base_image or 'python:3.10-slim'}", "WORKDIR /repo"]
    recipe = []
    for c in inner_cmds:
        cmd = (c.get("command") or "").strip()
        if not cmd:
            continue
        if is_recipe(cmd):
            recipe.append(cmd)
            lines.append(f"RUN {cmd}")
    return "\n".join(lines) + "\n", recipe

def extract_base_image(trajectory, meta):
    if meta and meta.get("base_image"):
        return meta["base_image"]
    for msg in (trajectory or []):
        m = re.search(r"container\s+([\w][\w:.\-/]+)", str(msg.get("content", "")))
        if m:
            return m.group(1).rstrip(".")
    return None

def phase_timing(meta, tool_stats, outer_cmds, inner_cmds):
    ts = tool_stats or {}
    def tool_time(name): return (ts.get(name) or {}).get("total_time", 0.0)
    llm_s = sum(c["LLM_time"] for c in (outer_cmds or []) if "LLM_time" in c)
    install_s = sum(c.get("time", 0.0) for c in (inner_cmds or []) if is_recipe(c.get("command") or ""))
    explore_s = sum(c.get("time", 0.0) for c in (inner_cmds or []) if not is_recipe(c.get("command") or ""))
    test_s = tool_time("run-pytest")
    collect_s = tool_time("run-pytest-collect")
    # bottleneck over the three real cost centers (no double-count of the pytest run)
    core = {"llm_time_s": round(llm_s, 2), "install_exec_s": round(install_s, 2),
            "test_exec_s": round(test_s, 2)}
    bottleneck = max(core, key=core.get) if any(core.values()) else None
    return {"duration_s": (meta or {}).get("duration_s"), **core,
            "collect_s": round(collect_s, 2), "explore_exec_s": round(explore_s, 2),
            "bottleneck": bottleneck}

def compact_steps(inner_cmds):
    steps = []
    for i, c in enumerate(inner_cmds or []):
        steps.append({"seq": i, "command": (c.get("command") or "")[:400],
                      "returncode": c.get("returncode"), "time_s": round(c.get("time", 0.0), 2),
                      "is_recipe": is_recipe(c.get("command") or "")})
    return steps

def resolve_head_sha(tdir, meta):
    if meta and meta.get("head_sha"):
        return meta["head_sha"]
    sha_txt = os.path.join(tdir, "sha.txt")
    if os.path.isfile(sha_txt):
        s = read_text(sha_txt).strip()
        if s:
            return s.split()[0]
    repo_dir = tdir.replace("/output/", "/input/repo/")
    if os.path.isdir(os.path.join(repo_dir, ".git")):
        import subprocess
        try:
            out = subprocess.run(["git", "-C", repo_dir, "rev-parse", "HEAD"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
            return out or None
        except Exception:
            return None
    return None

_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)

def extract_reasoning_trace(trajectory, cap=6000):
    """Per-assistant-turn reasoning: the model's <think> content paired with the
    action it took that turn. Populated for reasoning models (MiniMax); empty for
    non-reasoning models (deepseek). Full raw reasoning always remains in trajectory.json."""
    trace = []
    turn = 0
    for m in trajectory or []:
        if m.get("role") != "assistant":
            continue
        turn += 1
        c = m.get("content", "") or ""
        mt = _THINK_RE.search(c)
        reasoning = mt.group(1).strip() if mt else ""
        if not reasoning:
            continue
        action = _THINK_RE.sub("", c).strip()
        trace.append({
            "turn": turn,
            "reasoning_chars": len(reasoning),
            "reasoning": reasoning[:cap],
            "reasoning_truncated": len(reasoning) > cap,
            "action": action[:500],
        })
    return trace

def find_emitted_dockerfile(task_dir):
    # repo2run/ccdf/radical emit a real Dockerfile somewhere under the task dir
    for root, _, files in os.walk(task_dir):
        if "/input/repo/" in root or "/utils/repo/" in root:
            continue
        for f in files:
            if f == "Dockerfile" and "output" in root:
                return os.path.join(root, f)
    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--model", default=None)
    ap.add_argument("--llm", default=None)
    ap.add_argument("--dataset", default=None)
    args = ap.parse_args()
    RUN = args.run_dir.rstrip("/")
    run_name = os.path.basename(RUN)

    size_by_name = {}
    if args.dataset:
        ds = load(args.dataset, [])
        entries = ds.values() if isinstance(ds, dict) else ds
        for r in entries:
            if isinstance(r, dict) and r.get("full_name"):
                size_by_name[r["full_name"]] = r.get("size")

    # per-repo metrics from compute_essr
    scored = compute_essr.score_agent(RUN)
    rows_by_name = {r["full_name"]: r for r in scored.get("rows", [])}
    runner_commit = (load(os.path.join(RUN, "rat_results.json"), {}) or {}).get("runner_commit")

    out_root = os.path.join(RUN, "output")
    tasks = []
    for owner in sorted(os.listdir(out_root)) if os.path.isdir(out_root) else []:
        odir = os.path.join(out_root, owner)
        if not os.path.isdir(odir):
            continue
        for repo in sorted(os.listdir(odir)):
            tdir = os.path.join(odir, repo)
            if not os.path.isfile(os.path.join(tdir, "_result_row.json")):
                continue
            full = f"{owner}/{repo}"
            tasks.append((full, tdir))

    jsonl = open(os.path.join(RUN, "case_studies.jsonl"), "w")
    summary = []
    for full, tdir in tasks:
        rr = load(os.path.join(tdir, "_result_row.json"), {}) or {}
        usage = load(os.path.join(tdir, "usage.json"), {}) or {}
        meta = load(os.path.join(tdir, "_meta.json"), {}) or {}
        tool_stats = load(os.path.join(tdir, "tool_stats.json"), {}) or {}
        inner = load(os.path.join(tdir, "inner_commands.json"), []) or []
        outer = load(os.path.join(tdir, "outer_commands.json"), []) or []
        traj = load(os.path.join(tdir, "trajectory.json"), []) or []
        summ = load(os.path.join(tdir, "agent_run_summary.json"), {}) or {}
        row = rows_by_name.get(full, {})
        head_sha = resolve_head_sha(tdir, meta)

        base_image = extract_base_image(traj, meta) or summ.get("base_image")
        _img = _read_image_size(tdir)
        emitted = find_emitted_dockerfile(tdir)
        if emitted:
            dockerfile = read_text(emitted)
            _, recipe = synth_dockerfile(base_image, inner)
            df_source = "emitted"
        else:
            dockerfile, recipe = synth_dockerfile(base_image, inner)
            df_source = "synthesized_from_inner_commands"
        if df_source == "emitted":
            m = re.search(r"^\s*FROM\s+(\S+)", dockerfile, re.I | re.M)
            if m:
                base_image = m.group(1)  # the real eval base, not the agent-runner image

        tot = (usage.get("total_tokens") or (summ.get("token_usage") or {}).get("total_tokens")) or 0
        turns = usage.get("llm_turns") or summ.get("agent_turns") or 0
        case = {
            "schema_version": "1.1",
            "task": {
                "full_name": full, "language": rr.get("language"),
                "size": size_by_name.get(full), "head_sha": head_sha,
                "run_name": run_name, "model": args.model or summ.get("source"), "llm": args.llm,
                "llm_model": (args.llm.split("/")[-1] if args.llm else None),
                "runner_commit": runner_commit,
            },
            "outcome": {
                "status": rr.get("status"),
                "ebsr": bool(row.get("ebsr")),
                "essr": row.get("pass_rate"),
                "essr_exclude_code_issues": row.get("pass_rate_excl"),
                "pass_rate_over_all": rr.get("pytest_pass_rate"),
                "full_pass": (row.get("pass_rate") == 1.0),
                "passes_agent_goal": row.get("passes_agent_goal"),
                "attribution": row.get("attribution"),
            },
            "tests": {
                "collect_success": rr.get("pytest_collect_success"),
                "executed": rr.get("pytest_executed"),
                "total": rr.get("pytest_total_tests"), "passed": rr.get("pytest_passed"),
                "failed": rr.get("pytest_failed"), "errors": rr.get("pytest_errors"),
                "eff_total_verified": row.get("eff_total"),
                "timeout_unverified": rr.get("pytest_timeout_unverified"),
                "error_breakdown": rr.get("error_breakdown"),
                "parse_method": row.get("parse_method"),
            },
            "environment": {
                "base_image": base_image, "in_container": True,
                "dockerfile_source": df_source,
                "final_dockerfile": dockerfile,
                "recipe_commands": recipe,
                "pip_installs": [c for c in recipe if re.search(r"pip.*install|uv pip", c, re.I)],
                "apt_installs": [c for c in recipe if re.search(r"apt", c, re.I)],
                "total_inner_commands": len(inner),
                "image_size_human": _img.get("size_human"),
                "image_size_bytes": _img.get("size_bytes"),
                "image_size_source": _img.get("source"),
            },
            "cost": {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": tot, "llm_turns": turns,
                "tokens_per_turn": round(tot / turns) if turns else None,
                "agent_cost_usd": summ.get("agent_cost_usd"),
                "token_source": ("usage.json" if usage.get("total_tokens")
                                 else ("agent_run_summary.json" if tot else None)),
            },
            "in_sandbox": {
                "source": summ.get("source"),
                "configuration_success": summ.get("configuration_success"),
                "best_result": summ.get("best_in_sandbox_test_result"),
                "verified_test_command": summ.get("verified_test_command"),
            },
            "timing": phase_timing(meta, tool_stats, outer, inner),
            "steps": compact_steps(inner),
            "reasoning_trace": extract_reasoning_trace(traj),
            "provenance": {
                "start_ts": meta.get("start_ts"), "end_ts": meta.get("end_ts"),
                "free_disk_gb": meta.get("free_disk_gb"), "pid": meta.get("pid"),
                "failure_reason": meta.get("failure_reason"),
                "artifacts": {k: (os.path.join("output", full, k) if os.path.isfile(os.path.join(tdir, k)) else None)
                              for k in ["run.log", "trajectory.json", "inner_commands.json",
                                        "outer_commands.json", "run_pytest_results.json",
                                        "junit_report.xml", "usage.json", "tool_stats.json"]},
            },
        }
        json.dump(case, open(os.path.join(tdir, "case_study.json"), "w"), indent=1)
        # compact jsonl line (drop big fields)
        line = {**case, "steps": f"<{len(case['steps'])} steps>",
                "environment": {**case["environment"], "final_dockerfile": f"<{len(dockerfile)} chars>"}}
        jsonl.write(json.dumps(line) + "\n")
        summary.append({"full_name": full, "size": case["task"]["size"],
                        "model": case["task"]["model"],
                        "ebsr": case["outcome"]["ebsr"], "essr": case["outcome"]["essr"],
                        "attribution": case["outcome"]["attribution"],
                        "total_tokens": tot, "llm_turns": turns,
                        "agent_cost_usd": summ.get("agent_cost_usd"),
                        "duration_s": case["timing"]["duration_s"],
                        "bottleneck": case["timing"]["bottleneck"],
                        "image_size_bytes": _img.get("size_bytes")})
    jsonl.close()
    _isz = [s["image_size_bytes"] for s in summary if s.get("image_size_bytes")]
    _img_agg = {
        "n_measured": len(_isz),
        "total_bytes": sum(_isz) if _isz else 0,
        "mean_gb": round(sum(_isz) / len(_isz) / 1e9, 3) if _isz else None,
        "max_gb": round(max(_isz) / 1e9, 3) if _isz else None,
        "min_gb": round(min(_isz) / 1e9, 3) if _isz else None,
    }
    json.dump({"run_name": run_name, "n_tasks": len(summary),
               "image_size_aggregate": _img_agg, "tasks": summary},
              open(os.path.join(RUN, "case_studies_summary.json"), "w"), indent=1)
    print(f"consolidated {len(summary)} task(s) -> case_study.json each + case_studies.jsonl + summary")
    for s in summary:
        print(f"  {s['full_name']:28} ebsr={s['ebsr']} essr={s['essr']} "
              f"tok={s['total_tokens']} turns={s['llm_turns']} {s['bottleneck']}")

if __name__ == "__main__":
    main()
