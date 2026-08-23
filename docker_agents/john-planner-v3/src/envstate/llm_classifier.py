"""LLM error-classifier tier for the residual handler (spec §6).

Injectable, network-free factory. `make_llm_classifier(complete_fn)` returns a
callable with the SAME shape as `runtime_classify.classify_observation`
((command, output) -> Discovery | None), so it drops into the `classifiers`
sequence of `ingest_runtime_failures`. The pure python_deps modules stay
LLM-free; this src.envstate module is the allowed bridge.

Invariants:
  * temperature 0 (the orchestrator's complete_fn sets it);
  * every env Discovery carries a real check_command (SERVICE is advisory,
    check=None) — host certifies, so a hallucinated node is inert;
  * REPO_BUG/FLAKY/UNKNOWN -> None (honest give-up; no graph pollution).
"""
from __future__ import annotations

from collections.abc import Callable

from python_deps.depgraph.runtime_classify import Discovery
from python_deps.depgraph.schema import Layer, NodeType
from src.envstate.jsonutil import extract_json_object

# kind -> (node type, install layer). Layer is derived from kind, not trusted
# from the model, for robustness.
_KIND_MAP: dict[str, tuple[NodeType, Layer]] = {
    "PACKAGE": (NodeType.PACKAGE, Layer.PIP),
    "SYSTEM_LIB": (NodeType.SYSTEM_LIB, Layer.SYSTEM),
    "TOOL": (NodeType.TOOL, Layer.TOOLCHAIN),
    "CONFIG": (NodeType.CONFIG, Layer.CONFIG),
    "SERVICE": (NodeType.SERVICE, Layer.SERVICES),
}

_SYSTEM_PROMPT = (
    "You classify a single failed-command error into ONE environment obligation, "
    "or decide it is not one. Respond with ONLY a JSON object with keys: "
    "kind (PACKAGE|SYSTEM_LIB|TOOL|CONFIG|SERVICE|REPO_BUG|FLAKY|UNKNOWN), name, "
    "layer (pip|apt|none), install_hint, check_command, requires_of, confidence, rationale.\n"
    "Every environment obligation MUST include a check_command that proves its presence "
    "(an import, `command -v`, `ldconfig -p`, `dpkg -s`). If you cannot give a real check, "
    "you do not know — classify UNKNOWN.\n"
    "If the error is NOT an environment/dependency gap (assertion failure, logic bug, "
    "network timeout), classify REPO_BUG or FLAKY. Do NOT invent a package to explain it.\n"
    "Set requires_of to the node id of the package this is a dependency OF when the error "
    "is scoped to one package (e.g. pkg:psycopg2), else leave it empty."
)


def _build_messages(command: str, output: str) -> list[dict]:
    tail = "\n".join((output or "").splitlines()[-40:])    # error is at the tail
    user = (
        f"COMMAND:\n{command}\n\nERROR (tail):\n{tail}\n\n"
        "Classify per the schema. Respond with ONLY the JSON object."
    )
    return [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": user}]


def make_llm_classifier(
    complete_fn: Callable[[list[dict]], str],
    *,
    note_out_of_scope: Callable[[str, str], None] | None = None,
) -> Callable[[str, str], Discovery | None]:
    """Build a (command, output) -> Discovery | None classifier."""

    def _classify(command: str, output: str) -> Discovery | None:
        try:
            text = complete_fn(_build_messages(command, output))
            obj = extract_json_object(text)
        except Exception:                       # never break the run (spec §11)
            return None
        if not isinstance(obj, dict):
            return None

        kind = str(obj.get("kind", "")).strip().upper()
        rationale = str(obj.get("rationale", "")).strip()
        if kind not in _KIND_MAP:               # REPO_BUG / FLAKY / UNKNOWN / junk
            if note_out_of_scope is not None:
                note_out_of_scope(command, rationale or f"non-env: {kind or 'unparseable'}")
            return None

        node_type, layer = _KIND_MAP[kind]
        name = str(obj.get("name", "")).strip()
        check = (obj.get("check_command") or "").strip() or None
        owner = (obj.get("requires_of") or "").strip() or None

        if not name:
            return None
        # Every env obligation needs a real check; SERVICE is advisory (check=None).
        if node_type is not NodeType.SERVICE and not check:
            if note_out_of_scope is not None:
                note_out_of_scope(command, f"{kind} '{name}' had no check_command")
            return None

        return Discovery(
            node_type=node_type,
            name=name,
            layer=layer,
            evidence=(output or "")[-500:],
            check_command=check,
            confidence="runtime-llm",
            requires_of=owner,
        )

    return _classify
