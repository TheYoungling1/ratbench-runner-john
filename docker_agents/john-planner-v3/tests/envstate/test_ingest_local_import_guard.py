"""Task 8 (companion-plan): the local-import guard must also apply at INGEST
time, not just at repair time.

``_repair_or_route`` (the ``_dep_emit_phase``/install-failure repair site)
already routes every bundle through ``diagnose_all``/``RepoContext`` before
spending a repair turn -- see
``tests/envstate/scenarios/test_repo_local_import_guard.py``. But
``_runtime_ingest_phase`` (the discover-gate/``ingest_runtime_failures`` site)
built its classifier tier from the raw ``classify_observation`` regex
classifier, which knows nothing about repo-local names. A
``ModuleNotFoundError: No module named 'docs_src'`` surfaced by the
deterministic discover gate (``VERIFY_TEST_CMD``) therefore got ingested next
cycle as a bogus ``pkg:docs_src`` node -- which then fails every fresh replay
forever, since no such PyPI distribution exists.

These two tests drive that exact DISCOVER -> INGEST path end-to-end through
``run_v3`` (fake sandbox, no exec_readonly/client -- pure deterministic
classifier tier) and assert:
  1. a repo-local import (``docs_src``, a real ``tmp_path`` package with an
     ``__init__.py``) never becomes a ``pkg:docs_src``/``pkg:docs-src`` node.
  2. a genuine external import (``requests``) IS still ingested as
     ``pkg:requests`` -- proving the guard filters, it does not over-block.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from src.envstate import orchestrator
from src.envstate.ledger import ActionLedger
from src.envstate.world_model import initial_map
from python_deps.depgraph.schema import DepGraph


class _Agent:
    """No ``client`` attribute -> ``_runtime_ingest_phase`` uses the
    deterministic-only classifier tier (no LLM tier constructed)."""


class _NoopMaintainer:
    def update(self, world_map, report):
        return world_map


def _empty_map():
    return initial_map(
        base_image="python:3.11-slim", workdir="/repo", language="python",
        build_system="pip", repo_layout=(), dep_graph=DepGraph(),
    )


def _make_repo_with_local_docs_src(tmp_path):
    """A real filesystem tree: tmp_path/docs_src/__init__.py, so
    ``scan.local_module_names`` discovers it for real (not monkeypatched)."""
    pkg_dir = tmp_path / "docs_src"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    return tmp_path


def _failing_verify_sandbox(stderr_line: str):
    """Fake sandbox: VERIFY_TEST_CMD always fails with the given traceback
    line. Never called for anything else in this scenario (exec_readonly is
    None, so ``_dep_emit_phase`` never touches the sandbox; no typed-repair
    task ever fires since no node ever reaches ``State.MISSING``)."""
    def sandbox_execute(cmd):
        return (False, f"collecting tests...\n{stderr_line}\n1 error\n")
    return sandbox_execute


def _unreachable_run_install_script(script):
    raise AssertionError(
        "run_install_script must never be called in this scenario "
        "(exec_readonly=None disables _dep_emit_phase's certify/emit path)"
    )


def test_discover_gate_repo_local_import_never_becomes_package_node(tmp_path):
    repo = _make_repo_with_local_docs_src(tmp_path)
    sandbox_execute = _failing_verify_sandbox(
        "ModuleNotFoundError: No module named 'docs_src'"
    )

    final_map, stop = orchestrator.run_v3(
        build_agent=_Agent(),
        maintainer=_NoopMaintainer(),
        initial_world_map=_empty_map(),
        ledger=ActionLedger(),
        sandbox_execute=sandbox_execute,
        max_cycles=5,
        exec_readonly=None,
        enable_dep_emit=True,
        reset_to_base=lambda: None,
        run_install_script=_unreachable_run_install_script,
        repo_path=str(repo),
    )

    all_ids = tuple(n.id for n in (final_map.dep_graph.nodes if final_map.dep_graph else ()))
    assert not any(nid in ("pkg:docs_src", "pkg:docs-src") for nid in all_ids), (
        "a bogus package node was added for the repo-local import 'docs_src' "
        f"via the discover-gate/runtime-ingest path: {all_ids}"
    )
    # The graph never gains an actionable obligation from a repo-local import,
    # so the scheduler can never close the loop -- the run must give up
    # honestly, never report success.
    assert stop != "planner_done"


def test_discover_gate_external_import_is_not_auto_fabricated(tmp_path):
    # Design (import->dist identity-fallback deletion): the deterministic layer
    # NEVER guesses a package from an unmapped runtime import ("never guessed as
    # itself", per test_runtime_parsers). A genuine external import (requests) is
    # deferred to the LLM repair path, NOT auto-discovered/installed deterministically.
    # (The docs_src repo-local case is covered by the sibling test above.)
    repo = _make_repo_with_local_docs_src(tmp_path)
    sandbox_execute = _failing_verify_sandbox(
        "ModuleNotFoundError: No module named 'requests'"
    )

    final_map, stop = orchestrator.run_v3(
        build_agent=_Agent(),
        maintainer=_NoopMaintainer(),
        initial_world_map=_empty_map(),
        ledger=ActionLedger(),
        sandbox_execute=sandbox_execute,
        max_cycles=5,
        exec_readonly=None,
        enable_dep_emit=True,
        reset_to_base=lambda: None,
        run_install_script=_unreachable_run_install_script,
        repo_path=str(repo),
    )

    all_ids = tuple(n.id for n in (final_map.dep_graph.nodes if final_map.dep_graph else ()))
    assert "pkg:requests" not in all_ids, (
        "an unmapped external import must NOT be deterministically fabricated into a "
        f"package node -- it is deferred to the LLM repair path: {all_ids}"
    )
    # No deterministic obligation was minted, so the loop cannot close on it: it must
    # give up honestly rather than report success.
    assert stop != "planner_done"


# ── LLM tier (``_guarded_llm``) coverage ─────────────────────────────────────
#
# The two tests above only ever exercise the deterministic tier
# (``make_diagnostic_classifier``) -- ``_Agent`` has no ``.client``, so
# ``_runtime_ingest_phase`` never constructs the LLM tier at all. This gap was
# flagged by review: ``_guarded_llm`` (orchestrator.py:818-826, wraps
# ``_bounded_llm`` and drops any ``Discovery`` whose
# ``disc.data.get("import_name") or disc.name`` is a repo-local import) has no
# test that ever runs it.
#
# To close it: (1) give ``build_agent`` a truthy ``.client`` so the LLM tier IS
# constructed; (2) fail ``VERIFY_TEST_CMD`` with a generic, unclassifiable
# error string so the deterministic tier misses (``diagnose()`` returns
# ``Mode.AMBIGUOUS``, ``discovery=None`` -- confirmed directly against
# ``diagnose()`` before writing this test) and the cascade falls through to
# the LLM tier; (3) monkeypatch the orchestrator's lazy-imported
# ``src.envstate.llm_classifier.make_llm_classifier`` (same interception point
# as ``tests/test_residual_handler_wiring.py``) to return a stub that always
# yields a ``Discovery`` for a FIXED name, independent of the (generic, name-
# free) failure text -- this isolates ``_guarded_llm``'s own guard from
# ``diagnose()``'s pre-existing REPO_INTERNAL_REF detection, and a call
# counter proves the stub actually ran (not a vacuous pass via the
# deterministic tier alone).
from python_deps.depgraph.runtime_classify import Discovery
from python_deps.depgraph.schema import Layer, NodeType


class _FakeClient:
    """Non-None sentinel so build_agent.client is truthy."""


class _LLMAgent:
    """Build agent stub with a truthy ``.client`` -> wires the LLM tier."""
    client = _FakeClient()
    model = "test-model"


_UNCLASSIFIABLE_STDERR = (
    "collecting tests...\nSomeMysteryError: unclassifiable failure xyz\n1 error\n"
)


def _make_stub_llm_classifier(discovery_name: str, calls: list):
    """Factory matching ``make_llm_classifier``'s signature (positional
    ``complete_fn`` + keyword ``note_out_of_scope``) -- monkeypatched in for
    the real ``src.envstate.llm_classifier.make_llm_classifier``. Returns a
    fixed ``Discovery`` for ``discovery_name`` on every call, regardless of
    the (cmd, out) it is given, and records every invocation in ``calls`` so
    the test can assert the LLM tier was genuinely reached."""

    def _fake_make(complete_fn, *, note_out_of_scope=None):
        def _classify(cmd, out):
            calls.append((cmd, out))
            return Discovery(
                node_type=NodeType.PACKAGE,
                name=discovery_name,
                layer=Layer.PIP,
                evidence=(out or "")[-500:],
                check_command=f'python3 -c "import {discovery_name}"',
                confidence="runtime-llm",
            )
        return _classify

    return _fake_make


def test_guarded_llm_drops_repo_local_discovery(tmp_path, monkeypatch):
    """``_guarded_llm`` must drop an LLM-proposed Discovery whose name is a
    repo-local import, exactly like the deterministic tier does -- even when
    the failure text itself gives the deterministic tier (and, per this
    stub, the LLM prompt) no clue that 'docs_src' is involved."""
    repo = _make_repo_with_local_docs_src(tmp_path)
    sandbox_execute = _failing_verify_sandbox(_UNCLASSIFIABLE_STDERR.strip("\n"))

    calls: list = []
    import src.envstate.llm_classifier as _llm_mod
    monkeypatch.setattr(
        _llm_mod, "make_llm_classifier", _make_stub_llm_classifier("docs_src", calls)
    )

    final_map, stop = orchestrator.run_v3(
        build_agent=_LLMAgent(),
        maintainer=_NoopMaintainer(),
        initial_world_map=_empty_map(),
        ledger=ActionLedger(),
        sandbox_execute=sandbox_execute,
        max_cycles=5,
        exec_readonly=None,
        enable_dep_emit=True,
        reset_to_base=lambda: None,
        run_install_script=_unreachable_run_install_script,
        repo_path=str(repo),
    )

    assert calls, (
        "the stubbed LLM classifier was never invoked -- the deterministic "
        "tier must have short-circuited (test is vacuous unless the LLM "
        "tier genuinely ran)"
    )
    all_ids = tuple(n.id for n in (final_map.dep_graph.nodes if final_map.dep_graph else ()))
    assert not any(nid in ("pkg:docs_src", "pkg:docs-src") for nid in all_ids), (
        "a bogus package node was added for the repo-local import 'docs_src' "
        f"via the LLM tier (_guarded_llm failed to drop it): {all_ids}"
    )
    assert stop != "planner_done"


def test_guarded_llm_keeps_external_discovery(tmp_path, monkeypatch):
    """Companion/control: ``_guarded_llm`` must NOT over-block -- an
    LLM-proposed Discovery for a genuine external name ('requests', not in
    local_names) must still be ingested as a package node."""
    repo = _make_repo_with_local_docs_src(tmp_path)
    sandbox_execute = _failing_verify_sandbox(_UNCLASSIFIABLE_STDERR.strip("\n"))

    calls: list = []
    import src.envstate.llm_classifier as _llm_mod
    monkeypatch.setattr(
        _llm_mod, "make_llm_classifier", _make_stub_llm_classifier("requests", calls)
    )

    final_map, stop = orchestrator.run_v3(
        build_agent=_LLMAgent(),
        maintainer=_NoopMaintainer(),
        initial_world_map=_empty_map(),
        ledger=ActionLedger(),
        sandbox_execute=sandbox_execute,
        max_cycles=5,
        exec_readonly=None,
        enable_dep_emit=True,
        reset_to_base=lambda: None,
        run_install_script=_unreachable_run_install_script,
        repo_path=str(repo),
    )

    assert calls, (
        "the stubbed LLM classifier was never invoked -- the deterministic "
        "tier must have short-circuited (test is vacuous unless the LLM "
        "tier genuinely ran)"
    )
    all_ids = tuple(n.id for n in (final_map.dep_graph.nodes if final_map.dep_graph else ()))
    assert "pkg:requests" in all_ids, (
        "a genuine external import ('requests') proposed by the LLM tier was "
        f"NOT ingested as a package node (_guarded_llm over-blocked): {all_ids}"
    )
