# harness_cli/provision.py
from __future__ import annotations

import os
import subprocess


def symlink_glue(src_dir: str, dest_dir: str, filenames: list[str]) -> list[str]:
    """Symlink our model files from the harness checkout into the RAT eval/models
    tree. Idempotent. Leaves any other (third-party) files in dest untouched.
    Returns the created link paths."""
    created: list[str] = []
    for name in filenames:
        src = os.path.join(src_dir, name)
        dst = os.path.join(dest_dir, name)
        if not os.path.exists(src):
            raise FileNotFoundError(f"glue source missing: {src}")
        if os.path.islink(dst) or os.path.exists(dst):
            os.remove(dst)
        os.symlink(src, dst)
        created.append(dst)
    return created


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
