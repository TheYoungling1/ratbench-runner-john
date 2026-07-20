"""TDD tests for the pkg_layer repair ladder (Task 6).

``repair_import`` resolves ONE under-declared RUNTIME import to a
distribution via a TRUST-ORDERED ladder (module_name -> curated -> pypi ->
llm). Every candidate must be verified before acceptance, and a rung that
produces MORE THAN ONE verified provider is flagged ambiguous rather than
guessed -- this is the DeepSeek guardrail: install-verification proves
importability, not intent, so it cannot disambiguate e.g. psycopg2 vs
psycopg2-binary; only a declared contract may pick a variant.

All three effectful collaborators (PyPI probe, LLM probe, install verifier)
are injected Protocols and faked here -- no live network/Docker/LLM.
"""
from __future__ import annotations

from python_deps.pkg_layer.repair import RepairOutcome, repair_import


# --------------------------------------------------------------------------- #
# Fakes for the three injected Protocols
# --------------------------------------------------------------------------- #

class _FakeVerifier:
    """Verifies iff (dist, import_name) is in the configured allow-set."""

    def __init__(self, verified_pairs):
        self._verified = set(verified_pairs)

    def provides(self, dist: str, import_name: str) -> bool:
        return (dist, import_name) in self._verified


class _FakeProbe:
    """PyPI-probe fake: returns configured candidates per import name."""

    def __init__(self, candidates_by_import=None, *, forbid_call: bool = False):
        self._map = candidates_by_import or {}
        self._forbid_call = forbid_call

    def candidates(self, import_name: str) -> tuple[str, ...]:
        if self._forbid_call:
            raise AssertionError(
                f"ProviderProbe.candidates must not be called for "
                f"{import_name!r} once an earlier rung already resolved"
            )
        return self._map.get(import_name, ())


class _FakeLlm:
    """LLM-probe fake: same shape as _FakeProbe, kept distinct for clarity."""

    def __init__(self, candidates_by_import=None, *, forbid_call: bool = False):
        self._map = candidates_by_import or {}
        self._forbid_call = forbid_call

    def candidates(self, import_name: str) -> tuple[str, ...]:
        if self._forbid_call:
            raise AssertionError(
                f"LlmProbe.candidates must not be called for {import_name!r} "
                f"once an earlier rung already resolved"
            )
        return self._map.get(import_name, ())


_EMPTY_PROBE = _FakeProbe()
_EMPTY_LLM = _FakeLlm()


# --------------------------------------------------------------------------- #
# Rung 1: module_name identity
# --------------------------------------------------------------------------- #

def test_identity_import_verified_at_rung_one():
    verifier = _FakeVerifier({("requests", "requests")})
    # Later rungs must never be consulted -- the ladder stops at rung 1.
    probe = _FakeProbe(forbid_call=True)
    llm = _FakeLlm(forbid_call=True)

    outcome = repair_import("requests", probe, llm, verifier)

    assert outcome == RepairOutcome(
        import_name="requests", resolved_dist="requests", source="module_name"
    )


def test_identity_not_verified_falls_through_to_next_rung():
    # "cv2" does not verify as itself (there is no dist literally named cv2);
    # the ladder must fall through to rung 2 rather than accepting rung 1.
    verifier = _FakeVerifier({("opencv-python", "cv2")})

    outcome = repair_import("cv2", _EMPTY_PROBE, _EMPTY_LLM, verifier)

    assert outcome.source != "module_name"


# --------------------------------------------------------------------------- #
# Rung 2: curated (python_deps.import_mapping)
# --------------------------------------------------------------------------- #

def test_curated_alias_accepted_at_rung_two():
    # cv2 -> opencv-python is a curated alias in import_mapping.
    verifier = _FakeVerifier({("opencv-python", "cv2")})
    # Later rungs must never be consulted -- rung 2 already resolved.
    probe = _FakeProbe(forbid_call=True)
    llm = _FakeLlm(forbid_call=True)

    outcome = repair_import("cv2", probe, llm, verifier)

    assert outcome == RepairOutcome(
        import_name="cv2", resolved_dist="opencv-python", source="curated"
    )


def test_curated_candidate_rejected_by_verifier_falls_through():
    # cv2 is curated to opencv-python, but the verifier says that dist does
    # NOT actually provide cv2 in this (fake) environment -- must fall
    # through to rung 3 rather than accepting an unverified curated guess.
    probe = _FakeProbe({"cv2": ("opencv-python-headless",)})
    verifier = _FakeVerifier({("opencv-python-headless", "cv2")})

    outcome = repair_import("cv2", probe, _EMPTY_LLM, verifier)

    assert outcome == RepairOutcome(
        import_name="cv2", resolved_dist="opencv-python-headless", source="pypi"
    )


# --------------------------------------------------------------------------- #
# Rung 3: pypi probe
# --------------------------------------------------------------------------- #

def test_pypi_single_verified_provider_accepted():
    probe = _FakeProbe({"foobarmodule": ("foo-bar-pkg",)})
    verifier = _FakeVerifier({("foo-bar-pkg", "foobarmodule")})
    llm = _FakeLlm(forbid_call=True)

    outcome = repair_import("foobarmodule", probe, llm, verifier)

    assert outcome == RepairOutcome(
        import_name="foobarmodule", resolved_dist="foo-bar-pkg", source="pypi"
    )


