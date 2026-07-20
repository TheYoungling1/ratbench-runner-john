#!/usr/bin/env python3
"""Make the repo2run baseline emit agent_run_summary.json (eval/models/repo2run_model.py).

``Repo2RunModel.predict()`` returns only a tiny ``{status, root_path, full_name}`` dict and never
writes an ``agent_run_summary.json``. So the RATBench rollups (per_repo_table.json /
in_sandbox_score.json, built by scripts/emit_run_tables.py) see no telemetry for repo2run: its
in-container pytest result (``ib``) and its LLM token cost (``tok``) both surface as null. Yet the
data already exists on disk per repo: ``output/<repo>/track.json`` carries ``cost_tokens`` (a TOTAL
prompt+completion count -- the Repo2Run configuration agent accumulates
``cost_tokens += usage["total_tokens"]``) and ``output/<repo>/run_pytest_results.json`` carries the
in-sandbox pytest summary.

This patch injects a best-effort helper ``_write_repo2run_summary()`` and calls it immediately before
each ``return`` in ``predict()`` (success / 2x timeout / 2x error). The helper reads those two files
and writes ``output/<repo>/agent_run_summary.json`` with exactly the two fields emit_run_tables.py
consumes:

  * ``token_usage = {"total_tokens": cost_tokens}``  -> _tokens()  -> tok
  * ``best_in_sandbox_test_result = {command, success, pass_rate}`` -> _in_build() -> ib

``success`` uses the same strict all-green gate the other arms use (passed>0 AND failed==0 AND
errors==0); _in_build() only reports pass_rate when success, so a build that ran tests but had
failures yields ib=null (honest), matching dockeragent/radical semantics. The helper is wrapped so it
never raises, and is ALWAYS called -- even on failure paths it writes a stub (best=None /
token_usage=None) so a missing summary means "honest null", not silence.

It writes DIRECTLY into ``output/<repo>/`` (not the shared workplace), which is exactly where the
runner's _persist_agent_summary looks first and no-ops if a file already exists -- so this bypasses
cross-run contamination. It deliberately does NOT emit any ``{owner}__{repo}.json`` attribution
signal (out of scope; would risk mis-bucketing) and does NOT touch repair-loop tokens.

Idempotent (no-op if already patched). repo2run-only: editing this file does NOT affect a concurrent
``--model dockeragent`` run. Re-run after re-provisioning the RAT tree.

Usage: emit_repo2run_summary.py [RAT_ROOT]      # default $RAT_ROOT or /opt/runanything/src
"""
import os
import sys

RAT_ROOT = (sys.argv[1] if len(sys.argv) > 1
            else os.environ.get("RAT_ROOT", "/opt/runanything/src"))
F = os.path.join(RAT_ROOT, "eval", "models", "repo2run_model.py")

SENTINEL = "_write_repo2run_summary"

# Module-level helper inserted between the imports and `class Repo2RunModel`.
HELPER_FN = '''def _write_repo2run_summary(output_dir, full_name, status, start_time, error=None):
    """Emit agent_run_summary.json so repo2run's tokens (track.json) + in-container pytest
    surface as token_usage + best_in_sandbox_test_result. Best-effort; never raises."""
    try:
        import os
        import json
        cost_tokens = None
        tp = os.path.join(output_dir, "track.json")
        if os.path.exists(tp):
            try:
                with open(tp) as fh:
                    track = json.load(fh)
                cost_tokens = next((e["cost_tokens"] for e in reversed(track)
                                    if isinstance(e, dict) and "cost_tokens" in e), None)
            except Exception:
                cost_tokens = None
        best = None
        rp = os.path.join(output_dir, "run_pytest_results.json")
        if os.path.exists(rp):
            try:
                with open(rp) as fh:
                    s = (json.load(fh).get("summary") or {})
                total = s.get("total_tests", 0) or 0
                passed = s.get("passed", 0) or 0
                skipped = s.get("skipped", 0) or 0
                failed = s.get("failed", 0) or 0
                errors = s.get("errors", 0) or 0
                eff = total - skipped
                pass_rate = (passed / eff) if eff > 0 else ((passed / total) if total > 0 else None)
                success = (passed > 0) and (failed == 0) and (errors == 0)
                best = {"command": "pytest", "success": bool(success), "pass_rate": pass_rate}
            except Exception:
                best = None
        summary = {
            "source": "repo2run",
            "status": status,
            "error": error,
            "best_in_sandbox_test_result": best,
            "token_usage": ({"total_tokens": cost_tokens} if cost_tokens is not None else None),
        }
        os.makedirs(output_dir, exist_ok=True)
        with open(os.path.join(output_dir, "agent_run_summary.json"), "w") as f:
            json.dump(summary, f, indent=2)
    except Exception:
        pass
'''

