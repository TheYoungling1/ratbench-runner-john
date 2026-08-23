"""Typed evidence ledger (design §3.3). Orthogonal to ledger.ActionEvent. Pure."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

_CONTAINER_KINDS = ("canonical", "lab", "fresh_replay")


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    container_kind: str
    command: str
    rc: int
    output_excerpt: str
    cycle: int
    block_id: str | None = None
    node_id: str | None = None
    gate_id: str | None = None

    def __post_init__(self):
        if self.container_kind not in _CONTAINER_KINDS:
            raise ValueError(f"container_kind must be one of {_CONTAINER_KINDS}")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Evidence":
        return cls(**d)


@dataclass(frozen=True)
class EvidenceBundle:
    items: tuple[Evidence, ...] = ()

    def with_item(self, ev: Evidence) -> "EvidenceBundle":
        return EvidenceBundle(items=self.items + (ev,))


def write_jsonl(bundle: EvidenceBundle, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for ev in bundle.items:
            fh.write(json.dumps(ev.to_dict()) + "\n")
