# tests/depgraph/test_repair_grounding.py
"""P1.3 — RECORD grounding + provider selection (pure, injected provider).

No network: the ``RecordProvider`` is a fake dict lookup. These exercise the
confirm/deny/blind mapping ported from the spike's ``wheel_provides``/
``Judged.record``, plus shim-pruning, ambiguity refusal, blind-deferral, and the
canon-distinct counting contract carried over from the P1.2 review.
"""
from python_deps.depgraph.repair import (
    Candidate,
    RepairDecision,
    Verdict,
    choose_provider,
    record_grounds,
)
from python_deps.import_mapping import normalize_package_name


def make_provider(table):
    """A fake ``RecordProvider``: dist name -> top-level set, or ``None`` if unknown.

    Keyed by the packaging-canonical dist name so a dist registered once is hit
    via any spelling (mirrors a real index/RECORD read, which is canon-insensitive).
    """
    canon_table = {normalize_package_name(k): v for k, v in table.items()}

    def provider(dist):
        return canon_table.get(normalize_package_name(dist))

    return provider


# --------------------------------------------------------------------------- #
# record_grounds — 3-way confirm / deny / blind
# --------------------------------------------------------------------------- #
def test_record_grounds_confirm_when_top_level_present():
    prov = make_provider({"PyYAML": {"yaml"}})
    assert record_grounds("PyYAML", "yaml", prov) == "confirm"


def test_record_grounds_deny_when_module_absent():
    # The bs4 dummy dist whose RECORD lists a shim, not the module itself.
    prov = make_provider({"bs4": {"_bs4_shim"}})
    assert record_grounds("bs4", "bs4", prov) == "deny"


def test_record_grounds_blind_when_provider_returns_none():
    prov = make_provider({})  # provider knows nothing -> no wheel to read
    assert record_grounds("mystery-dist", "mystery", prov) == "blind"


def test_record_grounds_uses_top_level_only():
    prov = make_provider({"python-dateutil": {"dateutil"}})
    assert record_grounds("python-dateutil", "dateutil.parser", prov) == "confirm"


# --------------------------------------------------------------------------- #
# choose_provider — shim prune / ambiguity / blind / canon-distinct
# --------------------------------------------------------------------------- #
def test_choose_provider_shim_pruned_grounding_beats_naive_install():
    # bs4's own wheel lacks the module (deny-pruned); grounding elects the real dist.
    prov = make_provider({"bs4": {"_bs4_shim"}, "beautifulsoup4": {"bs4"}})
    decision = choose_provider(
        "bs4",
        [Candidate("bs4", "normalize"), Candidate("beautifulsoup4", "curated")],
        prov,
    )
    assert decision.verdict == Verdict.ACCEPT
    assert decision.dist == "beautifulsoup4"


def test_choose_provider_ambiguous_when_two_distinct_confirm():
    # attrs vs attr: genuinely different dists both ship top-level `attr`.
    prov = make_provider({"attrs": {"attr"}, "attr": {"attr"}})
    decision = choose_provider(
        "attr",
        [Candidate("attrs", "llm"), Candidate("attr", "normalize")],
        prov,
    )
    assert decision.verdict == Verdict.AMBIGUOUS
    assert decision.dist is None


def test_choose_provider_blind_only_defers_to_backstop():
    # Provider blind on the only candidate: never ACCEPT on blind alone;
    # surface it for P1.4's install backstop; UNRESOLVED with no backstop here.
    prov = make_provider({})
    decision = choose_provider(
        "mystery",
        [Candidate("mystery-dist", "normalize")],
        prov,
    )
    assert decision.verdict != Verdict.ACCEPT
    assert decision.verdict == Verdict.UNRESOLVED
    assert decision.dist is None
    assert "mystery-dist" in decision.candidates_considered


def test_choose_provider_canon_distinct_two_spellings_not_ambiguous():
    # Two candidate strings that canonicalize to the SAME dist, both confirm:
    # single canonical provider -> ACCEPT, NOT a spurious AMBIGUOUS.
    prov = make_provider({"foo-bar": {"foobar"}})
    decision = choose_provider(
        "foobar",
        [Candidate("Foo_Bar", "normalize"), Candidate("foo-bar", "curated")],
        prov,
    )
    assert decision.verdict == Verdict.ACCEPT
    assert decision.dist is not None
    assert normalize_package_name(decision.dist) == "foo-bar"


def test_choose_provider_all_deny_is_unresolved_empty():
    # Everything denied -> nothing survives -> empty candidates_considered.
    prov = make_provider({"wrongdist": {"somethingelse"}})
    decision = choose_provider(
        "target",
        [Candidate("wrongdist", "llm")],
        prov,
    )
    assert decision.verdict == Verdict.UNRESOLVED
    assert decision.dist is None
    assert decision.candidates_considered == ()


def test_choose_provider_deterministic():
    prov = make_provider({"bs4": {"_bs4_shim"}, "beautifulsoup4": {"bs4"}})
    cands = [Candidate("bs4", "normalize"), Candidate("beautifulsoup4", "curated")]
    assert choose_provider("bs4", cands, prov) == choose_provider("bs4", cands, prov)


def test_choose_provider_returns_repair_decision():
    prov = make_provider({"PyYAML": {"yaml"}})
    decision = choose_provider("yaml", [Candidate("PyYAML", "curated")], prov)
    assert isinstance(decision, RepairDecision)
