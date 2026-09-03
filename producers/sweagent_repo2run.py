# producers/sweagent_repo2run.py
#
# The `sweagent_repo2run` producer: SWE-agent driven by the Repo2Run paper's baseline settings
# (arXiv:2502.13681 appendix I.2, ported in producers/sweagent_repo2run_config.yaml), which ask
# the agent for a Dockerfile at /Dockerfile instead of an in-place container.
#
# This is an ADDITIONAL arm, not a replacement for `sweagent`. The two answer different questions:
#   sweagent            — a general coding agent doing env setup in place, no artifact
#                         (measure="none", inline-only, never scored on EBSR).
#   sweagent_repo2run — the published baseline's prompt, which yields a rebuildable artifact and
#                         so gets a real EBSR/ESSR row comparable with repo2run + dockeragent.
# The delta between them is the point: how much of the score is the agent vs. the scaffolding that
# tells it exactly what to emit.
#
# The paper's Dockerfile template homes the repo at /repo via the exact `mkdir /repo` +
# `cp -r /<clone>/. /repo` shape that repo2run's re-home transform already detects, so this is a
# `measure="rehome"` lane and `rehome_dockerfile` is reused verbatim — no new transform.
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
import time

from producers.base import ProduceContext, ProducedEnv, inject_clone_pin
from producers.repo2run import rehome_dockerfile

# bench.schema is on the path via producers.base's shim (imported above).
from bench.schema import RepoSpec  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_HERE, "sweagent_repo2run_config.yaml")
RUNNER_PATH = os.path.join(_HERE, "sweagent_repo2run_runner.py")
RESULT_MARK = "__SWEAGENT_DF_RESULT__"

# The paper's template declares `git config --global --add safe.directory /repo`, i.e. this
# pipeline DOES hit git's dubious-ownership check. The re-home moves the worktree to /testbed, so
# that declaration stops covering it — and bench's contract probe classifies the container by
# `git -C /testbed rev-parse --show-toplevel`, which fails closed to non_conforming when git
# refuses the directory. Re-declare it for the new path: one line between a real EBSR and a zero.
_SAFE_DIR = "RUN git config --global --add safe.directory /testbed\n"


# ── Pure inline-score harvest (design item 7: the DGSR-vs-EBSR delta) ─────────────────────────
#
# The Repo2Run prompt tells the agent to self-validate with `pytest --collect-only -q` before it
# is done. Its own read of that command is the cheapest signal of whether the agent THOUGHT the
# repo was ready — worth keeping even though it is never scored (that's bench's fresh-container
# collect gate). Mirrors producers/repo2run.py::_harvest_inline: pure, anti-vanish, unit-tested.
_COLLECT_ACTION_RE = re.compile(r"pytest\b.*--collect-only", re.IGNORECASE)
_COLLECT_OK_RE = re.compile(r"\b\d+\s+tests?\s+collected\b", re.IGNORECASE)


