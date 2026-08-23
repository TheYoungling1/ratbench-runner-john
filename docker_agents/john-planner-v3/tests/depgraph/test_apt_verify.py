"""Bug #1 — release-aware apt-name reconciliation (apt_verify.py). No Docker."""

from __future__ import annotations

from python_deps.depgraph.apt_verify import (
    apt_name_installable,
    parse_showpkg_reverse_provides,
    reconcile_apt_names,
    resolve_installable_apt_name,
    resolve_virtual_provider,
    t64_variant,
)
from python_deps.depgraph.ids import syslib_id
from python_deps.depgraph.schema import (
    DepGraph,
    DiscoveredBy,
    Layer,
    Node,
    NodeType,
)


def _exec():
    from conftest import FakeExecutor, make_result  # type: ignore

    return FakeExecutor(
        responses={
            "apt-get update": make_result(stdout="Reading package lists..."),
            "apt-cache show libgl1": make_result(stdout="Package: libgl1\n"),
            # base name absent on the t64 image; t64 variant present
            "apt-cache show libglib2.0-0": make_result(returncode=100, stdout=""),
            "apt-cache show libglib2.0-0t64": make_result(
                stdout="Package: libglib2.0-0t64\nVersion: 2.80\n"
            ),
        },
        default=make_result(returncode=100, stdout=""),  # unknown -> not installable
    )


def _syslib(name: str, apt: str) -> Node:
    return Node(
        id=syslib_id(name), type=NodeType.SYSTEM_LIB, name=name,
        layer=Layer.SYSTEM, discovered_by=DiscoveredBy.RESOLVER,
        fix_candidates=(f"apt:{apt}",),
    )


def test_apt_name_installable():
    assert apt_name_installable("Package: libgl1\nVersion: 1\n") is True
    assert apt_name_installable("") is False
    assert apt_name_installable("N: Unable to locate package libglib2.0-0") is False


def test_t64_variant():
    assert t64_variant("libglib2.0-0") == "libglib2.0-0t64"
    assert t64_variant("libglib2.0-0t64") is None


def test_resolve_keeps_valid_name():
    assert resolve_installable_apt_name("libgl1", _exec()) == "libgl1"


def test_resolve_remaps_to_t64_when_base_absent():
    assert resolve_installable_apt_name("libglib2.0-0", _exec()) == "libglib2.0-0t64"


def test_resolve_unchanged_when_nothing_installable():
    from conftest import FakeExecutor, make_result  # type: ignore

    ex = FakeExecutor(default=make_result(returncode=100, stdout=""))
    assert resolve_installable_apt_name("libwhatever", ex) == "libwhatever"


def test_reconcile_remaps_seed_node_name_and_fix():
    g = DepGraph().with_node(_syslib("libglib2.0-0", "libglib2.0-0"))
    out = reconcile_apt_names(g, _exec())
    node = out.get(syslib_id("libglib2.0-0"))  # id is stable
    assert node.name == "libglib2.0-0t64"  # named by apt pkg -> name updated
    assert node.fix_candidates == ("apt:libglib2.0-0t64",)


def test_reconcile_remaps_seed_node_check_command():
    """Regression: a seed node's `dpkg -s <name>` check must remap with the name,
    else certify checks the stale name and reports MISSING forever once the
    correct (t64) package is installed."""
    seed = Node(
        id=syslib_id("libglib2.0-0"), type=NodeType.SYSTEM_LIB,
        name="libglib2.0-0", layer=Layer.SYSTEM, discovered_by=DiscoveredBy.RESOLVER,
        fix_candidates=("apt:libglib2.0-0",),
        check_command="dpkg -s libglib2.0-0",
    )
    out = reconcile_apt_names(DepGraph().with_node(seed), _exec())
    node = out.get(syslib_id("libglib2.0-0"))
    assert node.check_command == "dpkg -s libglib2.0-0t64"


def test_reconcile_does_not_touch_soname_ldconfig_check():
    """A soname node's ldconfig check is release-independent — keep it as-is even
    while its apt fix-candidate is remapped."""
    node_in = Node(
        id=syslib_id("libgthread-2.0.so.0"), type=NodeType.SYSTEM_LIB,
        name="libgthread-2.0.so.0", layer=Layer.SYSTEM,
        discovered_by=DiscoveredBy.PROBE, fix_candidates=("apt:libglib2.0-0",),
        check_command="ldconfig -p | grep libgthread-2.0.so.0",
    )
    out = reconcile_apt_names(DepGraph().with_node(node_in), _exec())
    node = out.get(syslib_id("libgthread-2.0.so.0"))
    assert node.check_command == "ldconfig -p | grep libgthread-2.0.so.0"  # untouched
    assert node.fix_candidates == ("apt:libglib2.0-0t64",)  # fix still corrected


