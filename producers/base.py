# producers/base.py
#
# The PRODUCE-side contract (design §1 of
# docs/superpowers/specs/2026-07-21-produce-then-measure-runner-design.md).
#
# A Producer turns a RepoSpec into a ProducedEnv (a Dockerfile string + metadata).
# It NEVER builds, runs pytest, or scores — bench/ owns MEASURE. The only thing that
# crosses the produce->measure seam is a directory of static files written by
# `write_env_packet`, exactly at the paths `bench.harvest.discover()` already globs.
from __future__ import annotations

import json
import os
import re as _re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

# `bench.schema` is the SHARED type module (design §1: "producers and bench share only
# bench.schema types"). The bench package lives at <repo>/bench/bench/, so <repo>/bench must
# be importable. Add it here (idempotent) so `producers` works regardless of how it was loaded
# — including when driven from the RAT harness tree, where cwd/PYTHONPATH point elsewhere.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BENCH_DIR = os.path.join(_REPO_ROOT, "bench")
if _BENCH_DIR not in sys.path:
    sys.path.insert(0, _BENCH_DIR)

from bench.schema import RepoSpec  # noqa: E402  (import after the sys.path shim above)

CONTRACT_VERSION = 1


# ── Commit-pin injection for EMITTED Dockerfiles ─────────────────────────────────────────────
#
# bench REBUILDS the Dockerfile a producer emits and measures THAT build. If the Dockerfile's own
# `git clone` runs at the live default-branch HEAD, the measured environment silently drifts off
# the dataset SHA. `inject_clone_pin` rewrites the clone's checkout to the pinned commit — used by
# every producer whose emitted Dockerfile clones the repo at build time (dockeragent /
# claudecode-dockerfile / repo2run). This is the ONE robust place that shell-line-continuations,
# clone flags, and dest-vs-basename are handled correctly.

# Shell control operators that terminate the `git clone` argument list (tokens after them belong
# to a separate command, not to clone). Whitespace-tokenized, so only exact tokens match.
_SHELL_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", ">", ">>", "<", "2>", "2>&1"})
# `git clone` flags whose VALUE is the NEXT token (space form). The `=` form (e.g. `--depth=1`) is
# a single token starting with '-' and is dropped by the generic flag rule below.
_VALUE_FLAGS = frozenset({"--depth", "-b", "--branch", "-o", "--origin", "-j", "--jobs"})


def _clone_dest(after_clone: str, workdir: str = "/") -> Optional[str]:
    """Given the substring AFTER ``git clone``, return the clone DESTINATION path (or None if the
    URL can't be identified). An explicit dest token following the URL wins (e.g. ``/testbed``);
    otherwise the dest is the URL basename without ``.git`` (and without any ``?query``/``#frag``).

    A relative dest — the bare basename, or an explicit ``.``/``sub/dir`` — is resolved against
    ``workdir``, the WORKDIR in effect at that instruction. repo2run clones at WORKDIR ``/`` so
    ``https://github.com/o/r.git`` -> ``/r``; ExecutionAgent clones at ``WORKDIR /app`` so the same
    URL -> ``/app/r``. Assuming ``/`` for everyone pinned the wrong directory and broke the build.
    """
    tokens: list = []
    for t in after_clone.split():
        if t in _SHELL_SEPARATORS:
            break
        tokens.append(t)
    # Drop flags, plus the space-form value that follows a value-taking flag.
    positional: list = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("-"):
            i += 2 if t in _VALUE_FLAGS else 1
            continue
        positional.append(t)
        i += 1
    url_idx = next((k for k, t in enumerate(positional)
                    if "://" in t or t.endswith(".git") or "github.com" in t), None)
    if url_idx is None:
        return None
    if url_idx + 1 < len(positional):
        dest = positional[url_idx + 1]           # explicit destination (e.g. /testbed, or `.`)
    else:
        base = positional[url_idx].rstrip("/").rsplit("/", 1)[-1]
        base = base.split("?", 1)[0].split("#", 1)[0]   # drop ?query / #fragment before basename
        if base.endswith(".git"):
            base = base[: -len(".git")]
        dest = base
    if dest.startswith("/"):
        return dest
    return os.path.normpath(os.path.join(workdir or "/", dest))   # relative to the WORKDIR


