import textwrap

from python_deps.depgraph.service_scan import service_from_url, scan_compose_services, scan_ci_services


def _w(tmp_path, rel, src):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(src))


def test_postgres_url_full():
    assert service_from_url("postgres://u:p@db:5432/app") == ("postgres", "db", 5432)


def test_scheme_aliases():
    assert service_from_url("postgresql://x")[0] == "postgres"
    assert service_from_url("redis://cache:6379/0") == ("redis", "cache", 6379)
    assert service_from_url("mongodb://m/db")[0] == "mongo"
    assert service_from_url("amqp://broker")[0] == "rabbitmq"


def test_sqlite_and_unknown_return_none():
    assert service_from_url("sqlite:///db.sqlite3") is None
    assert service_from_url("not-a-url") is None
    assert service_from_url("") is None


def test_scan_compose_services(tmp_path):
    _w(tmp_path, "docker-compose.yml", """
        services:
          db:
            image: postgres:15
            ports: ["5432:5432"]
          cache:
            image: redis:7
    """)
    found = scan_compose_services(str(tmp_path))
    assert found["postgres"]["image"] == "postgres:15"
    assert "redis" in found


def test_scan_ci_services_and_presence(tmp_path):
    _w(tmp_path, ".github/workflows/ci.yml", """
        jobs:
          test:
            services:
              postgres:
                image: postgres:14
    """)
    found, present = scan_ci_services(str(tmp_path))
    assert present is True
    assert "postgres" in found


def test_scan_ci_no_services_block(tmp_path):
    _w(tmp_path, ".github/workflows/ci.yml", "jobs:\n  test:\n    steps: []\n")
    found, present = scan_ci_services(str(tmp_path))
    assert found == {} and present is False


from python_deps.depgraph.service_scan import classify_service_error


def test_classify_service_errors():
    assert classify_service_error("psycopg2.OperationalError: could not connect to server") == "postgres"
    assert classify_service_error("redis.exceptions.ConnectionError: Error 111 connecting") == "redis"
    assert classify_service_error("pymongo.errors.ServerSelectionTimeoutError: ...") == "mongo"
    assert classify_service_error("ImportError: no module named foo") is None


def test_scan_compose_services_modern_compose_yaml(tmp_path):
    # Compose v2 canonical filename — must be discovered (gap ③ regression test).
    _w(tmp_path, "compose.yml", """
        services:
          db:
            image: postgres:18
    """)
    found = scan_compose_services(str(tmp_path))
    assert "postgres" in found and found["postgres"]["image"] == "postgres:18"


def test_scan_compose_services_override_variant(tmp_path):
    _w(tmp_path, "compose.override.yml", """
        services:
          cache:
            image: redis:7
    """)
    assert "redis" in scan_compose_services(str(tmp_path))


def test_scan_compose_services_ignores_lookalike(tmp_path):
    # must NOT match red herrings like composer.yml / compose-notes.yml
    _w(tmp_path, "composer.yml", "services:\n  db:\n    image: postgres:16\n")
    _w(tmp_path, "compose-notes.yml", "services:\n  db:\n    image: postgres:16\n")
    assert scan_compose_services(str(tmp_path)) == {}


from python_deps.depgraph.service_scan import service_db_from_url


def test_service_db_from_url():
    assert service_db_from_url("postgres://u:p@db:5432/appdb") == "appdb"
    assert service_db_from_url("postgresql://h/only_db") == "only_db"
    assert service_db_from_url("postgres://h:5432/") is None
    assert service_db_from_url("not-a-url") is None


def test_compose_meta_captures_healthcheck(tmp_path):
    (tmp_path / "compose.yml").write_text(
        "services:\n"
        "  db:\n"
        "    image: postgres:16\n"
        "    ports: ['5432:5432']\n"
        "    healthcheck:\n"
        "      test: ['CMD-SHELL', 'pg_isready -U postgres']\n")
    meta = scan_compose_services(str(tmp_path))
    pg = meta.get("postgres") or next(iter(meta.values()))
    assert "pg_isready" in str(pg.get("healthcheck", ""))


def test_compose_meta_healthcheck_absent_returns_empty(tmp_path):
    (tmp_path / "compose.yml").write_text(
        "services:\n"
        "  db:\n"
        "    image: postgres:16\n"
        "    ports: ['5432:5432']\n")
    meta = scan_compose_services(str(tmp_path))
    pg = meta.get("postgres") or next(iter(meta.values()))
    assert pg.get("healthcheck") == ""
