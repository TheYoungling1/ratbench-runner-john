#!/usr/bin/env python3
"""Overnight watchdog for a claudecode-dockerfile run on the VM. Stdlib only.

Every --interval seconds it writes a status JSON (what the monitoring agent reads) and, with --act,
takes the few actions that cannot wait for an agent wake-up:

  * disk: prunes UNUSED Docker build cache older than a few hours when free space runs low. Never
    images, volumes, containers or run output. The stock measure stage has no disk gate of its own.
  * credit burst: when the last N finished repos all failed on a provider credit error (HTTP 402),
    stops the run. Without this a drained balance turns every remaining repo into an instant
    failure within minutes (seen 2026-11-10: 34 of 50 repos lost before anyone looked).
  * proxy: restarts the thinking proxy tmux session if nothing listens on its port.

ponytail: one process, polling, decisions from the scheduler log + per-repo files. Upgrade path: a
real supervisor if this ever has to run unattended for more than a night.

  python3 tools/ccdf_watch.py --run-name full200-v4pro --log /opt/ratbench/full200_v4pro.log --act
  python3 tools/ccdf_watch.py --run-name full200-v4pro --log ... --once      # print status, no acts
"""
import argparse
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import time

ROOT = "/opt/ratbench"
INFRA = {"credit", "rate_limit", "api_5xx", "connection"}     # retryable: not the agent's fault
_DONE = re.compile(r"\[scheduler/done\s*\]\s+(\S+)\s+status=(\S+)")


def parse_done_lines(log_text: str) -> list:
    """[(full_name, status), ...] in finish order."""
    return _DONE.findall(log_text or "")


def classify_failure(repo_dir) -> str:
    """Why a repo's produce failed. INFRA classes are safe to retry; the rest are results."""
    repo_dir = str(repo_dir)
    try:
        meta = json.load(open(os.path.join(repo_dir, "_meta.json")))
    except (OSError, ValueError):
        meta = {}
    try:
        stream = open(os.path.join(repo_dir, "claude_stream.jsonl"), errors="replace").read()
    except OSError:
        stream = ""
    m = re.search(r'"api_error_status":\s*(\d+)', stream)
    code = int(m.group(1)) if m else None
    if code == 402 or "available credits" in stream or "insufficient" in stream.lower():
        return "credit"
    if code == 429:
        return "rate_limit"
    if code and code >= 500:
        return "api_5xx"
    if re.search(r"ECONNREFUSED|Connection error|fetch failed|ETIMEDOUT", stream):
        return "connection"
    if meta.get("agent_timed_out"):
        return "timeout"
    if (meta.get("dsml_text_blocks") or 0) > 0:
        return "dsml"
    if (meta.get("llm_calls") or 0) >= 100:
        return "turn_cap"
    return "no_dockerfile" if "Dockerfile.gen" in (meta.get("note") or "") else "other"


def credit_burst(last_classes: list, n: int = 3) -> bool:
    return len(last_classes) >= n and all(c == "credit" for c in last_classes[-n:])


