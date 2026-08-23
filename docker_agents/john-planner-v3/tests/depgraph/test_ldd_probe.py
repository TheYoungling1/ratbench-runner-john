"""Unit tests for Stage 4.5 ldd-based native library discovery.

Most tests use ``FakeExecutor`` (from conftest) — no Docker, no network.
Two helper tests run ``EXT_SO_MAP_CMD`` on the LOCAL HOST via
``LocalSubprocessExecutor`` to verify shell-quoting and ``files=None``
handling (no Docker needed — just a real Python interpreter).
"""

from __future__ import annotations

import json
import os
import re
import sys

from python_deps.depgraph.ids import package_id, syslib_id
from python_deps.depgraph.ldd_probe import (
    EXT_SO_MAP_CMD,
    ldd_probe,
    parse_ext_so_map,
    parse_ldd_not_found,
)
from python_deps.depgraph.schema import (
    DepGraph,
    DiscoveredBy,
    Edge,
    EdgeType,
    Layer,
    Node,
    NodeType,
    State,
)


# ── Shared node-builder helpers ───────────────────────────────────────────────

def _package(name: str, version: str) -> Node:
    return Node(
        id=package_id(name, version),
        type=NodeType.PACKAGE,
        name=name,
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.RESOLVER,
        version=version,
    )


def _predicted_syslib(key: str) -> Node:
    """A resolver-predicted SystemLib node (id keyed by ``key``).

    Post Task 9, callers pass the canonical SONAME (e.g. ``libGL.so.1``) — this
    mirrors what ``seed._predicted_syslib_node`` now actually builds.
    """
    return Node(
        id=syslib_id(key),
        type=NodeType.SYSTEM_LIB,
        name=key,
        layer=Layer.SYSTEM,
        discovered_by=DiscoveredBy.RESOLVER,
        state=State.UNKNOWN,
        check_command=f"dpkg -s {key}",
        fix_candidates=(f"apt:{key}",),
    )


# ── parse_ldd_not_found ───────────────────────────────────────────────────────

def test_parse_ldd_not_found_single_file():
    stdout = (
        "\tlinux-vdso.so.1 (0x00007fff...) => ...\n"
        "\tlibGL.so.1 => not found\n"
        "\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6\n"
    )
    assert parse_ldd_not_found(stdout) == ["libGL.so.1"]


def test_parse_ldd_not_found_multi_file_deduped():
    """Multi-file ldd output: sonames deduped across all file sections."""
    stdout = (
        "/path/cv2.cpython-311.so:\n"
        "\tlibGL.so.1 => not found\n"
        "\tlibgthread-2.0.so.0 => not found\n"
        "/path/other.cpython-311.so:\n"
        "\tlibGL.so.1 => not found\n"  # duplicate — must appear only once
        "\tlibc.so.6 => /lib/libc.so.6\n"
    )
    result = parse_ldd_not_found(stdout)
    assert result == ["libGL.so.1", "libgthread-2.0.so.0"]


def test_parse_ldd_not_found_ignores_found_libs():
    stdout = (
        "\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6\n"
        "\tlibm.so.6 => /lib/x86_64-linux-gnu/libm.so.6\n"
    )
    assert parse_ldd_not_found(stdout) == []


def test_parse_ldd_not_found_empty_input():
    assert parse_ldd_not_found("") == []


def test_parse_ldd_not_found_ignores_per_file_headers():
    """Path: header lines must not be mistaken for sonames."""
    stdout = "/some/path/foo.so:\n\tlibfoo.so.1 => not found\n"
    result = parse_ldd_not_found(stdout)
    assert result == ["libfoo.so.1"]
    assert "/some/path/foo.so:" not in result


# ── parse_ext_so_map ─────────────────────────────────────────────────────────

def test_parse_ext_so_map_valid_json():
    data = {
        "opencv-python": [
            "/usr/local/lib/python3.11/dist-packages/cv2/cv2.cpython-311-x86_64-linux-gnu.so"
        ],
        "numpy": [],
    }
    out = parse_ext_so_map(json.dumps(data))
    assert out["opencv-python"] == data["opencv-python"]
    assert out["numpy"] == []


