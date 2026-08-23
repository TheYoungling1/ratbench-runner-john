from src.envstate.contracts.patch import GraphPatch, parse_graph_patch


def test_parse_reads_only_semantic_keys():
    p = parse_graph_patch({
        "add_contracts": [{"id": "contract:python_import:cv2", "type": "Contract", "level": "atomic"}],
        "add_blockers": [{"id": "blocker:b", "type": "Blocker", "signature": "x", "active": True}],
        "add_edges": [{"source": "blocker:b", "type": "violates", "target": "contract:python_import:cv2"}],
        "update_blocker_classification": [{"blocker_id": "blocker:b", "root_or_downstream": "root"}],
        "update_contract_description": [{"contract_id": "contract:python_import:cv2", "description": "needs cv2"}],
        "diagnostic_notes": ["cv2 blocked by libGL"],
        "add_status_events": [{"contract_id": "x", "status": "satisfied"}],  # IGNORED (not a key)
    })
    assert len(p.add_contracts) == 1 and len(p.add_blockers) == 1 and len(p.add_edges) == 1
    assert p.update_blocker_classification[0]["root_or_downstream"] == "root"
    assert p.diagnostic_notes == ("cv2 blocked by libGL",)


def test_parse_tolerates_garbage():
    assert parse_graph_patch(None).is_empty()
    assert parse_graph_patch({"add_contracts": "nope"}).is_empty()