def _last_collect_only_step(traj_path: str) -> dict | None:
    """The LAST `pytest --collect-only` step (by trajectory order) in one SWE-agent `.traj` file,
    or None if the file has no such step / can't be read. Anti-vanish: never raises."""
    try:
        with open(traj_path, encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
        for step in reversed(data.get("trajectory") or []):
            if _COLLECT_ACTION_RE.search(step.get("action") or ""):
                return step
        return None
    except Exception:                                    # noqa: BLE001 — anti-vanish, never propagate
        return None


def _harvest_collect_inline(trajectory_dir: str, tail_chars: int = 2000) -> dict | None:
    """The agent's own last `pytest --collect-only` result, from every `.traj` file under
    `trajectory_dir` (SWE-agent writes `sweagent_trajectories/<id>/<id>.traj`). Returns
    ``{"command", "observation_tail", "clean"}`` — `clean` is a cheap heuristic ("N tests
    collected" present, no "error" anywhere in the observation), not a scored verdict. None when
    no trajectory contains a collect-only step. Anti-vanish: a missing/malformed trajectory tree
    (or a single corrupt .traj among several) never raises; each file degrades independently."""
    try:
        paths = sorted(glob.glob(os.path.join(trajectory_dir, "**", "*.traj"), recursive=True))
    except Exception:                                    # noqa: BLE001 — anti-vanish
        return None
    last = None
    for p in paths:
        step = _last_collect_only_step(p)
        if step is not None:
            last = step
    if last is None:
        return None
    obs = last.get("observation") or ""
    return {"command": "pytest --collect-only",
            "observation_tail": obs[-tail_chars:],
            "clean": bool(_COLLECT_OK_RE.search(obs)) and "error" not in obs.lower()}


def finalize(dockerfile: str, commit: str | None, repo_url: str | None) -> tuple:
    """Pure transform: pin the clone, re-home /repo -> /testbed, re-declare safe.directory.

    Returns ``(dockerfile, pin_warning)``. The pin runs FIRST so the checkout lands right after the
    `git clone` RUN and before the `cp -r` that seeds /repo — otherwise the copy (and every install
    on top of it) is made from the default-branch HEAD instead of the dataset SHA.
    """
    warning = ""
    if commit:
        dockerfile, injected = inject_clone_pin(dockerfile, commit, repo_url)
        if not injected:
            warning = "no git clone instruction to pin"
    return rehome_dockerfile(dockerfile).rstrip() + "\n" + _SAFE_DIR, warning


def repo_clone_dir(workdir: str, full_name: str) -> str:
    """Host path to clone into. The BASENAME must be exactly `repo`.

    SWE-agent's `LocalRepoConfig` uploads a local tree to `/{repo_name}`, where `repo_name` is the
    basename of this path (`sweagent/environment/repo.py`: `Path(self.path).resolve().name`, then
    `upload(target_path=f"/{self.repo_name}")`). The Repo2Run paper's prompt hardcodes "The
    repository is cloned into /repo" and tells the agent to validate with
    `pytest /repo --collect-only -q`, and `finalize()`'s re-home moves `/repo` to `/testbed`.
    Cloning to `.../<owner>/<name>` would put the repo at `/<name>` and leave the agent working
    from a path its own instructions contradict, for the whole run."""
    return os.path.join(workdir, "input", full_name, "repo")


def _clone_pinned(repo: RepoSpec, dest: str) -> None:
    """Clone the repo locally at the dataset SHA. SWE-agent's `repo: type: local` deployment copies
    THIS tree into the container and resets it to base_commit, so the SHA has to be fetched here —
    a bare `--depth=1` clone of the default branch does not contain it."""
    if os.path.isdir(os.path.join(dest, ".git")):
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    subprocess.run(["git", "clone", "--depth=1", f"{repo.repo_url}.git", dest], check=True)
    if repo.commit:
        subprocess.run(["git", "fetch", "--depth", "1", "origin", repo.commit],
                       cwd=dest, check=True)
        subprocess.run(["git", "checkout", "--detach", repo.commit], cwd=dest, check=True)


def run_sweagent_repo2run(repo: RepoSpec, ctx: ProduceContext, *, llm: str | None,
                            num_turn: int) -> dict:
    """LIVE-ONLY: clone the repo, run SWE-agent under the paper's config in the py3.11 venv, and
    return the /Dockerfile it wrote. Raises on failure; the producer wraps it for the anti-vanish
    invariant. Never exercised by unit tests (they inject a stub runner) — it needs docker, keys,
    and the sweagent install."""
    venv_py = os.environ.get("SWEAGENT_VENV_PY", "/opt/sweagent_venv/bin/python")
    if not os.path.isfile(venv_py):
        raise RuntimeError(
            f"cannot run {venv_py!r}: SWE-agent needs its own Python 3.11 venv with sweagent "
            "installed from git (it is not installable from PyPI). Point SWEAGENT_VENV_PY at it "
            "— see README, 'SWE-agent setup'.")

    out_dir = os.path.join(ctx.workdir, "output", repo.full_name)
    os.makedirs(out_dir, exist_ok=True)
    repo_path = repo_clone_dir(ctx.workdir, repo.full_name)
    _clone_pinned(repo, repo_path)

    df_out = os.path.join(out_dir, "sweagent_repo2run.gen")
    cmd = [venv_py, RUNNER_PATH,
           "--full-name", repo.full_name,
           "--repo-path", repo_path,
           "--config", os.environ.get("SWEAGENT_REPO2RUN_CONFIG", CONFIG_PATH),
           "--out", df_out,
           "--trajectory-dir", os.path.join(out_dir, "sweagent_trajectories"),
           "--llm", str(llm),
           "--language", (repo.language or "python"),
           "--cost-limit", os.environ.get("SWEAGENT_COST_LIMIT", "2.0"),
           "--call-limit", str(num_turn)]
    if repo.commit:
        cmd += ["--commit", repo.commit]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=ctx.timeout)
    try:
        with open(os.path.join(out_dir, "sweagent_df_stdout.txt"), "w", encoding="utf-8") as fh:
            fh.write((proc.stdout or "") + "\n--- stderr ---\n" + (proc.stderr or ""))
    except OSError:
        pass                                     # telemetry must never fail a paid-for run

    payload = {}
    for line in reversed((proc.stdout or "").splitlines()):
        if line.startswith(RESULT_MARK):
            try:
                payload = json.loads(line[len(RESULT_MARK):])
            except json.JSONDecodeError:
                payload = {}
            break
    if not payload:
        raise RuntimeError(f"no result line from the sweagent runner (rc={proc.returncode}); "
                           "see sweagent_df_stdout.txt")

    dockerfile = ""
    if os.path.isfile(df_out):
        with open(df_out, encoding="utf-8", errors="replace") as fh:
            dockerfile = fh.read()
    inline = _harvest_collect_inline(os.path.join(out_dir, "sweagent_trajectories"))
    return {"dockerfile": dockerfile, "note": payload.get("note") or "",
            "agent_settings": payload.get("agent_settings") or {},
            "economy": payload.get("economy") or {},
            "exit_status": payload.get("exit_status"),
            "deploy_image_digest": payload.get("deploy_image_digest"),
            "inline": inline}


