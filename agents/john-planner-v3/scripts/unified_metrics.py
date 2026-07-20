#!/usr/bin/env python3
"""Unified HONEST cross-method metrics for the RATBench harness.

ONE metric authority: ``compute_essr.score_agent(root)`` recomputes every number from raw
``run_pytest_results.json`` — we NEVER trust ``rat_results.json`` or ``_result_row.status``.
This tool only *arranges* score_agent's per-repo rows + aggregate into honest comparison tables
and enriches them with run-log token/step telemetry that score_agent does not carry.

Usage:
  unified_metrics.py rat=/opt/runs/baselines/rat_run_rat_corrected \
                     arm0=/opt/runs/radical/rat_run_dockeragent \
                     repo2run=/opt/runs/baselines/rat_run_repo2run \
                     [--arm <key>=<arm>] ...

The leading NAME of each ``name=path`` arg encodes the arm (rat / repo2run / arm0 / v1g).
Where the arm cannot be inferred and no ``--arm <key>=<arm>`` override is given, per-arm
columns (steps / in-build) print ``?``/``N-A`` rather than mislabel (skeptic revision 5).

Tables (each reuses score_agent; degrades to N/A, never 0, where a signal is absent):
  T1 Headline            n, EBSR, real-success, full-pass, ÷all, ÷exec, micro, hollow
  T2 Two-stage fidelity  in_build_real (strict) / in_build_any (loose) vs eval-reproduced  (the synth gap)
  T3 Attribution         A/B/C/D/U per method
  T4 Economy             build steps (per-arm) + agent_loop tokens over the anti-hollow floor set
  T5 Intersection econ   economy over repos solved by ALL methods (gated behind status != running)
"""
from __future__ import annotations

import json
import os
import re
import sys
from glob import glob
from typing import Any, Dict, List, Optional, Tuple

# score_agent is the SOLE metric authority. attribution.py sits beside it; both must import.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compute_essr  # noqa: E402

NA = "N/A"
UNK = "?"

# ---------------------------------------------------------------------------------------------
# Arm inference + run-status (revision 4/5)
# ---------------------------------------------------------------------------------------------
KNOWN_ARMS = ("rat", "repo2run", "arm0", "v1g")


def infer_arm(name: str, manifest_variety: Optional[str], override: Optional[str]) -> Optional[str]:
    """Best-effort arm from the name key, then the manifest variety. None => caller prints '?'."""
    if override:
        return override
    n = (name or "").lower()
    if "repo2run" in n:
        return "repo2run"
    if "arm0" in n or "dockeragent" in n:
        return "arm0"
    if "v1" in n:  # v1, v1g, v1live, ...
        return "v1g"
    if "rat" in n:  # generic; checked last so it never shadows the others
        return "rat"
    v = (manifest_variety or "").lower()
    if "v1" in v:
        return "v1g"
    if "dockeragent" in v or "arm0" in v:
        return "arm0"
    if "repo2run" in v:
        return "repo2run"
    if "rat" in v:
        return "rat"
    return None


def read_manifest(root: str) -> Dict[str, Any]:
    p = os.path.join(root, "_run_manifest.json")
    if os.path.exists(p):
        try:
            return json.load(open(p)) or {}
        except Exception:
            return {}
    return {}


def run_status(root: str) -> str:
    """'running' | 'done' | 'failed' | 'unknown'.

    Newer runs carry ``_run_manifest.json`` with an explicit ``status``. Legacy complete runs
    (the baselines) have no manifest but a finished ``rat_results.json`` list => treat as done.
    Neither present => the run is still in-progress / incomplete.
    """
    man = read_manifest(root)
    if man:
        return str(man.get("status") or "unknown")
    if os.path.exists(os.path.join(root, "rat_results.json")):
        return "done"
    return "running"


def is_complete(root: str) -> bool:
    """A root is safe to economy-compare only when its run has ENDED with results on disk."""
    st = run_status(root)
    if st == "running":
        return False
    if st in ("done", "complete"):
        return True
    # failed/unknown: complete only if results were actually written.
    return os.path.exists(os.path.join(root, "rat_results.json"))


