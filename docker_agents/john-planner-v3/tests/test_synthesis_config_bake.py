# tests/test_synthesis_config_bake.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from python_deps.depgraph.schema import (  # noqa: E402
    DepGraph, Node, NodeType, Layer, State, DiscoveredBy,
)
from src.envstate.synthesis import bakeable_config_env  # noqa: E402


def _cfg(var, value):
    """A CONFIG node exactly as config_scan._config_node builds it."""
    fix = f"env:{var}={value}"
    return Node(
        id=f"config:{var}", type=NodeType.CONFIG, name=var, layer=Layer.CONFIG,
        discovered_by=DiscoveredBy.STATIC_SCAN, state=State.UNKNOWN,
        check_command=f"printenv {var}", fix_candidates=(fix,), chosen_fix=fix,
    )


def test_extracts_known_value():
    g = DepGraph().with_node(_cfg("DATABASE_URL", "postgresql://localhost:5432/db"))
    assert bakeable_config_env(g) == [("DATABASE_URL", "postgresql://localhost:5432/db")]


def test_skips_unknown_value():
    g = DepGraph().with_node(_cfg("DEBUG", "?"))     # chosen_fix == "env:DEBUG=?"
    assert bakeable_config_env(g) == []


def test_skips_secret_named_vars():
    g = (DepGraph()
         .with_node(_cfg("API_KEY", "sk-123"))
         .with_node(_cfg("DJANGO_SECRET_KEY", "abc"))
         .with_node(_cfg("DB_PASSWORD", "hunter2")))
    assert bakeable_config_env(g) == []


def test_skips_denylisted_incidentals():
    g = DepGraph().with_node(_cfg("PYTHONPATH", "/app"))   # in _ENV_DENYLIST
    assert bakeable_config_env(g) == []


def test_exclude_param_drops_named_vars():
    g = (DepGraph()
         .with_node(_cfg("DATABASE_URL", "postgresql://localhost/db"))
         .with_node(_cfg("REDIS_URL", "redis://localhost:6379/0")))
    out = bakeable_config_env(g, exclude=frozenset({"DATABASE_URL"}))
    assert out == [("REDIS_URL", "redis://localhost:6379/0")]


def test_value_with_equals_sign_is_preserved():
    g = DepGraph().with_node(_cfg("DATABASE_URL", "postgresql://u:p@h/db?sslmode=require"))
    assert bakeable_config_env(g) == [("DATABASE_URL", "postgresql://u:p@h/db?sslmode=require")]


def test_non_config_nodes_ignored():
    pkg = Node(id="pkg:requests", type=NodeType.PACKAGE, name="requests", layer=Layer.PIP,
               discovered_by=DiscoveredBy.STATIC_SCAN, state=State.MISSING,
               check_command="python -c 'import requests'", version="2.0", chosen_fix="pip:requests")
    assert bakeable_config_env(DepGraph().with_node(pkg)) == []
