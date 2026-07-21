# producers/repo2run.py
#
# The repo2run SYNTHESIZER producer (design §3, method 3: "re-home"). repo2run builds a
# *working* env but homes the repo at /repo with deps in system pip (base python:3.10, NO venv).
# bench runs pytest at /testbed, so a raw repo2run image measures an EMPTY /testbed (false green).
#
# `rehome_dockerfile` appends a deterministic stanza that MOVES the installed clone to /testbed
# (a real dir — a symlinked /testbed would defeat the guard's `find -P`) and leaves a reverse
# symlink /repo -> /testbed so any editable install that recorded /repo paths still resolves.
# The transform is PURE + unit-tested; running the actual repo2run tool is a live-only wrapper.
from __future__ import annotations

import json
import os
import re
import subprocess
import time

from producers.base import ProduceContext, ProducedEnv, ensure_rat_on_path, inject_clone_pin

# bench.schema is on the path via producers.base's shim (imported above).
from bench.schema import RepoSpec  # noqa: E402


# ── The pure re-home transform (design §3, method 3) ─────────────────────────────────────────
#
# repo2run's uniform pattern is `... mkdir /repo && cp -r /<name>/. /repo` on one RUN line, then
# `WORKDIR /repo`. We detect the destination so a hypothetical non-/repo variant still re-homes
# correctly, and fall back to /repo (the verified invariant) when nothing is detectable.
_MKDIR_CP = re.compile(
    r"mkdir\s+(?:-p\s+)?(?P<dest>/\S+)"        # mkdir /repo
    r".*?"                                      # && (same RUN line — no DOTALL, so bounded to one line)
    r"cp\s+-r\s+\S+\s+(?P=dest)(?=[\s/&|;]|$)"  # cp -r /<name>/. /repo
)
_CP_DEST = re.compile(r"cp\s+-r\s+\S+\s+(/\S+)")
_WORKDIR = re.compile(r"^\s*WORKDIR\s+(/\S+)\s*$", re.MULTILINE)
# A detected dest is only trusted if it is a strict absolute path — no shell punctuation
# (`;`, `&`, `|`, quotes, `$`, ...) that a greedy `\S+` capture could smuggle into the `mv`
# stanza (injection / broken build). Anything else falls back to the safe default /repo.
_SAFE_DEST = re.compile(r"^/[A-Za-z0-9_./-]+$")

_REHOME_STANZA = (
    "# bench re-home: expose repo2run's installed repo at /testbed (real dir, NOT a symlink — a\n"
    "# symlinked /testbed defeats the guard's `find -P`). Reverse symlink keeps /repo-recorded\n"
    "# editable installs resolving. System python is default (no venv PATH).\n"
    "RUN mv {dest} /testbed && ln -sfn /testbed {dest}\n"
    "WORKDIR /testbed\n"
)


def _detect_repo_dest(dockerfile: str) -> str | None:
    """Best-effort detection of repo2run's repo destination. Returns None when undetectable or
    when the candidate fails the strict absolute-path guard (see _SAFE_DEST) — a rejected
    candidate is treated as undetectable so the caller falls back to the safe default."""
    m = _MKDIR_CP.search(dockerfile)
    if m and _SAFE_DEST.match(m.group("dest")):
        return m.group("dest")
    # Conservative fallback: a WORKDIR that is ALSO a `cp -r` destination (repo2run copies the
    # clone into the dir it then works in). Never root "/". Cross-checking avoids grabbing an
    # unrelated build WORKDIR; the _SAFE_DEST guard rejects punctuated paths.
    cp_dests = {d.rstrip("/") for d in _CP_DEST.findall(dockerfile)}
    for wd in reversed(_WORKDIR.findall(dockerfile)):
        w = wd.rstrip("/")
        if w and w != "/" and w in cp_dests and _SAFE_DEST.match(w):
            return w
    return None


def rehome_dockerfile(dockerfile: str, repo_dest: str = "/repo") -> str:
    """Append the re-home stanza to a repo2run Dockerfile (design §3, method 3).

    `repo_dest` is the fallback used when the destination can't be detected from the Dockerfile;
    it defaults to /repo (repo2run's verified invariant). A detected destination wins over it.
    The stanza `mv`s the installed repo to /testbed (a REAL dir, never a symlink) and adds the
    reverse symlink /repo -> /testbed. No `|| true`: a wrong dest surfaces as the guard flagging
    empty_testbed/non_conforming — fail-safe, never a false green.
    """
    dest = _detect_repo_dest(dockerfile) or repo_dest
    return dockerfile.rstrip() + "\n" + _REHOME_STANZA.format(dest=dest)


