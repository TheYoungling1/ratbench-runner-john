"""Clone corpus repos at their pinned ref into a gitignored _smoke root (shallow,
single-ref). Reused by the CLI's --fetch and --run."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.eval.build_script_eval.corpus import RepoSpec  # noqa: E402

_SMOKE = _REPO_ROOT / "outputs" / "build_script_eval" / "_smoke"


def smoke_root() -> Path:
    _SMOKE.mkdir(parents=True, exist_ok=True)
    return _SMOKE


def fetch_repo(spec: RepoSpec, *, smoke_root: Path | None = None) -> Path:
    """Shallow-clone one repo at its pinned ref. Idempotent: an existing non-empty
    dir is left as-is (delete to re-fetch)."""
    root = smoke_root or _SMOKE
    root.mkdir(parents=True, exist_ok=True)
    dest = root / spec.name
    if dest.exists() and any(dest.iterdir()):
        return dest
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", spec.ref, spec.git_url, str(dest)],
        check=True, capture_output=True, text=True, timeout=600,
    )
    return dest


def fetch_corpus(specs, *, smoke_root: Path | None = None) -> list[Path]:
    return [fetch_repo(s, smoke_root=smoke_root) for s in specs]
