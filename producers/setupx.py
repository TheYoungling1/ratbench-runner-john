# producers/setupx.py
#
# The `setupx` producer: OpenDataBox/SetupX (XPU knowledge store + speculative execution +
# prosecutor/judge verification) as a baseline arm.
#
# SetupX emits NO Dockerfile — it mutates a live container and writes a JSON report. What it does
# record is `setup.history`: every shell command it ran, in order. This producer replays that
# history into one build layer on top of a pinned clone and re-homes /workspace/repo -> /testbed,
# so the arm gets a real EBSR/ESSR row comparable with repo2run / executionagent / dockeragent.
# Same shape as producers/executionagent.py, different trace format.
#
# This module is the LIVE half: launching SetupX, building the per-repo pinned mirror image, and
# the Producer itself. The trajectory->Dockerfile transforms live in producers/setupx_replay.py
# (split out to keep both files small); they are re-exported here as the module's public surface.
from __future__ import annotations

import glob
import hashlib
import json
import os
import subprocess
import time
from urllib.parse import urlsplit

from producers.base import ProduceContext, ProducedEnv

# bench.schema is on the path via producers.base's shim (imported above).
from bench.schema import RepoSpec  # noqa: E402

# Re-exported so `producers.setupx` stays the one import site for the whole producer.
from producers.setupx_replay import (  # noqa: F401
    DEFAULT_BASE_IMAGE, REPLAY_BASENAME, WORK_DIR, plan_replay, render_dockerfile, render_replay)

# Same coercion the trajectory transforms use: a JSON number that arrives as a string must still
# read as a number.
from producers.setupx_replay import _as_int  # noqa: E402

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
# The patch this producer's semantics rest on, resolved from this file so it follows the repo.
PATCH_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "tools", "setupx-bench.patch")
# A BACKSTOP, not the intended bound. main.py's Phase1Timeout handler returns before Stage 3, so a
# wall-clock timeout writes no report and the run is lost entirely; the step budget (--max-steps,
# from varieties.toml) is what should end a run, because step exhaustion still reports.
DEFAULT_PHASE1_TIMEOUT = 3600


def usage_economy(report: dict) -> dict:
    """The report's `usage` block in producers/base.py's packet vocabulary.

    Absent on an unpatched checkout — stock SetupX discards the API `usage` block — and then every
    key is None, exactly what the packet recorded before this landed. Never raises: a run that
    already cost money must not be lost to a malformed telemetry field.

    cost_usd is deliberately NOT computed here: DeepSeek prices cache hits, cache misses and
    output separately, across peak/off-peak windows, and that pricing belongs with the metering
    ledger. The inputs are not thrown away — the cache hit/miss split has no home in the shared
    packet schema, but main.py writes the WHOLE report to <out_dir>/*_result.json, which this
    producer keeps per repo, so `usage.prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` are
    on disk there for the ledger to price later.
    """
    usage = report.get("usage")
    if not isinstance(usage, dict):
        return {"tokens_in": None, "tokens_out": None, "total_tokens": None}
    tokens_in = _as_int(usage.get("prompt_tokens"), 0)
    tokens_out = _as_int(usage.get("completion_tokens"), 0)
    return {"tokens_in": tokens_in, "tokens_out": tokens_out,
            # The provider's own total when it sent one; the sum is only a fallback.
            "total_tokens": _as_int(usage.get("total_tokens"), tokens_in + tokens_out)}


