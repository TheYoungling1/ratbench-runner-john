"""Clean-tier service_recipes additions (CR2): render_setup, normalize_probe (the
admissibility firewall), render_probe_poll. Pure, no docker/network. Does NOT touch
the existing test_service_recipes.py (render_start/render_bind stay covered there)."""
from python_deps.depgraph.service_recipes import (
    _KIND_BASE, render_setup, normalize_probe, render_probe_poll,
)


def test_render_setup_redis():
    r = render_setup("redis", {})
    assert any("redis-server" in step for step in r["install"])
    assert r["start"] == "redis-server --daemonize yes"
    assert r["probe"] == "redis-cli ping"


def test_render_setup_postgres_with_params():
    r = render_setup("postgres", {"db": "app", "user": "u", "password": "p"})
    assert r["createdb"] is not None
    assert "app" in r["createdb"]
    # Tight: the real formatted password (PASSWORD 'p') and a non-trivial user
    # occurrence (CREATE USER u ) — not just "p"/"u" substrings that also hit
    # "postgres"/"psql".
    assert any(
        "CREATE USER u " in step and "PASSWORD 'p'" in step for step in r["post"]
    )
    assert r["probe"].startswith("pg_isready")


def test_postgres_recipe_no_sudo():
    r = render_setup("postgres", {"db": "app", "user": "u", "password": "p"})
    for step in r["post"]:
        assert "sudo " not in step
        assert "su postgres -c" in step
    assert "sudo " not in r["createdb"]
    assert "su postgres -c" in r["createdb"]


def test_render_setup_unknown_kind_none():
    assert render_setup("kafka", {}) is None


def test_render_setup_mongo_routes_exotic():
    # mongo has no Debian-repo package, so it is NOT a deterministic kind; it must route
    # to the exotic LLM path (render_setup returns None).
    assert render_setup("mongo", {}) is None


def test_all_kind_probes_read_only():
    from python_deps.depgraph.patch_gate import is_read_only
    for kind in _KIND_BASE:
        assert is_read_only(_KIND_BASE[kind].probe), f"{kind} probe not read-only"


def test_normalize_curl_probe_with_port():
    from python_deps.depgraph.patch_gate import is_read_only
    result = normalize_probe("curl -f http://localhost:8080/health", 8080)
    assert is_read_only(result)
    assert result == "nc -z 127.0.0.1 8080"


def test_normalize_curl_probe_no_port_empty():
    assert normalize_probe("curl -f http://x/health", None) == ""


def test_normalize_known_kind_deterministic():
    r = normalize_probe("curl -f http://localhost:5432", 5432, "postgres")
    assert r == _KIND_BASE["postgres"].probe


def test_normalize_passthrough_read_only():
    assert normalize_probe("pg_isready -q", 5432) == "pg_isready -q"


def test_render_probe_poll_shape():
    poll = render_probe_poll("nc -z 127.0.0.1 6379")
    assert "seq 1 15" in poll
    assert "nc -z 127.0.0.1 6379" in poll
    assert "exit 0" in poll
    assert "exit 1" in poll