# ── Pure telemetry harvest (unit-tested; the old runner/models/repo2run_model.py wrapper used to
# fold these into agent_run_summary.json — M4.5a moves the harvest into the producer so a
# produce-only run still carries repo2run's token total + its own live pytest score) ──────────
def _harvest_total_tokens(output_dir: str) -> int | None:
    """repo2run's cumulative token total from output/<full_name>/track.json (a LIST; the last
    entry with a NUMERIC `cost_tokens` holds the running total — repo2run keeps ONE total, no
    in/out split). Anti-vanish: a missing/malformed track.json OR a non-numeric cost_tokens yields
    None (never a raise, and never a bogus non-int `tok` that would suppress the summary fallback)."""
    try:
        with open(os.path.join(output_dir, "track.json")) as fh:
            track = json.load(fh)
        # Only accept a genuine number (int/float, NOT bool, which is an int subclass) — a
        # non-numeric cost_tokens is skipped so we fall through to None, not persist junk.
        return next((e["cost_tokens"] for e in reversed(track)
                     if isinstance(e, dict)
                     and isinstance(e.get("cost_tokens"), (int, float))
                     and not isinstance(e.get("cost_tokens"), bool)), None)
    except Exception:                                   # noqa: BLE001 — anti-vanish, never propagate
        return None


def _harvest_inline(output_dir: str) -> dict | None:
    """repo2run's own in-container pytest score from output/<full_name>/run_pytest_results.json,
    as `{command, success, pass_rate}` (the delta between the method's live score and the bench
    replay). Uses the SAME arithmetic the old repo2run_model wrapper used. Anti-vanish: an absent
    or malformed file yields None, never a raise."""
    try:
        with open(os.path.join(output_dir, "run_pytest_results.json")) as fh:
            summary = json.load(fh).get("summary") or {}
        total = summary.get("total_tests", 0) or 0
        passed = summary.get("passed", 0) or 0
        skipped = summary.get("skipped", 0) or 0
        failed = summary.get("failed", 0) or 0
        errors = summary.get("errors", 0) or 0
        eff = total - skipped
        pass_rate = (passed / eff) if eff > 0 else ((passed / total) if total > 0 else None)
        success = (passed > 0) and (failed == 0) and (errors == 0)
        return {"command": "pytest", "success": bool(success), "pass_rate": pass_rate}
    except Exception:                                   # noqa: BLE001 — anti-vanish, never propagate
        return None


# ── The live-only real runner (lazily touches the RAT tree; never reached by unit tests) ──────
def run_repo2run(repo: RepoSpec, ctx: ProduceContext, *, llm: str | None, num_turn: int) -> dict:
    """LIVE-ONLY: run the repo2run tool and return its RAW (pre-re-home) Dockerfile + metadata.

    Reuses the same invocation as harness/eval/models/repo2run_model.py (clone -> SHA ->
    Repo2Run/build_agent/main.py -> output/<full_name>/Dockerfile). `ctx.workdir` is the
    root_path. Raises on any failure; callers wrap it for the anti-vanish invariant. This is
    never exercised by unit tests (they inject a stub runner) — it needs a real repo + keys.
    """
    ensure_rat_on_path()   # FIX C: add <repo>/rat to sys.path BEFORE the lazy libkit import
    from libkit.command import init_output_and_repo  # lazy: only producers/{rat,repo2run}.py touch libkit

    root_path = ctx.workdir
    full_name = repo.full_name
    output_dir = os.path.join(root_path, "output", full_name)
    repo_path = os.path.join(root_path, "input", "repo", full_name)

    init_output_and_repo(root_path, full_name, renew=True)
    subprocess.run(f"git clone --depth=1 {repo.repo_url}.git {repo_path}", shell=True, check=True)
    # Commit pin BEFORE reading the SHA so `git rev-parse HEAD` reports the pinned commit and
    # repo2run runs its tool on the pinned checkout. Falsy commit => unchanged (HEAD).
    if repo.commit:
        subprocess.run(f"git fetch --depth 1 origin {repo.commit}", cwd=repo_path,
                       shell=True, check=True)
        subprocess.run(f"git checkout --detach {repo.commit}", cwd=repo_path,
                       shell=True, check=True)
    sha = subprocess.run("git rev-parse HEAD", cwd=repo_path, shell=True, check=True,
                         capture_output=True, text=True).stdout.strip()

    os.makedirs(os.path.join(root_path, "utils", "repo"), exist_ok=True)
    repo2run_main = os.path.join(root_path, "Repo2Run", "build_agent", "main.py")
    if not os.path.exists(repo2run_main):
        raise RuntimeError(f"Repo2Run entrypoint not found: {repo2run_main}")
    subprocess.run(
        ["python3", repo2run_main, "--full_name", full_name, "--sha", sha,
         "--root_path", root_path, "--num_turn", str(num_turn), "--llm", str(llm)],
        timeout=ctx.timeout, check=True)

    dockerfile_path = os.path.join(output_dir, "Dockerfile")
    if not os.path.exists(dockerfile_path):
        raise RuntimeError("Repo2Run did not generate a Dockerfile; configuration likely failed.")
    with open(dockerfile_path) as fh:
        dockerfile = fh.read()
    base = re.search(r"^\s*FROM\s+(\S+)", dockerfile, re.MULTILINE)

    # M4.5a: harvest repo2run's own telemetry from output/<full_name>/ (never raise — anti-vanish).
    total_tokens = _harvest_total_tokens(output_dir)
    economy: dict = {}
    if total_tokens is not None:
        economy["total_tokens"] = total_tokens
    inline = _harvest_inline(output_dir)
    return {"dockerfile": dockerfile, "base_image": (base.group(1) if base else None),
            "head_sha": sha, "economy": economy, "inline": inline}