def test_parse_ext_so_map_malformed_returns_empty():
    assert parse_ext_so_map("not json") == {}
    assert parse_ext_so_map("") == {}
    assert parse_ext_so_map("[1, 2, 3]") == {}


def test_parse_ext_so_map_skips_non_string_path_entries():
    raw = '{"opencv-python": ["/path/cv2.so", 42, null, "/path/other.so"]}'
    out = parse_ext_so_map(raw)
    assert out["opencv-python"] == ["/path/cv2.so", "/path/other.so"]


def test_ext_so_regex_matches_two_and_three_digit_cpython_tags():
    """The embedded EXT regex must match BOTH 2-digit (3.0-3.9) and 3-digit
    (3.10+) cpython ABI tags, else ldd_probe silently skips every extension
    module on Python 3.9 and never discovers its native libs.

    Regression: the quantifier was once ``[0-9]{3}`` (3-digit only), which
    matched 0/32 of pygame's ``*.cpython-39-*.so`` files on python:3.9-slim.
    """
    # Extract the actual EXT pattern compiled inside EXT_SO_MAP_CMD so a change
    # to the command (e.g. a revert to {3}) fails this test.
    m = re.search(r"EXT = re\.compile\('([^']*)'\)", EXT_SO_MAP_CMD)
    assert m, "could not locate EXT pattern in EXT_SO_MAP_CMD"
    ext = re.compile(m.group(1))

    matches = [
        "image.cpython-39-aarch64-linux-gnu.so",   # 3.9  (the bug)
        "_speedups.cpython-38-x86_64-linux-gnu.so",  # 3.8
        "cv2.cpython-311-x86_64-linux-gnu.so",       # 3.11
        "_ext.cpython-313-aarch64-linux-gnu.so",     # 3.13
        "_cffi_backend.abi3.so",                     # stable ABI
    ]
    for bn in matches:
        assert ext.search(bn), f"EXT should match {bn!r}"

    # Non-extension shared objects must still NOT match.
    for bn in ("libfoo.so", "libfoo.so.6", "module.py"):
        assert not ext.search(bn), f"EXT should NOT match {bn!r}"


# ── EXT_SO_MAP_CMD local execution (no Docker) ───────────────────────────────

def _local_cmd(cmd: str) -> str:
    """Replace the ``python`` interpreter in a container command with the local one.

    ``EXT_SO_MAP_CMD`` uses the bare ``python`` name which is correct inside a
    Docker container (python:3.X-slim) but may not exist on the test host (macOS
    ships only ``python3``).  Swap the interpreter for local execution only.
    """
    return cmd.replace("python -c", f"{sys.executable} -c", 1)


def test_ext_so_map_cmd_does_not_crash_on_host():
    """EXT_SO_MAP_CMD runs on the local host without crashing.

    This exercises the ``d.files is None`` guard: at least some installed
    distributions on a real system have no RECORD file (e.g. system-managed
    packages), and the command must skip them gracefully.
    """
    from python_deps.depgraph.executor import LocalSubprocessExecutor

    result = LocalSubprocessExecutor().run(_local_cmd(EXT_SO_MAP_CMD))
    assert result.ok, f"EXT_SO_MAP_CMD crashed: {result.stderr[:400]}"
    data = json.loads(result.stdout)  # valid JSON
    assert isinstance(data, dict)


def test_ext_so_map_cmd_excludes_bundled_helpers_locally():
    """EXT_SO_MAP_CMD output contains NO bundled manylinux helper paths.

    Verifies the BND regex filter and the ``.libs/`` path check embedded in
    the command itself.  Any path containing ``.libs/`` or whose basename
    matches ``^lib<name>-<8hex>.so`` is a manylinux auditwheel-bundled helper
    and must be excluded (standalone ldd on them produces false ``not found``
    because they can't follow their parent's RPATH).
    """
    from python_deps.depgraph.executor import LocalSubprocessExecutor

    result = LocalSubprocessExecutor().run(_local_cmd(EXT_SO_MAP_CMD))
    assert result.ok
    data = parse_ext_so_map(result.stdout)
    bnd = re.compile(r"^lib[a-z0-9._+-]+-[0-9a-f]{8}\.so$")
    for paths in data.values():
        for p in paths:
            assert ".libs/" not in p, f"bundled helper slipped through: {p!r}"
            bn = os.path.basename(p)
            assert not bnd.match(bn), f"bundled helper basename kept: {bn!r}"


