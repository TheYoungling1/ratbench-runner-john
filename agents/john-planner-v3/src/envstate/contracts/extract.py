"""Deterministic failure-signature -> (subject, blocker_kind) + atomic contract promotion (spec §6.4)."""
from __future__ import annotations
import re
from . import ids
from .nodes import Node

# (compiled pattern, blocker_kind, contract_kind)
_RULES = [
    (re.compile(r"No module named ['\"]([A-Za-z0-9_.]+)['\"]"), "module_not_found", "python_import"),
    (re.compile(r"ModuleNotFoundError:\s*([A-Za-z0-9_.]+)"), "module_not_found", "python_import"),
    (re.compile(r"([A-Za-z0-9_.+-]+)\s*:?\s*(?:command not found|executable not found)", re.I),
     "missing_binary", "binary"),
    (re.compile(r"(lib[A-Za-z0-9_.+-]+\.so[0-9.]*)\s*:\s*cannot open shared object", re.I),
     "missing_system_library", "system_library"),
    (re.compile(r"fatal error:\s*([A-Za-z0-9_./+-]+\.h)\b", re.I), "missing_system_library", "system_library"),
]


CONTRACT_LAYERS: dict[str, str] = {
    "python_import": "deps",
    "binary": "system",
    "system_library": "system",
}


def extract_blocker_match(line: str) -> tuple[str, str, str, str] | None:
    """Return (subject, blocker_kind, contract_kind, matched_text) for the
    first rule that fires, else None.  matched_text is group(0) — the
    verbatim portion of the line that triggered the rule."""
    for pat, bkind, ckind in _RULES:
        m = pat.search(line)
        if m:
            return m.group(1), bkind, ckind, m.group(0)
    return None


def extract_blocker_subject(signature: str) -> tuple[str | None, str]:
    if not signature:
        return None, "unknown"
    for pat, kind, _ in _RULES:
        m = pat.search(signature)
        if m:
            return m.group(1), kind
    return None, "unknown"


def _contract_kind_for(signature: str) -> tuple[str | None, str | None]:
    for pat, _kind, ckind in _RULES:
        m = pat.search(signature)
        if m:
            return m.group(1), ckind
    return None, None


def promote_atomic_contracts(graph: object, signatures: list[str]) -> list[Node]:
    out: list[Node] = []
    seen: set[str] = set()
    for sig in signatures:
        subject, ckind = _contract_kind_for(sig)
        if subject is None or ckind is None:
            continue
        cid = ids.contract_id(ckind, subject)
        if cid in seen or graph.has_node(cid):  # type: ignore[union-attr]
            continue
        seen.add(cid)
        layer = CONTRACT_LAYERS[ckind]
        out.append(Node(cid, "Contract", {"level": "atomic", "kind": ckind, "subject": subject,
            "layer": layer, "check": "", "source_refs": [f"signature:{sig[:60]}"],
            "evidence_refs": [], "description": f"{ckind} obligation: {subject}.", "metadata": {}}))
    return out
