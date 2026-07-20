"""The single producer of node install commands for the static path.

Pure: no Docker, no network, no LLM, no src.envstate. populate_setup_commands
fills node.setup_commands for the reciped tiers (Package/SystemLib/Tool) so the
renderer can be a dumb emitter. _command_for here is the ONLY copy of the
per-node install-command logic in the static path — build_script._install_command
is deleted in favour of it.
"""
from __future__ import annotations

from dataclasses import replace

from python_deps.depgraph.emit import (
    _apt_name,
    _is_installable_project,
    _is_reciped,
    _pip_spec,
)
from python_deps.depgraph.schema import DepGraph, Node, NodeType, Strength

# The repo under test, installed editable as the capstone AFTER its dependencies.
# --no-deps: the pinned closure emitted above already provides every dependency,
# so pip must not re-resolve them (one uniform pip policy, matching the Package
# lines). Build isolation is left ON (no --no-build-isolation) on purpose: the
# PEP 517 backend (setuptools/hatchling/poetry-core…) is usually absent from the
# resolved closure, so pip fetches it into an isolated build env; the container
# already has network for the closure install, so this never fails for a
# missing backend — whereas --no-build-isolation would.
_EDITABLE_INSTALL = "python3 -m pip install --break-system-packages --no-deps -e ."


def _command_for(node: Node) -> str:
    """The install command for a populatable node: apt for SystemLib/Tool, pinned
    --no-deps pip for Package, editable install for the installable Project. The
    single source of this derivation."""
    apt = _apt_name(node)
    if apt is not None:
        return f"apt-get install -y --no-install-recommends {apt}"
    if node.type is NodeType.PACKAGE:
        return f"python3 -m pip install --break-system-packages --no-deps {_pip_spec(node)}"
    if node.type is NodeType.PROJECT:
        return _EDITABLE_INSTALL
    return node.chosen_fix or ""  # defensive; reciped syslib/tool are always apt


def _should_populate(node: Node) -> bool:
    """Nodes whose install command the renderer emits: the reciped third-party set
    plus the installable Project (its editable capstone install)."""
    return _is_reciped(node) or _is_installable_project(node)


def populate_setup_commands(graph: DepGraph) -> DepGraph:
    """Return a NEW graph in which every populatable node lacking setup_commands
    gets its install command + strength=HARD. Idempotent; leaves Service/Config,
    non-installable projects, and already-populated nodes untouched."""
    new = graph
    for node in graph.nodes:
        if node.setup_commands:
            continue
        if not _should_populate(node):
            continue
        cmd = _command_for(node)
        if not cmd:
            continue
        new = new.with_node(replace(node, setup_commands=(cmd,), strength=Strength.HARD))
    return new
