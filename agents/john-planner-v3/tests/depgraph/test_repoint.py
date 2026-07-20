"""Pure unit tests for repoint (generic compose-host -> localhost DSN rewrite)."""

from python_deps.depgraph.provisioning_spec import ProvisioningSpec
from python_deps.depgraph.repoint import render_bind_steps


def test_repoint_redis_host_swap():
    specs = [ProvisioningSpec("cache", "redis", "redis:7")]
    configs = [("CACHE_URL", "redis://redis:6379/0")]
    assert render_bind_steps(specs, configs) == [
        "export CACHE_URL=redis://127.0.0.1:6379/0"
    ]


def test_repoint_postgres_preserves_creds():
    specs = [ProvisioningSpec("db", "postgres", "postgres:16")]
    configs = [("DATABASE_URL", "postgresql://app:secret@db:5432/app")]
    assert render_bind_steps(specs, configs) == [
        "export DATABASE_URL=postgresql://app:secret@127.0.0.1:5432/app"
    ]


def test_repoint_skips_unmatched_kind():
    specs = [ProvisioningSpec("db", "postgres", "postgres:16")]
    configs = [("CACHE_URL", "redis://redis:6379/0")]
    assert render_bind_steps(specs, configs) == []


def test_repoint_skips_non_dsn():
    specs = [ProvisioningSpec("db", "postgres", "postgres:16")]
    configs = [("X", "not-a-url")]
    assert render_bind_steps(specs, configs) == []


def test_repoint_preserves_query_and_no_port():
    specs = [ProvisioningSpec("db", "postgres", "postgres:16")]
    configs = [("DATABASE_URL", "postgresql://app@db/app?sslmode=require")]
    assert render_bind_steps(specs, configs) == [
        "export DATABASE_URL=postgresql://app@127.0.0.1/app?sslmode=require"
    ]


def test_repoint_order_preserving_multiple():
    specs = [
        ProvisioningSpec("db", "postgres", "postgres:16"),
        ProvisioningSpec("cache", "redis", "redis:7"),
    ]
    configs = [
        ("DATABASE_URL", "postgresql://app:secret@db:5432/app"),
        ("SOMETHING", "not-a-url"),
        ("CACHE_URL", "redis://redis:6379/0"),
    ]
    assert render_bind_steps(specs, configs) == [
        "export DATABASE_URL=postgresql://app:secret@127.0.0.1:5432/app",
        "export CACHE_URL=redis://127.0.0.1:6379/0",
    ]


def test_repoint_ignores_specs_without_kind():
    # A spec with kind=None does not contribute a declared kind.
    specs = [ProvisioningSpec("mystery", None, "weird:1")]
    configs = [("CACHE_URL", "redis://redis:6379/0")]
    assert render_bind_steps(specs, configs) == []