def _repo_slug(repo_url: Optional[str]) -> Optional[str]:
    """`owner/repo` (lowercased, `.git`/query stripped) from a clone URL, or None if unparseable.
    Used to pin the clone of the TARGET repo specifically, not an unrelated helper-tool clone."""
    if not repo_url:
        return None
    s = repo_url.strip().rstrip("/").split("://", 1)[-1].split("@", 1)[-1]   # drop scheme + user@
    parts = s.replace(":", "/").split("/")                                   # host[:/]owner/repo
    if len(parts) < 3:
        return None
    slug = "/".join(parts[-2:]).split("?", 1)[0].split("#", 1)[0]
    if slug.endswith(".git"):
        slug = slug[: -len(".git")]
    return slug.lower()


# A clone RUN that ALSO copies/moves/removes the clone dir is unsafe to pin AFTER (the dir may be
# gone / the install already ran on HEAD). Refuse it (visible miss) rather than emit a false pin.
_MUTATES_CLONE = _re.compile(r"(?:^|\s)(?:rm|mv|cp)\s")


def inject_clone_pin(dockerfile: str, commit: str, repo_url: Optional[str] = None) -> tuple:
    """Pin the TARGET repo's ``RUN ... git clone ...`` instruction to ``commit``.

    Finds the git-clone RUN for ``repo_url`` (matched by its ``owner/repo`` slug; falls back to the
    first git-clone RUN when ``repo_url`` is None), following backslash line-continuations so a
    multi-physical-line ``RUN`` is treated as ONE instruction, and inserts ``RUN git -C <dest> fetch
    --depth 1 origin <commit> && git -C <dest> checkout --detach <commit>`` right AFTER the
    instruction's LAST physical line (never mid-instruction — that corrupts the build). Only real
    ``RUN`` instructions match (comments / non-RUN lines are skipped). Returns
    ``(new_dockerfile, True)`` on inject, or ``(dockerfile, False)`` — a VISIBLE miss the caller
    surfaces — when there is no matching clone RUN, the URL can't be parsed, or the clone RUN also
    cp/mv/rm's the clone (pinning after it would be wrong). The pin is never silently dropped.
    Trailing newline preserved."""
    target = _repo_slug(repo_url)
    lines = dockerfile.split("\n")

    def _continued(s: str) -> bool:
        return s.rstrip().endswith("\\")

    i, n = 0, len(lines)
    workdir = "/"          # the WORKDIR in effect at the instruction being scanned
    while i < n:
        start = i
        # Extend across backslash continuations to the instruction's last physical line.
        while i < n - 1 and _continued(lines[i]):
            i += 1
        end = i  # inclusive
        parts = []
        for j in range(start, end + 1):
            s = lines[j].rstrip()
            if s.endswith("\\"):
                s = s[:-1]
            parts.append(s)
        joined = " ".join(parts)
        stripped = joined.lstrip()
        # Track WORKDIR so a relative clone dest resolves against it (a Dockerfile WORKDIR is
        # itself relative to the previous one). Only plain absolute/relative paths are tracked;
        # a $VAR form leaves the previous value standing rather than guessing at build-time env.
        if stripped.upper().startswith("WORKDIR ") and not stripped.startswith("#"):
            wd = stripped.split(None, 1)[1].strip().strip('"\'')
            if wd and "$" not in wd:
                workdir = wd if wd.startswith("/") else os.path.normpath(os.path.join(workdir, wd))
            i = end + 1
            continue
        # Only pin real `RUN` shell instructions — never a comment (`# ... git clone ...`) or a
        # non-RUN line that merely mentions the words.
        if not stripped.upper().startswith("RUN ") or stripped.startswith("#"):
            i = end + 1
            continue
        gc = joined.find("git clone")
        if gc == -1:
            i = end + 1
            continue
        after = joined[gc + len("git clone"):]
        # When we know the target repo, only pin the clone of THAT repo (skip helper-tool clones).
        if target is not None and target not in joined.lower():
            i = end + 1
            continue
        if _MUTATES_CLONE.search(after):
            return dockerfile, False              # compound clone+cp/mv/rm — refuse, surface a miss
        dest = _clone_dest(after, workdir)
        if dest is None:
            return dockerfile, False
        pin = (f"RUN git -C {dest} fetch --depth 1 origin {commit} "
               f"&& git -C {dest} checkout --detach {commit}")
        return "\n".join(lines[: end + 1] + [pin] + lines[end + 1:]), True
    return dockerfile, False