def _sh(cmd: str, timeout: int = 600) -> str:
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except subprocess.TimeoutExpired:
        return "timeout"


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def status(run_name: str, log_path: str, dataset_n: int) -> dict:
    roots = sorted(glob.glob(f"{ROOT}/runs/claudecode-dockerfile/{run_name}-*/"))
    root = roots[-1] if roots else None
    log = open(log_path, errors="replace").read() if os.path.exists(log_path) else ""
    done = parse_done_lines(log)
    started = len(re.findall(r"\[scheduler/start\s*\]", log))
    classes = []
    failures = {}
    for name, st in done:
        cls = "ok" if st == "success" else classify_failure(f"{root}output/{name}") if root else "?"
        classes.append(cls)
        if cls != "ok":
            failures.setdefault(cls, []).append(name)
    measuring = bool(_sh("pgrep -f 'bench.unified_bench --harvest' || true"))
    rows = len(glob.glob(f"{root}measure/**/row.json", recursive=True)) if root else 0
    du = shutil.disk_usage("/")
    return {
        "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_root": root, "dataset_n": dataset_n,
        "phase": ("finished" if re.search(r"^EXIT=", log, re.M) else
                  "measure" if measuring else "produce" if started else "not_started"),
        "exit_line": (re.findall(r"^EXIT=\S+", log, re.M) or [None])[-1],
        "produce": {"started": started, "done": len(done), "ok": classes.count("ok"),
                    "in_flight": started - len(done)},
        "failures": {k: {"n": len(v), "repos": v[-10:]} for k, v in failures.items()},
        "infra_failures": sum(len(v) for k, v in failures.items() if k in INFRA),
        "last_classes": classes[-5:],
        "measure_rows": rows,
        "disk_free_gb": round(du.free / 2**30, 1),
        "build_cache": _sh("docker system df --format '{{.Type}} {{.Size}}' | grep -i build"),
        "containers": int(_sh("docker ps -q | wc -l") or 0),
        "mem_available_gb": round(int(_sh("awk '/MemAvailable/{print $2}' /proc/meminfo") or 0)
                                  / 2**20, 1),
        "proxy_up": _port_open(8931),
        "run_tmux_alive": _sh("tmux has-session -t ccdf 2>/dev/null && echo yes") == "yes",
    }


def act(st: dict, log: list, burst_n: int) -> None:
    free = st["disk_free_gb"]
    if free < 60 and st["phase"] != "finished":
        age = "1h" if free < 35 else "3h"
        log.append(f"disk {free} GB < {'35' if age == '1h' else '60'} GB: builder prune until={age}")
        log.append(_sh(f"docker builder prune -f --filter until={age} 2>&1 | tail -1"))
        if free < 35:
            log.append(_sh("docker image prune -f 2>&1 | tail -1"))
    if credit_burst(st["last_classes"], burst_n) and st["run_tmux_alive"]:
        log.append(f"CREDIT BURST: last {burst_n} repos failed on credit — stopping the run")
        open(f"{ROOT}/overnight-v4pro/STOPPED_CREDIT", "w").write(st["time_utc"] + "\n")
        _sh("tmux kill-session -t ccdf")
        _sh("pkill -f 'runner/benchmark.py' || true")
        _sh("docker ps -q --filter name=claudecode-dockerfile-producer- | xargs -r docker rm -f")
    if not st["proxy_up"]:
        log.append("proxy down: restarting tmux 'proxy'")
        up = _sh(f"grep -E '^PROXY_UPSTREAM_HOST=' {ROOT}/.env | cut -d= -f2-") or "api.linkapi.ai"
        _sh("tmux kill-session -t proxy 2>/dev/null; tmux new -d -s proxy "
            f"'cd {ROOT} && PROXY_UPSTREAM_HOST={up} .venv/bin/python "
            f"tools/anthropic_thinking_proxy.py 2>&1 | tee -a {ROOT}/proxy.log'")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-name", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--dataset-n", type=int, default=200)
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--burst", type=int, default=3)
    ap.add_argument("--act", action="store_true")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    out_dir = f"{ROOT}/overnight-v4pro"
    os.makedirs(out_dir, exist_ok=True)
    while True:
        st = status(a.run_name, a.log, a.dataset_n)
        if a.once:
            print(json.dumps(st, indent=1))
            return
        notes = []
        if a.act:
            act(st, notes, a.burst)
        st["actions"] = notes
        json.dump(st, open(f"{out_dir}/watch-status.json", "w"), indent=1)
        with open(f"{out_dir}/watch.log", "a") as fh:
            fh.write(json.dumps({k: st[k] for k in ("time_utc", "phase", "produce", "measure_rows",
                                                    "disk_free_gb", "infra_failures", "actions")})
                     + "\n")
        if st["phase"] == "finished":
            return
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
