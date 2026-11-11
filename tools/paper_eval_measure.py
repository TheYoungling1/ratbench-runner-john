"""Measure saved Dockerfiles with the PAPER's evaluator (bench at d7d7fb3), under both shells.

The paper's Claude Code rows for rat50 + envbench were measured by the clean d7d7fb3 harness
(`bash -lc`); rat50new on Sep 23, after `bash -c` was deployed. This driver imports an
UNMODIFIED d7d7fb3 `bench` package (pass its checkout via --bench-root) and, per repo, builds once
and measures twice on that image: `-lc` then `-c`. Rows land in <out>/{lc,c}/<agent>/<owner>/<repo>/row.json,
the same layout unified_bench writes, so `--aggregate-only` and the archive scripts read them.

Two deliberate deviations from d7d7fb3, both only reachable on a pathological repo:
  * docker exec calls with no timeout get --exec-timeout (d7d7fb3's collect gate had none, so a repo
    that hangs at import, e.g. NovaSky-AI/SkyRL, would stall the run forever);
  * the image is kept between the two shell passes (d7d7fb3 removes it after each measure()).

  python3 tools/paper_eval_measure.py --bench-root /opt/paper-eval/d7d7fb3/bench \
      --harvest paperev-v4pro=/opt/ratbench/runs/claudecode-dockerfile/<run>/output --out <dir>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict

SHELLS = ("lc", "c")
JUNIT_PATH = "/testbed/logs/junit.xml"     # d7d7fb3 python: measure() cats exactly this


def make_docker(base_cls, exec_timeout: int):
    class PaperDocker(base_cls):
        keep_image = False
        last_junit = None          # what measure() read back as the JUnit report, for saving

        def exec(self, name, argv, timeout=None):
            res = super().exec(name, argv, timeout=exec_timeout if timeout is None else timeout)
            if argv and argv[-1].startswith(f"cat {JUNIT_PATH}"):
                self.last_junit = res[1]
            return res

        def rm(self, name, tag):
            if self.keep_image:
                os.system(f"docker rm -f {name} >/dev/null 2>&1")
            else:
                super().rm(name, tag)
    return PaperDocker


def shell_argv(shell: str):
    return lambda cmd: ["bash", f"-{shell}", cmd]


def row_path(out: str, shell: str, agent: str, repo: str) -> str:
    return os.path.join(out, shell, agent, *repo.split("/"), "row.json")


def write_row(path: str, row) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(asdict(row), f, indent=2, default=list)
    os.replace(path + ".tmp", path)


def measure_both(env, out: str, *, measure_mod, docker, error_row, shells=SHELLS) -> list:
    """Each shell on one build; the test run's JUnit report is saved beside its row.json.
    Resumable: a shell whose row.json exists is skipped."""
    todo = [s for s in shells if not os.path.exists(row_path(out, s, env.agent, env.repo.full_name))]
    for i, shell in enumerate(todo):
        measure_mod._sh = shell_argv(shell)            # module-global lookup inside measure()
        docker.keep_image = i < len(todo) - 1
        docker.last_junit = None
        try:
            row = measure_mod.measure(env, docker=docker)
        except Exception as e:                       # same anti-vanish rule as d7d7fb3 run_one
            row = error_row(env, e)
        path = row_path(out, shell, env.agent, env.repo.full_name)
        write_row(path, row)
        if docker.last_junit:
            with open(os.path.join(os.path.dirname(path), "junit.xml"), "w") as f:
                f.write(docker.last_junit)
    return todo


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bench-root", required=True, help="the d7d7fb3 checkout's bench/ dir")
    ap.add_argument("--harvest", required=True, help="agent=<run>/output[,agent2=...]")
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", help="file of owner/repo names to measure (one per line)")
    ap.add_argument("--exec-timeout", type=int, default=1800)
    ap.add_argument("--shells", default="lc,c", help="comma list of lc and/or c")
    a = ap.parse_args()
    sys.path.insert(0, a.bench_root)
    import bench.measure as measure_mod
    from bench.docker_client import SubprocessDocker
    from bench.harvest import discover
    from bench.schema import MeasureRow
    if measure_mod._sh("x") != ["bash", "-lc", "x"]:
        sys.exit(f"{a.bench_root} is not the d7d7fb3 evaluator (its _sh is not bash -lc)")

    def error_row(env, e):
        m = env.meta or {}
        return MeasureRow(agent=env.agent, repo=env.repo.full_name, env_status=env.status,
                          build_ok=False, executed=False, ebsr=False, status="measure_error",
                          tokens_in=m.get("tokens_in"), tokens_out=m.get("tokens_out"),
                          llm_calls=m.get("llm_calls"), turns_used=m.get("turns_used"),
                          cost_usd=m.get("cost_usd"), produce_s=m.get("produce_s"),
                          meta=dict(m, error=repr(e)))

    roots = dict(kv.split("=", 1) for kv in a.harvest.split(","))
    envs = discover(roots)
    if a.only:
        keep = {l.strip().lower() for l in open(a.only) if l.strip()}
        envs = [e for e in envs if e.repo.full_name.lower() in keep]
    Docker = make_docker(SubprocessDocker, a.exec_timeout)
    shells = tuple(a.shells.split(","))
    if not shells or set(shells) - set(SHELLS):
        sys.exit(f"--shells must be a comma list of {SHELLS}")

    # ponytail: _sh is a module global switched per call, so one process measures serially.
    # Parallelism = one process per --only shard. Upgrade: thread the shell through measure().
    for env in envs:
        done = measure_both(env, a.out, measure_mod=measure_mod, docker=Docker(),
                            error_row=error_row, shells=shells)
        print(f"[paper-eval] {env.repo.full_name} shells={done or 'resumed'}", flush=True)


if __name__ == "__main__":
    main()
