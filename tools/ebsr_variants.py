"""Which execution difference moves EBSR? Rebuild saved recipes once each, then run the EBSR
collect-only gate on the same image under variants of HOW it is executed.

Factors (each: the current evaluator vs the Sep 3 code at 7edc830, before the September changes
that only reached git in abd4125):
  shell    bash -c            vs bash -lc (login shell; /etc/profile resets PATH, dropping ENV venvs)
  path     no path argument   vs explicit /testbed (overrides the repo's testpaths)
  config   repo's own pytest config restored at the pinned commit  vs the agent's config as built
  addopts  PYTEST_ADDOPTS/PYTEST_PLUGINS unset  vs inherited from the image
  ensure   install pytest only if missing      vs always `pip install pytest pytest-timeout`

Every variant runs in a fresh container (ensure/restore mutate it). Read-only towards everything
else: it builds into its own tags, writes only under --out, and removes its containers and images.

  python3 tools/ebsr_variants.py --workspace /opt/ratbench-remeasure/20261110-raw-v1 \
      --rows <dir with the arm's row.json files> --out <dir> [--mode ofat|full] [--controls 20]
"""
from __future__ import annotations

import argparse
import glob
import itertools
import json
import random
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

W = "/testbed"
FACTORS = ("shell", "path", "config", "addopts", "ensure")
NOW = {"shell": "c", "path": "none", "config": "restored", "addopts": "unset", "ensure": "new"}
OLD = {"shell": "lc", "path": "testbed", "config": "agent", "addopts": "kept", "ensure": "old"}
OLD_ENSURE = ("python -m pip install -q --break-system-packages pytest pytest-timeout "
              "|| python -m pip install -q pytest pytest-timeout || true")   # 7edc830
RESTORE_FAILED_RC = 97        # measure() fails closed when the config restore fails


def name(v: dict) -> str:
    return "now" if v == NOW else "old" if v == OLD else "+".join(f"{f}={v[f]}" for f in FACTORS)


def variant_matrix(mode: str) -> list:
    if mode == "full":
        return [dict(zip(FACTORS, c)) for c in itertools.product(*((NOW[f], OLD[f]) for f in FACTORS))]
    flips = [{**base, f: other[f]} for base, other in ((NOW, OLD), (OLD, NOW)) for f in FACTORS]
    return [NOW, OLD, *flips]       # one-factor-at-a-time from each baseline


def gate_script(v: dict, cmds: dict) -> str:
    ensure = OLD_ENSURE if v["ensure"] == "old" else cmds["ensure_new"]
    steps = [f"mkdir -p {W}/logs", f"( {ensure} ) >/dev/null 2>&1"]
    if v["config"] == "restored":
        steps.append(f"( {cmds['restore']} ) || exit {RESTORE_FAILED_RC}")
    unset = "unset PYTEST_ADDOPTS PYTEST_PLUGINS; " if v["addopts"] == "unset" else ""
    path = f" {W}" if v["path"] == "testbed" else ""
    steps.append(f"cd {W}; {unset}python -m pytest --collect-only -q --disable-warnings{path}; "
                 "exit ${PIPESTATUS[0]:-$?}")
    return "\n".join(steps)


def parse_collected(out: str) -> int:
    m = re.findall(r"(\d+) tests? collected", out or "")
    if m:
        return int(m[-1])
    # -q --collect-only: one "path/to/test_x.py: N" line per file, no summary line
    return sum(int(n) for n in re.findall(r"^\S+\.py: (\d+)$", out or "", re.M))


def summarize(results: list, current_clean: dict) -> dict:
    out = {}
    for r in results:
        if "rc" not in r:
            continue
        s = out.setdefault(r["variant"], {"n": 0, "pass": 0, "fail_to_pass": [], "pass_to_fail": []})
        ok = r["rc"] in (0, 5)
        s["n"] += 1
        s["pass"] += ok
        was = current_clean.get(r["repo"])
        if ok and was is False:
            s["fail_to_pass"].append(r["repo"])
        if not ok and was is True:
            s["pass_to_fail"].append(r["repo"])
    return out


def sh(args: list, timeout: int) -> tuple:
    try:
        p = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout)
        return p.returncode, p.stdout + p.stderr, False
    except subprocess.TimeoutExpired:
        return 124, "", True


def select_repos(rows_dir: str, controls: int, seed: int = 0) -> tuple:
    rows = {}
    for p in glob.glob(f"{rows_dir}/**/row.json", recursive=True):
        r = json.load(open(p))
        rows[r["repo"].lower()] = r
    clean = {k: bool(r.get("collect_clean")) for k, r in rows.items()}
    failing = sorted(k for k, r in rows.items() if r.get("build_ok") and not r.get("collect_clean"))
    passing = sorted(k for k, ok in clean.items() if ok)
    return failing + random.Random(seed).sample(passing, min(controls, len(passing))), clean