def test_reconcile_leaves_valid_name_untouched():
    g = DepGraph().with_node(_syslib("libgl1", "libgl1"))
    out = reconcile_apt_names(g, _exec())
    node = out.get(syslib_id("libgl1"))
    assert node.name == "libgl1"
    assert node.fix_candidates == ("apt:libgl1",)


def test_reconcile_soname_node_keeps_identity_but_fixes_apt():
    # a soname-named node (name != apt pkg): only the fix-candidate is remapped.
    g = DepGraph().with_node(_syslib("libgthread-2.0.so.0", "libglib2.0-0"))
    out = reconcile_apt_names(g, _exec())
    node = out.get(syslib_id("libgthread-2.0.so.0"))
    assert node.name == "libgthread-2.0.so.0"  # identity preserved
    assert node.fix_candidates == ("apt:libglib2.0-0t64",)  # fix corrected


def test_reconcile_noop_without_apt_nodes_skips_apt_update():
    from conftest import FakeExecutor  # type: ignore

    ex = FakeExecutor()
    # a graph with no apt fix-candidates
    g = DepGraph().with_node(
        Node(id="import:os", type=NodeType.IMPORT, name="os",
             layer=Layer.NAMING, discovered_by=DiscoveredBy.STATIC_SCAN)
    )
    out = reconcile_apt_names(g, ex)
    assert out is g
    assert "apt-get update" not in ex.calls  # paid nothing


# ---------------------------------------------------------------------------
# showpkg / resolve_virtual_provider tests
# ---------------------------------------------------------------------------

# Realistic apt-cache showpkg output for a virtual package with TWO version
# lines for the SAME real provider (the common t64 rename scenario).
_SHOWPKG_LIBGLIB_TWO_VERSIONS = """\
Package: libglib2.0-0
Versions:

Reverse Depends:

Dependencies:

Provides:

Reverse Provides:
libglib2.0-0t64 2.80.0-6
libglib2.0-0t64 2.74.6-2+deb12u4
"""

# showpkg output with NO Reverse Provides section (installed real package).
_SHOWPKG_NO_REVERSE_PROVIDES = """\
Package: libgl1
Versions:

Reverse Depends:

Dependencies:

Provides:

"""

# showpkg output with MULTIPLE DISTINCT providers (ambiguous — None expected).
_SHOWPKG_MULTIPLE_DISTINCT = """\
Package: libfoo
Versions:

Reverse Provides:
libfoo-real 1.0
libfoo-other 1.0
"""

# showpkg output where a real section header FOLLOWS the Reverse Provides data —
# exercises the section-break stop condition (review MEDIUM-1).
_SHOWPKG_SECTION_AFTER_PROVIDES = """\
Package: libglib2.0-0
Versions:

Reverse Provides:
libglib2.0-0t64 2.80.0-6
Reverse Depends:
something-else 1.0
"""

# showpkg resolves to a provider that apt-cache show then can't verify as
# installable — must fall through to the t64 suffix (review MEDIUM-2).
_SHOWPKG_PROVIDER_NOT_INSTALLABLE = """\
Package: libglib2.0-0
Versions:

Reverse Provides:
ghostprovider 2.80.0-6
"""


def test_parse_showpkg_reverse_provides_two_version_lines():
    """Two lines for the same provider are both returned (dedup is caller's job)."""
    providers = parse_showpkg_reverse_provides(_SHOWPKG_LIBGLIB_TWO_VERSIONS)
    assert providers == ["libglib2.0-0t64", "libglib2.0-0t64"]


def test_parse_showpkg_reverse_provides_empty_section():
    """No 'Reverse Provides:' section returns empty list."""
    providers = parse_showpkg_reverse_provides(_SHOWPKG_NO_REVERSE_PROVIDES)
    assert providers == []


def test_parse_showpkg_reverse_provides_empty_string():
    assert parse_showpkg_reverse_provides("") == []


def test_resolve_virtual_provider_deduplicates_same_provider():
    """Multiple version lines for the same provider dedup to one -> returned."""
    from conftest import FakeExecutor, make_result  # type: ignore

    ex = FakeExecutor(
        responses={
            "apt-cache showpkg libglib2.0-0": make_result(
                stdout=_SHOWPKG_LIBGLIB_TWO_VERSIONS
            ),
        }
    )
    assert resolve_virtual_provider("libglib2.0-0", ex) == "libglib2.0-0t64"


def test_resolve_virtual_provider_no_section_returns_none():
    """No Reverse Provides section -> None."""
    from conftest import FakeExecutor, make_result  # type: ignore

    ex = FakeExecutor(
        responses={
            "apt-cache showpkg libgl1": make_result(stdout=_SHOWPKG_NO_REVERSE_PROVIDES),
        }
    )
    assert resolve_virtual_provider("libgl1", ex) is None


