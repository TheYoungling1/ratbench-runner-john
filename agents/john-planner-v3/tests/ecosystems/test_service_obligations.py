"""Phase-3 seam: provider.service_obligations wraps the injected service classifier."""
from __future__ import annotations

from ecosystems.python.provider import PythonProvider
from ecosystems.registry import PROVIDERS
from python_deps.depgraph.schema import DepGraph


def test_none_classifier_is_passthrough():
    g = DepGraph()
    assert PythonProvider().service_obligations(g, "/repo", None) is g


def test_injected_classifier_runs_and_result_flows():
    g = DepGraph()
    sentinel = DepGraph()
    seen = {}

    def fake_classifier(graph, repo):
        seen["repo"] = repo
        seen["graph"] = graph
        return sentinel

    out = PythonProvider().service_obligations(g, "/repo", fake_classifier)
    assert out is sentinel
    assert seen == {"repo": "/repo", "graph": g}


def test_all_registered_providers_expose_service_obligations():
    for p in PROVIDERS:
        assert callable(getattr(p, "service_obligations", None))