def test_pypi_filters_unverified_candidates_before_counting():
    # Two raw candidates come back from the probe, but only one verifies --
    # this must still be treated as a single-provider accept, not ambiguous.
    probe = _FakeProbe({"foobarmodule": ("dist-a", "dist-b")})
    verifier = _FakeVerifier({("dist-a", "foobarmodule")})

    outcome = repair_import("foobarmodule", probe, _EMPTY_LLM, verifier)

    assert outcome == RepairOutcome(
        import_name="foobarmodule", resolved_dist="dist-a", source="pypi"
    )


# --------------------------------------------------------------------------- #
# Ambiguity guardrail: >1 verified provider at a rung -> flagged, never guessed
# --------------------------------------------------------------------------- #

def test_two_verifying_providers_flagged_ambiguous_psycopg2_guardrail():
    # The DeepSeek guardrail case: the plain `psycopg2` source distribution
    # fails to verify (e.g. no pg_config in this environment -- rungs 1 and
    # 2 both try dist=="psycopg2" and both fail), but the PyPI probe then
    # surfaces two DIFFERENT variants that both install and both satisfy
    # `import psycopg2`. Install-verification alone cannot tell them apart
    # -- only a declared contract could -- so this MUST be flagged, never
    # auto-picked.
    probe = _FakeProbe({"psycopg2": ("psycopg2-binary", "psycopg2cffi")})
    verifier = _FakeVerifier(
        {("psycopg2-binary", "psycopg2"), ("psycopg2cffi", "psycopg2")}
    )
    llm = _FakeLlm(forbid_call=True)

    outcome = repair_import("psycopg2", probe, llm, verifier)

    assert outcome.source == "flagged_ambiguous"
    assert outcome.resolved_dist is None
    assert outcome.import_name == "psycopg2"


def test_two_verifying_providers_at_llm_rung_also_flagged_ambiguous():
    verifier = _FakeVerifier(
        {("weird-pkg-a", "weirdmod"), ("weird-pkg-b", "weirdmod")}
    )
    llm = _FakeLlm({"weirdmod": ("weird-pkg-a", "weird-pkg-b")})

    outcome = repair_import("weirdmod", _EMPTY_PROBE, llm, verifier)

    assert outcome.source == "flagged_ambiguous"
    assert outcome.resolved_dist is None


# --------------------------------------------------------------------------- #
# Rung 4: llm probe, and the "verifier is the final word" guarantee
# --------------------------------------------------------------------------- #

def test_llm_single_verified_provider_accepted_when_pypi_empty():
    probe = _FakeProbe({"weirdmod": ()})
    llm = _FakeLlm({"weirdmod": ("weird-pkg",)})
    verifier = _FakeVerifier({("weird-pkg", "weirdmod")})

    outcome = repair_import("weirdmod", probe, llm, verifier)

    assert outcome == RepairOutcome(
        import_name="weirdmod", resolved_dist="weird-pkg", source="llm"
    )


def test_pypi_candidate_rejected_by_verifier_falls_through_to_llm():
    # A pypi candidate exists but the verifier rejects it -- it must never be
    # accepted on the strength of merely being *a* candidate. The ladder
    # falls through to the llm rung, which succeeds.
    probe = _FakeProbe({"weirdmod": ("wrong-pkg",)})
    llm = _FakeLlm({"weirdmod": ("weird-pkg",)})
    verifier = _FakeVerifier({("weird-pkg", "weirdmod")})

    outcome = repair_import("weirdmod", probe, llm, verifier)

    assert outcome == RepairOutcome(
        import_name="weirdmod", resolved_dist="weird-pkg", source="llm"
    )


def test_llm_candidate_rejected_by_verifier_never_accepted():
    probe = _FakeProbe({"weirdmod": ()})
    llm = _FakeLlm({"weirdmod": ("wrong-pkg",)})
    verifier = _FakeVerifier(set())

    outcome = repair_import("weirdmod", probe, llm, verifier)

    assert outcome.resolved_dist is None
    assert outcome.source == "flagged_missing"


# --------------------------------------------------------------------------- #
# Rung 5: nothing verifies anywhere -> flagged_missing
# --------------------------------------------------------------------------- #

def test_nothing_verifies_flagged_missing():
    verifier = _FakeVerifier(set())

    outcome = repair_import("totallyabsentmodule", _EMPTY_PROBE, _EMPTY_LLM, verifier)

    assert outcome == RepairOutcome(
        import_name="totallyabsentmodule", resolved_dist=None, source="flagged_missing"
    )


def test_empty_candidate_lists_at_every_rung_flagged_missing():
    probe = _FakeProbe({"totallyabsentmodule": ()})
    llm = _FakeLlm({"totallyabsentmodule": ()})
    verifier = _FakeVerifier(set())

    outcome = repair_import("totallyabsentmodule", probe, llm, verifier)

    assert outcome.source == "flagged_missing"
    assert outcome.resolved_dist is None
