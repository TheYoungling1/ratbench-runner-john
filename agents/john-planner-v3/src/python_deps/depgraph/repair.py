"""Under-declaration repair ladder — candidate generation + 3-way decide.

PURE module: no Executor, no network, no graph, no subprocess. Ported from the
validated spike ``src/eval/graph_fidelity/underdeclaration_repair_poc.py``
(only its pure rungs — the ``_http_json``/``wheel_provides``/``install_and_import``
machinery is deliberately NOT here).

This is the candidate-generation + decision half of the ladder: given a runtime
import the declared/closure set does not provide, propose candidate *distribution
names* and, once someone hands back the empirically grounded subset, make a 3-way
decision. The RECORD-grounding half (P1.3) lands in this same file; the fixpoint
that drives it (P1.4) wires it into construction. Nothing here reaches a model,
a socket, or the graph.

Invariants held here:
  * Roots = declared only. This module proposes candidate distribution names for
    an unsatisfied import; it fabricates a root nowhere.
  * The LLM rung is an INJECTED callable defaulting to ``None`` — the
    deterministic core never calls a model on its own.
  * Never guess a variant: more than one grounded provider ⇒ AMBIGUOUS, never
    silently pick one.
  * Pure functions, frozen dataclasses, no mutation.
"""
from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Callable

from python_deps.import_mapping import (
    CURATED_IMPORT_TO_PACKAGE,
    normalize_package_name,
    top_level_import_name,
)


@dataclass(frozen=True)
class Candidate:
    """A proposed distribution name and the rung that produced it."""

    dist: str
    source: str  # "normalize" | "curated" | "llm"


class Verdict(enum.Enum):
    """Outcome of grounding the candidate ladder for one import."""

    ACCEPT = "ACCEPT"
    AMBIGUOUS = "AMBIGUOUS"
    UNRESOLVED = "UNRESOLVED"


def normalize_candidates(import_name: str) -> list[str]:
    """Mechanical name variants for an import's top-level, canon-deduped.

    Verbatim from the spike: ``top``, ``top.lower()``, dashed
    (``[_.]+`` -> ``-``), ``python-<dashed>``, ``<dashed>-python``. The first
    occurrence of each canonical form wins.
    """
    top = top_level_import_name(import_name)
    dashed = re.sub(r"[_.]+", "-", top.lower())
    raw = [top, top.lower(), dashed, f"python-{dashed}", f"{dashed}-python"]
    seen: set[str] = set()
    out: list[str] = []
    for candidate in raw:
        key = normalize_package_name(candidate)
        if key not in seen:
            seen.add(key)
            out.append(candidate)
    return out


def curated_candidates(import_name: str) -> list[str]:
    """The demoted curated remap, now an untrusted candidate source.

    Looks up ``CURATED_IMPORT_TO_PACKAGE`` by the lowercased top-level import
    name and returns ``[hit]`` when present, else ``[]``. The table is NO LONGER
    a root authority — it contributes one candidate rung that still has to be
    grounded downstream.

    Note: the lookup key is ``top_level.lower()`` (mirroring
    ``import_mapping.map_import_to_package``), not the packaging canonical form,
    because the table is keyed by real import spellings — some of which keep
    underscores (e.g. ``django_filters``) that canonicalization would mangle.
    """
    key = top_level_import_name(import_name).lower()
    hit = CURATED_IMPORT_TO_PACKAGE.get(key)
    return [hit] if hit else []


def generate_candidates(
    import_name: str,
    *,
    llm: Callable[[str], list[str]] | None = None,
) -> list[Candidate]:
    """Ordered candidate distributions: normalize -> curated -> (optional) llm.

    Deterministic rungs come first (``normalize`` then ``curated``); the ``llm``
    rung runs ONLY when an ``llm`` callable is injected, and its guesses land
    last. Canon-deduped with the first (cheapest) source winning. ``llm``
    defaults to ``None`` so the deterministic core never calls a model.
    """
    ordered: list[Candidate] = [
        Candidate(dist, "normalize") for dist in normalize_candidates(import_name)
    ]
    ordered += [Candidate(dist, "curated") for dist in curated_candidates(import_name)]
    if llm is not None:
        ordered += [Candidate(dist, "llm") for dist in llm(import_name)]

    seen: set[str] = set()
    out: list[Candidate] = []
    for candidate in ordered:
        key = normalize_package_name(candidate.dist)
        if key not in seen:
            seen.add(key)
            out.append(candidate)
    return out


