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
import shutil
import subprocess
import sys
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
# utf-8-sig, not utf-8. pipreqs ast.parse()s the raw decoded text, so ONE file with a UTF-8 BOM
# raises "SyntaxError: invalid non-printable character U+FEFF" and aborts the WHOLE scan, leaving
# the repo with no requirements at all. Verified on Azure/azure-cli: fails under utf-8, yields 51
# packages under utf-8-sig, and utf-8-sig changes nothing for repos without a BOM.
_ENCODING = "utf-8-sig"


def _pipreqs_python() -> str:
    """Interpreter to run the pipreqs scan under.

    pipreqs parses every .py with `ast.parse` under the interpreter EXECUTING it, so it cannot
    scan a repo written in syntax newer than that interpreter — one PEP 695 `type X = ...` line
    (3.12+) aborts the entire scan. The runner venv is deliberately old (3.10/3.11 floor, see
    requirements.txt), which would handicap this arm for a reason that has nothing to do with
    pipreqs' dependency-detection method. $PIPREQS_PYTHON points at the newest interpreter on the
    box that has pipreqs installed; without it we fall back to the venv running the runner.
    Verified on PostHog/posthog: 0 packages (crash) under 3.11, 191 packages under 3.14."""
    return os.environ.get("PIPREQS_PYTHON") or sys.executable


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
    # ctx.workdir is the SHARED run root (runner/benchmark.py passes root_path), so the clone MUST
    # be scoped by full_name or repo #2 of a 50-repo run clones onto repo #1's tree and git aborts
    # with "destination path already exists and is not an empty directory". Same input/<full_name>
    # convention every sibling producer uses.
    repo_path = os.path.join(ctx.workdir, "input", repo.full_name, "repo")
    os.makedirs(os.path.dirname(repo_path), exist_ok=True)

    try:
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
        # Invoked as a MODULE, not the `pipreqs` console script: the console script is pinned to the
        # venv that installed it, which defeats the whole point of choosing the interpreter above.
        runner([_pipreqs_python(), "-m", "pipreqs.pipreqs", repo_path, "--savepath", req_path,
                "--force", "--ignore", _IGNORE_DIRS, "--mode", _PIPREQS_MODE,
                "--encoding", _ENCODING],
               check=True, capture_output=True, timeout=timeout)

        with open(req_path) as f:           # raises FileNotFoundError if pipreqs never wrote it
            requirements = f.read()
    finally:
        # The clone exists ONLY for the duration of the scan; the requirements string is the whole
        # deliverable. Keeping it cost 7.7 GB per 50-repo run against a run whose actual results
        # (output/ + measure/) are 8 MB — a disk-full risk on a 100-repo sweep, and worse at
        # --concurrency 8 where several clones are live at once. Reproducible at any time from the
        # pinned SHA, so nothing durable is lost.
        shutil.rmtree(repo_path, ignore_errors=True)

    return {"requirements": requirements, "produce_s": round(time.time() - start, 2)}


_DOCKERFILE_TEMPLATE = """FROM python:3.10
WORKDIR /
RUN pip install pytest pytest-xdist
RUN git clone {repo_url}.git /testbed
WORKDIR /testbed
COPY requirements_pipreqs.txt /requirements_pipreqs.txt
RUN pip install -r /requirements_pipreqs.txt
"""
# No `|| true` on the install, matching the Repo2Run paper's own reference template for this
# baseline (rat/eval/pipreqs/pipreqs_ref.md). It was briefly appended on the theory that a bad
# requirement should not abort the build before `pytest --collect-only` runs, so that an EBSR-0
# would read as "the tests failed" rather than "the Dockerfile didn't build". Measuring it on
# rat_python50 showed that reasoning backwards: `pip install -r` is ALL-OR-NOTHING, so one
# requirement whose metadata cannot be generated installs NOTHING from the file. `|| true` then
# turns that into a green build with an empty environment, and the failure resurfaces as a
# ModuleNotFoundError at collect time — filing an INSTALL failure under the COLLECT heading.
# It affected 15 of 47 "successful" builds and reported rebuild_ok_rate=0.94 for environments
# that had installed zero dependencies. Letting the build fail is both truthful and the paper's
# definition; a pipreqs requirements file that does not install IS a baseline failure.


class PipreqsProducer:
    """Static, non-agentic baseline: pipreqs-detected requirements + a fixed Dockerfile shape.
    No docker build, no pytest, no scoring here — bench/ owns MEASURE, same as every producer."""

    name = "pipreqs"
    needs_llm = False       # the first producer in the registry with this flag — see the plan's
                            # Global Constraints for why that needs no special registry handling
    measurable = True

    def __init__(self, llm: str | None = None, runner=run_pipreqs):
        # `llm` is accepted and ignored: runner/benchmark.py:148 passes llm= to EVERY produce-able
        # producer with no needs_llm check, so dropping the parameter is a TypeError on the first
        # real run. Same signature shape as every sibling producer.
        self._runner = runner

    def produce(self, repo: RepoSpec, ctx: ProduceContext) -> ProducedEnv:
        try:
            result = self._runner(repo, ctx)
        except Exception as e:                        # noqa: BLE001 — anti-vanish, never raise
            return ProducedEnv(repo=repo, dockerfile=None, status="error",
                               note=repr(e), producer_name=self.name)

        dockerfile = _DOCKERFILE_TEMPLATE.format(repo_url=repo.repo_url)
        note = ""
        if repo.commit:
            dockerfile, injected = inject_clone_pin(dockerfile, repo.commit, repo.repo_url)
            if not injected:
                note = "pin_warning: could not pin the emitted Dockerfile's clone"

        return ProducedEnv(
            repo=repo,
            dockerfile=dockerfile,
            setup_scripts={"requirements_pipreqs.txt": result["requirements"]},
            base_image="python:3.10",
            head_sha=repo.commit,
            status="produced",
            conformance="native",   # ProducedEnv's enum; the toml's measure="conforming" is separate
            note=note,
            producer_name=self.name,
            economy={"produce_s": result.get("produce_s")},
            # No tokens_in/out, llm_calls, turns_used, cost_usd: nothing to report — this arm
            # spends zero LLM tokens and zero dollars. write_env_packet already handles these
            # as None when absent from `economy` (see producers/base.py::write_env_packet).
        )
