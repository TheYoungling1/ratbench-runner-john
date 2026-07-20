"""Committed corpus manifest for the e2e build-script eval. No held-out recipe
is required (there is no oracle) — the only membership rule is a runnable test
suite, and S_syslib rows are chosen so their tests import the native extension
(a missing .so then surfaces at the import/collect rung, service-independent).
"""
from __future__ import annotations

from dataclasses import dataclass

STRATA: frozenset[str] = frozenset({"S_control", "S_syslib"})


@dataclass(frozen=True)
class RepoSpec:
    name: str                    # unique dir name under the _smoke root
    full_name: str               # "org/repo" (display / output id)
    git_url: str
    ref: str                     # pinned tag or sha (see fetch.py; verify with git ls-remote)
    stratum: str                 # one of STRATA
    top_import: str | None = None      # import-check override; else derived
    feasible: bool = True              # False ⇒ excluded from the headline denominator
    network_in_tests: bool = False     # True ⇒ keep network during the pytest rung


# Starter corpus. Refs are concrete tags; Task 5 verifies each with `git ls-remote`
# before the first fetch and pins to a sha if desired. Keep S_control apt-empty
# (over-prediction baseline) and S_syslib native-ext-importing.
CORPUS: tuple[RepoSpec, ...] = (
    # --- S_control: pure-Python, no apt expected ---
    RepoSpec("typer", "fastapi/typer",
             "https://github.com/fastapi/typer", "0.12.5", "S_control", top_import="typer"),
    RepoSpec("python-semantic-release", "python-semantic-release/python-semantic-release",
             "https://github.com/python-semantic-release/python-semantic-release",
             "v9.8.6", "S_control", top_import="semantic_release"),
    # --- S_syslib: source-form native deps; tests import the extension ---
    RepoSpec("psycopg2", "psycopg/psycopg2",
             "https://github.com/psycopg/psycopg2", "2.9.9", "S_syslib", top_import="psycopg2"),
    RepoSpec("pygraphviz", "pygraphviz/pygraphviz",
             "https://github.com/pygraphviz/pygraphviz", "pygraphviz-1.12", "S_syslib",
             top_import="pygraphviz"),
    RepoSpec("lxml", "lxml/lxml",
             "https://github.com/lxml/lxml", "lxml-5.2.2", "S_syslib", top_import="lxml"),
    # --- diagnostic expansion (2026-07-06): breadth to distinguish root-cause
    #     clusters from per-repo artifacts. Pure-Python controls (over-prediction
    #     baseline) + more native repos (under-prediction / generalization proof). ---
    RepoSpec("click", "pallets/click",
             "https://github.com/pallets/click", "8.4.2", "S_control", top_import="click"),
    RepoSpec("flask", "pallets/flask",
             "https://github.com/pallets/flask", "3.1.3", "S_control", top_import="flask"),
    RepoSpec("jinja", "pallets/jinja",
             "https://github.com/pallets/jinja", "3.1.6", "S_control", top_import="jinja2"),
    RepoSpec("requests", "psf/requests",
             "https://github.com/psf/requests", "v2.34.2", "S_control", top_import="requests"),
    RepoSpec("httpx", "encode/httpx",
             "https://github.com/encode/httpx", "0.28.1", "S_control", top_import="httpx"),
    RepoSpec("rich", "Textualize/rich",
             "https://github.com/Textualize/rich", "v15.0.0", "S_control", top_import="rich"),
    RepoSpec("python-dotenv", "theskumar/python-dotenv",
             "https://github.com/theskumar/python-dotenv", "v1.2.2", "S_control", top_import="dotenv"),
    RepoSpec("pyyaml", "yaml/pyyaml",
             "https://github.com/yaml/pyyaml", "6.0.3", "S_syslib", top_import="yaml"),
    RepoSpec("pyzmq", "zeromq/pyzmq",
             "https://github.com/zeromq/pyzmq", "v27.1.0", "S_syslib", top_import="zmq"),
    RepoSpec("pillow", "python-pillow/Pillow",
             "https://github.com/python-pillow/Pillow", "12.3.0", "S_syslib", top_import="PIL"),
    RepoSpec("cryptography", "pyca/cryptography",
             "https://github.com/pyca/cryptography", "49.0.0", "S_syslib", top_import="cryptography"),
)


def select(only: frozenset[str] = frozenset(),
           strata: frozenset[str] = frozenset()) -> list[RepoSpec]:
    """Filter CORPUS by repo name (`only`) and/or stratum (`strata`). Empty set =
    no filter on that axis. Raises ValueError on an unknown stratum or an unknown
    name (fail-fast on a typo)."""
    if strata - STRATA:
        raise ValueError(f"unknown stratum(s): {sorted(strata - STRATA)}; valid={sorted(STRATA)}")
    names = {r.name for r in CORPUS}
    if only - names:
        raise ValueError(f"unknown repo name(s): {sorted(only - names)}")
    return [r for r in CORPUS
            if (not only or r.name in only) and (not strata or r.stratum in strata)]
