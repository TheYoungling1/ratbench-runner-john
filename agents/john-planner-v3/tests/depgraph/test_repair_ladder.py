# tests/depgraph/test_repair_ladder.py
"""P1.2 — pure candidate ladder (generation + 3-way decide).

No network, no Executor, no graph: these exercise only the deterministic rungs
and the injected-LLM seam.
"""
from python_deps.depgraph.repair import (
    Candidate,
    Verdict,
    curated_candidates,
    decide,
    generate_candidates,
    normalize_candidates,
)
from python_deps.import_mapping import normalize_package_name


# --------------------------------------------------------------------------- #
# normalize_candidates
# --------------------------------------------------------------------------- #
def test_normalize_candidates_contains_python_prefix():
    out = normalize_candidates("dateutil")
    assert "python-dateutil" in out


def test_normalize_candidates_canon_deduped():
    # No two survivors share a canonical form (first occurrence wins).
    out = normalize_candidates("dateutil")
    canons = [normalize_package_name(c) for c in out]
    assert len(canons) == len(set(canons))


def test_normalize_candidates_uses_top_level_only():
    out = normalize_candidates("dateutil.parser")
    assert "python-dateutil" in out
    assert all("parser" not in c for c in out)


# --------------------------------------------------------------------------- #
# curated_candidates  (demoted table — untrusted candidate source)
# --------------------------------------------------------------------------- #
def test_curated_candidates_yaml_real_value():
    # The real curated dist for the `yaml` import is PyYAML.
    assert curated_candidates("yaml") == ["PyYAML"]


def test_curated_candidates_requests_is_empty():
    # `requests` is not a curated remap -> no curated candidate.
    assert curated_candidates("requests") == []


# --------------------------------------------------------------------------- #
# generate_candidates
# --------------------------------------------------------------------------- #
def test_generate_candidates_normalize_before_curated():
    cands = generate_candidates("yaml")
    sources = [c.source for c in cands]
    dists = [c.dist for c in cands]
    assert "PyYAML" in dists
    first_curated = sources.index("curated")
    # everything before the first curated rung is a normalize rung
    assert all(s == "normalize" for s in sources[:first_curated])


def test_generate_candidates_llm_last_and_only_when_provided():
    cands = generate_candidates("yaml", llm=lambda n: ["extra"])
    # the llm guess lands last and is the only llm-sourced candidate
    assert cands[-1].dist == "extra"
    assert cands[-1].source == "llm"
    llm_positions = [i for i, c in enumerate(cands) if c.source == "llm"]
    assert llm_positions == [len(cands) - 1]


def test_generate_candidates_no_llm_source_by_default():
    # explicit None
    assert all(c.source != "llm" for c in generate_candidates("yaml", llm=None))
    # and the plain default (no kwarg) never calls a model
    assert all(c.source != "llm" for c in generate_candidates("yaml"))


def test_generate_candidates_returns_candidate_instances():
    cands = generate_candidates("yaml")
    assert cands and all(isinstance(c, Candidate) for c in cands)


# --------------------------------------------------------------------------- #
# decide — 3-way self-check
# --------------------------------------------------------------------------- #
def test_decide_unresolved_on_none():
    assert decide([]) == (Verdict.UNRESOLVED, "-")


def test_decide_accept_on_exactly_one():
    verdict, dist = decide(["a"])
    assert verdict == Verdict.ACCEPT
    assert dist == "a"


def test_decide_ambiguous_on_more_than_one():
    verdict, _ = decide(["a", "b"])
    assert verdict == Verdict.AMBIGUOUS
