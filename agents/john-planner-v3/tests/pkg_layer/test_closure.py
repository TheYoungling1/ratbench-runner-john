"""TDD tests for the pkg_layer Closure plane (Task 4).

``build_closure`` wraps a resolve SOURCE into ``ClosurePkg`` nodes tagged
DIRECT/TRANSITIVE, keeping the resolve source itself pluggable via the
``ResolveSource`` protocol so a later swap ("fresh uv lock" vs "adopt
committed lock") only touches the injected source, never ``build_closure``.

Every test here drives a FAKE ``ResolveSource`` returning canned
``(name, version, is_direct)`` rows -- no live ``uv``, no Docker, no
subprocess. ``UvResolveSource`` (the real host-uv adapter) is imported only
to confirm it exists and is structurally a resolve source; its ``.resolve``
is never called in this suite (that would shell out to ``uv``).
"""
from __future__ import annotations

from python_deps.pkg_layer.closure import ResolveSource, UvResolveSource, build_closure
from python_deps.pkg_layer.planes import ClosurePkg, Tier


class _FakeResolveSource:
    """A canned ``ResolveSource`` returning fixed ``(name, version, is_direct)`` rows."""

    def __init__(self, rows: tuple[tuple[str, str | None, bool], ...]) -> None:
        self._rows = rows

    def resolve(self, roots: tuple[str, ...]) -> tuple[tuple[str, str | None, bool], ...]:
        return self._rows


def test_direct_row_tagged_tier_direct():
    source = _FakeResolveSource((("requests", "2.31.0", True),))

    closure = build_closure(("requests",), source)

    assert closure == (ClosurePkg(name="requests", version="2.31.0", tier=Tier.DIRECT),)


def test_transitive_row_tagged_tier_transitive():
    source = _FakeResolveSource((("urllib3", "2.0.0", False),))

    closure = build_closure(("requests",), source)

    assert closure == (ClosurePkg(name="urllib3", version="2.0.0", tier=Tier.TRANSITIVE),)


def test_mixed_direct_and_transitive_rows():
    source = _FakeResolveSource(
        (
            ("requests", "2.31.0", True),
            ("urllib3", "2.0.0", False),
            ("certifi", "2024.2.2", False),
        )
    )

    closure = build_closure(("requests",), source)

    by_name = {pkg.name: pkg for pkg in closure}
    assert by_name["requests"].tier is Tier.DIRECT
    assert by_name["urllib3"].tier is Tier.TRANSITIVE
    assert by_name["certifi"].tier is Tier.TRANSITIVE


def test_canon_dedup_collapses_case_and_separator_variants():
    # "Flask" and "flask" share the same PEP 503 canon key; the source
    # surfacing both (e.g. once as the direct root, once re-surfaced as a
    # transitive dependency of something else) must collapse to ONE
    # ClosurePkg -- first-seen wins, so the direct tagging survives.
    source = _FakeResolveSource(
        (
            ("Flask", "3.0.0", True),
            ("flask", "3.0.0", False),
        )
    )

    closure = build_closure(("Flask",), source)

    assert len(closure) == 1
    assert closure[0].name == "Flask"
    assert closure[0].tier is Tier.DIRECT


def test_canon_dedup_keeps_first_seen_even_when_direct_seen_second():
    # First-seen wins regardless of which row is direct -- build_closure does
    # not "upgrade" tier on a later duplicate.
    source = _FakeResolveSource(
        (
            ("flask", "3.0.0", False),
            ("Flask", "3.0.0", True),
        )
    )

    closure = build_closure(("flask",), source)

    assert len(closure) == 1
    assert closure[0].name == "flask"
    assert closure[0].tier is Tier.TRANSITIVE


def test_empty_rows_returns_empty_closure():
    source = _FakeResolveSource(())

    closure = build_closure((), source)

    assert closure == ()


def test_result_is_tuple_of_closure_pkg():
    source = _FakeResolveSource((("requests", "2.31.0", True),))

    closure = build_closure(("requests",), source)

    assert isinstance(closure, tuple)
    assert all(isinstance(pkg, ClosurePkg) for pkg in closure)


def test_version_none_is_preserved():
    # A row with no pinned version still produces a ClosurePkg with
    # version=None -- build_closure does not invent a version.
    source = _FakeResolveSource((("mystery-pkg", None, True),))

    closure = build_closure(("mystery-pkg",), source)

    assert closure == (ClosurePkg(name="mystery-pkg", version=None, tier=Tier.DIRECT),)


def test_uv_resolve_source_satisfies_resolve_source_protocol():
    # UvResolveSource is the ONLY place that shells out to uv; it is NOT run
    # here (that would require a host uv binary + a real/fake Executor).
    # This only confirms it is importable and structurally a ResolveSource
    # (has a `.resolve` method) without ever calling it.
    assert hasattr(UvResolveSource, "resolve")
    assert callable(UvResolveSource.resolve)
    assert isinstance(_FakeResolveSource(()), ResolveSource)
