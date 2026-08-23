# tests/test_world_model_v2.py
from src.envstate.world_model import (
    initial_map, merge_map, map_to_dict, map_from_dict, derive_open_problems,
    DependencyState, RecipeStep, RecipePatch, Fact,
)
from src.envstate.contracts.graph import ContractGraph
from src.envstate.contracts.nodes import Node, Edge


def _base():
    return initial_map(base_image="b", workdir="/r", language="python 3.11",
                       build_system="pip", repo_layout=("tests/",))


def test_new_fields_roundtrip():
    m = merge_map(_base(), dependency_state=DependencyState(declared=(Fact("cv2", ""),)),
                  import_results=(("cv2", False),), host_satisfied=frozenset({"contract:goal:repo_tests_pass"}))
    m2 = map_from_dict(map_to_dict(m))
    assert m2.dependency_state.declared[0].name == "cv2"
    assert m2.import_results == (("cv2", False),)
    assert "contract:goal:repo_tests_pass" in m2.host_satisfied


def test_recipe_patch_types():
    rp = RecipePatch(steps=(RecipeStep("s1", "system_install", "apt-get install -y libgl1",
                                       ("contract:system_library:libGL.so.1",)),))
    assert rp.steps[0].command.startswith("apt-get")


def test_derive_open_problems_from_active_blockers():
    g = ContractGraph(nodes=(
        Node("blocker:a", "Blocker", {"signature": "ImportError: libGL.so.1", "active": True,
                                      "summary": "libGL missing", "layer": "system"}),
        Node("blocker:gone", "Blocker", {"signature": "old", "active": False, "summary": "x", "layer": "deps"}),))
    ops = derive_open_problems(g)
    assert len(ops) == 1 and ops[0].layer == "system"