# ── ldd_probe orchestrator (FakeExecutor) ────────────────────────────────────

# Canonical path used across multiple tests (a realistic cpython ext module path).
_CV2_SO = (
    "/usr/local/lib/python3.11/dist-packages/cv2/cv2.cpython-311-x86_64-linux-gnu.so"
)

_OPENCV_LDD_OUTPUT = (
    f"{_CV2_SO}:\n"
    "\tlinux-vdso.so.1 (0x00007fff...) => ...\n"
    "\tlibGL.so.1 => not found\n"
    "\tlibgthread-2.0.so.0 => not found\n"
    "\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6\n"
)


def test_ldd_probe_opencv_creates_syslib_nodes(fake_executor, make_result_fixture):
    """opencv-like canned ldd → SystemLib nodes with correct apt fix-candidates."""
    pkg = _package("opencv-python", "4.9.0.80")
    graph = DepGraph().with_node(pkg)
    fake_executor.responses = {
        # FakeExecutor matches by substring; "locate_file" is unique to EXT_SO_MAP_CMD.
        "locate_file": make_result_fixture(
            stdout=json.dumps({"opencv-python": [_CV2_SO]})
        ),
        "ldd ": make_result_fixture(stdout=_OPENCV_LDD_OUTPUT),
    }

    out = ldd_probe(graph, fake_executor)

    # libGL.so.1 is in os_resolver.PROVIDER_TABLE -> apt=libgl1; no RESOLVER
    # seed -> fresh node keyed by soname (syslib_id("libGL.so.1")).
    gl = out.get(syslib_id("libGL.so.1"))
    assert gl is not None
    assert gl.type is NodeType.SYSTEM_LIB
    assert gl.layer is Layer.SYSTEM
    assert gl.discovered_by is DiscoveredBy.PROBE
    assert gl.state is State.MISSING
    assert gl.fix_candidates == ("apt:libgl1",)
    assert gl.chosen_fix == "apt:libgl1"
    assert gl.check_command == "ldconfig -p | grep libGL.so.1"
    assert gl.provenance == "ldd (observed)"
    assert "libGL.so.1" in (gl.evidence or "")

    # libgthread-2.0.so.0 -> apt=libglib2.0-0
    gt = out.get(syslib_id("libgthread-2.0.so.0"))
    assert gt is not None
    assert gt.fix_candidates == ("apt:libglib2.0-0",)


def test_ldd_probe_creates_requires_edges(fake_executor, make_result_fixture):
    """ldd_probe adds Package→SystemLib requires edges for each discovered soname."""
    pkg = _package("opencv-python", "4.9.0.80")
    graph = DepGraph().with_node(pkg)
    fake_executor.responses = {
        "locate_file": make_result_fixture(
            stdout=json.dumps({"opencv-python": [_CV2_SO]})
        ),
        "ldd ": make_result_fixture(stdout=_OPENCV_LDD_OUTPUT),
    }

    out = ldd_probe(graph, fake_executor)

    deps = out.requires_of(pkg.id)
    dep_ids = {d.id for d in deps}
    assert syslib_id("libGL.so.1") in dep_ids
    assert syslib_id("libgthread-2.0.so.0") in dep_ids