@dataclass(frozen=True)
class ProduceContext:
    """Everything a producer needs to run for ONE repo (design §1)."""
    llm: Optional[str]          # model slug; None => producer default
    workdir: str                # scratch for THIS repo (agent workplace/, clones) — never the build ctx
    num_turn: Optional[int] = None
    timeout: int = 3600
    agent_root: Optional[str] = None  # the dockeragent checkout root (holds the branch's own multi_docker_eval_adapter.py)


@dataclass(frozen=True)
class ProducedEnv:
    """The produce->measure interface value (design §1). `status`/`conformance`/`unreplayed`/
    `inline`/`economy` are metadata surfaced in `_meta.json` for bench + audit."""
    repo: RepoSpec
    dockerfile: Optional[str]                             # None => this method emits no static artifact
    setup_scripts: dict = field(default_factory=dict)    # {basename: content} the Dockerfile COPYs
    base_image: Optional[str] = None
    head_sha: Optional[str] = None                        # commit the Dockerfile pins the clone to
    status: str = "produced"                             # "produced" | "unmeasurable" | "error"
    note: str = ""                                       # why unmeasurable/error, or "synthesized (lossy)"
    exit_status: Optional[str] = None                    # agent-reported outcome (e.g. sweagent's
                                                           # result.info["exit_status"]: "submitted",
                                                           # "exit_cost", ...). OPTIONAL: absent for
                                                           # every producer that doesn't have one.
    agent_settings: dict = field(default_factory=dict)   # EFFECTIVE agent knobs as they reached the
                                                         # wire, for settings an env var can flip at
                                                         # run time. The copied config file records
                                                         # the DEFAULT; an override would leave no
                                                         # trace anywhere else, and some of these
                                                         # (e.g. DeepSeek thinking mode) are not
                                                         # recoverable from the trajectory.
    deploy_image_digest: Optional[str] = None            # resolved digest of the agent's OWN
                                                           # deployment/base image (provenance only)
    conformance: str = "native"                          # "native" | "rehomed" | "synthesized"
    unreplayed: bool = False                             # True only for lossy replay (rat)
    inline: Optional[dict] = None                        # method's own live score for the delta
    economy: dict = field(default_factory=dict)          # tokens_in/out, llm_calls, turns_used, produce_s
    producer_name: str = ""                              # registry key of the producer that made this


@runtime_checkable
class Producer(Protocol):
    name: str            # registry key (matches varieties.toml `producer = ...`)
    needs_llm: bool      # True => produce needs API keys (skipped in a pure --measure run)
    measurable: bool     # False => NEVER harvested into reproducible EBSR/ESSR (gated; no false EBSR-0)

    def produce(self, repo: RepoSpec, ctx: ProduceContext) -> ProducedEnv: ...


