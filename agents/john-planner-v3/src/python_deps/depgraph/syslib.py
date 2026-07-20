"""Single definition of the ``SystemLib`` graph-node shape.

Both the post-install ``ldd_probe`` stage and the pre-install
``wheel_preflight`` stage create ``SystemLib`` nodes; keying them by canonical
soname is what lets a pre-install prediction and a post-install observation
collapse onto ONE node. Centralizing the node shape here keeps the two
producers from drifting (e.g. on the ``check_command`` format or the
apt->fix mapping).
"""

from __future__ import annotations

from python_deps.depgraph.ids import syslib_id
from python_deps.depgraph.schema import DiscoveredBy, Layer, Node, NodeType, State


def make_syslib_node(
    soname: str,
    *,
    discovered_by: DiscoveredBy,
    state: State,
    apt: str | None = None,
    evidence: str | None = None,
    provenance: str | None = None,
) -> Node:
    """Build a ``SystemLib`` node keyed by canonical soname.

    ``apt`` (when given) fills both ``fix_candidates`` and ``chosen_fix`` as
    ``apt:<name>``; a ``None`` apt leaves them empty (need surfaced, apt name
    unknown). The check is always ``ldconfig -p | grep <soname>``. Callers set
    ``discovered_by``/``state`` (RESOLVER/UNKNOWN for a pre-install prior,
    PROBE/MISSING for an ldd observation) and append any ``Attempt`` themselves.
    """
    return Node(
        id=syslib_id(soname),
        type=NodeType.SYSTEM_LIB,
        name=soname,
        layer=Layer.SYSTEM,
        discovered_by=discovered_by,
        state=state,
        check_command=f"ldconfig -p | grep {soname}",
        evidence=evidence,
        fix_candidates=(f"apt:{apt}",) if apt else (),
        chosen_fix=f"apt:{apt}" if apt else None,
        provenance=provenance,
    )
