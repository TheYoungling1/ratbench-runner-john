#!/usr/bin/env python
"""Offline §16 metrics over a contract_graph.jsonl trace. Usage: contract_graph_metrics.py <file.jsonl>"""
from __future__ import annotations

import json
import sys


def _latest_status(graph: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for ev in graph.get("contract_status_events", []):
        out[ev["contract_id"]] = ev["status"]
    return out


def _passing_command_ids(graph: dict) -> set[str]:
    return {n["id"] for n in graph.get("nodes", []) if n.get("type") == "CommandExecution" and int(n.get("exit_code", 1)) == 0}


def compute_metrics(rows: list[dict]) -> dict:
    ungrounded = 0
    grounded = 0
    repeated_repairs = 0
    seen_targets: dict[str, int] = {}
    for r in rows:
        dec = r.get("decision", {})
        if dec.get("action") == "task":
            targets = dec.get("target_node_ids") or []
            if targets:
                grounded += 1
                key = "|".join(sorted(targets))
                seen_targets[key] = seen_targets.get(key, 0) + 1
                if seen_targets[key] > 1:
                    repeated_repairs += 1
            else:
                ungrounded += 1

    final_graph = rows[-1].get("contract_graph", {}) if rows else {}
    status = _latest_status(final_graph)
    passing = _passing_command_ids(final_graph)
    evidence_by_contract: dict[str, list[str]] = {}
    for ev in final_graph.get("contract_status_events", []):
        if ev["status"] == "satisfied":
            evidence_by_contract[ev["contract_id"]] = ev.get("evidence_ids", [])
    satisfied = [c for c, s in status.items() if s == "satisfied"]
    satisfied_with_evidence = [c for c in satisfied if any(e in passing for e in evidence_by_contract.get(c, []))]

    required_goals = [
        n["id"] for n in final_graph.get("nodes", [])
        if n.get("type") == "Contract" and n.get("level") == "goal" and n.get("required")
    ]
    final_ready = bool(required_goals) and all(status.get(g) == "satisfied" for g in required_goals)

    pct = round(100.0 * len(satisfied_with_evidence) / len(satisfied), 1) if satisfied else 0.0
    return {
        "cycles": len(rows),
        "ungrounded_task_actions": ungrounded,
        "grounded_task_actions": grounded,
        "repeated_repairs": repeated_repairs,
        "satisfied_contracts": len(satisfied),
        "satisfied_with_evidence": len(satisfied_with_evidence),
        "satisfied_with_evidence_pct": pct,
        "final_goal_ready": final_ready,
    }


def main(path: str) -> None:
    rows = [json.loads(line) for line in open(path) if line.strip()]
    print(json.dumps(compute_metrics(rows), indent=2))


if __name__ == "__main__":
    main(sys.argv[1])