class SweAgentRepo2RunProducer:
    """SWE-agent under the Repo2Run paper's Dockerfile-emitting settings; re-homed to /testbed."""
    name = "sweagent_repo2run"
    needs_llm = True
    measurable = True
    conformance = "rehomed"

    def __init__(self, llm: str | None = None, num_turn: int = 100, runner=None):
        self.llm = llm
        self.num_turn = num_turn
        self._runner = runner   # injectable for tests; None => the real live runner

    def produce(self, repo: RepoSpec, ctx: ProduceContext) -> ProducedEnv:
        # Anti-vanish invariant (design §1): a SWE-agent crash yields ProducedEnv(status="error"),
        # never a raised exception.
        start = time.time()
        try:
            runner = self._runner or run_sweagent_repo2run
            num_turn = ctx.num_turn if ctx.num_turn is not None else self.num_turn
            res = runner(repo, ctx, llm=(ctx.llm or self.llm), num_turn=num_turn)

            economy = dict(res.get("economy") or {})
            economy.setdefault("produce_s", round(time.time() - start, 2))
            raw = res.get("dockerfile")
            note = res.get("note") or ""
            exit_status = res.get("exit_status")
            deploy_image_digest = res.get("deploy_image_digest")
            inline = res.get("inline")
            if not raw:
                # agent_settings belongs here too, not just on the success path: the paper reports
                # DGSR 26.9% for this baseline, so "no Dockerfile" is the MAJORITY outcome, and
                # dropping the effective thinking mode exactly there would leave most of the run
                # un-diagnosable — the failure rows are the ones you go back and interrogate.
                return ProducedEnv(repo=repo, dockerfile=None, status="error",
                                   note=note or "SWE-agent wrote no /Dockerfile",
                                   agent_settings=res.get("agent_settings") or {},
                                   exit_status=exit_status, deploy_image_digest=deploy_image_digest,
                                   inline=inline,
                                   conformance=self.conformance, producer_name=self.name,
                                   economy=economy)

            dockerfile, pin_warning = finalize(raw, repo.commit, repo.repo_url)
            if pin_warning:
                economy["pin_warning"] = pin_warning
                note = (note + "; " if note else "") + "unpinned clone"
            base = re.search(r"^\s*FROM\s+(\S+)", dockerfile, re.MULTILINE)
            return ProducedEnv(repo=repo, dockerfile=dockerfile,
                               agent_settings=res.get("agent_settings") or {},
                               base_image=(base.group(1) if base else None),
                               head_sha=repo.commit or "",
                               exit_status=exit_status, deploy_image_digest=deploy_image_digest,
                               inline=inline,
                               status="produced", conformance=self.conformance,
                               producer_name=self.name, economy=economy, note=note)
        except Exception as exc:                # noqa: BLE001 — boundary guard, never propagate
            return ProducedEnv(repo=repo, dockerfile=None, status="error", note=repr(exc),
                               conformance=self.conformance, producer_name=self.name,
                               economy={"produce_s": round(time.time() - start, 2)})
