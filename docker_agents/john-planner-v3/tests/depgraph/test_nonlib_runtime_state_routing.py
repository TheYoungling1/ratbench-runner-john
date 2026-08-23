"""Non-library runtime state routes to Config/Service — never SystemLib.

Design §3: a missing data file (proj.db / GDAL_DATA), CA certs, or a live DB
service is not a .so; no system-lib tier models it. These lock that the
system-lib path (extract_needs / test_gate_probe) refuses them and the existing
Config/Service classifiers own them.
"""
from __future__ import annotations

from python_deps.depgraph.failure_signatures import extract_needs
from python_deps.depgraph.probe import test_gate_probe
from python_deps.depgraph.runtime_classify import classify_observation
from python_deps.depgraph.schema import DepGraph, NodeType


def test_gdal_data_env_var_routes_to_config_not_syslib():
    out = "KeyError: 'GDAL_DATA'"
    disc = classify_observation("python -m pytest", out)
    assert disc is not None and disc.node_type is NodeType.CONFIG
    assert disc.name == "GDAL_DATA"
    # And the system-lib extractor refuses it.
    assert extract_needs(out, context_hint="runtime") == []
    assert test_gate_probe(DepGraph(), None, out).nodes == ()


def test_proj_db_data_file_is_not_a_soname():
    out = ("pyproj/database.py:12: in _load\n"
           "FileNotFoundError: [Errno 2] No such file or directory: "
           "'/usr/share/proj/proj.db'\n")
    assert [n for n in extract_needs(out, context_hint="runtime") if n.kind == "soname"] == []
    assert test_gate_probe(DepGraph(), None, out).nodes == ()


def test_live_db_service_routes_to_service_not_syslib():
    out = "psycopg2.OperationalError: could not connect to server: Connection refused"
    disc = classify_observation("python -m pytest", out)
    assert disc is not None and disc.node_type is NodeType.SERVICE
    assert disc.name == "postgres"
    assert test_gate_probe(DepGraph(), None, out).nodes == ()
