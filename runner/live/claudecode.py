#!/usr/bin/env python3
"""ClaudeCodeModel — runs the Claude Code CLI as an autonomous env-setup agent.

RAT env-setup task model: the agent mutates a live container in place (installs the
deps/system packages the repo needs so its test suite runs), then RAT's pytest tools
run in the same container and the result JSONs are copied out for the scorers.

Mirrors dockeragent_model.py's subprocess-driven Docker pattern. The base image
(claude-runner) pre-installs Node + the Claude Code CLI so per-repo cost is just the
repo's own deps. Auth: CLAUDE_CODE_OAUTH_TOKEN (Pro/Max subscription) or
ANTHROPIC_API_KEY, passed through into the container.
"""
import json
import os
import sys
import time
import subprocess

import weave

# RAT_ROOT is set by bench, the runner, and the tests (the runner also puts it on
# sys.path before importing this module). The fallback resolves the RAT tree at
# <repo>/rat from this file's home at runner/live/ (repo root is two dirs up).
RAT_ROOT = os.environ.get("RAT_ROOT") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "rat"
)
sys.path[:0] = [RAT_ROOT]

from libkit.command import init_output_and_repo, download_repo   # RAT repo
from eval.common.base_model import BaseEvalModel                 # RAT repo
from eval.common.utils import TimeoutException                   # RAT repo
from producers._claudecode_helpers import (                             # noqa: E402
    agent_env, budget_flags, has_auth, run_claude_capped, settled_cost, stop_agent,
    summarize_stream,
)

RP = f"{RAT_ROOT}/libkit/tools/run_pytest.py"
RPC = f"{RAT_ROOT}/libkit/tools/run_pytest_collect.py"
PYTEST_TIMEOUT = int(os.environ.get("RAT_PYTEST_TIMEOUT", "1800"))
# Time reserved for the post-agent pytest phase, so the agent budget isn't starved
# by PYTEST_TIMEOUT (a cap, not an expected duration). Override via env.
PYTEST_RESERVE = int(os.environ.get("CLAUDE_PYTEST_RESERVE", "600"))
W = "/testbed"

SETUP_PROMPT = (
    "You are configuring a Python repository so its EXISTING test suite can run. "
    "The repository is at /testbed (your working directory). "
    "Install ALL Python dependencies and any required system packages so that pytest "
    "can collect and run the tests. Install packages into the SYSTEM Python using "
    "`sudo pip install ...` and use `sudo apt-get install -y ...` for system libraries "
    "— do NOT create a virtualenv (the grader runs the system python3). "
    "You may edit configuration files and create files. "
    "DO NOT modify, add, or delete any test files. DO NOT run the test suite yourself. "
    "When the environment is ready, stop."
)

# The Claude Code CLI accepts model aliases (sonnet/opus/haiku) or full IDs.
_CLAUDE_PREFIXES = ("claude", "sonnet", "opus", "haiku")


def _normalize_model(llm: str) -> str:
    """Claude Code needs a Claude model. Fall back to 'sonnet' for non-Claude slugs
    (e.g. the runner's default deepseek/...), so `bench claudecode` works even if the
    variety llm wasn't overridden."""
    low = (llm or "").lower()
    if low.startswith(_CLAUDE_PREFIXES):
        return llm
    return "sonnet"


def _final_text(stream_text: str) -> str:
    """The agent's closing message, which only the final `result` event carries (so: empty on a
    capped or walled run). Everything else about the stream is parsed by the SHARED
    summarize_stream — this lane used to keep its own copy of that parser, and the copy silently
    fell behind, which is why a run could report usage_source="computed" with no cost."""
    for raw in (stream_text or "").splitlines():
        try:
            obj = json.loads(raw)
        except Exception:            # noqa: BLE001 — partial streams are expected
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            return obj.get("result") or ""
    return ""


