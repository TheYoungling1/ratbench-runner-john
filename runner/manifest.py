from __future__ import annotations

import json
import os

MANIFEST_NAME = "_run_manifest.json"


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


def manifest_fields_from_env(*, model: str, tier: str, num_turn,
                             concurrency, status: str) -> dict:
    """Build run-level provenance from the env the bench launcher exported."""
    return dict(
        variety=os.environ.get("RUN_VARIETY", "unknown"),
        agent_branch=os.environ.get("RUN_AGENT_BRANCH", ""),
        agent_commit=os.environ.get("AGENT_COMMIT", ""),
        harness_commit=os.environ.get("HARNESS_COMMIT", ""),
        dirty=False,
        model=model,
        tier=tier,
        num_turn=num_turn,
        concurrency=concurrency,
        status=status,
    )
