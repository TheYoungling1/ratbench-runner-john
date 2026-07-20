"""P1.1 — DiscoveredBy.AUDIT provenance stamp for repair-sourced roots.

AUDIT marks an under-declared root added by the (later) Phase-A repair overlay:
a package discovered by auditing imports against the installed environment, kept
distinct from a manifest declaration (RESOLVER) and a static-scan import
(STATIC_SCAN). This slice only asserts the enum member exists and serializes.
"""

from __future__ import annotations

from python_deps.depgraph.schema import (
    DiscoveredBy,
    Layer,
    Node,
    NodeType,
)


def test_audit_member_value():
    assert DiscoveredBy.AUDIT.value == "audit"


def test_audit_is_distinct_from_every_other_member():
    others = [m for m in DiscoveredBy if m is not DiscoveredBy.AUDIT]
    assert DiscoveredBy.AUDIT not in others
    # distinct .value too (no accidental collision with an existing stamp)
    assert DiscoveredBy.AUDIT.value not in {m.value for m in others}


def test_node_with_audit_provenance_serializes():
    node = Node(
        id="pkg:requests",
        type=NodeType.PACKAGE,
        name="requests",
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.AUDIT,
    )
    assert node.discovered_by is DiscoveredBy.AUDIT
    assert node.to_dict()["discovered_by"] == "audit"
