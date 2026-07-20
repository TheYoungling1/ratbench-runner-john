"""Task 2.2 — the Phase-B wheel-preflight pre-pass is WIRED into build.py.

``_python_package_obligations`` (the Python Phase-1 seam) must run
``wheel_preflight_probe`` at the aux-once stage (next to ``seed_wheel_oracle_prior``),
so a wheel-classified ``Package`` node's bundled ``DT_NEEDED`` sonames become
proactive ``RESOLVER``/``UNKNOWN`` ``SystemLib`` priors BEFORE the reactive
``ldd_probe`` (Phase 2) runs. The seam function returns before ``native_obligations``
(ldd) and ``certify_all``, so a node it exposes as ``RESOLVER``/``UNKNOWN`` here is
proof the prior was seeded pre-install (not created by ldd, which would stamp
``PROBE``/``MISSING``).

The two unit tests are Docker-free: ``wheel_preflight.download_target_wheel`` and
``wheel_preflight.inspect_wheel_sonames`` are monkeypatched to a fake wheel path +
a known soname set. Resolution is driven by a ``SequencedFakeExecutor`` whose
``uv lock`` response is a real universal ``uv.lock`` classifying ``opencv-python``
as a platform-matching wheel (``build_from_source is False``) — the exact stamp
``wheel_preflight_probe`` filters on.
"""

from __future__ import annotations

from python_deps.depgraph import wheel_preflight
from python_deps.depgraph.build import _python_package_obligations
from python_deps.depgraph.executor import CommandResult
from python_deps.depgraph.ids import syslib_id
from python_deps.depgraph.schema import DiscoveredBy, NodeType, State