# ---------------------------------------------------------------------------------------------
# Per-repo telemetry (NOT carried by score_agent): tokens + steps from run.log / sidecars.
# ---------------------------------------------------------------------------------------------
TOKENS_RE = re.compile(r"\[Tokens\]\s*Input:\s*(\d+),\s*Output:\s*(\d+),\s*Total:\s*(\d+)")
_V1G_LEDGER_NAMES = ("action_ledger.json", "ledger.json", "env_ledger.json", "maintainer_ledger.json")
_V1G_SUMMARY_NAMES = ("agent_run_summary.json", "run_summary.json", "v1_run_summary.json")


def repo_dir_for(root: str, full_name: str) -> str:
    return os.path.join(root, "output", full_name)


def arm0_runlog_telemetry(repo_dir: str) -> Optional[Dict[str, int]]:
    """arm0 agent-loop telemetry from run.log.

    Every ``[Tokens]`` line falls inside ``[Step 1/4] Running DockerAgent`` (the agent loop);
    the synth + image-selector phases emit none, so these are agent_loop ONLY (revision 2).
    step (LLM call) == one ``[Tokens]`` line. Returns None if there is no run.log / no signal.
    """
    log = os.path.join(repo_dir, "run.log")
    if not os.path.exists(log):
        return None
    calls = 0
    total = 0
    try:
        with open(log, errors="replace") as fh:
            for line in fh:
                m = TOKENS_RE.search(line)
                if m:
                    calls += 1
                    total += int(m.group(3))
    except OSError:
        return None
    if calls == 0:
        return None
    return {"llm_calls": calls, "agent_loop_tokens": total}


def v1g_ledger_telemetry(repo_dir: str) -> Optional[Dict[str, int]]:
    """v1g step == env-mutating ledger entry. None until the A2/A3 re-run emits a ledger sidecar."""
    for name in _V1G_LEDGER_NAMES:
        p = os.path.join(repo_dir, name)
        if not os.path.exists(p):
            continue
        try:
            d = json.load(open(p))
        except Exception:
            return None
        entries = d if isinstance(d, list) else (d.get("entries") or d.get("ledger") or [])
        muts = [e for e in entries if isinstance(e, dict) and (e.get("mutating") or e.get("env_mutating") or e.get("rc") is not None)]
        return {"env_mut_steps": len(muts)}
    return None


def v1g_token_telemetry(repo_dir: str) -> Optional[Dict[str, int]]:
    """v1g agent-loop tokens from a run-summary sidecar. None until A3 persists it."""
    for name in _V1G_SUMMARY_NAMES:
        p = os.path.join(repo_dir, name)
        if not os.path.exists(p):
            continue
        try:
            d = json.load(open(p))
        except Exception:
            return None
        tu = (d.get("token_usage") or {}).get("total") or d.get("token_usage") or {}
        inp = tu.get("input_tokens")
        out = tu.get("output_tokens")
        tot = tu.get("total_tokens")
        if tot is None and (inp is not None or out is not None):
            tot = (inp or 0) + (out or 0)
        if tot is None:
            continue
        return {"agent_loop_tokens": int(tot)}
    return None


def ledger_has_rc0_testrun(repo_dir: str) -> bool:
    """v1g real in-build proxy: a ledger test-run entry with rc==0 (revision 6). False until emitted."""
    for name in _V1G_LEDGER_NAMES:
        p = os.path.join(repo_dir, name)
        if not os.path.exists(p):
            continue
        try:
            d = json.load(open(p))
        except Exception:
            return False
        entries = d if isinstance(d, list) else (d.get("entries") or d.get("ledger") or [])
        for e in entries:
            if not isinstance(e, dict):
                continue
            kind = str(e.get("kind") or e.get("action") or e.get("type") or "").lower()
            if e.get("rc") == 0 and ("test" in kind or "pytest" in str(e.get("command") or "").lower()):
                return True
    return False


