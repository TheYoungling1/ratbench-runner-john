from python_deps.depgraph import wheel_preflight
from python_deps.depgraph.ids import package_id, syslib_id
from python_deps.depgraph.schema import (
    DepGraph,
    DiscoveredBy,
    Layer,
    Node,
    NodeType,
    State,
)
from python_deps.depgraph.target_env import TargetEnv


def _target_env():
    return TargetEnv(
        python_full="3.11.0",
        python_version="3.11",
        platform_machine="x86_64",
        sys_platform="linux",
        os_name="posix",
        platform_system="Linux",
        python_platform_tag="x86_64-manylinux_2_28",
    )


def _pkg(name, version, build_from_source=False):
    return Node(
        id=package_id(name, version),
        type=NodeType.PACKAGE,
        name=name,
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.RESOLVER,
        version=version,
        build_from_source=build_from_source,  # default: map-classified wheel
    )


def _graph(*pkgs):
    g = DepGraph()
    for p in pkgs:
        g = g.with_node(p)
    return g


def test_predicts_soname_as_resolver_unknown_prior(monkeypatch):
    monkeypatch.setattr(wheel_preflight, "download_target_wheel", lambda *a, **k: "/tmp/fake.whl")
    monkeypatch.setattr(wheel_preflight, "inspect_wheel_sonames", lambda p: {"libGL.so.1"})
    g = _graph(_pkg("opencv-python", "4.9.0.80"))

    out = wheel_preflight.wheel_preflight_probe(g, object(), _target_env())

    node = out.get(syslib_id("libGL.so.1"))
    assert node is not None
    assert node.discovered_by is DiscoveredBy.RESOLVER
    assert node.state is State.UNKNOWN
    assert node.chosen_fix == "apt:libgl1"  # table hit
    assert any(
        e.dst == syslib_id("libGL.so.1") and e.origin == "resolver" for e in out.edges
    )


def test_table_miss_leaves_fix_empty_but_node_present(monkeypatch):
    monkeypatch.setattr(wheel_preflight, "download_target_wheel", lambda *a, **k: "/tmp/x.whl")
    monkeypatch.setattr(wheel_preflight, "inspect_wheel_sonames", lambda p: {"libweird.so.9"})
    g = _graph(_pkg("weirdpkg", "1.0"))

    out = wheel_preflight.wheel_preflight_probe(g, object(), _target_env())

    node = out.get(syslib_id("libweird.so.9"))
    assert node is not None
    assert node.fix_candidates == ()
    assert node.state is State.UNKNOWN


def test_wheel_download_none_adds_nothing(monkeypatch):
    monkeypatch.setattr(wheel_preflight, "download_target_wheel", lambda *a, **k: None)
    called = {"n": 0}

    def _inspect(p):
        called["n"] += 1
        return set()

    monkeypatch.setattr(wheel_preflight, "inspect_wheel_sonames", _inspect)
    g = _graph(_pkg("psycopg2", "2.9.12"))  # build_from_source=False (wheel)

    out = wheel_preflight.wheel_preflight_probe(g, object(), _target_env())

    assert called["n"] == 0  # never inspected — download returned None first
    assert [n for n in out.nodes if n.type is NodeType.SYSTEM_LIB] == []


def test_sdist_package_is_not_inspected(monkeypatch):
    calls = {"n": 0}

    def _dl(*a, **k):
        calls["n"] += 1
        return "/tmp/should-not-happen.whl"

    monkeypatch.setattr(wheel_preflight, "download_target_wheel", _dl)
    monkeypatch.setattr(wheel_preflight, "inspect_wheel_sonames", lambda p: {"libpq.so.5"})
    g = _graph(_pkg("psycopg2", "2.9.12", build_from_source=True))  # map: sdist

    out = wheel_preflight.wheel_preflight_probe(g, object(), _target_env())

    assert calls["n"] == 0  # sdist packages are skipped — no wheel to inspect
    assert [n for n in out.nodes if n.type is NodeType.SYSTEM_LIB] == []


def test_unclassified_package_is_not_inspected(monkeypatch):
    calls = {"n": 0}

    def _dl(*a, **k):
        calls["n"] += 1
        return "/tmp/nope.whl"

    monkeypatch.setattr(wheel_preflight, "download_target_wheel", _dl)
    monkeypatch.setattr(wheel_preflight, "inspect_wheel_sonames", lambda p: {"libGL.so.1"})
    g = _graph(_pkg("mystery", "1.0", build_from_source=None))  # map had no opinion

    out = wheel_preflight.wheel_preflight_probe(g, object(), _target_env())

    assert calls["n"] == 0  # only build_from_source is False (wheel) is inspected
    assert [n for n in out.nodes if n.type is NodeType.SYSTEM_LIB] == []


def test_unresolved_placeholder_package_is_skipped(monkeypatch):
    calls = {"n": 0}

    def _dl(*a, **k):
        calls["n"] += 1
        return None

    monkeypatch.setattr(wheel_preflight, "download_target_wheel", _dl)
    monkeypatch.setattr(wheel_preflight, "inspect_wheel_sonames", lambda p: set())
    # a resolver placeholder: Package node with version=None (unresolved conflict)
    placeholder = Node(
        id="pkg:foo",
        type=NodeType.PACKAGE,
        name="foo",
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.RESOLVER,
        version=None,
    )
    g = DepGraph().with_node(placeholder)
    out = wheel_preflight.wheel_preflight_probe(g, object(), _target_env())
    assert calls["n"] == 0  # never attempted a download for an unresolved placeholder
    assert [n for n in out.nodes if n.type is NodeType.SYSTEM_LIB] == []


def test_same_soname_two_packages_one_node_two_edges(monkeypatch):
    monkeypatch.setattr(wheel_preflight, "download_target_wheel", lambda *a, **k: "/tmp/x.whl")
    monkeypatch.setattr(wheel_preflight, "inspect_wheel_sonames", lambda p: {"libGL.so.1"})
    g = _graph(_pkg("opencv-python", "4.9"), _pkg("opencv-python-headless", "4.9"))

    out = wheel_preflight.wheel_preflight_probe(g, object(), _target_env())

    syslibs = [n for n in out.nodes if n.id == syslib_id("libGL.so.1")]
    assert len(syslibs) == 1
    edges = [e for e in out.edges if e.dst == syslib_id("libGL.so.1")]
    assert len(edges) == 2


import logging


def test_wheel_preflight_logs_inspected_and_skipped(monkeypatch, caplog):
    monkeypatch.setattr(wheel_preflight, "download_target_wheel", lambda *a, **k: None)
    monkeypatch.setattr(wheel_preflight, "inspect_wheel_sonames", lambda p: set())
    g = _graph(
        _pkg("wheelpkg", "1.0"),                                  # build_from_source=False (inspected)
        _pkg("sdistpkg", "1.0", build_from_source=True),          # skipped_sdist
        _pkg("unknownpkg", "1.0", build_from_source=None),        # skipped_unknown
    )
    with caplog.at_level(logging.INFO, logger="python_deps.depgraph.wheel_preflight"):
        wheel_preflight.wheel_preflight_probe(g, object(), _target_env())
    line = next(r.getMessage() for r in caplog.records if "wheel_preflight: inspected=" in r.getMessage())
    assert "inspected=1" in line and "skipped_sdist=1" in line and "skipped_unknown=1" in line