def test_ldd_probe_two_packages_same_soname_preserve_attempts(
    fake_executor, make_result_fixture
):
    """Two packages both reporting the same not-found soname (no RESOLVER seed)
    collapse onto ONE soname-keyed node whose attempt history records BOTH
    packages' ldd probes — the second package must not silently overwrite the
    first package's attempt (review MEDIUM-1)."""
    so_a = "/usr/local/lib/python3.11/dist-packages/aaa/aaa.cpython-311-x86_64-linux-gnu.so"
    so_b = "/usr/local/lib/python3.11/dist-packages/bbb/bbb.cpython-311-x86_64-linux-gnu.so"
    pkg_a = _package("aaa", "1.0.0")
    pkg_b = _package("bbb", "2.0.0")
    graph = DepGraph().with_node(pkg_a).with_node(pkg_b)
    fake_executor.responses = {
        "locate_file": make_result_fixture(
            stdout=json.dumps({"aaa": [so_a], "bbb": [so_b]})
        ),
        "ldd ": make_result_fixture(stdout="\tlibGL.so.1 => not found\n"),
    }

    out = ldd_probe(graph, fake_executor)

    # Exactly ONE node for the shared soname.
    syslibs = [n for n in out.nodes if n.type is NodeType.SYSTEM_LIB]
    assert len(syslibs) == 1
    node = out.get(syslib_id("libGL.so.1"))
    assert node is not None
    # Both packages' ldd attempts retained (no overwrite of the first package's).
    assert len(node.attempts) == 2
    commands = {a.command for a in node.attempts}
    assert any(so_a in c for c in commands)
    assert any(so_b in c for c in commands)
    # One requires edge per package.
    srcs = {
        e.src
        for e in out.edges
        if e.dst == syslib_id("libGL.so.1") and e.relation is EdgeType.REQUIRES
    }
    assert srcs == {pkg_a.id, pkg_b.id}


def test_ldd_probe_pure_python_no_syslib(fake_executor, make_result_fixture):
    """A package absent from the so-map (pure-python) produces no SystemLib nodes
    and no ldd invocation."""
    pkg = _package("requests", "2.31.0")
    graph = DepGraph().with_node(pkg)
    # so-map has numpy but NOT requests
    fake_executor.responses = {
        "locate_file": make_result_fixture(
            stdout=json.dumps(
                {"numpy": ["/usr/local/lib/python3.11/dist-packages/numpy/core.cpython-311.so"]}
            )
        ),
    }

    out = ldd_probe(graph, fake_executor)

    assert not any(n.type is NodeType.SYSTEM_LIB for n in out.nodes)
    # ldd must NOT have been called (requests has no so-paths)
    assert not any("ldd" in c for c in fake_executor.calls)


def test_ldd_probe_reconciles_resolver_prediction_keeps_discovered_by(
    fake_executor, make_result_fixture
):
    """When a RESOLVER seed prediction exists for the same canonical soname id,
    reconcile_predicted is called: discovered_by stays RESOLVER (not PROBE), and
    no duplicate node is created.
    """
    pkg = _package("opencv-python", "4.9.0.80")
    # Seed stage pre-emitted a prediction keyed by the canonical SONAME (post
    # Task 9: the soname is the SystemLib identity, not the apt name).
    predicted = _predicted_syslib("libGL.so.1")  # id = syslib:libGL.so.1
    graph = (
        DepGraph()
        .with_node(pkg)
        .with_node(predicted)
        .with_edge(
            Edge(
                src=pkg.id,
                dst=predicted.id,
                relation=EdgeType.REQUIRES,
                origin="resolver",
            )
        )
    )
    fake_executor.responses = {
        "locate_file": make_result_fixture(
            stdout=json.dumps({"opencv-python": [_CV2_SO]})
        ),
        "ldd ": make_result_fixture(
            stdout=f"{_CV2_SO}:\n\tlibGL.so.1 => not found\n"
        ),
    }

    out = ldd_probe(graph, fake_executor)

    # Prediction reconciled in-place: keeps RESOLVER origin, ONE node total.
    node = out.get(syslib_id("libGL.so.1"))
    assert node is not None
    assert node.discovered_by is DiscoveredBy.RESOLVER
    assert len([n for n in out.nodes if n.type is NodeType.SYSTEM_LIB]) == 1

    # check_command updated from the seed's dpkg -s to the real ldconfig check.
    assert node.check_command == "ldconfig -p | grep libGL.so.1"

    # Edge from pkg→prediction deduped to exactly one (seed + ldd = same key).
    requires_to_predicted = [
        e
        for e in out.edges
        if e.src == pkg.id
        and e.dst == syslib_id("libGL.so.1")
        and e.relation is EdgeType.REQUIRES
    ]
    assert len(requires_to_predicted) == 1


