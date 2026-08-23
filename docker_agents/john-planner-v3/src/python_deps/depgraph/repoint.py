"""Generic compose-host -> localhost DSN rewrite (clean tier Inc1).

Pure, deterministic. Replaces the old postgres-only binding contract with a generic
host-swap: for each declared service DSN whose kind matches a declared
``ProvisioningSpec``, emit an ``export <VAR>=<dsn>`` step with the host rewritten to
``127.0.0.1`` and everything else — scheme (incl. dialect suffix), userinfo/creds,
port, path, query, fragment — preserved. The daemon started by ``render_setup`` binds
on loopback, so a pure host-swap keeps the app's own declared creds consistent (no
credential injection here). Works for redis/mysql/mongo/postgres/... alike.

Reuses ``service_scan.service_from_url`` to identify a DSN's kind. Nothing imports
this module yet; a later increment (CR5) will consume it.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

from python_deps.depgraph.provisioning_spec import ProvisioningSpec
from python_deps.depgraph.service_scan import service_from_url

_LOCALHOST = "127.0.0.1"


def _repoint_host(value: str) -> str:
    """Return ``value`` with its host rewritten to loopback, all else preserved."""
    u = urlsplit(value)
    userinfo = ""
    if u.username:
        userinfo = u.username + (f":{u.password}" if u.password else "") + "@"
    netloc = f"{userinfo}{_LOCALHOST}" + (f":{u.port}" if u.port else "")
    return urlunsplit((u.scheme, netloc, u.path, u.query, u.fragment))


def render_bind_steps(
    specs: Iterable[ProvisioningSpec],
    configs: Iterable[tuple[str, str]],
) -> list[str]:
    """``export <VAR>=<loopback DSN>`` steps for configs matching a declared service.

    ``specs`` are the declared services; ``configs`` are ``(env_var, dsn_value)``
    pairs. A config is emitted only when ``service_from_url`` recognizes its value as
    a DSN AND that DSN's kind is one of the declared service kinds. Order is preserved
    over ``configs``; non-DSN / unmatched-kind configs are skipped.
    """
    declared_kinds = {s.kind for s in specs if s.kind}
    steps: list[str] = []
    for var, value in configs:
        parsed = service_from_url(value)
        if parsed is None:
            continue
        kind = parsed[0]
        if kind not in declared_kinds:
            continue
        steps.append(f"export {var}={_repoint_host(value)}")
    return steps