# Anchor blocks (verified byte-exact against the file). Each is globally unique; the helper call is
# inserted immediately BEFORE the corresponding `return {` (never inside a finally).
_CALL = "                _write_repo2run_summary(output_dir, full_name, "

_SUCCESS_OLD = (
    '                return {\n'
    '                    "status": "success",\n'
)
_TIMEOUT_TE_OLD = (
    '                return {\n'
    '                    "status": "timeout",\n'
    '                    "root_path": self.root_path,\n'
    '                    "full_name": full_name,\n'
    '                }\n'
    '            except TimeoutException as e:\n'
)
_TIMEOUT_TX_OLD = (
    '                return {\n'
    '                    "status": "timeout",\n'
    '                    "root_path": self.root_path,\n'
    '                    "full_name": full_name,\n'
    '                }\n'
    '            except subprocess.CalledProcessError as e:\n'
)
_ERROR_CPE_OLD = (
    '                return {\n'
    '                    "status": "error",\n'
    '                    "root_path": self.root_path,\n'
    '                    "full_name": full_name,\n'
    '                    "error": str(e),\n'
    '                }\n'
    '            except Exception as e:\n'
)
_ERROR_EXC_OLD = (
    '                traceback.print_exc()\n'
    '                return {\n'
    '                    "status": "error",\n'
)


def _edits(src):
    edits = []
    # 1. top-level `import json` (only if absent) -- keep imports alphabetical.
    if "\nimport json\n" not in src:
        edits.append((
            "import os\nimport subprocess\nimport sys\nimport time\n",
            "import json\nimport os\nimport subprocess\nimport sys\nimport time\n",
        ))
    # 2. module-level helper between the last import and the class.
    edits.append((
        "from eval.common.utils import TimeoutException\n\n\nclass Repo2RunModel(BaseEvalModel):\n",
        "from eval.common.utils import TimeoutException\n\n\n"
        + HELPER_FN
        + "\n\nclass Repo2RunModel(BaseEvalModel):\n",
    ))
    # 3. one helper call before each return (success / timeout x2 / error x2).
    edits.append((
        _SUCCESS_OLD,
        _CALL + '"success", start_time)\n' + _SUCCESS_OLD,
    ))
    edits.append((
        _TIMEOUT_TE_OLD,
        _CALL + '"timeout", start_time, str(e))\n' + _TIMEOUT_TE_OLD,
    ))
    edits.append((
        _TIMEOUT_TX_OLD,
        _CALL + '"timeout", start_time, str(e))\n' + _TIMEOUT_TX_OLD,
    ))
    edits.append((
        _ERROR_CPE_OLD,
        _CALL + '"error", start_time, str(e))\n' + _ERROR_CPE_OLD,
    ))
    edits.append((
        '                traceback.print_exc()\n                return {\n                    "status": "error",\n',
        '                traceback.print_exc()\n'
        + _CALL + '"error", start_time, str(e))\n'
        + '                return {\n                    "status": "error",\n',
    ))
    return edits


def main():
    if not os.path.isfile(F):
        sys.exit(f"ERROR: repo2run_model.py not found at {F} (set RAT_ROOT)")
    src = open(F).read()
    if SENTINEL in src:
        print(f"already patched (idempotent no-op): {F}")
        return 0
    for i, (old, new) in enumerate(_edits(src), 1):
        n = src.count(old)
        if n != 1:
            sys.exit(f"ERROR edit {i}: anchor found {n}x (expected 1) -- aborting, no changes "
                     f"written.\n  anchor: {old!r}")
        src = src.replace(old, new, 1)
    open(F, "w").write(src)
    print(f"patched: {F}")
    print("  + import json")
    print("  + module-level _write_repo2run_summary() helper")
    print("  + helper call before each return (success / 2x timeout / 2x error)")
    print("  -> writes output/<repo>/agent_run_summary.json {token_usage, best_in_sandbox_test_result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