def instance_in_build_passed_ge1(repo_dir: str, full_name: str) -> bool:
    """Read the adapter ``<owner>__<repo>.json`` for an ``in_build_passed_ge1`` flag (revision 1/6).

    Absent in current data — A2 adds it. Checked at top-level and under logs. False if unreadable.
    """
    inst = os.path.join(repo_dir, full_name.replace("/", "__") + ".json")
    if not os.path.exists(inst):
        return False
    try:
        d = json.load(open(inst))
    except Exception:
        return False
    if d.get("in_build_passed_ge1") is True:
        return True
    logs = d.get("logs") or {}
    return logs.get("in_build_passed_ge1") is True


# ---------------------------------------------------------------------------------------------
# In-build fidelity classification per row (revision 1 + 6), operationalized PER ARM.
# ---------------------------------------------------------------------------------------------
def inbuild_class(row: Dict[str, Any], arm: Optional[str], repo_dir: str) -> str:
    """Return 'real' | 'loose' | 'none' | 'nosignal'.

    'real'  = a genuine in-sandbox test pass.
    'loose' = a done-marker only (v1g v1_done_flag) — counts toward in_build_any, NOT _real.
    'none'  = an in-build-capable arm with no pass signal for this repo.
    'nosignal' = the arm has no in-build operationalization at all (rat/repo2run baselines).
    """
    if row.get("agent_signal_missing"):
        return "nosignal"
    vs = row.get("verification_source")
    cmd = row.get("verified_test_command") or ""
    collect_only = "--collect-only" in cmd

    if arm == "v1g":
        if vs == "v1_test_run_finalize" or instance_in_build_passed_ge1(repo_dir, row["full_name"]) or ledger_has_rc0_testrun(repo_dir):
            return "real"
        if vs == "v1_done_flag":
            return "loose"
        if row.get("test_success") and cmd and not collect_only:
            return "real"
        return "none"

    if arm == "arm0":
        # arm0 is collect-only by design; a real pass requires a non-collect verified test run.
        if row.get("test_success") and cmd and not collect_only:
            return "real"
        return "none"

    # rat / repo2run / unknown: no agent in-sandbox operationalization.
    return "nosignal"


def inbuild_summary(rows: List[Dict[str, Any]], arm: Optional[str], root: str) -> Dict[str, Any]:
    real = loose = none = nosignal = 0
    for r in rows:
        c = inbuild_class(r, arm, repo_dir_for(root, r["full_name"]))
        real += c == "real"
        loose += c == "loose"
        none += c == "none"
        nosignal += c == "nosignal"
    has_op = (real + loose + none) > 0  # arm produced at least one in-build-capable row
    return {
        "real": real,
        "any": real + loose,            # strict-real + done-only (revision 1 loose tier)
        "loose_only": loose,
        "has_signal": has_op and (real + loose) > 0,
        "nosignal": nosignal,
    }


# ---------------------------------------------------------------------------------------------
# Economy: average tokens / steps over a repo set (revision 2/5).
# ---------------------------------------------------------------------------------------------
def economy_over(root: str, arm: Optional[str], full_names: List[str]) -> Dict[str, Any]:
    """Average per-repo step + agent_loop-token telemetry over the given repos.

    Returns {'n': k, 'llm_calls': avg|None, 'env_mut_steps': avg|None, 'agent_loop_tokens': avg|None}.
    None means the telemetry is absent for this arm (=> print N/A, never a fake 0).
    """
    n = len(full_names)
    calls_vals: List[int] = []
    mut_vals: List[int] = []
    tok_vals: List[int] = []
    for fn in full_names:
        rd = repo_dir_for(root, fn)
        if arm == "arm0":
            t = arm0_runlog_telemetry(rd)
            if t:
                calls_vals.append(t["llm_calls"])
                tok_vals.append(t["agent_loop_tokens"])
        elif arm == "v1g":
            led = v1g_ledger_telemetry(rd)
            if led:
                mut_vals.append(led["env_mut_steps"])
            tok = v1g_token_telemetry(rd)
            if tok:
                tok_vals.append(tok["agent_loop_tokens"])
        # rat / repo2run / unknown: no agent telemetry => leave all empty (N/A)

    def avg(vals: List[int]) -> Optional[float]:
        return round(sum(vals) / len(vals), 1) if vals else None

    return {
        "n": n,
        "llm_calls": avg(calls_vals),
        "env_mut_steps": avg(mut_vals),
        "agent_loop_tokens": avg(tok_vals),
    }