def ensure_rat_on_path() -> str:
    """Prepend the RAT framework root (the dir that CONTAINS libkit/ and eval/) to sys.path.

    The live-only producer runners (producers/{repo2run,claudecode_dockerfile}.py) lazily import
    libkit/eval; from an env-bench checkout those live under <repo>/rat/, which is NOT on the path
    by default (the *models* set their own path up; standalone producers must too). First valid
    candidate wins: $RAT_ROOT, dirs derived from $AGENTS_ROOT/$DOCKERAGENT_ROOT, then <repo>/rat
    (env-bench) and <repo> (a /opt-style layout where the RAT root IS the repo root). Raises a
    clear error if libkit/command.py is nowhere — a loud failure beats a silent wrong path."""
    seen, cands = set(), []

    def _add(p):
        if p and p not in seen:
            seen.add(p)
            cands.append(p)

    _add(os.environ.get("RAT_ROOT"))
    for var in ("AGENTS_ROOT", "DOCKERAGENT_ROOT"):
        v = (os.environ.get(var) or "").rstrip("/")
        if v:
            _add(v)
            _add(os.path.join(v, "rat"))
    _add(os.path.join(_REPO_ROOT, "rat"))    # env-bench: <repo>/rat holds libkit/ + eval/
    _add(_REPO_ROOT)                          # /opt-style: RAT root == repo root
    for root in cands:
        if os.path.isfile(os.path.join(root, "libkit", "command.py")):
            if root not in sys.path:
                sys.path.insert(0, root)
            return root
    raise RuntimeError(
        "cannot locate the RAT framework root (libkit/command.py). Set RAT_ROOT to the directory "
        f"that contains libkit/ and eval/. Tried: {cands}")


def _atomic_write(path: str, content: str) -> None:
    """Write `content` to `path` atomically (write to a temp sibling, then os.replace)."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def write_env_packet(out_root: str, env: ProducedEnv) -> str:
    """Write the env packet for ONE repo under out_root/<owner>/<repo>/ (design §1).

    Layout:
      out_root/<full_name>/eval_build/Dockerfile   + COPY siblings  (ONLY when produced+dockerfile)
      out_root/<full_name>/_meta.json                               (ALWAYS)

    [verified-fix] An empty eval_build/ for a non-produced status would be mis-read by
    bench.harvest as a *vanished* Dockerfile, so we create eval_build/ ONLY when there is a
    real Dockerfile to land. `_meta.json` is ALWAYS written so harvest can distinguish a
    declared not-measurable env from a producer that silently vanished.
    """
    repo_dir = os.path.join(out_root, env.repo.full_name)   # out_root/<owner>/<repo>
    os.makedirs(repo_dir, exist_ok=True)
    build_dir = os.path.join(repo_dir, "eval_build")
    if env.status == "produced" and env.dockerfile:
        os.makedirs(build_dir, exist_ok=True)
        _atomic_write(os.path.join(build_dir, "Dockerfile"), env.dockerfile)
        for name, content in (env.setup_scripts or {}).items():
            _atomic_write(os.path.join(build_dir, os.path.basename(name)), content)
    else:
        # [verified-fix] A repo re-produced as error/unmeasurable must NOT keep a stale Dockerfile
        # from a prior produced run — harvest would still build+measure it. Drop any old eval_build/.
        shutil.rmtree(build_dir, ignore_errors=True)
    economy = env.economy or {}
    _atomic_write(os.path.join(repo_dir, "_meta.json"), json.dumps({
        "contract_version": CONTRACT_VERSION,
        "producer": env.producer_name,
        "status": env.status,
        "note": env.note,
        "exit_status": env.exit_status,
        "agent_settings": env.agent_settings or None,
        "deploy_image_digest": env.deploy_image_digest,
        "conformance": env.conformance,
        "unreplayed": env.unreplayed,
        "inline": env.inline,
        "base_image": env.base_image,
        "head_sha": env.head_sha,
        "full_name": env.repo.full_name,
        "repo_url": env.repo.repo_url,
        "language": env.repo.language,
        "tokens_in": economy.get("tokens_in"),
        "tokens_out": economy.get("tokens_out"),
        "llm_calls": economy.get("llm_calls"),
        "turns_used": economy.get("turns_used"),
        "produce_s": economy.get("produce_s"),
        "total_tokens": economy.get("total_tokens"),
        "cost_usd": economy.get("cost_usd"),
    }, indent=2))
    return repo_dir
