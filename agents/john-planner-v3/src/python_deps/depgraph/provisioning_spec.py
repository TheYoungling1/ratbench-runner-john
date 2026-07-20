"""Static compose service -> ProvisioningSpec (clean tier Inc1).

Pure, deterministic parse of a single compose ``services:`` entry into a
structured ``ProvisioningSpec`` (kind, params, probe, port, init files).
No Docker, no network, no LLM — just YAML + string parsing. Ported from the
validated scratchpad PoC (`poc_translate.py`); nothing imports this module yet.
"""

from __future__ import annotations

import glob
import os
from collections.abc import Iterator
from dataclasses import dataclass, field

import yaml

from python_deps.depgraph.service_scan import _kind_of


@dataclass(frozen=True)
class ProvisioningSpec:
    service_name: str
    kind: str | None
    image: str
    params: dict = field(default_factory=dict)
    init_files: tuple = ()
    probe: str | None = None
    port: int | None = None
    build: bool = False   # compose `build:` present -> locally-built (the app), not a pulled dep


_ENV_KEYS = {
    "postgres": {"db": ("POSTGRES_DB",), "user": ("POSTGRES_USER",), "password": ("POSTGRES_PASSWORD",)},
    "mysql": {"db": ("MYSQL_DATABASE",), "user": ("MYSQL_USER",), "password": ("MYSQL_PASSWORD", "MYSQL_ROOT_PASSWORD")},
    "mongo": {"db": ("MONGO_INITDB_DATABASE",), "user": ("MONGO_INITDB_ROOT_USERNAME",), "password": ("MONGO_INITDB_ROOT_PASSWORD",)},
}


def _env_dict(entry):
    env = entry.get("environment")
    if isinstance(env, dict):
        return {str(k): str(v) for k, v in env.items()}
    out = {}
    if isinstance(env, list):
        for item in env:
            s = str(item)
            if "=" in s:
                k, _, v = s.partition("="); out[k.strip()] = v.strip()
    return out


def _params_from_env(kind, entry):
    env = _env_dict(entry); keys = _ENV_KEYS.get(kind, {}); out = {}
    for role, names in keys.items():
        for n in names:
            if n in env:
                out[role] = env[n]; break
    return out


def _init_files(entry):
    vols = entry.get("volumes") or []; out = []
    for v in vols:
        s = v if isinstance(v, str) else ""
        if "docker-entrypoint-initdb.d" in s or "/initdb" in s:
            out.append(s.split(":")[0])
    return tuple(out)


def _probe(entry):
    hc = entry.get("healthcheck")
    if isinstance(hc, dict) and hc.get("test"):
        t = hc["test"]
        if isinstance(t, list):
            return " ".join(str(x) for x in t if x not in ("CMD", "CMD-SHELL"))
        return str(t)
    return None


def _port(entry):
    ports = entry.get("ports") or []
    if ports:
        tail = str(ports[0]).split(":")[-1].split("/")[0]
        return int(tail) if tail.isdigit() else None
    return None


def parse_provisioning_spec(name, entry):
    entry = entry if isinstance(entry, dict) else {}
    image = entry.get("image", "")
    kind = _kind_of(name, image)
    return ProvisioningSpec(name, kind, image, _params_from_env(kind, entry),
                            _init_files(entry), _probe(entry), _port(entry),
                            build=bool(entry.get("build")))


def _compose_paths(repo):
    """Every compose doc path under `repo`, deduplicated.

    ``glob(..., recursive=True)`` matches zero-or-more directories, so a root-level
    ``docker-compose.yml`` is returned by BOTH the flat and the recursive pattern;
    dedupe (order-preserving) so callers don't double-yield its services.
    """
    seen: set[str] = set()
    out: list[str] = []
    for path in glob.glob(os.path.join(repo, "*compose*.y*ml")) + \
                glob.glob(os.path.join(repo, "**/*compose*.y*ml"), recursive=True):
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _compose_services(repo):
    """Yield ``(relpath, name, entry)`` for every service in every compose doc in `repo`."""
    for path in _compose_paths(repo):
        try:
            with open(path) as fh:
                doc = yaml.safe_load(fh)
        except Exception:
            continue
        svcs = doc.get("services") if isinstance(doc, dict) else None
        if isinstance(svcs, dict):
            for name, entry in svcs.items():
                yield os.path.relpath(path, repo), name, entry


def iter_provisioning_specs(repo: str) -> Iterator[ProvisioningSpec]:
    """Walk every compose doc in `repo` and yield ONE ProvisioningSpec per service.

    Per-service (do NOT use ``service_scan.scan_compose_services``, which collapses
    by kind and would drop repeated/sibling services of the same kind).
    """
    for _cfile, name, entry in _compose_services(repo):
        yield parse_provisioning_spec(name, entry if isinstance(entry, dict) else {})
