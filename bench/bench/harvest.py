# bench/harvest.py
from __future__ import annotations

import json
import os
import re
from glob import glob

from bench.schema import HarvestedEnv, RepoSpec

_COPY_LINE = re.compile(r"^\s*COPY\s+(.*)$", re.MULTILINE)
# v3 legacy _meta.json uses the same key names we need; only base_image differs in nesting.
_META_NAMES = ("bench_meta.json", "_meta.json")


def _find_dockerfile(repo_dir: str) -> str | None:
    for cand in (os.path.join(repo_dir, "Dockerfile"), os.path.join(repo_dir, "eval_build", "Dockerfile")):
        if os.path.isfile(cand):
            return cand
    return None


def _load_meta(repo_dir: str) -> dict:
    for name in _META_NAMES:
        p = os.path.join(repo_dir, name)
        if os.path.isfile(p):
            try:
                with open(p) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                return {}
    return {}


_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _testbed_path(value: str) -> str:
    """Graph2Env builds at /app and evaluates at /testbed: rewrite colon-separated /app paths,
    exactly as its own EBSR runner does, or PYTHONPATH=/app makes pytest import tests differently."""
    return ":".join("/testbed" + p[4:] if p == "/app" or p.startswith("/app/") else p
                    for p in value.split(":"))


def _load_handoff(df_dir: str) -> dict:
    """runtime_handoff.json -> {"environment", "services"}, or {} if absent/unreadable.

    Schema versions 1 and 2 share these two keys; v2's `test`/`mounts` and `capabilities` are not
    applied (no measured handoff declares a capability)."""
    try:
        with open(os.path.join(df_dir, "runtime_handoff.json")) as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(d, dict) or d.get("version") not in (1, 2):
        return {}
    env = d.get("environment") if isinstance(d.get("environment"), dict) else {}
    environment = {k: _testbed_path(v) for k, v in env.items()
                   if isinstance(k, str) and _ENV_KEY.match(k) and isinstance(v, str)}
    services = [
        {k: s[k].strip() for k in ("kind", "start", "check")}
        for s in (d.get("services") if isinstance(d.get("services"), list) else [])
        if isinstance(s, dict) and all(isinstance(s.get(k), str) and s[k].strip()
                                       for k in ("kind", "start", "check"))
    ]
    return {"environment": environment, "services": services} if environment or services else {}


def _copy_sources(dockerfile: str) -> list:
    """All source operands across COPY lines (skips --flags and the destination)."""
    sources = []
    for args in _COPY_LINE.findall(dockerfile):
        args = args.strip()
        if args.startswith("["):                     # JSON-array form: ["src", ..., "dest"]
            try:
                parts = json.loads(args)
            except (json.JSONDecodeError, ValueError):
                continue
        else:
            parts = args.split()
        parts = [p for p in parts if not p.startswith("--")]   # drop --chown=, --from=, etc.
        if len(parts) >= 2:
            sources.extend(parts[:-1])               # everything except the destination
    return sources


def _sibling_scripts(df_dir: str, dockerfile: str) -> dict:
    """FILE siblings a COPY needs, as {name: text}. Directories are handled by _copy_dirs.

    Inlining a directory here was a mistake: it read and wrote every file with errors="replace",
    which corrupts binary content. RAT's `COPY repo` is a git checkout, so .git/index and the pack
    files came out mangled, `git rev-parse --show-toplevel` failed inside the image, and
    contract._probe_script reported not_git -> non_conforming -> a DISQUALIFIED row. Directories
    are copied on disk instead (see _copy_dirs), which is also why no size cap is needed.
    """
    out = {}
    for src in _copy_sources(dockerfile):
        p = os.path.join(df_dir, os.path.basename(src))
        if os.path.isfile(p):
            with open(p, encoding="utf-8", errors="replace") as f:
                out[os.path.basename(src)] = f.read()
    return out


def _copy_dirs(df_dir: str, dockerfile: str) -> dict:
    """DIRECTORY COPY sources as {context_name: absolute source path}.

    measure() copies these into the build context verbatim, so bytes survive and size is bounded
    by disk rather than memory (huggingface/transformers' checkout alone is 132MB).
    """
    out = {}
    for src in _copy_sources(dockerfile):
        base = os.path.basename(src)
        p = os.path.join(df_dir, base)
        if os.path.isdir(p):
            out[base] = p
    return out


def _is_post_contract(root: str) -> bool:
    """Did this run promise artifacts? The runner writes `_run_manifest.json` at the run root.

    `root` is the run's `output/` dir, so the manifest sits one level up. Its presence separates
    a MODERN run whose producer crashed from genuinely pre-contract data — the two need opposite
    treatment and were previously indistinguishable (design §2.5).
    """
    return os.path.isfile(os.path.join(os.path.dirname(os.path.abspath(root)), "_run_manifest.json"))


def discover(agent_roots: dict) -> list:
    envs = []
    for agent, root in agent_roots.items():
        post_contract = _is_post_contract(root)
        for repo_dir in sorted(glob(os.path.join(root, "*", "*"))):
            if not os.path.isdir(repo_dir):
                continue
            full_name = "/".join(repo_dir.split(os.sep)[-2:])
            meta = _load_meta(repo_dir)
            repo = RepoSpec(full_name, f"https://github.com/{full_name}",
                            language=meta.get("language") or "python")
            df_path = _find_dockerfile(repo_dir)
            # Status resolution (design §2.5): a producer-written _meta.status wins; otherwise this is
            # a legacy (pre-contract) run — legacy_ok if a Dockerfile is on disk, else legacy_missing
            # (NOT "missing", which would force a pre-contract non-producer into a counted EBSR-0).
            if "status" in meta:
                status = meta["status"]
            elif df_path is not None:
                status = "legacy_ok"
            elif post_contract:
                # A post-contract run with no Dockerfile is a PRODUCER FAILURE, not a
                # pre-contract non-producer. "missing" stays in every denominator, so it scores a
                # counted EBSR 0; "legacy_missing" would drop it (metrics.py:55, :214) and quietly
                # shrink the population an arm is judged over.
                status = "missing"
            else:
                status = "legacy_missing"
            if df_path is None:
                envs.append(HarvestedEnv(agent, repo, None, {}, meta.get("base_image"), status, meta))
                continue
            with open(df_path) as f:
                df = f.read()
            df_dir = os.path.dirname(df_path)
            scripts = _sibling_scripts(df_dir, df)
            envs.append(HarvestedEnv(agent, repo, df, scripts, meta.get("base_image"), status, meta,
                                     runtime=_load_handoff(df_dir),
                                     copy_dirs=_copy_dirs(df_dir, df)))
    return envs
