from __future__ import annotations

import sys
from pathlib import Path

# Put <repo>/src on the path so python_deps.depgraph.* resolves
# (mirrors the pattern in test_world_model_dep_graph.py and tests/depgraph/conftest.py).
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from src.envstate.depgraph_live import certify_refresh, ensure_python_shim  # noqa: E402
from python_deps.depgraph.schema import DepGraph, Node, NodeType, Layer, State, DiscoveredBy  # noqa: E402


def _pkg(name):
    return Node(id=f"pkg:{name}", type=NodeType.PACKAGE, name=name, layer=Layer.PIP,
                discovered_by=DiscoveredBy.RESOLVER, state=State.MISSING, version="1.0",
                check_command=f'python -c "import {name}"')


def test_certify_refresh_flips_state_from_live_checks():
    g = DepGraph(nodes=(_pkg("flask"), _pkg("ghost")))

    def exec_readonly(cmd):
        # flask import succeeds (rc 0); ghost fails (rc 1)
        return (0, "") if "import flask" in cmd else (1, "ModuleNotFoundError: ghost")

    out = certify_refresh(g, exec_readonly, cycle=3)
    assert out.get("pkg:flask").state is State.SATISFIED
    assert out.get("pkg:flask").certified_cycle == 3
    assert out.get("pkg:ghost").state is State.MISSING


def test_certify_refresh_noop_when_disabled_or_empty():
    g = DepGraph(nodes=(_pkg("flask"),))
    assert certify_refresh(g, None, cycle=0) is g          # no executor
    assert certify_refresh(None, lambda c: (0, ""), 0) is None  # no graph


def test_ensure_python_shim_emits_idempotent_symlink():
    # Fix #5: symlink python->python3 so `python -m pip show` certifies on a
    # python3-only base (else installs never certify and the drain re-emits forever).
    calls = []

    def sandbox_execute(cmd):
        calls.append(cmd)
        return True, ""

    ensure_python_shim(sandbox_execute)
    assert len(calls) == 1
    cmd = calls[0]
    assert "ln -sf" in cmd and "python3" in cmd   # symlink python -> python3
    # Single setup Action, NOT a `command -v python || ln -sf` compound. The
    # sandbox preflight rejects a compound that mixes a read-only check with a
    # setup mutation, which silently defeats the shim (see the preflight
    # regression below). `ln -sf` is idempotent on its own.
    assert "||" not in cmd


def test_ensure_python_shim_command_passes_sandbox_preflight():
    # Regression (live-e2e churn bug): the shim runs through the MUTATING,
    # preflight-gated sandbox_execute. The earlier `command -v python || ln -sf`
    # compound was rejected by `_get_invalid_compound_setup_prefix` (read-only
    # check + setup mutation), so every cycle: reset_to_base -> shim REJECTED ->
    # reset, never certifying. The emitted command must be admitted by the real
    # preflight. Built via `Sandbox.__new__` (no Docker) — preflight only needs
    # the command classifier.
    from src.sandbox import Sandbox
    from src.synthesizer import Synthesizer

    calls = []
    ensure_python_shim(lambda cmd: calls.append(cmd) or (True, ""))
    assert len(calls) == 1

    sandbox = Sandbox.__new__(Sandbox)
    sandbox._command_classifier = Synthesizer()
    assert sandbox._get_preflight_rejection_prefix(calls[0]) == ""


def test_ensure_python_shim_noop_and_safe_without_executor():
    ensure_python_shim(None)  # must not raise


# --- certify setup-shape in-image services via loopback probe (arm-gated) ---

def test_certify_refresh_certifies_setup_service_when_allowed():
    svc = Node(id="service:postgres", type=NodeType.SERVICE, name="postgres",
               layer=Layer.SERVICES, discovered_by=DiscoveredBy.STATIC_SCAN,
               state=State.UNKNOWN, check_command="pg_isready -h 127.0.0.1 -p 5432",
               data={"setup": {"image": "postgres:16", "ports": [5432]},
                     "service_kind": "postgres"})
    g = DepGraph().with_node(svc)
    out = certify_refresh(g, lambda cmd: (0, "accepting connections"), cycle=1,
                          allow_service_certify=True)
    assert out.get("service:postgres").state is State.SATISFIED
    out_off = certify_refresh(g, lambda cmd: (0, ""), cycle=1)   # default: off
    assert out_off.get("service:postgres").state is State.UNKNOWN
