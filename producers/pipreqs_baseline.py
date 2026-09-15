# producers/pipreqs_baseline.py
#
# The PRODUCE half of a static, non-agentic baseline: run `pipreqs` (import-scanning
# requirements.txt generator) against the dataset-pinned commit, then emit a Dockerfile that
# installs whatever it found. NO docker build, NO pytest, NO scoring — bench/ rebuilds and
# scores from the packet write_env_packet lands, exactly like every other producer.
#
# This is the first producer with needs_llm=False: no API key, no agent, no LLM turns at all.
# `pipreqs` itself still makes network calls (it queries PyPI live to resolve an import name it
# doesn't recognize locally — see the WARNING lines it prints), so this is not fully offline,
# but it spends zero LLM tokens and zero dollars.
from __future__ import annotations

import os
import subprocess
import time

from producers.base import ProduceContext, ProducedEnv, inject_clone_pin

# bench.schema is on the path via producers.base's shim (imported above).
from bench.schema import RepoSpec  # noqa: E402

# --mode no-pin (bare package names, no version): pipreqs's DEFAULT mode queries live PyPI and
# pins to whatever is newest RIGHT NOW. Re-running this baseline on the identical pinned commit
# weeks apart would then silently produce a DIFFERENT requirements_pipreqs.txt — the same
# "alias drift" reproducibility risk this repo already has to manage for LLM model slugs.
# no-pin removes that specific drift; pip's own resolver still varies at install time, which is
# an accepted, disclosed tradeoff, not a bug to fix later.
_PIPREQS_MODE = "no-pin"
_IGNORE_DIRS = ".venv,venv,env"


def run_pipreqs(repo: RepoSpec, ctx: ProduceContext, *, runner=subprocess.run,
                timeout: int = 120) -> dict:
    """Clone `repo` pinned to `repo.commit` (if set), run `pipreqs` against the pinned tree, and
    return {"requirements": <file content, possibly just "\\n">, "produce_s": float}.

    `runner` is the injectable subprocess seam (tests stub it; production leaves it as
    subprocess.run). Never swallows a failure — a clone error, a pipreqs CLI error, or a missing
    output file all raise, so the caller (PipreqsProducer.produce) can distinguish "genuinely no
    third-party imports found" (a valid "\\n") from "something actually broke" (raise).
    """
    start = time.time()
    repo_path = os.path.join(ctx.workdir, "repo")
    os.makedirs(ctx.workdir, exist_ok=True)

    runner(["git", "clone", "--depth=1", f"{repo.repo_url}.git", repo_path],
           check=True, capture_output=True, timeout=timeout)

    if repo.commit:
        # Pin BEFORE scanning: pipreqs must see the dataset-pinned tree, not live HEAD, or the
        # detected imports (and therefore the whole point of pinning the dataset) are wrong.
        runner(["git", "fetch", "--depth", "1", "origin", repo.commit],
               cwd=repo_path, check=True, capture_output=True, timeout=timeout)
        runner(["git", "checkout", "--detach", repo.commit],
               cwd=repo_path, check=True, capture_output=True, timeout=timeout)

    req_path = os.path.join(repo_path, "requirements_pipreqs.txt")
    runner(["pipreqs", repo_path, "--savepath", req_path, "--force",
            "--ignore", _IGNORE_DIRS, "--mode", _PIPREQS_MODE],
           check=True, capture_output=True, timeout=timeout)

    with open(req_path) as f:               # raises FileNotFoundError if pipreqs never wrote it
        requirements = f.read()

    return {"requirements": requirements, "produce_s": round(time.time() - start, 2)}
