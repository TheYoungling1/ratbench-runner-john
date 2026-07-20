from dataclasses import replace

import pytest

from python_deps.depgraph import build_deps
from python_deps.depgraph.build_deps import (
    PACKAGE_TO_BUILD_NEEDS, build_env_for, seed_build_deps,
)
from python_deps.depgraph.emit import _is_reciped
from python_deps.depgraph.ids import (
    apt_build_id, binary_id, header_id, package_id, pkgconfig_id,
)
from python_deps.depgraph.os_resolver import ObservedNeed
from python_deps.depgraph.schema import (
    DepGraph, DiscoveredBy, Layer, Node, NodeType, State,
)

from conftest import FakeExecutor, make_result  # type: ignore


@pytest.fixture(autouse=True)
def _no_cross_part(monkeypatch):
    # Curated-table tests must see ONLY the curated source; debian/broad tests
    # override debian_build_deps within the test.
    monkeypatch.setattr(build_deps, "debian_build_deps", lambda *a, **k: [])
    monkeypatch.setattr(build_deps, "pep725_external", lambda *a, **k: [])


_EX = FakeExecutor(responses={"apt-get install -s": make_result(returncode=0)})


def _pkg(name, version, build_from_source=True):
    return Node(
        id=package_id(name, version), type=NodeType.PACKAGE, name=name,
        layer=Layer.PIP, discovered_by=DiscoveredBy.RESOLVER, version=version,
        build_from_source=build_from_source,
    )


def _graph(*nodes):
    g = DepGraph()
    for n in nodes:
        g = g.with_node(n)
    return g


def test_seeds_capability_node_for_known_sdist_package():
    out = seed_build_deps(_graph(_pkg("psycopg2", "2.9.12")), _EX)
    node = out.get(binary_id("pg_config"))
    assert node is not None
    assert node.type is NodeType.TOOL           # build-time -> TOOLCHAIN tier
    assert node.layer is Layer.TOOLCHAIN
    assert node.discovered_by is DiscoveredBy.RESOLVER
    assert node.state is State.UNKNOWN
    assert node.chosen_fix == "apt:libpq-dev"   # resolved via PROVIDER_TABLE
    assert node.fix_candidates == ("apt:libpq-dev",)
    assert node.check_command == "command -v pg_config"
    assert node.data["resolution_status"] == "resolved"
    assert node.data["observation_strength"] == "curated"
    assert any(e.dst == binary_id("pg_config") and e.origin == "resolver" for e in out.edges)


def test_pkgconfig_capability_node():
    out = seed_build_deps(_graph(_pkg("pycairo", "1.25.1")), _EX)
    node = out.get(pkgconfig_id("cairo"))
    assert node.chosen_fix == "apt:libcairo2-dev"
    assert node.check_command == "pkg-config --exists cairo"


def test_pycairo_also_seeds_pkg_config_binary():
    # A pkgconfig module can't be resolved without the pkg-config binary itself.
    out = seed_build_deps(_graph(_pkg("pycairo", "1.25.1")), _EX)

    cairo_node = out.get(pkgconfig_id("cairo"))
    assert cairo_node is not None
    assert cairo_node.chosen_fix == "apt:libcairo2-dev"

    binary_node = out.get(binary_id("pkg-config"))
    assert binary_node is not None
    assert binary_node.chosen_fix == "apt:pkgconf"
    assert binary_node.check_command == "command -v pkg-config"
    assert any(
        e.src == package_id("pycairo", "1.25.1") and e.dst == binary_id("pkg-config")
        and e.origin == "resolver"
        for e in out.edges
    )


def test_dbus_python_also_seeds_pkg_config_binary():
    out = seed_build_deps(_graph(_pkg("dbus-python", "1.3.2")), _EX)

    binary_node = out.get(binary_id("pkg-config"))
    assert binary_node is not None
    assert binary_node.chosen_fix == "apt:pkgconf"
    assert binary_node.check_command == "command -v pkg-config"
    assert any(
        e.src == package_id("dbus-python", "1.3.2") and e.dst == binary_id("pkg-config")
        and e.origin == "resolver"
        for e in out.edges
    )


def test_source_built_package_gets_baseline_pkg_config_binary():
    # B3: every source-built package gets the baseline binary:pkg-config node —
    # even psycopg2, whose only curated need is binary:pg_config (no pkgconfig need).
    out = seed_build_deps(_graph(_pkg("psycopg2", "2.9.9")), _EX)
    node = out.get(binary_id("pkg-config"))
    assert node is not None
    assert node.chosen_fix == "apt:pkgconf"
    assert any(
        e.src == package_id("psycopg2", "2.9.9") and e.dst == binary_id("pkg-config")
        for e in out.edges
    )


