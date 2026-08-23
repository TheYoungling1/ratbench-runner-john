"""Tests for ids.py helpers — capability-id constructors and apt_build_id namespacing."""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from python_deps.depgraph.ids import (
    binary_id, header_id, pkgconfig_id, capability_id, syslib_id,
)
from python_deps.depgraph.ids import apt_build_id


def test_capability_id_constructors():
    assert header_id("libpq-fe.h") == "header:libpq-fe.h"
    assert binary_id("pg_config") == "binary:pg_config"
    assert pkgconfig_id("cairo") == "pkgconfig:cairo"
    assert syslib_id("libGL.so.1") == "syslib:libGL.so.1"


def test_capability_id_dispatches_on_kind():
    assert capability_id("header", "libpq-fe.h") == "header:libpq-fe.h"
    assert capability_id("binary", "pg_config") == "binary:pg_config"
    assert capability_id("pkgconfig", "cairo") == "pkgconfig:cairo"
    assert capability_id("soname", "libGL.so.1") == "syslib:libGL.so.1"


def test_apt_build_id_is_aptdep_namespaced():
    assert apt_build_id("libpq-dev") == "aptdep:libpq-dev"
    # distinct id space from every capability id (never collides with binary:/header:/...)
    assert apt_build_id("libpq-dev").split(":", 1)[0] == "aptdep"