def _setupx_root(ctx: ProduceContext | None = None) -> str:
    # ctx.agent_root first, matching producers/executionagent.py; benchmark.py maps SETUPX_ROOT
    # into it. Falling back to the env var keeps the producer usable standalone.
    root = (ctx.agent_root if ctx else None) or os.environ.get("SETUPX_ROOT")
    if not root or not os.path.isfile(os.path.join(root, "src", "main.py")):
        raise RuntimeError(
            "cannot locate the SetupX checkout (src/main.py). Set SETUPX_ROOT to a clone of "
            "https://github.com/OpenDataBox/SetupX — see README, 'SetupX setup'.")
    # src/config.py runs load_dotenv(..., override=True) at import, so a dotenv file in the
    # checkout SILENTLY overrides the environment we pass in — including the model and the DSN.
    # Fail loudly rather than pay for a run that quietly used the wrong endpoint.
    for name in (".env", ".env.local"):
        if os.path.isfile(os.path.join(root, name)):
            raise RuntimeError(
                f"{os.path.join(root, name)} exists. SetupX loads it with override=True, which "
                "would silently replace the model/endpoint/DSN this producer passes in. Remove it "
                "— the producer supplies the whole environment.")
    # tools/setupx-bench.patch adds the switches this producer's budget, isolation and accounting
    # rest on. Unpatched they are inert and every failure is SILENT and expensive, so each one gets
    # ONE marker per functional area of the patch, not a spot-check. A live run recorded null
    # tokens while agent_settings said setupx_patched=true, because the checkout carried an OLDER
    # copy of the patch: it had the two markers below and no usage capture at all, and nothing
    # noticed. A stale patch reporting itself as fully applied is worse than no check — a missing
    # token column is visibly missing, but a false "patched" actively misleads. So every capability
    # the patch adds needs its own marker here, and a new capability needs a new line.
    for rel, marker, inert in (
            ("src/environment_manager.py", "_ckpt_repo",
             "concurrent repos corrupt each other's rollbacks"),
            ("src/llm_engine.py", "SETUPX_MAX_LLM_CALLS",
             "the LLM-call budget is inert and the run spends to the phase-1 alarm"),
            # llm_usage_used, not _record_usage: main.py imports this one by name, so it is the
            # identifier both halves of the usage hunk agree on.
            ("src/llm_engine.py", "llm_usage_used",
             "no token counts are captured and the run's cost is unrecoverable")):
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as fh:
                patched = marker in fh.read()
        except OSError:
            patched = False
        if not patched:
            raise RuntimeError(
                f"{os.path.join(root, rel)} has no {marker}: tools/setupx-bench.patch is not "
                f"applied, or the checkout carries an older copy of it — {inert}. Re-apply the "
                "CURRENT patch (see README, 'SetupX setup').")
    return root


