# producers/claudecode_dockerfile.py
#
# The PRODUCE half of today's claudecode_dockerfile_model.predict (design §3, method 2). The
# agent sets up a live env at /testbed and writes /testbed/Dockerfile.gen; the producer pulls
# that Dockerfile out and returns it. Because the agent clones to /testbed, the emitted
# Dockerfile is already CONFORMING (conformance="native") — no re-home needed.
#
# NO docker build, NO pytest, NO scoring — bench/ rebuilds and scores from the packet.
from __future__ import annotations

import os
import time

from producers.base import ProduceContext, ProducedEnv, ensure_rat_on_path, inject_clone_pin

# bench.schema is on the path via producers.base's shim (imported above).
from bench.schema import RepoSpec  # noqa: E402


def _ensure_test_runner(dockerfile: str, language: str) -> str:
    """Append the LANGUAGE's test-runner install if the emitted Dockerfile never mentions it.

    Was `_ensure_pytest`, which appended `RUN pip install ... pytest` unconditionally. That is not
    a harmless extra layer on a non-Python base: `node:20` has no pip, so the line failed the build
    of every Node repo before a single test could run. The per-language rule lives on the profile;
    Node's is None because NodeLanguage.ensure_cmd installs its JUnit reporters at measure time."""
    import re
    from producers._claudecode_helpers import get_profile   # stdlib-only, no RAT tree
    spec = get_profile(language).test_runner
    if spec is None:
        return dockerfile
    probe, install = spec
    if re.search(probe, dockerfile):
        return dockerfile
    return dockerfile.rstrip() + "\n" + install + "\n"


def _capture_claude_stream(claude_cmd: list, timeout, stdin_text: str | None = None,
                           max_turns: int | None = None) -> tuple:
    """Run the Claude Code CLI and return its ``(stdout, stderr)`` as text.

    ``stdin_text`` carries the PROMPT. It is fed on stdin rather than as an argv operand because
    the argv is world-readable inside the container: with ``claude -p "<prompt>"`` the agent's own
    process listed as ``claude -p You are configuring a Rust repository at /testbed ...``, so an
    agent clearing a hung build with ``pkill -f "cargo test --no-run"`` — a string its own prompt
    contains — matched and killed itself. See the tests for the measured casualties.

    The timeout branch is the COMMON case — the agent is routinely killed by ``--max-budget-usd``
    or ``ctx.timeout`` — and it is subtle: on ``TimeoutExpired`` CPython populates
    ``exc.stdout``/``exc.stderr`` with RAW UNDECODED BYTES even though ``text=True`` was passed,
    and either may be ``None`` if the wall hit before any output was read. ``_as_text`` handles
    both, so a timed-out run still yields its partial trajectory rather than a ``b'...'`` repr or
    a crash. Split out from the call site so this decoding is unit-testable without Docker."""
    import subprocess
    from producers._claudecode_helpers import _as_text, run_claude_capped
    if max_turns:
        # The capped path streams the events instead of buffering them, so it can stop the agent
        # on the Nth LLM call. Same (stdout, stderr) contract; the wall is enforced there too.
        res = run_claude_capped(claude_cmd, timeout, stdin_text=stdin_text, max_turns=max_turns)
        return res["stdout"], res["stderr"]
    try:
        proc = subprocess.run(claude_cmd, capture_output=True, text=True, timeout=timeout,
                              input=stdin_text)
        return _as_text(proc.stdout), _as_text(proc.stderr)
    except subprocess.TimeoutExpired as exc:
        # Partial work may still have written Dockerfile.gen; the partial stream is also the only
        # record of what the agent did before the wall, so keep both.
        return _as_text(exc.stdout), _as_text(exc.stderr)


