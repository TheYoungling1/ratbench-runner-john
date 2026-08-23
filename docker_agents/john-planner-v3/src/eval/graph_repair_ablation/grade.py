"""Deterministic localization grader: match an agent's diagnostic actions + final
patch against the injection oracle's correct_action. Pure, no LLM."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src"
for _p in (_REPO_ROOT, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from src.eval.graph_repair_ablation.oracle import Injection  # noqa: E402
from src.eval.language_package_eval.coverage import canon_pip  # noqa: E402


@dataclass(frozen=True)
class LocalizationScore:
    localized_at_1: bool
    localized_at_3: bool
    first_correct_rank: int | None
    mislocalized: bool
    wasted_rate: float
    success_action: dict | None


def _target_tokens(action_target: str) -> set[str]:
    """Canonical tokens for a target string ('apt:libgraphviz-dev' -> {'libgraphviz-dev'};
    'requests' -> {canon_pip('requests')})."""
    raw = action_target.split(":", 1)[1] if ":" in action_target else action_target
    return {raw, canon_pip(raw)}


def _action_hits_target(text: str, target: str) -> bool:
    toks = _target_tokens(target)
    low = text.lower()
    return any(t and t.lower() in low for t in toks)


def grade_localization(trace: dict, inj: Injection) -> LocalizationScore:
    kind = inj.correct_action["kind"]
    target = inj.correct_action["target"]
    actions = trace.get("actions", []) or []
    patch = trace.get("patch")

    # per-action correctness: does the action touch the correct target?
    ranks = [i + 1 for i, a in enumerate(actions) if _action_hits_target(a, target)]
    # a drop-class action that INSTALLS the target is NOT localizing; it's the wrong move.
    if kind == "drop":
        ranks = [i + 1 for i, a in enumerate(actions)
                 if _action_hits_target(a, target) and "install" not in a.lower()]

    first_action_rank = ranks[0] if ranks else None

    # final-patch correctness
    patch_correct = False
    mislocalized = False
    if patch:
        p_kind, p_target = patch.get("kind"), patch.get("target", "")
        hits = _action_hits_target(p_target, target)
        if kind == "drop":
            patch_correct = (p_kind == "drop" and hits)
            mislocalized = (p_kind == "install" and hits) or (not hits and p_kind is not None)
        else:
            patch_correct = (p_kind == kind and hits) or hits
            mislocalized = bool(p_target) and not hits

    # first_correct_rank folds in the final patch as rank = len(actions)+1
    first_correct_rank = first_action_rank
    if first_correct_rank is None and patch_correct:
        first_correct_rank = len(actions) + 1

    localized_at_1 = first_correct_rank == 1
    localized_at_3 = first_correct_rank is not None and first_correct_rank <= 3
    wasted = sum(1 for a in actions if not _action_hits_target(a, target))
    wasted_rate = wasted / max(1, len(actions))
    success = {"kind": kind, "target": target} if patch_correct else None
    return LocalizationScore(localized_at_1, localized_at_3, first_correct_rank,
                             mislocalized, wasted_rate, success)
