"""Per-kind provisioning recipe renderer (design 2026-07-03, guard #1). Pure, LLM-free.

The SOLE owner of provisioning shell. The LLM classifier proposes a service KIND and
structured PARAMS (db name, the binding var); this module renders the known-good,
reproducible shell. Kinds with no corroborated local-daemon recipe (neo4j/milvus/
elasticsearch — Docker-only in every research sample) render None: advisory-only, no
invented commands.
"""
from __future__ import annotations

from dataclasses import dataclass

from python_deps.depgraph.service_tables import SERVICE_DEFAULTS
from python_deps.depgraph.service_scan import service_bind_url

# kind -> daemon start command (+ optional createdb template for relational kinds).
_START: dict[str, dict[str, str]] = {
    "postgres": {"start": "service postgresql start", "createdb": "createdb {db}"},
    "mysql": {"start": "service mysql start",
              "createdb": "mysql -e 'CREATE DATABASE IF NOT EXISTS {db}'"},
    "redis": {"start": "redis-server --daemonize yes"},
    "rabbitmq": {"start": "service rabbitmq-server start"},
}

# Relational kinds whose config binding is a DSN rewrite (Option B). alter_user sets the
# loopback credentials service_bind_url encodes (postgres:postgres). Binding is postgres-only:
# service_bind_url hardcodes postgres:postgres creds, so a mysql entry here would emit an
# unusable DSN. mysql can still START via _START; only its auto-binding is unsupported.
_ALTER_USER: dict[str, str] = {
    "postgres": "sudo -u postgres psql -c \"ALTER USER postgres PASSWORD 'postgres'\"",
}

RECIPE_KINDS: frozenset[str] = frozenset(_START)


def render_start(kind: str, params: dict) -> dict | None:
    """`{"start": ..., "createdb": ...?}` for a daemon-backed kind, else None."""
    spec = _START.get(kind)
    if spec is None:
        return None
    out = {"start": spec["start"]}
    db = (params or {}).get("db")
    tmpl = spec.get("createdb")
    if db and tmpl:
        out["createdb"] = tmpl.format(db=db)
    return out


def render_bind(kind: str, params: dict) -> dict | None:
    """`{"alter_user": ..., "bind_profile": ...}` for a relational DSN binding, else None."""
    alter = _ALTER_USER.get(kind)
    if alter is None:
        return None                       # only relational kinds carry a URL-rewrite binding
    p = params or {}
    var, db = p.get("var"), p.get("db")
    if not (var and db):
        return None
    _image, default_port = SERVICE_DEFAULTS.get(kind, ("", 0))
    port = p.get("port") or default_port
    scheme = p.get("scheme") or kind
    return {"alter_user": alter,
            "bind_profile": f"export {var}={service_bind_url(scheme, port, db)}"}


# ---------------------------------------------------------------------------
# Clean tier (Inc 1): per-kind base recipe (install + start + probe, optionally
# createuser/createdb) plus the setup/probe/poll renderers. ADDITIVE — the block
# above (_START/_ALTER_USER/render_start/render_bind) stays until Inc 3/5 removes
# it once the graph-side callers are migrated.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class KindBase:
    install: tuple
    start: str
    probe: str
    createuser: str | None = None
    createdb: str | None = None


# kind -> full daemon recipe. Every `probe` here MUST be read-only (patch_gate.is_read_only);
# test_all_kind_probes_read_only is the enforcing gate.
_KIND_BASE: dict[str, KindBase] = {
    "postgres": KindBase(
        ("postgresql",), "service postgresql start", "pg_isready",
        # su (not sudo): the KNOWN path never runs apply_env, and the target root
        # container has no sudo installed — sudo here would fail silently while
        # pg_isready still greens the node (false SATISFIED). `su postgres -c`
        # needs no password as root. Double-quote the su -c wrapper because the
        # inner psql SQL uses single quotes around the password literal.
        "su postgres -c \"psql -c \\\"CREATE USER {user} PASSWORD '{password}'\\\"\"",
        "su postgres -c 'createdb -O {user} {db}'"),
    "mysql": KindBase(
        ("default-mysql-server",), "service mysql start", "mysqladmin ping --silent",
        "mysql -e \"CREATE USER '{user}'@'localhost' IDENTIFIED BY '{password}'\"",
        "mysql -e 'CREATE DATABASE IF NOT EXISTS {db}'"),
    "redis": KindBase(("redis-server",), "redis-server --daemonize yes", "redis-cli ping"),
    "rabbitmq": KindBase(("rabbitmq-server",), "service rabbitmq-server start", "rabbitmqctl status"),
    # mongo intentionally omitted: `mongodb-org-server` is not in Debian repos (only
    # MongoDB's own repo), so a deterministic recipe would never provision. mongo routes
    # through the exotic LLM path (render_setup -> None), which emits the official-repo
    # install and is verify + certify-demote gated. Re-add here only with a container-
    # validated repo recipe.
}


def render_setup(kind: str, params: dict) -> dict | None:
    """Known-kind provisioning dict, or None for an unknown kind.
    `{"install": [...], "start": str, "probe": str, "createdb": str | None, "post": [...]}`
    `bind` is deliberately absent — it needs the repo's configs and is rendered by
    `repoint.render_bind_steps` in a later task."""
    base = _KIND_BASE.get(kind)
    if base is None:
        return None
    p = params or {}
    install = ["apt-get update",
               f"DEBIAN_FRONTEND=noninteractive apt-get install -y {' '.join(base.install)}"]
    probe = normalize_probe(base.probe, None, kind)
    post: list[str] = []
    if base.createuser and p.get("user"):
        post.append(base.createuser.format(**{"user": "app", "password": "app", "db": "app", **p}))
    createdb = (base.createdb.format(**{"user": "app", "db": "app", **p})
               if base.createdb and p.get("db") else None)
    return {"install": install, "start": base.start, "probe": probe,
            "createdb": createdb, "post": post}


def normalize_probe(probe: str | None, port: int | None, kind: str | None = None) -> str:
    """Return an admissible (read-only) probe command — the admissibility firewall
    every service probe must pass through before it can reach node admission:
    - known recipe kind -> its deterministic read-only probe (base.probe)
    - else if the given probe is already read-only -> return it verbatim
    - else if a port is known -> 'nc -z 127.0.0.1 <port>'
    - else -> '' (no admissible probe; caller lets the node demote at certify)
    """
    # Lazy import: patch_gate already imports this module (render_start/render_bind),
    # so a top-level `from patch_gate import is_read_only` here would be circular.
    from python_deps.depgraph.patch_gate import is_read_only

    if kind in _KIND_BASE:
        return _KIND_BASE[kind].probe
    if probe and is_read_only(probe):
        return probe
    if port:
        return f"nc -z 127.0.0.1 {port}"
    return ""


def render_probe_poll(probe: str) -> str:
    """Bounded readiness loop. Input is ALWAYS a normalize_probe output."""
    return f"for i in $(seq 1 15); do {probe} && exit 0; sleep 2; done; exit 1"