def _persist_stream(out_dir: str, stdout: str, stderr: str) -> dict:
    """Write the agent's raw event stream + a readable action log under the repo's packet dir,
    and return the economy dict `write_env_packet` consumes.

    This stream is the ONLY record of what the ccdf agent cost and did — cost, turn count and
    trajectory cannot be reconstructed from any other artifact after the run, so it is written
    at produce time. Persistence is best-effort: an IO failure must never fail the produce (the
    Dockerfile is the deliverable, design §1), so the parsed numbers are returned regardless.

    `encoding="utf-8"` is explicit and `except Exception` is deliberate. Without the encoding the
    writes inherit the ambient locale, and under the `C`/`POSIX` locale that minimal images
    default to, one non-ASCII character in the agent's own prose raises UnicodeEncodeError — which
    is NOT an OSError, so a narrow guard lets it escape and `produce()`'s outer handler downgrades
    a successful, already-paid-for run to status="error". Telemetry must never be able to do that.
    """
    from producers._claudecode_helpers import summarize_stream   # stdlib-only, no RAT tree
    info = summarize_stream(stdout)
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "claude_stream.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(stdout or "")
        with open(os.path.join(out_dir, "claude_actions.log"), "w", encoding="utf-8") as fh:
            fh.write(info.get("actions") or "(no parsed actions)")
        if stderr:
            with open(os.path.join(out_dir, "claude_stderr.txt"), "w", encoding="utf-8") as fh:
                fh.write(stderr)
    except Exception as exc:      # noqa: BLE001 — see docstring: must never fail the produce
        # Loud enough to debug a missing trajectory (this lands in the per-repo run.log), but
        # never fatal.
        print(f"[ccdf] telemetry persist failed for {out_dir}: {exc!r}", flush=True)
    return {
        "tokens_in": info["tokens_in"], "tokens_out": info["tokens_out"],
        "total_tokens": info["total_tokens"], "llm_calls": info["llm_calls"],
        # A turn IS an LLM call, so turns_used == llm_calls. NOT the CLI's own num_turns
        # (info["turns"]): that counts user+assistant, and only a run reaching its final
        # `result` event has one — a capped or walled run would report None and read as
        # converged to bench.verdict.
        "turns_used": info["llm_calls"], "cost_usd": info["cost_usd"],
        "tool_calls": info["tool_calls"], "agent_is_error": info["is_error"],
        "rate_limited": info["rate_limited"],
    }


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
        W, AUTH_KEYS, ENV_KEYS, _normalize_model, build_prompt, get_profile, resolve_base,
        stop_agent,
        resolve_workbench, DOCKERFILE_GEN_PATH,
    )
    # download_repo/init_output_and_repo are libkit utilities (producers may use libkit).
    # Lazy: only touch the RAT tree on the live path — add <repo>/rat to sys.path first.
    ensure_rat_on_path()
    from libkit.command import download_repo, init_output_and_repo  # noqa: E402

    # The dataset's `language` (already lowered by runner/benchmark.py) picks the prompt AND the
    # emitted FROM. CLAUDE_DOCKERFILE_BASE stays a GLOBAL override, so a mixed-language run must
    # leave it unset or every repo gets the same base.
    profile = get_profile(repo.language)
    dockerfile_base = resolve_base(profile, os.environ.get("CLAUDE_DOCKERFILE_BASE"))
    # FIX 3: the CONTAINER image must be a claude-runner workbench (has the `agent` user + claude
    # CLI), mirroring the deleted wrapper. The generated Dockerfile's FROM is a SEPARATE thing
    # (dockerfile_base, above) and stays a vanilla language base — only the container image was
    # wrong. The workbench is per-language: the original ships python3 AND node 20, but an agent
    # asked to set up a Rust or Java repo inside it has no cargo and no JDK, so it cannot run the
    # gate it is being scored on. CLAUDE_RUNNER_IMAGE remains a global override.
    base_image = resolve_workbench(profile, os.environ.get("CLAUDE_RUNNER_IMAGE"))
    if not any(os.environ.get(k) for k in AUTH_KEYS):
        raise RuntimeError("set CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY")
    auth = {k: os.environ[k] for k in ENV_KEYS if os.environ.get(k)}

    root_path = ctx.workdir
    full_name = repo.full_name
    slug = full_name.lower().replace("/", "-")
    container = f"claudecode-dockerfile-producer-{slug}"
    out_dir = f"{root_path}/output/{full_name}"
    gen_dst = f"{out_dir}/eval_build/Dockerfile.gen"
    try:
        init_output_and_repo(root_path, full_name, renew=True)
        download_repo(root_path, full_name, has_issue=False, use_repo_dockerfile=False,
                      commit=repo.commit)
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

        prompt = build_prompt(full_name, dockerfile_base, profile)
        max_budget = os.environ.get("CLAUDE_MAX_BUDGET_USD", "2.0")
        # `-i` keeps stdin attached and `-p` is left flag-only, so the CLI reads the prompt from
        # the pipe. The prompt must NOT be an argv operand: it would then appear in the container's
        # process table, where the agent's own `pkill -f "<something the prompt says>"` matches and
        # kills it. Measured on rust-full50-20260727-145029 — 3 of the first 12 repos died that way.
        claude_cmd = [
            "docker", "exec", "-i", "-u", "agent", "-w", W, container,
            "claude", "-p", "--permission-mode", "bypassPermissions",
            "--max-budget-usd", str(max_budget), "--model", _normalize_model(llm),
            "--output-format", "stream-json", "--verbose",
        ]
        stdout, stderr = _capture_claude_stream(claude_cmd, ctx.timeout, prompt,
                                                max_turns=ctx.num_turn)
        # The cap and the wall both kill the docker exec client only; the agent keeps
        # mutating /testbed unless it is stopped inside the container BEFORE the copy.
        stop_agent(container)

        try:
            subprocess.run(["docker", "cp", f"{container}:{DOCKERFILE_GEN_PATH}", gen_dst],
                           check=True, timeout=120)
            # encoding="utf-8" for the same reason _persist_stream pins it: under a C/POSIX
            # locale a non-ASCII byte in the agent's Dockerfile (a comment, a package name)
            # raises UnicodeDecodeError, which is a ValueError — NOT caught below — and would
            # discard the artifact this whole run exists to produce.
            with open(gen_dst, encoding="utf-8") as fh:
                text = fh.read().strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError):
            text = ""
        # Persisted AFTER the Dockerfile is in hand, so telemetry sits downstream of the
        # deliverable on every path (belt-and-braces with _persist_stream's own total guard).
        economy = _persist_stream(out_dir, stdout, stderr)
        return {"dockerfile": text or None, "base_image": base_image, "economy": economy}
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

            dockerfile = _ensure_test_runner(dockerfile, repo.language)
            # Fix #1: the agent's emitted Dockerfile does a plain `git clone … /testbed` (its prompt
            # requires it) so the MEASURED build runs at HEAD. Pin its clone to the dataset SHA;
            # surface a pin_warning on miss so the drift is never silent. (Mirrors dockeragent.)
            note = ""
            if repo.commit:
                dockerfile, injected = inject_clone_pin(dockerfile, repo.commit, repo.repo_url)
                if not injected:
                    note = "no git clone instruction to pin"
                    economy["pin_warning"] = note
            return ProducedEnv(repo=repo, dockerfile=dockerfile,
                               base_image=res.get("base_image"),
                               head_sha=res.get("head_sha") or "",
                               status="produced", conformance="native",
                               producer_name=self.name, economy=economy, note=note)
        except Exception as exc:                    # noqa: BLE001 — boundary guard, never propagate
            return ProducedEnv(repo=repo, dockerfile=None, status="error",
                               note=repr(exc), conformance="native",
                               producer_name=self.name,
                               economy={"produce_s": round(time.time() - start, 2)})