class Repo2RunProducer:
    """Synthesizer producer: run repo2run, then re-home its Dockerfile to /testbed (lossless)."""
    name = "repo2run"
    needs_llm = True
    measurable = True
    conformance = "rehomed"

    def __init__(self, llm: str | None = None, num_turn: int = 40,
                 repo_dest: str = "/repo", runner=None):
        self.llm = llm
        self.num_turn = num_turn
        self.repo_dest = repo_dest
        self._runner = runner   # injectable for tests; None => the real live run_repo2run

    def produce(self, repo: RepoSpec, ctx: ProduceContext) -> ProducedEnv:
        # Anti-vanish invariant (design §1): the ENTIRE body is guarded so a repo2run tool failure
        # (or a malformed result) yields a ProducedEnv(status="error"), never a raised exception.
        start = time.time()
        try:
            runner = self._runner or run_repo2run
            llm = ctx.llm or self.llm
            num_turn = ctx.num_turn if ctx.num_turn is not None else self.num_turn
            res = runner(repo, ctx, llm=llm, num_turn=num_turn)

            raw = res.get("dockerfile")
            economy = dict(res.get("economy") or {})
            economy.setdefault("produce_s", round(time.time() - start, 2))
            inline = res.get("inline")
            if not raw:
                return ProducedEnv(repo=repo, dockerfile=None, status="error",
                                   note="repo2run produced no Dockerfile",
                                   base_image=res.get("base_image"), conformance="rehomed",
                                   producer_name=self.name, economy=economy, inline=inline)

            # Fix #2: repo2run's RAW Dockerfile clones at HEAD (`git clone <url>.git` -> /<basename>)
            # then `cp -r /<basename>/. /repo` and installs; the re-home later moves /repo -> /testbed.
            # So the MEASURED build clones HEAD. Pin the clone BEFORE re-home (and before repo2run's
            # cp/install) so the checkout runs right after the clone. Surface a pin_warning on miss.
            pin_note = ""
            if repo.commit:
                raw, injected = inject_clone_pin(raw, repo.commit, repo.repo_url)
                if not injected:
                    pin_note = "no git clone instruction to pin"
                    economy["pin_warning"] = pin_note

            rehomed = rehome_dockerfile(raw, self.repo_dest)
            # FIX 1 (false-green): repo2run wrote the RAW /repo-homed Dockerfile at
            # output/<full_name>/Dockerfile. bench.harvest._find_dockerfile checks that path BEFORE
            # eval_build/Dockerfile, so a surviving raw file shadows the re-homed one → harvest
            # measures an empty /testbed (false green). Remove it (mirroring the deleted wrapper's
            # _finish_produce_only); if it stubbornly survives, fail safe with status="error".
            raw_path = os.path.join(ctx.workdir, "output", repo.full_name, "Dockerfile")
            try:
                os.remove(raw_path)
            except OSError:
                pass
            if os.path.exists(raw_path):
                return ProducedEnv(repo=repo, dockerfile=None, status="error",
                                   note="raw repo2run Dockerfile still present; harvest would "
                                        "measure the /repo-homed raw instead of the re-homed one",
                                   base_image=res.get("base_image"),
                                   head_sha=res.get("head_sha") or "",
                                   conformance="rehomed", producer_name=self.name,
                                   economy=economy, inline=inline)
            return ProducedEnv(repo=repo, dockerfile=rehomed,
                               base_image=res.get("base_image"),
                               head_sha=res.get("head_sha") or "",
                               status="produced", conformance="rehomed",
                               producer_name=self.name, economy=economy, inline=inline,
                               note=pin_note)
        except Exception as exc:                    # noqa: BLE001 — boundary guard, never propagate
            return ProducedEnv(repo=repo, dockerfile=None, status="error",
                               note=repr(exc), conformance="rehomed",
                               producer_name=self.name,
                               economy={"produce_s": round(time.time() - start, 2)})
