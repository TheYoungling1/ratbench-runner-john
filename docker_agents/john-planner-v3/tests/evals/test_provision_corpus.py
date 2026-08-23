"""Well-formedness checks for the provisioning corpus (`provision_corpus.py`).

Pure, no docker/model: validates the labeled `ServiceCase` corpus (18 rows) and the
`provision_expectations` extension to `level3_labels.LABELS`. Everything here is a
structural/shape check against the ground-truth table in the task brief — it never
re-derives a label, only confirms the transcription is complete and internally
consistent.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for _p in (str(_ROOT), str(_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402

from evals.service_config_detection import level3_labels  # noqa: E402
from evals.service_config_detection.provision_corpus import (  # noqa: E402
    PROVISION_CASES,
    ServiceCase,
)

_ALLOWED_EXPECT = {"provisionable", "non_provisionable"}
_ALLOWED_KNOWN_FAILURE = {"arch", "root", "hallucination", "none"}
_ALLOWED_PROBE_FAMILY = {"tcp", "http", "pg", "mysql", "redis", "cql", "etcdctl"}


@pytest.mark.parametrize("case", PROVISION_CASES, ids=lambda c: c.name)
def test_all_cases_well_formed(case: ServiceCase):
    assert case.name
    assert case.kind
    assert case.compose_entry
    assert case.expect
    assert case.expected_probe_family
    assert case.known_failure
    assert case.expect in _ALLOWED_EXPECT
    assert case.known_failure in _ALLOWED_KNOWN_FAILURE
    assert case.expected_probe_family in _ALLOWED_PROBE_FAMILY
    assert "image:" in case.compose_entry


def test_adversary_counts():
    assert len(PROVISION_CASES) == 18
    non_provisionable = [c for c in PROVISION_CASES if c.expect == "non_provisionable"]
    assert len(non_provisionable) == 2
    assert {c.name for c in non_provisionable} == {"memcached", "milvus"}
    arch_cases = [c for c in PROVISION_CASES if c.known_failure == "arch"]
    assert len(arch_cases) == 1
    assert arch_cases[0].name == "qdrant"


def test_known_kinds_match_tables():
    from python_deps.depgraph.service_tables import KNOWN_SERVICE_KINDS

    checked_kinds = {"redis", "mysql", "postgres", "mongo", "rabbitmq"}
    matched = [c for c in PROVISION_CASES if c.kind in checked_kinds]
    assert matched, "expected at least one case with a checkable kind"
    for case in matched:
        assert case.kind in KNOWN_SERVICE_KINDS


def test_level3_provision_expectations():
    assert level3_labels.LABELS, "expected at least one labeled entry"
    for repo_name, entry in level3_labels.LABELS.items():
        assert "provision_expectations" in entry, repo_name
        assert len(entry["provision_expectations"]) == len(entry["services_declared"]), (
            repo_name
        )
