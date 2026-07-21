# harness_cli/provision.py
from __future__ import annotations

import os
import subprocess


def git_commit(path: str) -> str:
    """Short HEAD commit of a checkout."""
    out = subprocess.run(
        ["git", "-C", path, "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def provision_agent(agent_root: str, branch: str, remote: str = "origin") -> str:
    """Fetch the pushed branch HEAD and hard-reset to it. NEVER git clean — this
    preserves untracked files (e.g. the agent's workplace/). Returns short commit."""
    if not os.environ.get("SKIP_PROVISION"):
        subprocess.run(["git", "-C", agent_root, "fetch", remote, branch], check=True)
        subprocess.run(["git", "-C", agent_root, "reset", "--hard", "FETCH_HEAD"], check=True)
    return git_commit(agent_root)
