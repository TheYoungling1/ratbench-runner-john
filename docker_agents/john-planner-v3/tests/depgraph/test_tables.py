"""Task 6 — curated native provider tables.

Post-1.2d: the soname / build-tool / header -> apt tables were unified into
``os_resolver.PROVIDER_TABLE`` (covered by ``test_os_resolver.py``). This module
now keeps only the runtime-CLI authority (``CLI_TOOL_TO_APT``) and the native-risk
gate (``NATIVE_RISK_PACKAGES``), which the resolver does not supersede.
"""

from __future__ import annotations

from python_deps.depgraph.tables import (
    CLI_TOOL_TO_APT,
    NATIVE_RISK_PACKAGES,
)


def test_native_risk_packages_membership():
    for pkg in ("opencv-python", "psycopg2", "lxml", "mysqlclient"):
        assert pkg in NATIVE_RISK_PACKAGES


def test_tables_are_nonempty_dicts():
    assert isinstance(CLI_TOOL_TO_APT, dict) and CLI_TOOL_TO_APT
    assert isinstance(NATIVE_RISK_PACKAGES, frozenset) and NATIVE_RISK_PACKAGES
