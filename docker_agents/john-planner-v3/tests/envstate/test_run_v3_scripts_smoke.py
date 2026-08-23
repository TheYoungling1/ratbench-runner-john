"""Task 8d: smoke tests for the two e2e-proof driver scripts.

``scripts/run_v3_e2e.py`` and ``scripts/run_v3_proof.py`` both require Docker
+ a real LLM API key to run end to end, so they get NO execution coverage
here — only:
  1. the module imports cleanly (heavy imports stay deferred inside main()),
  2. its argparse parser builds and parses the flags this task added
     (``--trace-out`` / ``--repos``) without touching the Docker/LLM path.

The pure logic each driver delegates to (``finalize_trace``/``repo_row``/
``aggregate``/``canonical_success``/``trace_from_dict``) is covered by
``tests/envstate/test_proof.py``.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------------------------------------------------------------------------
# run_v3_e2e.py
# ---------------------------------------------------------------------------

def test_run_v3_e2e_imports_without_docker_or_llm_key():
    module = importlib.import_module("scripts.run_v3_e2e")
    assert hasattr(module, "main")
    assert hasattr(module, "_build_arg_parser")


def test_run_v3_e2e_argparse_parses_trace_out():
    module = importlib.import_module("scripts.run_v3_e2e")
    args = module._build_arg_parser().parse_args(
        ["/tmp/some-repo", "--trace-out", "/tmp/out/trace.json"]
    )
    assert args.repo == "/tmp/some-repo"
    assert args.trace_out == "/tmp/out/trace.json"


def test_run_v3_e2e_argparse_trace_out_defaults_to_none():
    module = importlib.import_module("scripts.run_v3_e2e")
    args = module._build_arg_parser().parse_args(["/tmp/some-repo"])
    assert args.trace_out is None


def test_run_v3_e2e_argparse_still_has_existing_flags():
    module = importlib.import_module("scripts.run_v3_e2e")
    args = module._build_arg_parser().parse_args(
        ["/tmp/some-repo", "--model", "gpt-4o", "--base-image", "python:3.12-slim",
         "--out", "/tmp/out/setup.sh"]
    )
    assert args.model == "gpt-4o"
    assert args.base_image == "python:3.12-slim"
    assert args.out == "/tmp/out/setup.sh"


# ---------------------------------------------------------------------------
# run_v3_proof.py
# ---------------------------------------------------------------------------

def test_run_v3_proof_imports_without_docker_or_llm_key():
    module = importlib.import_module("scripts.run_v3_proof")
    assert hasattr(module, "main")
    assert hasattr(module, "_build_arg_parser")


def test_run_v3_proof_argparse_parses_repos_list():
    module = importlib.import_module("scripts.run_v3_proof")
    args = module._build_arg_parser().parse_args(["--repos", "/tmp/repo-a", "/tmp/repo-b"])
    assert args.repos == ["/tmp/repo-a", "/tmp/repo-b"]
    assert args.manifest is None


def test_run_v3_proof_argparse_parses_manifest():
    module = importlib.import_module("scripts.run_v3_proof")
    args = module._build_arg_parser().parse_args(["--manifest", "/tmp/repos.txt"])
    assert args.manifest == "/tmp/repos.txt"
    assert args.repos is None


def test_run_v3_proof_argparse_requires_repos_or_manifest():
    module = importlib.import_module("scripts.run_v3_proof")
    import pytest
    with pytest.raises(SystemExit):
        module._build_arg_parser().parse_args([])


def test_run_v3_proof_load_repos_from_explicit_list():
    module = importlib.import_module("scripts.run_v3_proof")
    args = module._build_arg_parser().parse_args(["--repos", "/tmp/repo-a", "/tmp/repo-b"])
    assert module._load_repos(args) == ["/tmp/repo-a", "/tmp/repo-b"]


def test_run_v3_proof_load_repos_from_manifest_file(tmp_path):
    manifest = tmp_path / "repos.txt"
    manifest.write_text("/tmp/repo-a\n# a comment\n\n/tmp/repo-b\n")
    module = importlib.import_module("scripts.run_v3_proof")
    args = module._build_arg_parser().parse_args(["--manifest", str(manifest)])
    assert module._load_repos(args) == ["/tmp/repo-a", "/tmp/repo-b"]