def child_env(llm: str | None, base_image: str, ckpt_ns: str, max_llm_calls: int,
              log_dir: str) -> dict:
    """The environment `python -m src.main` runs under. Validated here, at the boundary.

    `ckpt_ns` namespaces SetupX's checkpoint images so concurrent repos cannot delete or overwrite
    each other's snapshots, and gives the caller a handle to sweep the leftovers with.
    `max_llm_calls` is the arm's budget in COMPLETIONS, not agent steps: SetupX's own --max-steps
    counts steps, and one step can cost many completions. Both switches come from
    tools/setupx-bench.patch.
    `log_dir` is where SetupX's own INFO log goes. src/logger.py logs to a FILE, not to stdout —
    ~10k lines per run of XPU retrieval decisions, per-command output and agent reasoning, against
    the 12-line summary stdout carries. Unset, it defaults to $SETUPX_ROOT/log/, where every repo
    of every run piles up under timestamped names that cannot be matched back to a row afterwards.
    Caller must have created the directory.
    """
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set; the setupx variety routes through "
                           "DeepSeek's own API (see README, 'SetupX setup').")
    # `deepseek/deepseek-v4-flash` is a litellm-style slug; SetupX POSTs the value verbatim as
    # {"model": ...} to an OpenAI-compatible endpoint, which wants the bare name.
    model = (llm or "deepseek/deepseek-v4-flash").split("/", 1)[-1]
    env = dict(os.environ,
               LLM_PROVIDER="openai",
               OPENAI_API_KEY=key,
               OPENAI_BASE_URL=os.environ.get("SETUPX_BASE_URL", DEEPSEEK_BASE_URL),
               OPENAI_MODEL=model,
               DOCKER_BASE_IMAGE=base_image,
               DOCKER_WORK_DIR="/workspace",
               SETUPX_CKPT_NS=ckpt_ns,
               # num_turn means LLM CALLS here, matching sweagent_repo2run's per_instance_call_limit
               # and claudecode's cap. SetupX's own --max-steps counts agent STEPS, and one step can
               # cost many completions (a VERIFY step runs the verifier's whole ReAct sub-loop;
               # phase 2 adds the prosecutor and judge), so steps are not a comparable budget.
               SETUPX_MAX_LLM_CALLS=str(max_llm_calls),
               # bridge networking: host networking shares the host port space, so two concurrent
               # repos collide the moment either binds a port.
               SETUPX_NETWORK_MODE=os.environ.get("SETUPX_NETWORK_MODE", "bridge"),
               LOG_DIR=log_dir)
    dsn = os.environ.get("SETUPX_DB_DSN")
    if dsn:
        # All THREE, not just the key. text_to_embedding takes the dedicated branch as soon as
        # EMBEDDING_API_KEY is set, and then passes base_url=None straight through — sending an
        # OpenRouter key to api.openai.com. VectorXPUClient.query swallows the 401 and returns [],
        # so the arm would silently degrade to no-XPU, which is exactly what this guard exists to
        # prevent. (The OPENAI_* fallback is no better: those now point at DeepSeek, which serves
        # no /embeddings route at all.)
        missing = [k for k in ("EMBEDDING_API_KEY", "EMBEDDING_BASE_URL", "EMBEDDING_MODEL")
                   if not os.environ.get(k)]
        if missing:
            raise RuntimeError(
                f"{', '.join(missing)} not set, but SETUPX_DB_DSN is — XPU retrieval embeds every "
                "query and would fail silently, leaving an arm that reports as XPU-on while "
                "retrieving nothing. See README, 'SetupX setup'.")
        env.update(XPU_ENABLED="1", XPU_VECTOR_ENABLED="1", dns=dsn, XPU_DB_DNS=dsn,
                   FREEZE_TELEMETRY="1",     # native switch: skips every telemetry write
                   XPU_READONLY="1")         # patched-in switch: skips _store_xpu_experience, so
                                             # concurrent repos cannot race on dedup_and_store and
                                             # results do not depend on repo order
    else:
        # Pin the arm OFF explicitly. `dict(os.environ, ...)` above copies the ambient shell, so an
        # XPU_ENABLED exported for the on-arm would leak into the off-arm and hand SetupX an
        # XPU-on child with no store — silently degrading the very control the variety is measured
        # against. Arm membership is the producer's call, not the shell's.
        env.update(XPU_ENABLED="0", XPU_VECTOR_ENABLED="0", XPU_READONLY="0")
    return env


def _xpu_store(dsn: str) -> str:
    """The DATABASE NAME out of a DSN, and nothing else. "" if it cannot be read safely.

    SETUPX_DB_DSN carries a password. `_meta.json` is committed alongside the run and gets pasted
    into issues, so the DSN itself must never reach it — the store's identity is all an analyst
    needs to tell two XPU corpora apart.
    """
    try:
        parts = urlsplit(dsn)
    except ValueError:
        # urlsplit is NOT total: an unclosed IPv6 bracket ("@[::1:5433/db") and a netloc that
        # NFKC-normalizes into /?#@: both raise. This function is called above every try in
        # agent_settings, which is itself called above produce()'s try, and benchmark.py's
        # `prod.produce(...)` is unwrapped on the strength of "producer is anti-vanish (never
        # raises)" — so a ValueError here would vanish the row instead of recording an error one.
        return ""
    # A keyword-style DSN ("dbname=xpu_run password=hunter2") has no scheme/netloc and urlsplit
    # hands the WHOLE string back as the path — password included. Require a URL, then require the
    # result to be a bare identifier; anything else is dropped rather than risked.
    name = parts.path.lstrip("/") if parts.scheme and parts.netloc else ""
    return name if name and all(c.isalnum() or c in "_-." for c in name) else ""