class Runner:
    def __init__(self, a):
        self.a, self.lock = a, threading.Lock()
        self.ws = Path(a.workspace)
        sys.path[:0] = [str(self.ws / "source/bench"), str(self.ws / "source")]
        from bench.languages.python import PythonLanguage   # the campaign's frozen evaluator
        self.lang = PythonLanguage()
        self.out = Path(a.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.results_path = self.out / "results.jsonl"
        self.done = set()
        if self.results_path.exists():
            for line in self.results_path.read_text().splitlines():
                r = json.loads(line)
                self.done.add((r["repo"], r["variant"]))

    def record(self, rec: dict):
        rec["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self.lock, open(self.results_path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(json.dumps({k: rec[k] for k in rec if k != "tail"}), flush=True)

    def repo(self, repo: str, variants: list):
        todo = [v for v in variants if (repo, name(v)) not in self.done]
        if not todo or (repo, "build") in self.done:
            return
        task_path = self.ws / "inputs" / self.a.arm / repo / "task.json"
        if not task_path.exists():
            return self.record({"repo": repo, "variant": "build", "error": "no task.json"})
        task = json.loads(task_path.read_text())
        if task["state"] not in ("ready", "needs_checkout"):
            return self.record({"repo": repo, "variant": "build", "error": f"input {task['state']}"})
        tag = "ebsrvar-" + re.sub(r"[^a-z0-9_.-]", "-", repo)
        rc, log, to = sh(["docker", "build", "--progress=plain", "--no-cache", "-t", tag, "-f",
                          f"{task['input_dir']}/Dockerfile", task["context"]], 3600)
        if rc:
            return self.record({"repo": repo, "variant": "build", "error": "build_fail",
                                "timed_out": to, "tail": log[-1500:]})
        cmds = {"ensure_new": self.lang.ensure_cmd(W), "restore": self.lang.config_restore_cmd(W, task["commit"])}
        try:
            for v in todo:
                ctr = f"{tag}-{abs(hash(name(v))) % 10**8}"
                sh(["docker", "rm", "-f", ctr], 60)
                sh(["docker", "run", "-d", "--name", ctr, "--memory", self.a.memory, "--cpus",
                    self.a.cpus, "-w", W, tag, "tail", "-f", "/dev/null"], 300)
                t0 = time.time()
                rc, out, to = sh(["docker", "exec", ctr, "bash", f"-{v['shell']}", gate_script(v, cmds)],
                                 self.a.gate_timeout)
                sh(["docker", "rm", "-f", ctr], 120)
                self.record({"repo": repo, "variant": name(v), **v, "rc": rc, "pass": rc in (0, 5),
                             "timed_out": to, "collected": parse_collected(out),
                             "secs": round(time.time() - t0, 1), "tail": out[-1500:]})
        finally:
            sh(["docker", "rmi", "-f", tag], 300)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--rows", required=True, help="dir of the arm's current row.json files")
    ap.add_argument("--out", required=True)
    ap.add_argument("--arm", default="claudecode")
    ap.add_argument("--mode", choices=("ofat", "full"), default="ofat")
    ap.add_argument("--controls", type=int, default=20, help="clean repos sampled as controls")
    ap.add_argument("--repos", help="file of repos (one per line) instead of the default selection")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--memory", default="6g")
    ap.add_argument("--cpus", default="4")
    ap.add_argument("--gate-timeout", type=int, default=600)
    ap.add_argument("--summary-only", action="store_true")
    a = ap.parse_args()
    repos, clean = select_repos(a.rows, a.controls)
    if a.repos:
        repos = [l.strip().lower() for l in open(a.repos) if l.strip() and not l.startswith("#")]
    r = Runner(a)
    if not a.summary_only:
        variants = variant_matrix(a.mode)
        with ThreadPoolExecutor(a.concurrency) as ex:
            list(ex.map(lambda repo: r.repo(repo, variants), repos))
    results = [json.loads(l) for l in r.results_path.read_text().splitlines()] if r.results_path.exists() else []
    s = summarize(results, clean)
    base = sum(clean.values())
    for v, d in sorted(s.items(), key=lambda kv: -kv[1]["pass"]):
        d["projected_ebsr_count"] = base + len(d["fail_to_pass"]) - len(d["pass_to_fail"])
    (r.out / "summary.json").write_text(json.dumps(s, indent=1))
    print(f"\ncurrent rows: {base}/{len(clean)} clean. Projection = current + flips among measured repos.")
    for v, d in sorted(s.items(), key=lambda kv: -kv[1]["projected_ebsr_count"]):
        print(f"  {v:70s} pass {d['pass']:3d}/{d['n']:3d}  +{len(d['fail_to_pass']):2d} "
              f"-{len(d['pass_to_fail']):2d}  -> {d['projected_ebsr_count']}/{len(clean)}")


if __name__ == "__main__":
    main()
