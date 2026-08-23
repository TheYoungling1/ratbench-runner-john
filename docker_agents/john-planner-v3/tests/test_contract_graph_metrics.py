import json

from scripts.contract_graph_metrics import compute_metrics


def _cycle(action, targets, graph):
    return {"cycle": 1, "decision": {"action": action, "target_node_ids": targets}, "contract_graph": graph}


def test_counts_ungrounded_actions_and_evidence_ratio():
    graph = {
        "nodes": [
            {"id": "contract:a", "type": "Contract", "level": "atomic"},
            {"id": "cmd:005", "type": "CommandExecution", "exit_code": 0},
        ],
        "edges": [],
        "contract_status_events": [
            {"contract_id": "contract:a", "status": "satisfied", "revision_id": "envrev:004", "evidence_ids": ["cmd:005"]},
        ],
    }
    rows = [
        _cycle("task", [], graph),                 # ungrounded (no targets)
        _cycle("task", ["contract:a"], graph),     # grounded
        _cycle("done", [], graph),                 # done not counted as ungrounded
    ]
    m = compute_metrics(rows)
    assert m["ungrounded_task_actions"] == 1
    assert m["satisfied_contracts"] == 1
    assert m["satisfied_with_evidence"] == 1
    assert m["satisfied_with_evidence_pct"] == 100.0
    assert m["final_goal_ready"] in (True, False)