def test_shared_pkg_config_binary_node_is_deduped_across_packages():
    out = seed_build_deps(_graph(
        _pkg("pycairo", "1.25.1"),
        _pkg("dbus-python", "1.3.2"),
    ), _EX)

    pkg_config_nodes = [n for n in out.nodes if n.id == binary_id("pkg-config")]
    assert len(pkg_config_nodes) == 1

    edges_into_pkg_config = {
        e.src for e in out.edges if e.dst == binary_id("pkg-config")
    }
    assert edges_into_pkg_config == {
        package_id("pycairo", "1.25.1"),
        package_id("dbus-python", "1.3.2"),
    }


def test_build_from_source_false_gets_no_node():
    out = seed_build_deps(_graph(_pkg("psycopg2", "2.9.12", build_from_source=False)), _EX)
    assert out.get(binary_id("pg_config")) is None
    # a confirmed wheel gets NO node — not even the baseline pkg-config (the
    # build_from_source-is-False guard precedes the baseline seed).
    assert out.get(binary_id("pkg-config")) is None


def test_unresolved_placeholder_package_gets_no_node():
    out = seed_build_deps(_graph(_pkg("psycopg2", None)), _EX)
    assert [n for n in out.nodes if n.type is NodeType.TOOL] == []


def test_absent_from_table_gets_only_baseline_pkg_config():
    out = seed_build_deps(_graph(_pkg("requests", "2.31.0")), _EX)
    tools = [n for n in out.nodes if n.type is NodeType.TOOL]
    assert [n.id for n in tools] == [binary_id("pkg-config")]
    assert out.get(binary_id("pkg-config")).chosen_fix == "apt:pkgconf"


def test_confident_subset_only():
    # V1 predicts the 9 container-verified packages; others fall through to
    # observe-time. pygobject is deliberately excluded (base-image drift).
    assert set(PACKAGE_TO_BUILD_NEEDS) == {
        "psycopg2", "mysqlclient", "pycairo", "pyaudio",
        "pyodbc", "python-ldap", "dbus-python", "python-snappy",
    }
    assert "pygobject" not in PACKAGE_TO_BUILD_NEEDS


def test_python_ldap_seeds_two_capability_nodes():
    out = seed_build_deps(_graph(_pkg("python-ldap", "3.4.4")), _EX)

    ldap_node = out.get(header_id("ldap.h"))
    assert ldap_node is not None
    assert ldap_node.chosen_fix == "apt:libldap-dev"
    assert ldap_node.check_command == (
        "find /usr/include /usr/local/include -name ldap.h 2>/dev/null | grep -q ."
    )

    sasl_node = out.get(header_id("sasl/sasl.h"))
    assert sasl_node is not None
    assert sasl_node.chosen_fix == "apt:libsasl2-dev"
    assert sasl_node.check_command == (
        "find /usr/include /usr/local/include -path '*/sasl/sasl.h' 2>/dev/null | grep -q ."
    )


def test_pycurl_seeds_binary_and_header_capability_nodes():
    out = seed_build_deps(_graph(_pkg("pycurl", "7.45.3")), _EX)

    binary_node = out.get(binary_id("curl-config"))
    assert binary_node is not None
    assert binary_node.chosen_fix == "apt:libcurl4-openssl-dev"
    assert binary_node.check_command == "command -v curl-config"

    header_node = out.get(header_id("openssl/ssl.h"))
    assert header_node is not None
    assert header_node.chosen_fix == "apt:libssl-dev"
    assert header_node.check_command == (
        "find /usr/include /usr/local/include -path '*/openssl/ssl.h' 2>/dev/null | grep -q ."
    )


def test_dbus_python_seeds_pkgconfig_capability_node():
    out = seed_build_deps(_graph(_pkg("dbus-python", "1.3.2")), _EX)

    dbus_node = out.get(pkgconfig_id("dbus-1"))
    assert dbus_node is not None
    assert dbus_node.chosen_fix == "apt:libdbus-1-dev"
    assert dbus_node.check_command == "pkg-config --exists dbus-1"

    glib_node = out.get(pkgconfig_id("glib-2.0"))
    assert glib_node is not None
    assert glib_node.chosen_fix == "apt:libglib2.0-dev"
    assert glib_node.check_command == "pkg-config --exists glib-2.0"


def test_curated_capability_node_seeded_with_edge():
    # psycopg2 curated -> binary:pg_config capability node (table-resolved) + edge.
    out = seed_build_deps(_graph(_pkg("psycopg2", "2.9.12")), _EX)
    node = out.get(binary_id("pg_config"))
    assert node is not None
    assert node.chosen_fix == "apt:libpq-dev"       # table-resolved capability
    assert node.data["observation_strength"] == "curated"
    assert _is_reciped(node) is True
    assert any(e.src == package_id("psycopg2", "2.9.12")
               and e.dst == binary_id("pg_config") for e in out.edges)