def test_ldd_probe_fills_chosen_fix_left_none_by_seed(fake_executor, make_result_fixture):
    """Real defect this guards: a RESOLVER seed (e.g. wheel_preflight) resolved
    NO provider (chosen_fix=None); ldd_probe's own resolve() finds the apt
    (table hit for libGL.so.1 -> libgl1) and reconcile_predicted must fill it
    into the SAME node instead of leaving it permanently unrenderable.
    """
    pkg = _package("opencv-python", "4.9.0.80")
    predicted = _predicted_syslib("libGL.so.1")
    assert predicted.chosen_fix is None  # seed resolved no provider
    graph = (
        DepGraph()
        .with_node(pkg)
        .with_node(predicted)
        .with_edge(
            Edge(
                src=pkg.id,
                dst=predicted.id,
                relation=EdgeType.REQUIRES,
                origin="resolver",
            )
        )
    )
    fake_executor.responses = {
        "locate_file": make_result_fixture(
            stdout=json.dumps({"opencv-python": [_CV2_SO]})
        ),
        "ldd ": make_result_fixture(
            stdout=f"{_CV2_SO}:\n\tlibGL.so.1 => not found\n"
        ),
    }

    out = ldd_probe(graph, fake_executor)

    node = out.get(syslib_id("libGL.so.1"))
    assert node is not None
    assert node.discovered_by is DiscoveredBy.RESOLVER  # reconciled, not replaced
    assert node.chosen_fix == "apt:libgl1"  # filled in, was None
    assert node.fix_candidates == ("apt:libgl1",)
    assert node.data["resolution_status"] == "resolved"


def test_seed_and_ldd_reconcile_even_when_apt_resolution_unresolved(
    fake_executor, make_result_fixture
):
    """Worst-case opencv/libGL production bug: soname->apt resolution is ABSENT
    at ldd time (table miss AND apt-file unavailable/uninstalled). Before the
    canonical-soname fix, reconciliation was keyed by the (possibly
    unresolvable) apt name, so an apt-resolution failure meant the observation
    could never find the seed's node and a rival PROBE node was created
    instead — two nodes, neither one ever gets installed. Canonical identity is
    the soname, so reconciliation succeeds by string match alone, independent
    of apt resolution.
    """
    # A soname NOT in os_resolver.PROVIDER_TABLE, so seed's own apt lookup
    # already came up empty (mirrors a real curated-table gap) and ldd's
    # os_resolver.resolve will also fail (FakeExecutor has no apt-file
    # response registered -> rc=127 -> empty candidates).
    predicted = Node(
        id=syslib_id("libcustomthing.so.2"),
        type=NodeType.SYSTEM_LIB,
        name="libcustomthing.so.2",
        layer=Layer.SYSTEM,
        discovered_by=DiscoveredBy.RESOLVER,
        state=State.UNKNOWN,
        check_command="ldconfig -p | grep libcustomthing.so.2",
        fix_candidates=(),
        chosen_fix=None,
    )
    pkg = _package("somepkg", "1.0.0")
    graph = (
        DepGraph()
        .with_node(pkg)
        .with_node(predicted)
        .with_edge(
            Edge(
                src=pkg.id,
                dst=predicted.id,
                relation=EdgeType.REQUIRES,
                origin="resolver",
            )
        )
    )
    _so = "/usr/local/lib/python3.11/dist-packages/somepkg/foo.cpython-311-x86_64-linux-gnu.so"
    fake_executor.responses = {
        "locate_file": make_result_fixture(stdout=json.dumps({"somepkg": [_so]})),
        "ldd ": make_result_fixture(
            stdout=f"{_so}:\n\tlibcustomthing.so.2 => not found\n"
        ),
        # No "sysconfig"/"apt-file" response registered -> resolve_soname_apt
        # returns (None, "unresolved") too, at ldd time as well as at seed time.
    }

    out = ldd_probe(graph, fake_executor)

    syslibs = [n for n in out.nodes if n.id == syslib_id("libcustomthing.so.2")]
    assert len(syslibs) == 1  # single canonical node, no rival PROBE node
    node = syslibs[0]
    assert node.discovered_by is DiscoveredBy.RESOLVER  # reconciled, not replaced
    edges = [
        e for e in out.edges if e.dst == node.id and e.relation is EdgeType.REQUIRES
    ]
    assert len(edges) == 1

    # Discriminate the CANONICAL reconcile_predicted path from the pre-existing
    # soname-keyed fallback (existing = new.get(predicted_id) -> with_attempt):
    # both collapse to one node with discovered_by=RESOLVER preserved (the
    # fallback's lookup was ALREADY soname-keyed before Task 9's ldd_probe.py
    # fix), so node-count/discovered_by alone do not prove the canonical path
    # ran. reconcile_predicted uniquely sets ``evidence`` to the real observed
    # ldd line (the fallback's with_attempt never touches evidence, so it would
    # stay None — the seed fixture above never set it). Only the canonical
    # path leaves this non-None and referencing the soname.
    assert node.evidence and "libcustomthing.so.2" in node.evidence
    assert edges[0].src == pkg.id