def decide(grounded_dists: list[str]) -> tuple[Verdict, str]:
    """3-way decision over the empirically grounded distributions.

    Exactly one grounded provider -> ``(ACCEPT, dist)``; more than one ->
    ``(AMBIGUOUS, "<joined>")`` (never pick a variant); none ->
    ``(UNRESOLVED, "-")``. Verbatim 3-way branching from the spike.
    """
    grounded = sorted(grounded_dists)
    if len(grounded) == 1:
        return Verdict.ACCEPT, grounded[0]
    if len(grounded) > 1:
        return Verdict.AMBIGUOUS, ", ".join(grounded)
    return Verdict.UNRESOLVED, "-"


# --------------------------------------------------------------------------- #
# P1.3 — RECORD grounding + provider selection (over an INJECTED provider)
# --------------------------------------------------------------------------- #
# The provider hands back the set of top-level module names a candidate dist's
# wheel RECORD/``top_level.txt`` ships, or ``None`` when there is no wheel to
# read. Injecting it keeps grounding pure and testable and keeps the
# network/PyPI detail (the spike's ``wheel_provides``) OUT of the decision core.
RecordProvider = Callable[[str], "set[str] | None"]


def record_grounds(
    candidate_dist: str,
    import_name: str,
    provider: RecordProvider,
) -> str:
    """3-way RECORD grounding for one candidate: ``confirm`` | ``deny`` | ``blind``.

    * ``confirm`` iff the provider's top-level set for ``candidate_dist`` contains
      the import's top-level module.
    * ``deny`` iff the provider returns a set that does NOT contain it — this
      prunes transitive-only shims (e.g. the ``bs4`` dummy dist whose RECORD
      lists something else) before they can be accepted.
    * ``blind`` iff the provider returns ``None`` (no wheel to read) — defer to
      P1.4's install backstop; never a decision on its own.

    Ports the spike's ``wheel_provides`` status mapping (wheel+provides ->
    confirm, wheel+!provides -> deny, no-wheel/too-big -> blind) and the
    ``Judged.record`` logic, but over the INJECTED provider — no network. The
    membership test is case-insensitive, matching the spike's lowercased
    ``top_level.txt`` comparison.
    """
    provided = provider(candidate_dist)
    if provided is None:
        return "blind"
    top = top_level_import_name(import_name).lower()
    if top in {module.lower() for module in provided}:
        return "confirm"
    return "deny"


@dataclass(frozen=True)
class RepairDecision:
    """Outcome of grounding + selecting a provider for one unsatisfied import.

    ``candidates_considered`` carries the ``blind`` distributions — those the
    injected provider could neither confirm nor deny — so the P1.4 fixpoint can
    hand exactly that set to its install backstop. It is empty whenever grounding
    was decisive (everything confirmed or denied), including on ``ACCEPT`` /
    ``AMBIGUOUS`` where no backstop is needed.
    """

    verdict: Verdict
    dist: str | None
    candidates_considered: tuple[str, ...]


def choose_provider(
    import_name: str,
    candidates: list[Candidate],
    provider: RecordProvider,
) -> RepairDecision:
    """Ground each candidate against the injected RECORD provider, then select.

    ``deny`` candidates (shims / hallucinations) are dropped. The surviving
    ``confirm`` set, deduped to CANON-DISTINCT distributions (by
    ``normalize_package_name`` — two spellings of one dist must never read as
    ambiguity), drives the verdict:

      * exactly one canonical confirm -> ``RepairDecision(ACCEPT, dist, ())``;
      * more than one -> ``RepairDecision(AMBIGUOUS, None, ())`` — never pick a
        variant (Global Constraint); two genuinely different confirming dists
        (e.g. ``attrs`` vs ``attr``) legitimately flag here;
      * zero confirms but >=1 ``blind`` -> ``RepairDecision(UNRESOLVED, None,
        <blind dists>)`` — blind is NEVER accepted on grounding alone; the blind
        set is surfaced for P1.4's install backstop to arbitrate;
      * nothing survives -> ``RepairDecision(UNRESOLVED, None, ())``.

    ``decide`` (P1.2) is deliberately NOT reused: it does not dedup its input, so
    counting ACCEPT/AMBIGUOUS goes through the canon-distinct sets built here.
    """
    confirmed: list[str] = []
    confirmed_canons: set[str] = set()
    blind: list[str] = []
    blind_canons: set[str] = set()
    for candidate in candidates:
        grounding = record_grounds(candidate.dist, import_name, provider)
        if grounding == "deny":
            continue
        canon = normalize_package_name(candidate.dist)
        if grounding == "confirm":
            if canon not in confirmed_canons:
                confirmed_canons.add(canon)
                confirmed.append(candidate.dist)
        elif canon not in blind_canons:  # blind
            blind_canons.add(canon)
            blind.append(candidate.dist)

    if len(confirmed) == 1:
        return RepairDecision(Verdict.ACCEPT, confirmed[0], ())
    if len(confirmed) > 1:
        return RepairDecision(Verdict.AMBIGUOUS, None, ())
    return RepairDecision(Verdict.UNRESOLVED, None, tuple(blind))