def _r(returncode: int = 0, stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(
        command="", returncode=returncode, stdout=stdout, stderr=stderr
    )


# A universal uv.lock: opencv-python ships a manylinux x86_64 wheel and no sdist,
# so native_risk_from_lock stamps build_from_source=False (a wheel) for the
# x86_64 target below — the classification wheel_preflight_probe inspects.
_WHEEL_LOCK = """\
version = 1
requires-python = ">=3.11"

[[package]]
name = "wheel-preflight-prepass-root"
version = "0.0.0"
source = { virtual = "." }
dependencies = [
    { name = "opencv-python" },
]

[[package]]
name = "opencv-python"
version = "4.9.0.80"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "https://files.pythonhosted.org/x/opencv_python-4.9.0.80-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", hash = "sha256:cv-wheel" },
]
"""


def _wheel_repo(tmp_path) -> str:
    (tmp_path / "app.py").write_text("import cv2\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="fx"\nversion="0"\ndependencies=["opencv-python"]\n'
    )
    return str(tmp_path)


def _seam(tmp_path, ex, monkeypatch, sonames):
    """Run the Phase-1 seam with download/inspect monkeypatched to ``sonames``."""
    monkeypatch.setattr(
        wheel_preflight, "download_target_wheel", lambda *a, **k: "/tmp/fake.whl"
    )
    monkeypatch.setattr(
        wheel_preflight, "inspect_wheel_sonames", lambda p: set(sonames)
    )
    # opencv-python provides cv2 -> coverage satisfied, fixpoint converges cleanly.
    provider = lambda dist: {"cv2"} if "opencv" in dist.lower() else None  # noqa: E731
    graph, _roots, _env, _newer = _python_package_obligations(
        _wheel_repo(tmp_path),
        ex,
        host_executor=ex,
        target_python="3.11",
        target_platform="x86_64-manylinux_2_28",
        record_provider=provider,
    )
    return graph


def test_wheel_preflight_prior_seeds_unknown_syslib(tmp_path, monkeypatch):
    """A wheel-classified package's DT_NEEDED soname is seeded as a
    RESOLVER/UNKNOWN SystemLib prior by the Phase-B pre-pass, BEFORE ldd runs."""
    from conftest import SequencedFakeExecutor  # type: ignore

    ex = SequencedFakeExecutor(
        responses={"uv lock": [_r(0, stdout=_WHEEL_LOCK)]},
        default=_r(0),
    )
    graph = _seam(tmp_path, ex, monkeypatch, {"libGL.so.1"})

    node = graph.get(syslib_id("libGL.so.1"))
    assert node is not None, "wheel_preflight pre-pass did not seed the soname prior"
    assert node.type is NodeType.SYSTEM_LIB
    # Seeded by the RESOLVER pre-pass, NOT by ldd (which would be PROBE/MISSING).
    assert node.discovered_by is DiscoveredBy.RESOLVER
    assert node.state is State.UNKNOWN
    # Table hit fills the apt fix without any container apt-file lookup.
    assert node.chosen_fix == "apt:libgl1"
    # A requires edge from the owning wheel package to the prior.
    from python_deps.depgraph.ids import package_id

    assert any(
        e.src == package_id("opencv-python", "4.9.0.80")
        and e.dst == syslib_id("libGL.so.1")
        and e.origin == "resolver"
        for e in graph.edges
    )


def test_wheel_preflight_prepass_is_additive_noop_for_fallback_closure(
    tmp_path, monkeypatch
):
    """Additive-safety guardrail: when resolution degrades to the `uv pip compile`
    fallback (no lock -> build_from_source is None), NO package is wheel-classified,
    so the pre-pass never attempts a download and seeds zero SystemLib nodes —
    byte-identical to the pre-wiring graph. This is the property that keeps every
    existing FakeExecutor build test green (they all take the fallback path)."""
    from conftest import SequencedFakeExecutor  # type: ignore

    downloads = {"n": 0}

    def _never(*a, **k):
        downloads["n"] += 1
        return "/tmp/should-not-happen.whl"

    monkeypatch.setattr(wheel_preflight, "download_target_wheel", _never)
    monkeypatch.setattr(wheel_preflight, "inspect_wheel_sonames", lambda p: {"libGL.so.1"})

    # uv lock FAILS -> resolve falls back to `uv pip compile` text (no
    # build_from_source signal -> None) -> nothing is a wheel to inspect.
    _closure = (
        "numpy==1.26.4\n    # via opencv-python\n"
        "opencv-python==4.9.0.80\n    # via -r -\n"
    )
    ex = SequencedFakeExecutor(
        responses={
            "uv lock": [_r(1, stderr="lock unavailable")],
            "uv pip compile": [_r(0, stdout=_closure)],
        },
        default=_r(0),
    )
    provider = lambda dist: {"cv2"} if "opencv" in dist.lower() else None  # noqa: E731
    graph, _roots, _env, _newer = _python_package_obligations(
        _wheel_repo(tmp_path),
        ex,
        host_executor=ex,
        target_python="3.11",
        target_platform="x86_64-manylinux_2_28",
        record_provider=provider,
    )

    assert downloads["n"] == 0, "pre-pass attempted a download for a non-wheel closure"
    assert [n for n in graph.nodes if n.type is NodeType.SYSTEM_LIB] == []


# A universal uv.lock: psycopg2 ships ONLY an sdist (no wheel), so
# native_risk_from_lock stamps build_from_source=True — the source-built
# classification seed_build_deps seeds a SPECIFIC -dev prior for (Task 3.2).
_SDIST_LOCK = """\
version = 1
requires-python = ">=3.11"

[[package]]
name = "sdist-prepass-root"
version = "0.0.0"
source = { virtual = "." }
dependencies = [
    { name = "psycopg2" },
]

[[package]]
name = "psycopg2"
version = "2.9.9"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://files.pythonhosted.org/x/psycopg2-2.9.9.tar.gz", hash = "sha256:psql-sdist" }
"""


def _sdist_repo(tmp_path) -> str:
    (tmp_path / "app.py").write_text("import psycopg2\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="fx"\nversion="0"\ndependencies=["psycopg2"]\n'
    )
    return str(tmp_path)


def test_sdist_gets_specific_dev_prior_and_generic_floor(tmp_path, monkeypatch):
    """Task 3.2 — an sdist package (``build_from_source=True``) gets its SPECIFIC
    ``-dev`` prior from ``seed_build_deps`` (psycopg2 -> ``binary:pg_config``,
    resolved to ``apt:libpq-dev`` via the curated table) AND still keeps the
    generic ``tool:build-essential`` FLOOR from ``seed_wheel_oracle_prior``. The
    two are DISTINCT nodes — the specific prior unions with the floor, neither
    erasing the other. Docker-free: the Debian/PEP725 paths degrade to empty
    under the FakeExecutor, so only the curated ``pg_config`` capability + the
    B3 baseline ``binary:pkg-config`` are added beyond the floor."""
    from conftest import SequencedFakeExecutor  # type: ignore

    from python_deps.depgraph.ids import binary_id, package_id, tool_id

    # An sdist closure never triggers a wheel download; if the pre-pass did, this
    # fake path would surface (guard: it must not be inspected for sonames).
    monkeypatch.setattr(
        wheel_preflight, "download_target_wheel", lambda *a, **k: "/tmp/nope.whl"
    )
    monkeypatch.setattr(wheel_preflight, "inspect_wheel_sonames", lambda p: set())

    ex = SequencedFakeExecutor(
        responses={"uv lock": [_r(0, stdout=_SDIST_LOCK)]},
        default=_r(0),
    )
    provider = lambda dist: {"psycopg2"} if "psycopg2" in dist.lower() else None  # noqa: E731
    graph, _roots, _env, _newer = _python_package_obligations(
        _sdist_repo(tmp_path),
        ex,
        host_executor=ex,
        target_python="3.11",
        target_platform="x86_64-manylinux_2_28",
        record_provider=provider,
    )

    pkg = package_id("psycopg2", "2.9.9")

    # SPECIFIC prior: psycopg2's pg_config capability, table-resolved to libpq-dev.
    specific = graph.get(binary_id("pg_config"))
    assert specific is not None, "seed_build_deps did not seed the specific -dev prior"
    assert specific.type is NodeType.TOOL
    # Seeded by the RESOLVER pre-pass, never SATISFIED-at-seed.
    assert specific.discovered_by is DiscoveredBy.RESOLVER
    assert specific.state is State.UNKNOWN
    assert specific.chosen_fix == "apt:libpq-dev"
    assert any(
        e.src == pkg and e.dst == binary_id("pg_config") and e.origin == "resolver"
        for e in graph.edges
    ), "no requires edge psycopg2 -> pg_config prior"

    # GENERIC FLOOR still present: build-essential from seed_wheel_oracle_prior.
    floor = graph.get(tool_id("build-essential"))
    assert floor is not None, "generic build-essential floor was erased"
    assert floor.type is NodeType.TOOL
    assert any(
        e.src == pkg and e.dst == tool_id("build-essential") and e.origin == "resolver"
        for e in graph.edges
    ), "no requires edge psycopg2 -> build-essential floor"