class ClaudeCodeModel(BaseEvalModel):
    llm: str
    num_turn: int = 30
    base_image: str = "claude-runner:latest"

    @weave.op
    def predict(self, full_name: str, commit: str | None = None, language: str | None = None) -> dict:
        start = time.time()
        slug = full_name.lower().replace("/", "-")
        container = f"claudecode-{slug}"
        out_dir = f"{self.root_path}/output/{full_name}"
        ok = {"root_path": self.root_path, "full_name": full_name}
        meta = {"requested_model": self.llm, "base_image": self.base_image}

        # ANTHROPIC_BASE_URL (e.g. https://api.deepseek.com/anthropic) rides along so this lane
        # can run the same LLM as the other varieties; an OAuth token is never sent to a
        # third-party endpoint, so a redirected run must carry that provider's key.
        auth = agent_env()
        if not has_auth(auth):
            return {"status": "error", "failure_reason": "no_auth",
                    "error": "set CLAUDE_CODE_OAUTH_TOKEN, or ANTHROPIC_API_KEY / "
                             "ANTHROPIC_AUTH_TOKEN for a non-Anthropic ANTHROPIC_BASE_URL",
                    **ok, **meta}

        try:
            try:
                init_output_and_repo(self.root_path, full_name, renew=True)
                download_repo(self.root_path, full_name, has_issue=False,
                              use_repo_dockerfile=False, commit=commit)
                repo_src = f"{self.root_path}/input/repo/{full_name}"
                os.makedirs(out_dir, exist_ok=True)
                self._check_timeout(start, "clone")

                # 1) Start the runner container; copy the repo to /testbed; give it to `agent`.
                subprocess.run(f"docker rm -f {container} >/dev/null 2>&1", shell=True)
                run_cmd = ["docker", "run", "-d", "--name", container, "-w", W]
                for k, v in auth.items():
                    run_cmd += ["-e", f"{k}={v}"]
                run_cmd += ["-e", "DISABLE_AUTOUPDATER=1",
                            self.base_image, "tail", "-f", "/dev/null"]
                subprocess.run(run_cmd, check=True, timeout=600)
                subprocess.run(["docker", "exec", container, "mkdir", "-p", W],
                               check=True, timeout=60)
                subprocess.run(["docker", "cp", f"{repo_src}/.", f"{container}:{W}/"],
                               check=True, timeout=600)
                subprocess.run(["docker", "exec", container, "chown", "-R", "agent:agent", W],
                               check=True, timeout=120)

                # 2) Run Claude Code as the autonomous setup agent (non-root `agent`).
                model = _normalize_model(self.llm)
                # The CLI has no turn flag (2.1.258 ships only --max-budget-usd), so `num_turn` is
                # enforced by run_claude_capped. See _claudecode_helpers.budget_flags for why the
                # dollar cap is opt-in rather than defaulted.
                claude_cmd = [
                    "docker", "exec", "-u", "agent", "-w", W, container,
                    "claude", "-p", SETUP_PROMPT,
                    "--permission-mode", "bypassPermissions",
                    *budget_flags(),
                    "--model", model,
                    # Emit the full agentic event stream so each experiment records the
                    # agent's actual actions (tool calls). stream-json requires --verbose.
                    # message_delta carries per-response output tokens, the only way a
                    # turn-capped run can be priced (see summarize_stream).
                    "--output-format", "stream-json", "--verbose",
                    "--include-partial-messages",
                ]
                agent_budget = max(60, self.timeout - int(time.time() - start) - PYTEST_RESERVE)
                # num_turn is the variety's step budget (sweagent's per_instance_call_limit),
                # enforced by counting `assistant` events on the stream. Partial output survives
                # both the cap and the wall.
                res = run_claude_capped(claude_cmd, agent_budget, max_turns=self.num_turn)
                agent_stdout, agent_stderr = res["stdout"], res["stderr"]
                # A "turn" IS an LLM call across the arms, so the counted assistant events ARE
                # agent_turns (the report's turns[cc]) — not the CLI's own num_turns, which counts
                # user+assistant and exists only on runs that reached a final `result` event,
                # never on a capped or walled one.
                meta["agent_turns"] = res["turns"]
                meta["agent_turn_cap"] = self.num_turn
                if self.num_turn and res["turns"] >= self.num_turn:
                    meta["agent_turn_capped"] = True
                if res["timed_out"]:
                    meta["agent_timed_out"] = True  # score the partial env, but record it
                # Whatever stopped it, the agent is still alive INSIDE the container (only the
                # docker exec client was killed) and pytest runs there next — stop it first.
                stop_agent(container)
                if res["turns"] == 0:
                    # No `assistant` event at all: bad key, bad base URL, or an image without the
                    # CLI. The container is untouched, so scoring it would publish a zero-effort
                    # env as a measured result — the old num_turns reported None here, which no
                    # longer distinguishes itself from a real count.
                    self._write_agent_logs(out_dir, agent_stdout, agent_stderr, meta,
                                           model, auth.get("ANTHROPIC_BASE_URL", ""))
                    return {"status": "error", "failure_reason": "agent_no_llm_calls",
                            "error": (agent_stderr or "claude emitted no assistant events")[:500],
                            **ok, **meta}
                # Persist raw stream + readable action log + metrics (handles the
                # partial/timed-out stream too).
                self._write_agent_logs(out_dir, agent_stdout, agent_stderr, meta,
                                       model, auth.get("ANTHROPIC_BASE_URL", ""))
                self._check_timeout(start, "agent")

                # 3) Run RAT's pytest tools at /testbed; copy result JSONs out.
                subprocess.run(["docker", "exec", "-u", "agent", container,
                                "mkdir", "-p", f"{W}/logs"], check=True, timeout=60)
                subprocess.run(["docker", "cp", RPC, f"{container}:/run_pytest_collect.py"],
                               check=True, timeout=120)
                subprocess.run(["docker", "cp", RP, f"{container}:/run_pytest.py"],
                               check=True, timeout=120)
                subprocess.run(["docker", "exec", "-u", "agent", "-w", W, container,
                                "python3", "/run_pytest_collect.py"], check=False,
                               timeout=PYTEST_TIMEOUT)
                subprocess.run(["docker", "cp",
                                f"{container}:{W}/logs/run_pytest_collect_results.json",
                                f"{out_dir}/run_pytest_collect_results.json"],
                               check=True, timeout=120)
                subprocess.run(["docker", "exec", "-u", "agent", "-w", W, container,
                                "python3", "/run_pytest.py"], check=False,
                               timeout=PYTEST_TIMEOUT)
                subprocess.run(["docker", "cp",
                                f"{container}:{W}/logs/run_pytest_results.json",
                                f"{out_dir}/run_pytest_results.json"],
                               check=True, timeout=120)
                return {"status": "success", "failure_reason": None, **ok, **meta}

            except TimeoutException:
                return {"status": "timeout", "failure_reason": "agent_timeout",
                        "error": "exceeded per-repo timeout", **ok, **meta}
            except subprocess.TimeoutExpired as e:
                return {"status": "timeout", "failure_reason": "docker_timeout",
                        "error": str(e), **ok, **meta}
            except subprocess.CalledProcessError as e:
                return {"status": "error", "failure_reason": "docker_error",
                        "error": str(e), **ok, **meta}
            except Exception as e:
                return {"status": "error", "failure_reason": "repo_error",
                        "error": str(e), **ok, **meta}
            finally:
                subprocess.run(f"docker rm -f {container} >/dev/null 2>&1", shell=True)
        except KeyboardInterrupt:
            subprocess.run(f"docker rm -f {container} >/dev/null 2>&1", shell=True)
            raise

    def _write_agent_logs(self, out_dir: str, stdout: str, stderr: str, meta: dict,
                          model: str = "", base_url: str = "") -> None:
        """Persist the raw event stream + a readable action log + final summary, and
        lift a few per-experiment metrics (turns/cost/rate-limit) into `meta`. Best-effort."""
        try:
            with open(f"{out_dir}/claude_stream.jsonl", "w") as f:
                f.write(stdout)
            with open(f"{out_dir}/claude_stderr.txt", "w") as f:
                f.write(stderr)
            info = summarize_stream(stdout)
            with open(f"{out_dir}/claude_actions.log", "w") as f:
                f.write(info["actions"] or "(no parsed actions)")
            with open(f"{out_dir}/claude_final.txt", "w") as f:
                f.write(_final_text(stdout))
            # The CLI's total_cost_usd is Anthropic-priced whatever the base URL says;
            # settled_cost recomputes it from the captured cache split on DeepSeek.
            cost, source = settled_cost(info, model, base_url)
            meta["agent_cost_usd"] = cost
            meta["agent_usage_source"] = source
            meta["agent_tokens_in"] = info["tokens_in"]
            meta["agent_tokens_out"] = info["tokens_out"]
            meta["agent_cache_read_tokens"] = info["cache_read_tokens"]
            meta["agent_dsml_text_blocks"] = info["dsml_text_blocks"]
            if info["rate_limited"]:
                meta["agent_rate_limited"] = True
        except Exception:
            pass
