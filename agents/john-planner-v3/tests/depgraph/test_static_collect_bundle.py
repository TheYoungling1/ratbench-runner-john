# tests/depgraph/test_static_collect_bundle.py
import json
from python_deps.depgraph.schema import DepGraph, Node, NodeType, Layer, DiscoveredBy
from python_deps.depgraph.ids import package_id, project_id
from python_deps.depgraph.static_collect import (
    DeterministicHit, collect_static_evidence, compact_bundle_json,
)


def _repo(tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "test.yml").write_text(
        "jobs:\n  t:\n    services:\n      postgres:\n        image: postgres:15\n"
        "        ports: ['5432:5432']\n")
    (tmp_path / ".env.example").write_text("DATABASE_URL=postgres://localhost/db\n")
    return str(tmp_path)


def test_ci_postgres_and_env_var_hits(tmp_path):
    hits = collect_static_evidence(_repo(tmp_path))
    kinds = {h.kind for h in hits}
    assert "ci_service" in kinds                       # postgres from CI
    assert any(h.kind == "env_var" and h.name == "DATABASE_URL" for h in hits)
    # every hit has a stable evidence_id and a file
    assert all(h.evidence_id and h.file for h in hits)


def test_compact_bundle_json_shape(tmp_path):
    hits = collect_static_evidence(_repo(tmp_path))
    bundle = json.loads(compact_bundle_json(hits))
    assert "goal" in bundle and isinstance(bundle["deterministic_hits"], list)
    assert {"evidence_id", "file", "kind"} <= set(bundle["deterministic_hits"][0])


def test_package_hits_added_when_graph_given(tmp_path):
    g = DepGraph().with_node(Node(id=package_id("psycopg2", "2.9.9"), type=NodeType.PACKAGE,
        name="psycopg2", layer=Layer.PIP, discovered_by=DiscoveredBy.RESOLVER, version="2.9.9"))
    hits = collect_static_evidence(str(tmp_path), g)
    pkg = [h for h in hits if h.kind == "package"]
    assert any(h.name == "psycopg2" for h in pkg)
    assert all(h.evidence_id.startswith("pkg.") for h in pkg)


def test_no_package_hits_without_graph(tmp_path):
    # back-compat: existing call sites pass no graph -> no package hits, no crash
    hits = collect_static_evidence(str(tmp_path))
    assert all(h.kind != "package" for h in hits)


def test_package_hit_carries_node_id(tmp_path):
    g = DepGraph().with_node(Node(id=package_id("psycopg", None), type=NodeType.PACKAGE,
        name="psycopg", layer=Layer.PIP, discovered_by=DiscoveredBy.RESOLVER))
    hits = collect_static_evidence(str(tmp_path), g)
    pkg = [h for h in hits if h.kind == "package"]
    assert pkg and pkg[0].node_id == "pkg:psycopg"
    # and it is serialized into the bundle JSON
    row = next(r for r in json.loads(compact_bundle_json(hits))["deterministic_hits"]
               if r.get("node_id") == "pkg:psycopg")
    assert row["name"] == "psycopg"


def test_project_node_emitted_with_node_id(tmp_path):
    g = DepGraph().with_node(Node(id=project_id("myrepo"), type=NodeType.PROJECT,
        name="myrepo", layer=Layer.PIP, discovered_by=DiscoveredBy.STATIC_SCAN))
    hits = collect_static_evidence(str(tmp_path), g)
    proj = [h for h in hits if h.kind == "project"]
    assert proj and proj[0].node_id == project_id("myrepo")


def test_basesettings_fields_feed_the_bundle(tmp_path):
    (tmp_path / "config.py").write_text(
        "from pydantic_settings import BaseSettings\n"
        "class Settings(BaseSettings):\n"
        "    POSTGRES_SERVER: str\n"
        "    POSTGRES_PORT: int = 5432\n"
        "    SECRET_KEY: str\n")
    hits = collect_static_evidence(str(tmp_path))
    names = {h.name for h in hits if h.kind == "env_var"}
    assert {"POSTGRES_SERVER", "POSTGRES_PORT", "SECRET_KEY"} <= names


def test_compose_snippet_includes_port_and_healthcheck(tmp_path):
    (tmp_path / "compose.yml").write_text(
        "services:\n"
        "  db:\n"
        "    image: postgres:16\n"
        "    ports: ['5432:5432']\n"
        "    healthcheck:\n"
        "      test: ['CMD-SHELL', 'pg_isready -U postgres']\n")
    hits = collect_static_evidence(str(tmp_path))
    svc = next(h for h in hits if h.kind == "compose_service")
    assert "5432" in svc.snippet and "pg_isready" in svc.snippet


def test_framework_config_deduped_against_env_read(tmp_path):
    # a var seen via os.environ must not be double-emitted by the framework-config source
    (tmp_path / "a.py").write_text("import os\nX = os.environ['SHARED_VAR']\n")
    (tmp_path / "config.py").write_text(
        "from pydantic_settings import BaseSettings\n"
        "class Settings(BaseSettings):\n    SHARED_VAR: str\n")
    hits = collect_static_evidence(str(tmp_path))
    shared = [h for h in hits if h.name == "SHARED_VAR"]
    assert len(shared) == 1
