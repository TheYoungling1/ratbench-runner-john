# tests/depgraph/test_evidence_bundle.py
import json
from python_deps.depgraph.evidence_log import Evidence, EvidenceBundle, write_jsonl


def _ev(i=0):
    return Evidence(evidence_id=f"ev.{i}", container_kind="canonical",
                    command="apt-get install -y libpq-dev", rc=0,
                    output_excerpt="ok", cycle=1, block_id="system.libpq",
                    node_id="syslib:libpq")


def test_evidence_roundtrip():
    ev = _ev()
    assert Evidence.from_dict(ev.to_dict()) == ev


def test_bundle_immutable_append():
    b0 = EvidenceBundle()
    b1 = b0.with_item(_ev(1))
    assert b0.items == () and len(b1.items) == 1     # b0 unchanged


def test_write_jsonl_lines(tmp_path):
    b = EvidenceBundle().with_item(_ev(1)).with_item(_ev(2))
    p = tmp_path / "evidence.jsonl"
    write_jsonl(b, str(p))
    lines = p.read_text().splitlines()
    assert len(lines) == 2 and all(json.loads(ln)["container_kind"] == "canonical" for ln in lines)
