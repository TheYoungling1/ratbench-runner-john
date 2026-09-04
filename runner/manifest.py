from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess

MANIFEST_NAME = "_run_manifest.json"
_SWEAGENT_MODELS = frozenset({"sweagent", "sweagent_repo2run"})


def write_manifest(out_dir: str, **fields) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, MANIFEST_NAME)
    with open(path, "w") as f:
        json.dump(fields, f, indent=2, sort_keys=True)
    return path


def update_status(out_dir: str, status: str) -> None:
    path = os.path.join(out_dir, MANIFEST_NAME)
    with open(path) as f:
        data = json.load(f)
    data["status"] = status
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def worktree_dirty(path: str | None = None, runner=subprocess.run) -> bool:
    """True when the harness tree has uncommitted changes to TRACKED files.

    `dirty` used to be hardcoded False, so every run claimed a clean tree and `harness_commit`
    named a commit that need not contain the code that produced the rows. That is the one
    provenance field whose whole job is telling a future reader whether the recorded commit
    describes the run — a constant False is worse than no field at all.

    Untracked files are deliberately NOT dirt: runs/, scratch datasets and editor droppings live
    in the tree constantly and would pin every run to dirty=True, which is the same lie inverted.
    Unreadable git state answers True, since "cannot prove clean" is not "clean".
    """
    root = path or os.environ.get("REPO_ROOT") or os.getcwd()
    try:
        r = runner(["git", "-C", root, "status", "--porcelain", "--untracked-files=no"],
                   capture_output=True, text=True, timeout=30)
    except Exception:                    # noqa: BLE001 — provenance must not sink a run
        return True
    return bool(r.stdout.strip()) if r.returncode == 0 else True


def manifest_fields_from_env(*, model: str, tier: str, num_turn,
                             concurrency, status: str,
                             sweagent_commit: str | None = None,
                             sweagent_config: dict | None = None,
                             host: dict | None = None,
                             dataset: dict | None = None) -> dict:
    """Build run-level provenance from the env the bench launcher exported.

    `sweagent_commit`/`sweagent_config`/`host`/`dataset` are ADDITIVE reproducibility fields
    (see sweagent_commit(), copy_sweagent_config(), host_platform(), dataset_provenance() below)
    — all optional, all default to None/absent so no existing key changes meaning."""
    return dict(
        variety=os.environ.get("RUN_VARIETY", "unknown"),
        agent_branch=os.environ.get("RUN_AGENT_BRANCH", ""),
        agent_commit=os.environ.get("AGENT_COMMIT", ""),
        harness_commit=os.environ.get("HARNESS_COMMIT", ""),
        dirty=worktree_dirty(),
        model=model,
        tier=tier,
        num_turn=num_turn,
        concurrency=concurrency,
        status=status,
        sweagent_commit=sweagent_commit,
        sweagent_config=sweagent_config,
        host=host,
        dataset=dataset,
    )


def sweagent_commit(model: str, venv_py: str, runner=subprocess.run) -> str:
    """Commit hash of the SWE-agent editable install `venv_py` resolves to (design item 1).

    Only meaningful for the sweagent/sweagent_repo2run arms, which subprocess a SWE-agent clone
    at `venv_py` — an unrelated arm never touches that venv, so reporting a commit for it would
    be misleading, not merely useless. Derived from the installed package's location
    (`sweagent.__file__`'s repo — `git -C <that dir> rev-parse HEAD` walks up to the repo root on
    its own, no separate `--show-toplevel` round trip needed). `runner` is injectable so this is
    unit-testable without a real venv or git. Never raises: degrades to "unknown" when the model
    isn't a sweagent arm, the venv/interpreter is missing, sweagent isn't importable there, or git
    fails for any reason — provenance capture must never be why a run dies."""
    if model not in _SWEAGENT_MODELS:
        return "unknown"
    # `import sweagent` PRINTS A BANNER TO STDOUT ("👋 INFO This is SWE-agent version ...").
    # So stdout is never just the value — always take the LAST non-empty line. An earlier version
    # captured the whole of stdout as a path and fed banner-plus-path to `git -C`, which failed and
    # silently reported "unknown" for every run.
    def _last_line(proc) -> str:
        if proc.returncode != 0:
            return ""
        lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
        return lines[-1] if lines else ""

    try:
        root = _last_line(runner(
            [venv_py, "-c",
             "import os, sweagent; print(os.path.dirname(os.path.abspath(sweagent.__file__)))"],
            capture_output=True, text=True, timeout=30))
        if not root:
            return "unknown"
        return _last_line(runner(["git", "-C", root, "rev-parse", "HEAD"],
                                 capture_output=True, text=True, timeout=30)) or "unknown"
    except Exception:                                    # noqa: BLE001 — anti-vanish, never propagate
        return "unknown"


def copy_sweagent_config(src: str, out_dir: str, subdir: str = "configs") -> dict:
    """Copy the sweagent_repo2run agent config verbatim into the run dir (design item 2), so a
    later edit to the source config cannot make this run un-interpretable, and record its
    sha256. Returns ``{"path": <run-relative dest>, "sha256": <hex>}``, or both None on any
    failure (missing source, unwritable run dir, ...) — never raises."""
    try:
        os.makedirs(os.path.join(out_dir, subdir), exist_ok=True)
        with open(src, "rb") as f:
            raw = f.read()
        dest_rel = os.path.join(subdir, os.path.basename(src))
        with open(os.path.join(out_dir, dest_rel), "wb") as f:
            f.write(raw)
        return {"path": dest_rel, "sha256": hashlib.sha256(raw).hexdigest()}
    except OSError:
        return {"path": None, "sha256": None}


def host_platform() -> dict:
    """Host machine/OS/python (design item 5): arm64 vs amd64 changes wheel resolution and
    therefore scores. Stdlib-only; cheap enough to call unconditionally on every run."""
    return {
        "machine": platform.machine(),
        "system": platform.system(),
        "python_version": platform.python_version(),
    }


def dataset_provenance(path: str) -> dict:
    """sha256 + repo count of the dataset file actually used (design item 6). Returns
    ``{"path", "sha256", "repo_count"}``; `sha256`/`repo_count` fall back to None on a missing or
    malformed file (a corrupt dataset must not be why a run's provenance capture fails) — `path`
    is always the value given, even when the file couldn't be read."""
    result = {"path": path, "sha256": None, "repo_count": None}
    if not path:
        return result
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return result
    result["sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw)
        repos = data["repos"] if isinstance(data, dict) else data
        result["repo_count"] = len(repos)
    except Exception:                                    # noqa: BLE001 — sha256 alone still stands
        pass
    return result