def test_debian_tight_seeds_aptdep_node_with_edge_and_check(monkeypatch):
    # 2 debian apt names (<= threshold, none covered) -> tight -> aptdep: nodes.
    monkeypatch.setattr(
        build_deps, "debian_build_deps",
        lambda name, ex: ["libgeos-dev", "libgdal-dev"],
    )
    out = seed_build_deps(_graph(_pkg("shapely", "2.0.1")), _EX)
    node = out.get(apt_build_id("libgeos-dev"))
    assert node is not None
    assert node.type is NodeType.TOOL
    assert node.layer is Layer.TOOLCHAIN
    assert node.discovered_by is DiscoveredBy.RESOLVER
    assert node.state is State.UNKNOWN
    assert node.chosen_fix == "apt:libgeos-dev"
    assert node.fix_candidates == ("apt:libgeos-dev",)
    assert "dpkg-query -W" in node.check_command and "libgeos-dev" in node.check_command
    assert node.provenance == "debian build-dep"
    assert node.data["source"] == "debian"
    # renders (chosen_fix apt: -> reciped) and is edged from the owner package.
    assert _is_reciped(node) is True
    assert any(e.src == package_id("shapely", "2.0.1")
               and e.dst == apt_build_id("libgeos-dev") for e in out.edges)


def test_all_debian_directives_become_aptdep_nodes(monkeypatch):
    # No threshold/pool: every Debian directive is seeded as an aptdep: node.
    monkeypatch.setattr(
        build_deps, "debian_build_deps",
        lambda name, ex: [f"lib{i}-dev" for i in range(5)],
    )
    out = seed_build_deps(_graph(_pkg("uwsgi", "2.0.21")), _EX)
    pkg = out.get(package_id("uwsgi", "2.0.21"))
    assert "debian_build_dep_pool" not in pkg.data
    for i in range(5):
        assert out.get(apt_build_id(f"lib{i}-dev")) is not None


def test_debian_apt_deduped_against_curated_no_aptdep_node(monkeypatch):
    # psycopg2 curated pg_config -> libpq-dev; debian also lists libpq-dev -> the
    # aptdep: node is NOT created (the capability node already installs it).
    monkeypatch.setattr(
        build_deps, "debian_build_deps",
        lambda name, ex: ["libpq-dev"],
    )
    out = seed_build_deps(_graph(_pkg("psycopg2", "2.9.12")), _EX)
    assert out.get(apt_build_id("libpq-dev")) is None       # deduped away
    assert out.get(binary_id("pg_config")).chosen_fix == "apt:libpq-dev"


def test_aptdep_node_shared_across_packages(monkeypatch):
    # two packages both need libz-dev -> ONE aptdep node, two edges.
    monkeypatch.setattr(
        build_deps, "debian_build_deps",
        lambda name, ex: ["libz-dev"],
    )
    out = seed_build_deps(
        _graph(_pkg("pkga", "1.0"), _pkg("pkgb", "2.0")), _EX)
    assert out.get(apt_build_id("libz-dev")) is not None
    edges = [e for e in out.edges if e.dst == apt_build_id("libz-dev")]
    assert {e.src for e in edges} == {package_id("pkga", "1.0"), package_id("pkgb", "2.0")}


def test_flavor_build_env_stamped_on_package_node():
    # pycurl: curated+flavor path (debian stub empty); build_env stamped, forced
    # openssl capabilities seeded strong + edged.
    out = seed_build_deps(_graph(_pkg("pycurl", "7.45.3")), _EX)
    pkg = out.get(package_id("pycurl", "7.45.3"))
    assert pkg.data["build_env"] == {"PYCURL_SSL_LIBRARY": "openssl"}
    assert out.get(binary_id("curl-config")).chosen_fix == "apt:libcurl4-openssl-dev"
    assert out.get(header_id("openssl/ssl.h")).chosen_fix == "apt:libssl-dev"


def test_wheel_package_skipped(monkeypatch):
    # build_from_source is False -> no prior seeded at all.
    monkeypatch.setattr(
        build_deps, "debian_build_deps",
        lambda name, ex: ["libgeos-dev"],
    )
    wheel = replace(_pkg("shapely", "2.0.1"), build_from_source=False)
    out = seed_build_deps(_graph(wheel), _EX)
    assert out.get(apt_build_id("libgeos-dev")) is None


def test_seed_build_deps_logs_aggregate(monkeypatch, caplog):
    import logging as _log
    monkeypatch.setattr(build_deps, "debian_build_deps",
                        lambda name, ex: ["libgeos-dev", "libgdal-dev"])
    with caplog.at_level(_log.INFO, logger="python_deps.depgraph.build_deps"):
        seed_build_deps(_graph(_pkg("shapely", "2.0.1")), _EX)
    line = next(r.getMessage() for r in caplog.records if "seed_build_deps: pkgs=" in r.getMessage())
    # cap_nodes=1: the baseline binary:pkg-config node (B3), shapely's own
    # curated plan contributes zero capability needs (debian-only).
    assert "pkgs=1" in line and "cap_nodes=1" in line and "aptdep_nodes=2" in line
