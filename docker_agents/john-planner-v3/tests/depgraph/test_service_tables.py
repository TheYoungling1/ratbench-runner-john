from python_deps.depgraph.service_tables import (
    services_for_package, service_defaults, KNOWN_SERVICE_KINDS,
)


def test_psycopg2_implies_postgres():
    assert "postgres" in services_for_package("psycopg2")


def test_lookup_normalized_and_fresh():
    a = services_for_package("Psycopg2")
    assert a == services_for_package("psycopg2")
    a.append("x")
    assert services_for_package("psycopg2") != a


def test_unknown_package_empty():
    assert services_for_package("requests") == []


def test_service_defaults():
    assert service_defaults("postgres") == ("postgres:16", 5432)
    assert service_defaults("redis")[1] == 6379
    assert set(KNOWN_SERVICE_KINDS) >= {"postgres", "redis", "mongo", "rabbitmq"}
