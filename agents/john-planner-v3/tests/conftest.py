import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Pre-existing collection errors unrelated to the contract-graph v2 rewrite.
# test_docker_build.py references a missing external file
#   (/Multi-Docker-Eval/evaluation/docker_build.py).
# test_run_rat_benchmark.py previously referenced a missing module (eval.common)
#   but Phase 1 stubs resolved that; it now imports and collects cleanly (29 passed).
collect_ignore = [
    "test_docker_build.py",
]
