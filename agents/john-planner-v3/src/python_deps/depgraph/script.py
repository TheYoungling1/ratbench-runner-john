"""Render annotated setup.sh from blocks and parse it back (design §3.2, §7). Pure."""
from __future__ import annotations

from python_deps.depgraph.block import Block

_PREAMBLE = "#!/usr/bin/env bash\nset -Eeuo pipefail\n"


def render_setup_sh(blocks: tuple[Block, ...]) -> str:
    parts = [_PREAMBLE]
    for b in blocks:
        parts.append(f"\n#@action id={b.block_id} wave={b.wave}")
        if b.target_node_ids:
            parts.append("#@targets " + " ".join(b.target_node_ids))
        if b.provider_ids:
            parts.append("#@provides " + " ".join(b.provider_ids))
        for chk in b.check_commands:
            parts.append(f"#@check {chk}")
        parts.extend(b.commands)
    return "\n".join(parts) + "\n"


def parse_setup_sh(text: str) -> tuple[Block, ...]:
    blocks: list[Block] = []
    cur: dict | None = None
    cmds: list[str] = []

    def _flush():
        if cur is not None:
            blocks.append(Block(
                block_id=cur["id"], wave=cur["wave"], commands=tuple(cmds),
                target_node_ids=tuple(cur.get("targets", ())),
                provider_ids=tuple(cur.get("provides", ())),
                check_commands=tuple(cur.get("checks", ())),
            ))

    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#@action"):
            _flush()
            cmds = []
            kv = dict(tok.split("=", 1) for tok in s[len("#@action"):].split() if "=" in tok)
            cur = {"id": kv.get("id", ""), "wave": kv.get("wave", ""),
                   "targets": (), "provides": (), "checks": []}
        elif s.startswith("#@targets") and cur is not None:
            cur["targets"] = tuple(s[len("#@targets"):].split())
        elif s.startswith("#@provides") and cur is not None:
            cur["provides"] = tuple(s[len("#@provides"):].split())
        elif s.startswith("#@check") and cur is not None:
            cur["checks"].append(s[len("#@check"):].strip())
        elif s.startswith("#!") or s.startswith("set -") or not s:
            continue
        elif cur is not None:
            cmds.append(line)
    _flush()
    return tuple(blocks)