def agent_settings(ctx: ProduceContext, num_turn: int) -> dict:
    """Which arm this row belongs to, and what the budget actually meant. NEVER raises.

    Two facts are unrecoverable once a run is over. (1) ARM: the XPU-on and XPU-off runs emit
    byte-identical artifact shapes, so without this the only thing separating them is the
    directory name the operator typed into --run-name — and the on/off comparison is the entire
    point of the variety. (2) PATCH STATE: `llm_call_budget` means COMPLETIONS only because
    tools/setupx-bench.patch is applied; against an unpatched checkout SETUPX_MAX_LLM_CALLS is
    inert, `--max-steps 9999` binds instead, and num_turn=100 silently becomes a step budget worth
    roughly four times the spend — producing data that looks identical and means something else.

    Computed from the same environment child_env reads, so the record and the child agree.
    """
    dsn = os.environ.get("SETUPX_DB_DSN") or ""
    settings = {"xpu": "on" if dsn else "off",
                "llm_call_budget": num_turn,
                "network_mode": os.environ.get("SETUPX_NETWORK_MODE", "bridge")}
    store = _xpu_store(dsn)
    if store:
        settings["xpu_store"] = store
    # A fingerprint of the PATCH FILE, which is what survives the next time the patch changes:
    # markers go stale silently, but this lets an analyst say "this run used patch X" and diff it
    # against tools/setupx-bench.patch as of the run's commit. NOT proof the checkout matches it —
    # that would take verifying applied hunks, which is not worth building. The marker loop in
    # _setupx_root catches the realistic failure (a capability missing outright); this makes the
    # intended version recoverable. Together they are a strong hint, not a guarantee.
    try:
        with open(PATCH_PATH, "rb") as fh:
            settings["setupx_patch_sha"] = hashlib.sha256(fh.read()).hexdigest()[:12]
    except OSError:                          # noqa: BLE001 — optional fact, and this runs outside
        pass                                 # produce()'s guard, so it must not raise
    try:
        # _setupx_root is the marker grep, not a second copy of it: it returns only for a checkout
        # that exists AND carries both patch markers, so "it returned" IS the patched signal.
        root = _setupx_root(ctx)
    except Exception:                        # noqa: BLE001 — provenance, never a failure path
        settings["setupx_patched"] = False   # also covers "no checkout here", e.g. the error guard
        return settings
    settings["setupx_patched"] = True
    try:
        # --show-toplevel alongside HEAD, in one call: rev-parse WALKS UP, so a checkout vendored
        # inside another repo would otherwise report the PARENT's HEAD — a wrong 40-hex value that
        # reads as authoritative, which for a provenance field is worse than no value at all.
        # 5s, not 30: this runs per repo and a local rev-parse takes milliseconds.
        out = subprocess.run(["git", "-C", root, "rev-parse", "--show-toplevel", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        top, _, commit = (out.stdout or "").partition("\n")
        if out.returncode != 0 or os.path.realpath(top.strip()) != os.path.realpath(root):
            commit = ""
    except Exception:                        # noqa: BLE001 — a checkout with no .git still runs
        commit = ""
    if commit.strip():
        settings["setupx_commit"] = commit.strip()
    return settings


def mirror_image(repo: RepoSpec, ctx: ProduceContext) -> str:
    """Build a per-repo base image holding the repo pinned at /mirror, and return its tag.

    SetupX clones `git clone --depth=1 <repo_url> /workspace/repo` at the live default-branch HEAD
    — it has no notion of a dataset SHA. Handing it a local path as the "repo url" makes its own
    clone land the pinned tree instead, so the agent works on the same commit the emitted
    Dockerfile builds, with no patch to the SetupX checkout.
    """
    # The SHA is part of the TAG, not just the context: SetupX resolves DOCKER_BASE_IMAGE by
    # tag when it creates the container, so two concurrent produces of the same repo at
    # different SHAs would race on one tag and an agent could get the other run's tree.
    tag = "setupx-mirror:" + repo.full_name.replace("/", "__").lower() + (
        f"_{repo.commit[:12]}" if repo.commit else "")
    ctx_dir = os.path.join(ctx.workdir, "mirror", repo.full_name)
    os.makedirs(ctx_dir, exist_ok=True)
    clone = (f"RUN git clone --depth=1 {repo.repo_url} /mirror\n" if not repo.commit else
             f"RUN git clone {repo.repo_url} /mirror \\\n"
             f" && git -C /mirror fetch --depth 1 origin {repo.commit} \\\n"
             # a branch, not a detached HEAD: cloning FROM a detached-HEAD repo checks out its
             # default branch, which would put the agent back on an unpinned tree.
             f" && git -C /mirror checkout -B pinned {repo.commit}\n")
    with open(os.path.join(ctx_dir, "Dockerfile"), "w", encoding="utf-8") as fh:
        fh.write(f"FROM {DEFAULT_BASE_IMAGE}\n"
                 "RUN apt-get update \\\n"
                 " && apt-get install -y --no-install-recommends git ca-certificates \\\n"
                 " && rm -rf /var/lib/apt/lists/*\n"
                 + clone)
    try:
        subprocess.run(["docker", "build", "-t", tag, ctx_dir],
                       check=True, capture_output=True, text=True, timeout=1800)
    except subprocess.CalledProcessError as exc:
        # CalledProcessError's repr() — which produce() stores as `note` — carries only the rc and
        # argv, so every mirror failure would look identical. Surface the build's own stderr.
        raise RuntimeError(f"mirror build failed for {repo.full_name}: "
                           f"{(exc.stderr or '')[-2000:]}") from exc
    return tag


def _sweep_checkpoints(ckpt_ns: str) -> None:
    """Remove the checkpoint images this run leaked.

    `agent.run()` calls `cleanup_snapshots()` on the happy path, but a crash or a phase-1 timeout
    skips it — and each snapshot is a full `docker commit` of the container, so a 50-repo run would
    otherwise leave dozens of multi-GB images behind. Never raises: this is housekeeping, not the run.
    """
    try:
        out = subprocess.run(["docker", "images", "-q", f"setup_agent_checkpoint_{ckpt_ns}"],
                             capture_output=True, text=True, timeout=60)
        ids = sorted(set((out.stdout or "").split()))
        if ids:
            subprocess.run(["docker", "rmi", "-f", *ids], capture_output=True, timeout=300)
    except Exception:                        # noqa: BLE001 — housekeeping must never fail a run
        pass


def run_setupx(repo: RepoSpec, ctx: ProduceContext, *, llm: str | None, num_turn: int) -> dict:
    """LIVE-ONLY: run SetupX for one repo and return its report. Raises on failure; the producer
    wraps it for the anti-vanish invariant. Never exercised by unit tests (they inject a stub) —
    it needs docker, keys, and the SetupX checkout."""
    root = _setupx_root(ctx)
    python = os.environ.get("SETUPX_PYTHON") or os.path.join(root, ".venv", "bin", "python")
    if not os.path.isfile(python):
        raise RuntimeError(f"cannot run {python!r}: SetupX needs its own venv with "
                           "requirements.txt installed. Point SETUPX_PYTHON at it — see README, "
                           "'SetupX setup'.")
    out_dir = os.path.join(ctx.workdir, "output", repo.full_name, "setupx_log")
    os.makedirs(out_dir, exist_ok=True)
    phase1 = int(os.environ.get("SETUPX_PHASE1_TIMEOUT", DEFAULT_PHASE1_TIMEOUT))

    # The positional argument is the REPO URL. `/mirror` is the pinned clone baked into
    # base_image, so SetupX's own `git clone --depth=1 /mirror /workspace/repo` lands the dataset
    # SHA. (git warns "--depth is ignored in local clones"; harmless.) One consequence: SetupX
    # names its report after the url basename, so every report here is `mirror_result.json` — the
    # glob below handles that, and out_dir is per-repo, so there is no collision.
    # Before mirror_image, not after: `produce_s` feeds mean_produce_s and the cross-arm cost
    # composite (bench/bench/metrics.py), and every other arm's clock covers its whole produce.
    start = time.time()
    base_image = mirror_image(repo, ctx)
    ckpt_ns = f"{repo.full_name.replace('/', '__').lower()}_{os.getpid()}"
    try:
        proc = subprocess.run(
            [python, "-m", "src.main", "/mirror",
             # Steps are deliberately unbounded: the budget is SETUPX_MAX_LLM_CALLS, and every step
             # costs at least one completion, so the call cap binds first and binds comparably.
             "--max-steps", "9999",
             "--phase1-timeout", str(phase1),
             "--output-dir", out_dir],
            cwd=root, env=child_env(llm, base_image, ckpt_ns, num_turn, out_dir),
            capture_output=True, text=True, timeout=ctx.timeout)
    finally:
        _sweep_checkpoints(ckpt_ns)
    try:
        with open(os.path.join(out_dir, "setupx_stdout.txt"), "w", encoding="utf-8") as fh:
            fh.write((proc.stdout or "") + "\n--- stderr ---\n" + (proc.stderr or ""))
    except OSError:
        pass                                     # telemetry must never fail a paid-for run

    reports = sorted(glob.glob(os.path.join(out_dir, "*_result.json")))
    if not reports:
        raise RuntimeError(f"SetupX wrote no *_result.json (rc={proc.returncode}); "
                           "see setupx_stdout.txt")
    with open(reports[-1], encoding="utf-8") as fh:
        report = json.load(fh)
    setup = report.get("setup") or {}
    return {"history": setup.get("history") or [],
            "completed": bool(setup.get("completed")),
            "steps_taken": setup.get("steps_taken"),
            "phase2": report.get("phase2") or {},
            # llm_calls and usage both come from the patched main.py (llm_engine.llm_calls_used()
            # / llm_usage_used()); on an unpatched checkout they are simply absent. cost_usd is
            # left unset — see usage_economy for where its inputs are persisted.
            "economy": {"turns_used": setup.get("steps_taken"),
                        "llm_calls": report.get("llm_calls"),
                        **usage_economy(report),
                        "produce_s": round(time.time() - start, 2)}}


class SetupXProducer:
    """SetupX (XPU + speculative execution + prosecutor/judge), replayed into one image at /testbed."""
    name = "setupx"
    needs_llm = True
    measurable = True
    conformance = "synthesized"

    # num_turn defaults to the variety's own budget (100 LLM CALLS), not to something unbounded:
    # runner/benchmark.py's kwarg list is hardcoded, so a future edit dropping `setupx` from it
    # would fall back to this default. Failing safe means failing at the arm's real budget.
    def __init__(self, llm: str | None = None, num_turn: int = 100, runner=None):
        self.llm = llm
        self.num_turn = num_turn
        self._runner = runner   # injectable for tests; None => the real live runner

    def produce(self, repo: RepoSpec, ctx: ProduceContext) -> ProducedEnv:
        # Anti-vanish invariant (design §1): a SetupX crash yields ProducedEnv(status="error"),
        # never a raised exception.
        start = time.time()
        num_turn = ctx.num_turn if ctx.num_turn is not None else self.num_turn
        # Hoisted out of the try, and onto EVERY return below: a run that failed still has to say
        # what arm and what budget it failed under, or its row cannot be attributed at all — and
        # the failures are the rows an analyst goes back to interrogate. agent_settings never
        # raises, so computing it here cannot cost the anti-vanish invariant.
        settings = agent_settings(ctx, num_turn)
        try:
            runner = self._runner or run_setupx
            res = runner(repo, ctx, llm=(ctx.llm or self.llm), num_turn=num_turn)

            economy = dict(res.get("economy") or {})
            economy.setdefault("produce_s", round(time.time() - start, 2))
            steps, lossy = plan_replay(res.get("history"))
            phase2 = res.get("phase2") or {}
            note = f"phase2={phase2.get('success')}: {(phase2.get('reason') or '')[:200]}"
            if not res.get("completed"):
                note = "phase1 incomplete; " + note
            if lossy:
                note += "; XPU trial commands not recorded (atom-rendered fallback)"

            if not steps:
                return ProducedEnv(repo=repo, dockerfile=None, status="error",
                                   note="no replayable steps survived; " + note,
                                   agent_settings=settings,
                                   conformance=self.conformance, producer_name=self.name,
                                   economy=economy)

            dockerfile, scripts = render_dockerfile(repo, steps)
            return ProducedEnv(repo=repo, dockerfile=dockerfile, setup_scripts=scripts,
                               base_image=DEFAULT_BASE_IMAGE, head_sha=repo.commit or "",
                               agent_settings=settings,
                               status="produced", conformance=self.conformance,
                               unreplayed=lossy, producer_name=self.name,
                               economy=economy, note=note)
        except Exception as exc:                # noqa: BLE001 — boundary guard, never propagate
            return ProducedEnv(repo=repo, dockerfile=None, status="error", note=repr(exc),
                               agent_settings=settings,
                               conformance=self.conformance, producer_name=self.name,
                               economy={"produce_s": round(time.time() - start, 2)})