def test_resolve_virtual_provider_multiple_distinct_returns_none():
    """More than one distinct provider -> None (ambiguous)."""
    from conftest import FakeExecutor, make_result  # type: ignore

    ex = FakeExecutor(
        responses={
            "apt-cache showpkg libfoo": make_result(stdout=_SHOWPKG_MULTIPLE_DISTINCT),
        }
    )
    assert resolve_virtual_provider("libfoo", ex) is None


def test_resolve_installable_uses_showpkg_remaps_libglib():
    """showpkg path remaps libglib2.0-0 -> libglib2.0-0t64 authoritatively.

    showpkg exits 0 with Reverse Provides content; the returned provider is
    verified installable before being returned.  The t64-suffix path is NOT the
    resolution mechanism here (the showpkg path fires first).
    """
    from conftest import FakeExecutor, make_result  # type: ignore

    ex = FakeExecutor(
        responses={
            "apt-cache show libglib2.0-0": make_result(stdout=""),  # absent
            "apt-cache showpkg libglib2.0-0": make_result(
                stdout=_SHOWPKG_LIBGLIB_TWO_VERSIONS
            ),
            "apt-cache show libglib2.0-0t64": make_result(
                stdout="Package: libglib2.0-0t64\nVersion: 2.80\n"
            ),
        }
    )
    result = resolve_installable_apt_name("libglib2.0-0", ex)
    assert result == "libglib2.0-0t64"
    # showpkg was actually invoked with the exact candidate argument (not bypassed
    # in favour of the t64 suffix alone) — review LOW-2.
    assert any(c == "apt-cache showpkg libglib2.0-0" for c in ex.calls)


def test_resolve_showpkg_no_reverse_provides_falls_through_to_t64():
    """When showpkg returns no Reverse Provides, fall through to t64 suffix."""
    from conftest import FakeExecutor, make_result  # type: ignore

    ex = FakeExecutor(
        responses={
            "apt-cache show libglib2.0-0": make_result(stdout=""),  # absent
            "apt-cache showpkg libglib2.0-0": make_result(
                stdout=_SHOWPKG_NO_REVERSE_PROVIDES
            ),
            "apt-cache show libglib2.0-0t64": make_result(
                stdout="Package: libglib2.0-0t64\nVersion: 2.80\n"
            ),
        }
    )
    assert resolve_installable_apt_name("libglib2.0-0", ex) == "libglib2.0-0t64"


def test_resolve_valid_installable_name_skips_showpkg():
    """A directly installable candidate is returned without calling showpkg."""
    from conftest import FakeExecutor, make_result  # type: ignore

    ex = FakeExecutor(
        responses={
            "apt-cache show libgl1": make_result(stdout="Package: libgl1\n"),
        }
    )
    assert resolve_installable_apt_name("libgl1", ex) == "libgl1"
    assert not any("showpkg" in c for c in ex.calls)


def test_parse_showpkg_stops_at_next_section_header():
    """The parser must stop at a real section header that follows the provider
    lines, so it does not over-collect names from the next section (review
    MEDIUM-1 — the section-break stop condition)."""
    providers = parse_showpkg_reverse_provides(_SHOWPKG_SECTION_AFTER_PROVIDES)
    assert providers == ["libglib2.0-0t64"]
    assert "something-else" not in providers


def test_resolve_showpkg_provider_not_installable_falls_through_to_t64():
    """showpkg yields a provider that apt-cache show can't verify -> fall through
    to the t64 suffix heuristic (review MEDIUM-2 — the provider-verify-False
    branch of resolve_installable_apt_name)."""
    from conftest import FakeExecutor, make_result  # type: ignore

    ex = FakeExecutor(
        responses={
            "apt-cache show libglib2.0-0": make_result(stdout=""),  # candidate absent
            "apt-cache showpkg libglib2.0-0": make_result(
                stdout=_SHOWPKG_PROVIDER_NOT_INSTALLABLE
            ),
            # ghostprovider is NOT installable (no apt-cache show response -> default);
            # the t64 variant IS, so it must win.
            "apt-cache show libglib2.0-0t64": make_result(
                stdout="Package: libglib2.0-0t64\nVersion: 2.80\n"
            ),
        },
        default=make_result(returncode=100, stdout=""),
    )
    assert resolve_installable_apt_name("libglib2.0-0", ex) == "libglib2.0-0t64"
    # the showpkg provider was looked up and rejected before t64 won.
    assert any(c == "apt-cache show ghostprovider" for c in ex.calls)