def test_ldd_probe_unknown_soname_empty_fix_candidates(fake_executor, make_result_fixture):
    """Option A: an unknown soname (table miss + apt-file absent) yields a SystemLib
    node with EMPTY fix_candidates — the *need* is surfaced, the apt name is not.

    The FakeExecutor returns rc=127 for all unregistered commands, which causes
    os_resolver.resolve to return [] (no candidates), giving fix_candidates=().
    """
    _UNKNOWN_SO = "/usr/local/lib/python3.11/dist-packages/somepkg/foo.cpython-311-x86_64-linux-gnu.so"
    pkg = _package("somepkg", "1.0.0")
    graph = DepGraph().with_node(pkg)
    fake_executor.responses = {
        "locate_file": make_result_fixture(
            stdout=json.dumps({"somepkg": [_UNKNOWN_SO]})
        ),
        "ldd ": make_result_fixture(
            stdout=f"{_UNKNOWN_SO}:\n\tlibfoo.so.99 => not found\n"
        ),
        # No "sysconfig" or "apt-file" response registered:
        # FakeExecutor returns rc=127 -> resolve_soname_apt returns (None, "unresolved")
    }

    out = ldd_probe(graph, fake_executor)

    node = out.get(syslib_id("libfoo.so.99"))
    assert node is not None
    assert node.type is NodeType.SYSTEM_LIB
    # Option A: need surfaced but apt name unknown.
    assert node.fix_candidates == ()
    assert node.chosen_fix is None
    # The package→syslib edge is still created.
    assert any(
        e.src == pkg.id and e.dst == syslib_id("libfoo.so.99")
        for e in out.edges
        if e.relation is EdgeType.REQUIRES
    )


def test_ldd_probe_noop_when_so_map_command_fails(fake_executor):
    """ldd_probe returns the input graph unchanged when EXT_SO_MAP_CMD fails."""
    pkg = _package("opencv-python", "4.9.0.80")
    graph = DepGraph().with_node(pkg)
    # FakeExecutor default: rc=127 (no response registered -> command fails)

    out = ldd_probe(graph, fake_executor)

    assert out is graph  # same object; no mutation


def test_ldd_probe_returns_new_graph_original_unchanged(fake_executor, make_result_fixture):
    """Immutability: ldd_probe always returns a NEW graph; the original is untouched."""
    pkg = _package("opencv-python", "4.9.0.80")
    graph = DepGraph().with_node(pkg)
    fake_executor.responses = {
        "locate_file": make_result_fixture(
            stdout=json.dumps({"opencv-python": [_CV2_SO]})
        ),
        "ldd ": make_result_fixture(stdout=_OPENCV_LDD_OUTPUT),
    }

    out = ldd_probe(graph, fake_executor)

    assert out is not graph
    assert graph.get(syslib_id("libGL.so.1")) is None  # original untouched
    assert not any(n.type is NodeType.SYSTEM_LIB for n in graph.nodes)
