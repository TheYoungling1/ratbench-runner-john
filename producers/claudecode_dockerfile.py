# producers/claudecode_dockerfile.py
#
# The PRODUCE half of today's claudecode_dockerfile_model.predict (design §3, method 2). The
# agent sets up a live env at /testbed and writes /testbed/Dockerfile.gen; the producer pulls
# that Dockerfile out and returns it. Because the agent clones to /testbed, the emitted
# Dockerfile is already CONFORMING (conformance="native") — no re-home needed.
#
# NO docker build, NO pytest, NO scoring — bench/ rebuilds and scores from the packet.
from __future__ import annotations

import time

from producers.base import ProduceContext, ProducedEnv, ensure_rat_on_path

# bench.schema is on the path via producers.base's shim (imported above).
from bench.schema import RepoSpec  # noqa: E402


def _ensure_pytest(dockerfile: str) -> str:
    """Append a pytest install if the Dockerfile never mentions pytest (the fresh-container
    measure needs it). Mirrors the model's `ensure_pytest` nicety without importing the RAT tree."""
    import re
    if re.search(r"\bpytest\b", dockerfile):
        return dockerfile
    return dockerfile.rstrip() + "\nRUN pip install --no-cache-dir pytest\n"


def run_claudecode_dockerfile(repo: RepoSpec, ctx: ProduceContext, *, llm: str | None) -> dict:
    """LIVE-ONLY: drive the claudecode-dockerfile agent and return its /testbed/Dockerfile.gen.

    Mirrors harness/eval/models/claudecode_dockerfile_model.py's produce orchestration (run a live
    container with the repo at /testbed -> `claude -p` -> `docker cp /testbed/Dockerfile.gen`), but
    returns the Dockerfile text instead of writing a packet. Raises on any failure; callers wrap it
    for the anti-vanish invariant. Never reached by unit tests (they inject a stub runner) — it
    needs docker + auth keys + a real agent. The primary MEASURED route is the model's produce-only
    predict; this exists so the standalone registry producer also works live.
    """
    import os
    import subprocess

    # Pure, RAT-tree-free helpers folded into producers/ so this producer never imports the
    # runner package or RAT model modules (dependency direction: producers must not depend
    # on the runner).
    from producers._claudecode_helpers import (
        W, AUTH_KEYS, _normalize_model, _as_text, build_prompt, DOCKERFILE_GEN_PATH,
    )
    # download_repo/init_output_and_repo are libkit utilities (producers may use libkit).
    # Lazy: only touch the RAT tree on the live path — add <repo>/rat to sys.path first.
    ensure_rat_on_path()
    from libkit.command import download_repo, init_output_and_repo  # noqa: E402

    dockerfile_base = os.environ.get("CLAUDE_DOCKERFILE_BASE", "python:3.11")
    base_image = os.environ.get("CLAUDE_BASE_IMAGE", "python:3.11")
    auth = {k: os.environ[k] for k in AUTH_KEYS if os.environ.get(k)}
    if not auth:
        raise RuntimeError("set CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY")

    root_path = ctx.workdir
    full_name = repo.full_name
    slug = full_name.lower().replace("/", "-")
    container = f"claudecode-dockerfile-producer-{slug}"
    out_dir = f"{root_path}/output/{full_name}"
    gen_dst = f"{out_dir}/eval_build/Dockerfile.gen"
    try:
        init_output_and_repo(root_path, full_name, renew=True)
        download_repo(root_path, full_name, has_issue=False, use_repo_dockerfile=False)
        repo_src = f"{root_path}/input/repo/{full_name}"
        os.makedirs(os.path.dirname(gen_dst), exist_ok=True)

        subprocess.run(f"docker rm -f {container} >/dev/null 2>&1", shell=True)
        run_cmd = ["docker", "run", "-d", "--name", container, "-w", W]
        for k, v in auth.items():
            run_cmd += ["-e", f"{k}={v}"]
        run_cmd += ["-e", "DISABLE_AUTOUPDATER=1", base_image, "tail", "-f", "/dev/null"]
        subprocess.run(run_cmd, check=True, timeout=600)
        subprocess.run(["docker", "exec", container, "mkdir", "-p", W], check=True, timeout=60)
        subprocess.run(["docker", "cp", f"{repo_src}/.", f"{container}:{W}/"], check=True, timeout=600)
        subprocess.run(["docker", "exec", container, "chown", "-R", "agent:agent", W],
                       check=True, timeout=120)

        prompt = build_prompt(full_name, dockerfile_base)
        max_budget = os.environ.get("CLAUDE_MAX_BUDGET_USD", "2.0")
        claude_cmd = [
            "docker", "exec", "-u", "agent", "-w", W, container,
            "claude", "-p", prompt, "--permission-mode", "bypassPermissions",
            "--max-budget-usd", str(max_budget), "--model", _normalize_model(llm),
            "--output-format", "stream-json", "--verbose",
        ]
        try:
            subprocess.run(claude_cmd, capture_output=True, text=True, timeout=ctx.timeout)
        except subprocess.TimeoutExpired:
            pass   # partial work may still have written Dockerfile.gen; try to pull it below

        try:
            subprocess.run(["docker", "cp", f"{container}:{DOCKERFILE_GEN_PATH}", gen_dst],
                           check=True, timeout=120)
            with open(gen_dst) as fh:
                text = fh.read().strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            text = ""
        return {"dockerfile": text or None, "base_image": base_image, "economy": {}}
    finally:
        subprocess.run(f"docker rm -f {container} >/dev/null 2>&1", shell=True)


class ClaudeCodeDockerfileProducer:
    """Direct producer: run the agent, pull /testbed/Dockerfile.gen (already conforming)."""
    name = "claudecode-dockerfile"
    needs_llm = True
    measurable = True
    conformance = "native"

    def __init__(self, llm: str | None = None, runner=None):
        self.llm = llm
        self._runner = runner   # injectable for tests; None => the real live agent runner

    def produce(self, repo: RepoSpec, ctx: ProduceContext) -> ProducedEnv:
        # Anti-vanish invariant (design §1): the ENTIRE body is guarded so a no-Dockerfile.gen
        # result or any agent/docker failure yields a ProducedEnv(status="error"), never a raise.
        start = time.time()
        try:
            runner = self._runner or run_claudecode_dockerfile
            llm = ctx.llm or self.llm
            res = runner(repo, ctx, llm=llm)

            dockerfile = res.get("dockerfile")
            economy = dict(res.get("economy") or {})
            economy.setdefault("produce_s", round(time.time() - start, 2))
            if not dockerfile:
                return ProducedEnv(repo=repo, dockerfile=None, status="error",
                                   note="agent wrote no /testbed/Dockerfile.gen",
                                   base_image=res.get("base_image"), conformance="native",
                                   producer_name=self.name, economy=economy)

            dockerfile = _ensure_pytest(dockerfile)
            return ProducedEnv(repo=repo, dockerfile=dockerfile,
                               base_image=res.get("base_image"),
                               head_sha=res.get("head_sha") or "",
                               status="produced", conformance="native",
                               producer_name=self.name, economy=economy)
        except Exception as exc:                    # noqa: BLE001 — boundary guard, never propagate
            return ProducedEnv(repo=repo, dockerfile=None, status="error",
                               note=repr(exc), conformance="native",
                               producer_name=self.name,
                               economy={"produce_s": round(time.time() - start, 2)})
