#!/usr/bin/env python3
"""SweAgentSubprocessModel — runs the official RAT SWEAgentModel under a separate
Python 3.11 venv, because sweagent requires >=3.11 while the runner is 3.10.

predict() subprocesses scripts/sweagent_runner.py under /opt/sweagent_venv; that
script reuses SWEAgentModel.predict, which writes run_pytest_results.json +
run_pytest_collect_results.json into {root_path}/output/{full_name}/ for the scorers.
The child's predict() dict is returned via a `__SWEAGENT_RESULT__<json>` stdout line.
"""
import json
import os
import subprocess
import sys

import weave

_HERE = os.path.dirname(os.path.abspath(__file__))          # runner/live
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))        # <repo>
RAT_ROOT = os.environ.get("RAT_ROOT") or os.path.join(_REPO_ROOT, "rat")
sys.path[:0] = [RAT_ROOT]

from eval.common.base_model import BaseEvalModel  # noqa: E402

SWEAGENT_VENV_PY = os.environ.get("SWEAGENT_VENV_PY", "/opt/sweagent_venv/bin/python")
HARNESS_ROOT = os.environ.get("DOCKERAGENT_ROOT") or _REPO_ROOT
# sweagent_runner.py now lives beside this file (runner/live/); default to it directly.
SWEAGENT_RUNNER = os.environ.get(
    "SWEAGENT_RUNNER", os.path.join(_HERE, "sweagent_runner.py"))
RESULT_MARK = "__SWEAGENT_RESULT__"


class SweAgentSubprocessModel(BaseEvalModel):
    llm: str
    num_turn: int = 15
    cost_limit: float = 2.0

    @weave.op
    def predict(self, full_name: str, commit: str | None = None) -> dict:
        ok = {"root_path": self.root_path, "full_name": full_name}
        meta = {"requested_model": self.llm}
        # Fix #3: SWE-agent copies the LOCAL clone (root_path/input/repo/<full_name>) into its
        # deployment container and resets to env.repo.base_commit. Reachable hook: pass --commit to
        # the py3.11 runner, which pins that local clone (download_repo commit=) AND sets
        # base_commit=<commit> so SWE-agent checks out the pin — no vendored `sweagent` package edit.
        cmd = [SWEAGENT_VENV_PY, SWEAGENT_RUNNER,
               "--full-name", full_name, "--root-path", self.root_path,
               "--llm", self.llm, "--num-turn", str(self.num_turn),
               "--timeout", str(self.timeout), "--cost-limit", str(self.cost_limit)]
        if commit:
            cmd += ["--commit", commit]
        env = dict(os.environ, RAT_ROOT=RAT_ROOT)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self.timeout + 180, env=env)
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "failure_reason": "sweagent_timeout", **ok, **meta}

        out_dir = f"{self.root_path}/output/{full_name}"
        try:
            os.makedirs(out_dir, exist_ok=True)
            with open(f"{out_dir}/sweagent_runner_stdout.txt", "w") as f:
                f.write(proc.stdout or "")
            with open(f"{out_dir}/sweagent_runner_stderr.txt", "w") as f:
                f.write(proc.stderr or "")
        except Exception:
            pass

        for line in reversed((proc.stdout or "").splitlines()):
            if line.startswith(RESULT_MARK):
                try:
                    res = json.loads(line[len(RESULT_MARK):])
                    # Prefer the child's status/root_path/full_name; meta only adds requested_model.
                    return {**res, **meta}
                except Exception:
                    break
        return {"status": "error", "failure_reason": "sweagent_subprocess",
                "error": f"no result (rc={proc.returncode}); see sweagent_runner_stderr.txt",
                **ok, **meta}
