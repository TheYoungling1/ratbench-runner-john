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


@dataclass(frozen=True)
class ProduceContext:
    """Everything a producer needs to run for ONE repo (design §1)."""
    llm: Optional[str]          # model slug; None => producer default
    workdir: str                # scratch for THIS repo (agent workplace/, clones) — never the build ctx
    num_turn: Optional[int] = None
    timeout: int = 3600


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
        "conformance": env.conformance,
        "unreplayed": env.unreplayed,
        "inline": env.inline,
        "base_image": env.base_image,
        "head_sha": env.head_sha,
        "full_name": env.repo.full_name,
        "repo_url": env.repo.repo_url,
        "tokens_in": economy.get("tokens_in"),
        "tokens_out": economy.get("tokens_out"),
        "llm_calls": economy.get("llm_calls"),
        "turns_used": economy.get("turns_used"),
        "produce_s": economy.get("produce_s"),
    }, indent=2))
    return repo_dir
