# harness_cli/provision.py
from __future__ import annotations

import os
import subprocess


def git_commit(path: str) -> str:
    """Short HEAD commit of a checkout, best-effort.

    A non-git deploy dir (e.g. /opt after the harness->runner rename — /opt is NOT a git repo,
    only the old /opt/harness was) has no .git, so `git rev-parse` would raise and crash the run
    before any benchmark launches. Degrade: fall back to a `.deployed_commit` sidecar (written at
    deploy time), then "unknown". Provenance must never be fatal. Real git checkouts (the agent
    roots under /opt/agents/<v>) still return their true short HEAD."""
    try:
        out = subprocess.run(
            ["git", "-C", path, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        try:
            with open(os.path.join(path, ".deployed_commit")) as f:
                return f.read().strip() or "unknown"
        except OSError:
            return "unknown"


def provision_agent(agent_root: str, branch: str, remote: str = "origin") -> str:
    """Fetch the pushed branch HEAD and hard-reset to it. NEVER git clean — this
    preserves untracked files (e.g. the agent's workplace/). Returns short commit."""
    if not os.environ.get("SKIP_PROVISION"):
        subprocess.run(["git", "-C", agent_root, "fetch", remote, branch], check=True)
        subprocess.run(["git", "-C", agent_root, "reset", "--hard", "FETCH_HEAD"], check=True)
    return git_commit(agent_root)
