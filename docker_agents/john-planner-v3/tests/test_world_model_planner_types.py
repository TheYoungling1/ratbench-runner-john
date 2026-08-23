# tests/test_world_model_planner_types.py
from src.envstate.world_model import PlannerDecision, Task, TransitionProposal


def test_task_has_grounding_defaults():
    t = Task(goal="g", done_when="d", layer="deps", facts=())
    assert t.target_node_ids == ()
    assert t.transition_proposal is None


def test_task_carries_transition_proposal():
    tp = TransitionProposal(kind="install_python_package", target="torch",
                            intent="install torch", command_templates=("pip install torch",))
    t = Task("g", "d", "deps", (), target_node_ids=("contract:python_package_importable:torch",), transition_proposal=tp)
    assert t.transition_proposal.kind == "install_python_package"
    assert t.target_node_ids == ("contract:python_package_importable:torch",)


def test_planner_decision_done_carries_goal_ids():
    d = PlannerDecision(action="done", satisfied_goal_contract_ids=("contract:goal:repo_tests_run",))
    assert d.satisfied_goal_contract_ids == ("contract:goal:repo_tests_run",)