# ---------------------------------------------------------------------------------------------
# Real-success tiers (revision 3) — both from score_agent rows, defined ONCE.
# ---------------------------------------------------------------------------------------------
def floor_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Anti-hollow floor: ebsr AND passed>=1. The economy / efficiency DENOMINATOR."""
    return [r for r in rows if r.get("ebsr") and (r.get("passed") or 0) >= 1]


def headline_solved(rows: List[Dict[str, Any]]) -> List[str]:
    """Headline real-success: passes_agent_goal (ebsr AND pass_rate>=0.8). The 'solved' predicate."""
    return [r["full_name"] for r in rows if r.get("passes_agent_goal")]


# ---------------------------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------------------------
def fmt(v: Any, width: int, prec: Optional[int] = None) -> str:
    if v is None:
        s = NA
    elif prec is not None and isinstance(v, (int, float)):
        s = f"{v:.{prec}f}"
    else:
        s = str(v)
    return s.rjust(width)


def print_table(title: str, header_cells: List[Tuple[str, int]], rows: List[List[Any]], footnotes: List[str]) -> None:
    print(f"\n{title}")
    head = "  ".join(h.rjust(w) if i else h.ljust(w) for i, (h, w) in enumerate(header_cells))
    print(head)
    print("-" * len(head))
    for r in rows:
        cells = []
        for i, ((_, w), val) in enumerate(zip(header_cells, r)):
            cells.append(str(val).ljust(w) if i == 0 else str(val).rjust(w))
        print("  ".join(cells))
    for fn in footnotes:
        print(f"  note: {fn}")


# ---------------------------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------------------------
def parse_args(argv: List[str]) -> Tuple[List[Tuple[str, str]], Dict[str, str]]:
    specs: List[Tuple[str, str]] = []
    overrides: Dict[str, str] = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--arm":
            i += 1
            if i >= len(argv) or "=" not in argv[i]:
                raise ValueError("--arm needs <key>=<arm> (e.g. --arm v1live=v1g)")
            k, v = argv[i].split("=", 1)
            overrides[k] = v
        elif "=" in a:
            name, path = a.split("=", 1)
            specs.append((name, path))
        else:
            raise ValueError(f"bad arg (need name=path or --arm key=arm): {a}")
        i += 1
    return specs, overrides


def main(argv: List[str]) -> int:
    try:
        specs, overrides = parse_args(argv[1:])
    except ValueError as e:
        print(f"error: {e}\n")
        print(__doc__)
        return 1
    if not specs:
        print(__doc__)
        return 1

    methods: List[Dict[str, Any]] = []
    for name, path in specs:
        if not os.path.isdir(path):
            print(f"  WARNING: '{name}' path missing: {path} — skipping")
            continue
        try:
            res = compute_essr.score_agent(path)
        except Exception as e:  # never crash on one bad root
            print(f"  WARNING: score_agent('{path}') failed for '{name}': {e} — skipping")
            continue
        man = read_manifest(path)
        arm = infer_arm(name, man.get("variety"), overrides.get(name))
        methods.append({
            "name": name,
            "path": path,
            "arm": arm,
            "arm_label": arm if arm else UNK,
            "status": run_status(path),
            "complete": is_complete(path),
            "res": res,
            "rows": res["rows"],
        })

    if not methods:
        print("No usable run roots. Nothing to report.")
        return 1

    print("=" * 96)
    print("UNIFIED HONEST CROSS-METHOD METRICS  (sole authority: compute_essr.score_agent — raw pytest)")
    print("=" * 96)
    for m in methods:
        print(f"  {m['name']:<12} arm={m['arm_label']:<8} status={m['status']:<8} n={m['res']['n']:<3} {m['path']}")

    # ---- T1 Headline -------------------------------------------------------------------------
    t1_rows = []
    for m in methods:
        r = m["res"]
        t1_rows.append([
            m["name"], r["n"],
            f"{r['n_ebsr']}/{r['n']}={r['EBSR_build_execute']:.2f}",
            f"{r['agent_goal_rate']:.2f}",
            r["n_agent_goal"],
            r["full_pass_repos"],
            f"{r['pass_rate_over_all']:.3f}",
            f"{r['ESSR_avg_pass_rate_official']:.3f}",
            f"{r['micro_pooled']:.3f}",
            len(r["hollow_collect_not_pass"]),
        ])
    print_table(
        "T1 HEADLINE  (real-success tier = passes_agent_goal: ebsr AND pass_rate>=0.8)",
        [("method", 12), ("n", 3), ("EBSR(build+exec)", 16), ("real>=.8", 8), ("nReal", 5),
         ("fullpass", 8), ("÷all", 6), ("÷exec", 6), ("micro", 6), ("hollow", 6)],
        t1_rows,
        ["real-success = ebsr AND pass_rate>=0.8 (agent done-gate bar); ÷all coverage-penalized, ÷exec is the paper macro."],
    )

    # ---- T2 Two-stage env fidelity (the synth gap) -------------------------------------------
    t2_rows = []
    for m in methods:
        ib = inbuild_summary(m["rows"], m["arm"], m["path"])
        eval_repro = len(floor_rows(m["rows"]))  # ebsr AND passed>=1
        if ib["has_signal"]:
            real_s = str(ib["real"])
            any_s = str(ib["any"])
            gap_s = str(ib["real"] - eval_repro)
        else:
            real_s = any_s = gap_s = NA
        t2_rows.append([m["name"], m["arm_label"], real_s, any_s, eval_repro, gap_s])
    print_table(
        "T2 TWO-STAGE ENV FIDELITY  (in-build self-report vs eval-reproduced; the synthesizer gap)",
        [("method", 12), ("arm", 8), ("in_build_real", 13), ("in_build_any", 12),
         ("eval_repro", 10), ("synth_gap", 9)],
        t2_rows,
        ["in_build_real = genuine in-sandbox pass (v1g: v1_test_run_finalize / in_build_passed_ge1 / ledger rc=0; arm0: non-collect test_success).",
         "in_build_any  = in_build_real + done-only markers (v1g v1_done_flag).  eval_repro = ebsr AND passed>=1.",
         "synth_gap = in_build_real - eval_repro (in-sandbox passes the eval replay failed to reproduce).",
         "N/A where the arm emits no in-build signal (rat/repo2run baselines; arm0 collect-only). Lights up after the v1g A2/A3 re-run."],
    )

    # ---- T3 Attribution ----------------------------------------------------------------------
    from attribution import (AGENT_BUILD_FAILURE, WEAK_VERIFICATION, SYNTHESIZER_FAILURE,
                             REPRODUCED_SUCCESS, UNATTRIBUTABLE)
    t3_rows = []
    for m in methods:
        c = m["res"]["attribution"]["counts"]
        t3_rows.append([
            m["name"],
            c[AGENT_BUILD_FAILURE], c[WEAK_VERIFICATION], c[SYNTHESIZER_FAILURE],
            c[REPRODUCED_SUCCESS], c[UNATTRIBUTABLE],
            m["res"]["attribution"]["agent_signal_missing"],
        ])
    print_table(
        "T3 ATTRIBUTION  (agent in-sandbox build+test vs eval replay)",
        [("method", 12), ("A_buildfail", 11), ("B_weakverify", 12), ("C_synthgap", 10),
         ("D_reproduced", 12), ("U_unattrib", 10), ("sig_missing", 11)],
        t3_rows,
        ["A=agent never built; B=collect-only/weak verify + eval failed; C=verified in-sandbox but eval did NOT reproduce;",
         "D=eval reproduced; U=no agent in-sandbox signal (baselines). C is structurally 0 until v1g emits a real in-build pass."],
    )

    # ---- T4 Economy (anti-hollow floor set) --------------------------------------------------
    t4_rows = []
    for m in methods:
        fr = floor_rows(m["rows"])
        names = [r["full_name"] for r in fr]
        eco = economy_over(m["path"], m["arm"], names)
        t4_rows.append([
            m["name"], m["arm_label"], len(names),
            NA if eco["llm_calls"] is None else f"{eco['llm_calls']:.1f}",
            NA if eco["env_mut_steps"] is None else f"{eco['env_mut_steps']:.1f}",
            NA if eco["agent_loop_tokens"] is None else f"{eco['agent_loop_tokens']:.0f}",
        ])
    print_table(
        "T4 ECONOMY  (per real-success repo, averaged over the anti-hollow FLOOR set: ebsr AND passed>=1)",
        [("method", 12), ("arm", 8), ("n_floor", 8), ("LLMcalls[arm0]", 14),
         ("envMutSteps[v1g]", 16), ("agentLoopTok", 12)],
        t4_rows,
        ["steps are per-arm and NOT conflated: arm0 step = one LLM call ([Tokens] line); v1g step = env-mutating ledger entry.",
         "agentLoopTok = agent-loop tokens only. arm0 synth + image_selector tokens are N/A (PARTIAL — overwritten workplace).",
         "rat/repo2run carry no agent token/step telemetry => N/A. v1g token/step N/A until the A2/A3 re-run emits them."],
    )

    # ---- T5 Intersection economy (gated behind status != running) ----------------------------
    print("\nT5 INTERSECTION ECONOMY  (economy over repos solved by ALL methods; solved = passes_agent_goal)")
    incomplete = [m for m in methods if not m["complete"]]
    if incomplete:
        print("  " + "!" * 88)
        print("  !! REFUSED: T5 requires every input run to be COMPLETE (revision 4 live-run gate).")
        for m in incomplete:
            print(f"  !!   '{m['name']}' status={m['status']} (root={m['path']}) — live/in-progress or no results on disk.")
        print("  !! Re-run T5 once all roots report status != 'running' with results written.")
        print("  " + "!" * 88)
    else:
        solved_sets = [set(headline_solved(m["rows"])) for m in methods]
        inter = set.intersection(*solved_sets) if solved_sets else set()
        inter_sorted = sorted(inter)
        print(f"  intersection size = {len(inter_sorted)} repos solved by ALL of: {', '.join(m['name'] for m in methods)}")
        if inter_sorted:
            for fn in inter_sorted:
                print(f"      - {fn}")
        else:
            print("      (empty intersection — no repo is headline-solved by every method)")
        t5_rows = []
        for m in methods:
            eco = economy_over(m["path"], m["arm"], inter_sorted)
            t5_rows.append([
                m["name"], m["arm_label"], len(inter_sorted),
                NA if eco["llm_calls"] is None else f"{eco['llm_calls']:.1f}",
                NA if eco["env_mut_steps"] is None else f"{eco['env_mut_steps']:.1f}",
                NA if eco["agent_loop_tokens"] is None else f"{eco['agent_loop_tokens']:.0f}",
            ])
        print_table(
            "   economy on the intersection set",
            [("method", 12), ("arm", 8), ("n_inter", 8), ("LLMcalls[arm0]", 14),
             ("envMutSteps[v1g]", 16), ("agentLoopTok", 12)],
            t5_rows,
            ["Same per-arm telemetry rules as T4; N/A where the arm carries no token/step signal."],
        )

    print("\n" + "=" * 96)
    print("All numbers recomputed from raw pytest by compute_essr.score_agent. rat_results.json / _result_row.status NOT trusted.")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
