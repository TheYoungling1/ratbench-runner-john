"""Phase 2: manual_blocks persists on WorldModelMap so the exported setup.sh
includes governed (LLM-admitted) blocks.

Mirrors the sys.path bootstrap used across tests/envstate (see
test_v3_repair_wiring.py) since python_deps is a top-level package under src/.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from src.envstate.world_model import WorldModelMap, initial_map, merge_map
from python_deps.depgraph.block import Block
from python_deps.depgraph.build_script import render_build_script


def _blk() -> Block:
    return Block(block_id="blk:1", wave="system", commands=("apt-get install -y libx",),
                 target_node_ids=("syslib:x",), provider_ids=(), check_commands=(), evidence_refs=())


def test_world_map_defaults_manual_blocks_empty():
    m = initial_map(base_image="python:3.12", workdir="/app", language="unknown",
                    build_system="pip", repo_layout=())
    assert m.manual_blocks == ()


def test_merge_map_carries_manual_blocks():
    m = initial_map(base_image="python:3.12", workdir="/app", language="unknown",
                    build_system="pip", repo_layout=())
    m2 = merge_map(m, manual_blocks=(_blk(),))
    assert len(m2.manual_blocks) == 1 and m2.manual_blocks[0].block_id == "blk:1"
    # immutability: original unchanged
    assert m.manual_blocks == ()


def test_final_render_includes_admitted_manual_block():
    """End-to-end wiring proof: a governed manual block admitted onto the map via
    merge_map (the same path orchestrator.py's _dep_emit_phase / task-branch use)
    survives into the FINAL rendered setup.sh artifact.

    This is the regression this task fixes: previously manual_blocks lived only
    as orchestrator-local state and never reached WorldModelMap, so the final
    render_build_script(dep_graph) call at scripts/run_v3_e2e.py:144 silently
    dropped every LLM-admitted block.
    """
    base = initial_map(base_image="python:3.12", workdir="/app", language="unknown",
                        build_system="pip", repo_layout=())
    final_map = merge_map(base, manual_blocks=(_blk(),))

    script = render_build_script(final_map.dep_graph, final_map.manual_blocks)

    assert "blk:1" in script
