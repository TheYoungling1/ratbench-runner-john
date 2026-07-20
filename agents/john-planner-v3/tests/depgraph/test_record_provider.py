"""P1.5 — pre-install PyPI wheel-metadata record provider + composite default.

Every test is hermetic: the network fetch is INJECTED (mirroring ``pins.py``'s
``_default_fetch`` seam), so nothing here touches urllib/PyPI. The real default
reader ``coverage._default_wheel_top_levels`` is the ONLY network code and is
never called — each test hands ``pypi_record_provider`` a fake ``fetch``.
"""

from __future__ import annotations

from python_deps.depgraph.coverage import (
    composite_record_provider,
    pypi_record_provider,
)


def _fake_fetch(mapping):
    """A fake ``fetch``: dist NAME -> ``set|None``, recording every call.

    Returns ``(fetch, calls)`` where ``calls["n"]`` counts invocations. A dist
    absent from ``mapping`` (or explicitly mapped to ``None``) returns ``None`` —
    the "not on PyPI / no wheel / sdist-only" signal the real seam produces.
    """
    calls = {"n": 0, "seen": []}

    def fetch(dist):
        calls["n"] += 1
        calls["seen"].append(dist)
        return mapping.get(dist)

    return fetch, calls


# --------------------------------------------------------------------------- #
# pypi_record_provider — pure over the injected fetch seam
# --------------------------------------------------------------------------- #
def test_pypi_provider_returns_fetch_set():
    fetch, _ = _fake_fetch({"PyYAML": {"yaml"}})
    provider = pypi_record_provider(fetch=fetch)
    assert provider("PyYAML") == {"yaml"}


def test_pypi_provider_none_when_fetch_blind():
    fetch, _ = _fake_fetch({})  # every dist -> None (not on PyPI / no wheel)
    provider = pypi_record_provider(fetch=fetch)
    assert provider("mystery") is None


def test_pypi_provider_none_for_sdist_only_dist():
    fetch, _ = _fake_fetch({"onlysdist": None})  # explicitly no wheel to read
    provider = pypi_record_provider(fetch=fetch)
    assert provider("onlysdist") is None


def test_pypi_provider_shim_fidelity():
    """bs4's OWN wheel RECORDs a shim module (not ``bs4``); beautifulsoup4 ships
    ``bs4``. The provider must return each dist's real top-levels verbatim, so
    downstream ``record_grounds`` DENYs bs4 and CONFIRMs beautifulsoup4 for the
    import ``bs4``."""
    fetch, _ = _fake_fetch({"bs4": {"_shim"}, "beautifulsoup4": {"bs4"}})
    provider = pypi_record_provider(fetch=fetch)
    assert provider("bs4") == {"_shim"}
    assert provider("beautifulsoup4") == {"bs4"}


def test_pypi_provider_caches_per_dist():
    fetch, calls = _fake_fetch({"PyYAML": {"yaml"}})
    provider = pypi_record_provider(fetch=fetch)
    assert provider("PyYAML") == {"yaml"}
    assert provider("PyYAML") == {"yaml"}
    assert calls["n"] == 1  # queried the underlying fetch exactly once


def test_pypi_provider_caches_blind_answer():
    fetch, calls = _fake_fetch({})  # -> None
    provider = pypi_record_provider(fetch=fetch)
    assert provider("ghost") is None
    assert provider("ghost") is None
    assert calls["n"] == 1  # a None answer is cached too, never re-fetched


# --------------------------------------------------------------------------- #
# composite_record_provider — installed short-circuits the candidate read
# --------------------------------------------------------------------------- #
def test_composite_short_circuits_installed_without_calling_candidate():
    """An installed dist is answered by the cheap post-install provider; the
    candidate (PyPI) fetch is NEVER invoked."""
    def installed(dist):
        return {"requests"} if dist == "requests" else None

    cand_fetch, cand_calls = _fake_fetch({"requests": {"requests"}})
    candidate = pypi_record_provider(fetch=cand_fetch)
    composite = composite_record_provider(installed, candidate)

    assert composite("requests") == {"requests"}
    assert cand_calls["n"] == 0  # candidate/PyPI path never consulted


def test_composite_consults_candidate_when_installed_blind():
    """A dist the installed provider lacks (``None``) falls through to the
    candidate provider, whose set is returned."""
    def installed(_dist):
        return None  # installed provider covers nothing

    cand_fetch, cand_calls = _fake_fetch({"PyYAML": {"yaml"}})
    candidate = pypi_record_provider(fetch=cand_fetch)
    composite = composite_record_provider(installed, candidate)

    assert composite("PyYAML") == {"yaml"}
    assert cand_calls["n"] == 1


def test_composite_caches_candidate_result():
    def installed(_dist):
        return None

    cand_fetch, cand_calls = _fake_fetch({"PyYAML": {"yaml"}})
    candidate = pypi_record_provider(fetch=cand_fetch)
    composite = composite_record_provider(installed, candidate)

    assert composite("PyYAML") == {"yaml"}
    assert composite("PyYAML") == {"yaml"}
    assert cand_calls["n"] == 1  # fetched once across repeated composite calls
