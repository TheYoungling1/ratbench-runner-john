"""Tests for eval stage 3 — translate quality on the SHIPPED exotic path.

NO live LLM / network. These mock ONLY the LLM boundary and drive the REAL
``translate_service`` pipeline, so ``apply_arch`` / ``apply_env`` / ``normalize_probe`` /
``verify`` integration is genuinely measured (never re-implemented here):

  * ``full_translate`` is monkeypatched to return canned RAW plans (the model's output).
  * ``verify_plan`` is monkeypatched only where a real plan would hit a URL.

Everything downstream of that boundary — the arch scrub, the probe firewall, and how a
failed verify demotes ``feasible`` — is the shipped code under test.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for _p in (str(_ROOT), str(_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import envstate.service_translate as st  # noqa: E402  (the monkeypatch target module)
from evals.service_config_detection.provision_corpus import PROVISION_CASES  # noqa: E402
from evals.service_config_detection.stage_translate import (  # noqa: E402
    _arch_clean,
    _exotic_cases,
    measure_translate,
)

ARM = {"dpkg": "arm64", "uname": "aarch64"}


def _only(name: str):
    """The single corpus case with this name, as a 1-element list."""
    return [c for c in PROVISION_CASES if c.name == name]


def _draws(res: dict, name: str) -> list[dict]:
    """Per-draw records for one case in a measure_translate result."""
    return next(pc["draws"] for pc in res["per_case"] if pc["name"] == name)


# ---------------------------------------------------------------------------
# corpus helper
# ---------------------------------------------------------------------------
def test_exotic_cases_excludes_recipe_kinds():
    """`_exotic_cases` keeps only non-recipe kinds (postgres/mysql/redis/rabbitmq out)."""
    names = {c.name for c in _exotic_cases()}
    assert "milvus" in names and "opensearch" in names
    # keydb->redis, mariadb->mysql, redis are recipe kinds -> excluded
    assert {"redis", "keydb", "mariadb"}.isdisjoint(names)


# ---------------------------------------------------------------------------
# stage-3 metrics driven through the SHIPPED translate_service
# ---------------------------------------------------------------------------
def test_parse_fail_rate(monkeypatch):
    """A parse-failed model reply yields parse_fail_rate 1.0 and no setup for any draw."""
    monkeypatch.setattr(
        st, "full_translate",
        lambda client, model, spec, arch: {"feasible": None, "note": "parse-failed"},
    )
    res = measure_translate(None, "stub", ARM, cases=_only("couchdb"), n=2)
    assert res["parse_fail_rate"] == 1.0
    assert res["total_draws"] == 2
    assert all(d["has_setup"] is False for d in _draws(res, "couchdb"))


def test_arch_clean_via_shipped_apply_arch(monkeypatch):
    """The SHIPPED apply_arch rewrites amd64->arm64 in a URL, so no foreign literal
    survives -> arch_clean_rate 1.0. Proves the real arch scrub works end-to-end."""
    monkeypatch.setattr(
        st, "full_translate",
        lambda client, model, spec, arch: {
            "install": ["wget https://h/app-linux-amd64.tar.gz"],
            "start": "x &", "probe": "true", "feasible": True, "note": "",
        },
    )
    monkeypatch.setattr(st, "verify_plan",
                        lambda plan: {"all_ok": True, "n": 0, "urls": []})
    res = measure_translate(None, "stub", ARM, cases=_only("couchdb"), n=2)
    assert res["arch_clean_rate"] == 1.0
    # the shipped scrub actually rewrote the literal
    draw = _draws(res, "couchdb")[0]
    assert draw["has_setup"] is True and draw["arch_clean"] is True


def test_probe_firewall_live_path(monkeypatch):
    """A curl probe on the live path is forced through the read-only firewall:
    probe_firewall_rate 1.0 and the setup probe becomes `nc -z 127.0.0.1 9200`."""
    monkeypatch.setattr(
        st, "full_translate",
        lambda client, model, spec, arch: {
            "install": [], "start": "opensearch &",
            "probe": "curl -f http://localhost:9200",
            "feasible": True, "note": "",
        },
    )
    monkeypatch.setattr(st, "verify_plan",
                        lambda plan: {"all_ok": True, "n": 0, "urls": []})
    res = measure_translate(None, "stub", ARM, cases=_only("opensearch"), n=2)  # port 9200
    assert res["probe_firewall_rate"] == 1.0
    assert _draws(res, "opensearch")[0]["probe"] == "nc -z 127.0.0.1 9200"


def test_verify_catch_hallucination(monkeypatch):
    """On the milvus hallucination (bad download URL), the shipped verify catches it:
    verify_catch 1.0 and the draw demotes to feasible False even though the model said True."""
    monkeypatch.setattr(
        st, "full_translate",
        lambda client, model, spec, arch: {
            "install": ["wget https://milvus.invalid/standalone-linux.tar.gz"],
            "start": "milvus run standalone &", "probe": "true",
            "feasible": True, "note": "",
        },
    )
    monkeypatch.setattr(
        st, "verify_plan",
        lambda plan: {"all_ok": False, "n": 1,
                      "urls": [{"url": "https://milvus.invalid/standalone-linux.tar.gz",
                                "status": None, "state": "error", "detail": "URLError"}]},
    )
    res = measure_translate(None, "stub", ARM, cases=_only("milvus"), n=2)
    assert res["verify_catch"] == 1.0
    assert all(d["feasible"] is False for d in _draws(res, "milvus"))


def test_metric_falsifiable(monkeypatch):
    """A foreign-arch literal NOT in a URL survives the shipped apply_arch (URL-only
    scrub) -> arch_clean_rate < 1.0. Proves the metric can actually fail."""
    monkeypatch.setattr(
        st, "full_translate",
        lambda client, model, spec, arch: {
            "install": [], "start": "run --arch amd64 &", "probe": "true",
            "feasible": True, "note": "",
        },
    )
    monkeypatch.setattr(st, "verify_plan",
                        lambda plan: {"all_ok": True, "n": 0, "urls": []})
    res = measure_translate(None, "stub", ARM, cases=_only("couchdb"), n=2)
    assert res["arch_clean_rate"] < 1.0
    assert _draws(res, "couchdb")[0]["arch_clean"] is False


# ---------------------------------------------------------------------------
# unit: the _arch_clean predicate itself
# ---------------------------------------------------------------------------
def test_arch_clean_predicate_matches_word_boundary():
    """_arch_clean flags a bare foreign token but not a substring, and passes amd64 host."""
    foreign = {"install": [], "start": "run --arch amd64 &", "post": []}
    clean = {"install": [], "start": "app-amd64bar &", "post": []}
    assert _arch_clean(foreign, ARM) is False
    assert _arch_clean(clean, ARM) is True
    # on an amd64 host, an amd64 literal is native, not foreign
    assert _arch_clean(foreign, {"dpkg": "amd64", "uname": "x86_64"}) is True


def test_build_live_client_is_bounded(monkeypatch):
    """The --live client must cap per-request timeout + retries so one hung request cannot
    wedge the whole sequential N-draw run (the observed 19-min-stall regression)."""
    from evals.service_config_detection.stage_translate import _build_live_client

    captured = {}

    class _FakeOpenAI:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr("openai.OpenAI", _FakeOpenAI)
    monkeypatch.setenv("OPENROUTER_API_BASE", "http://example")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    _build_live_client()
    assert captured["timeout"] == 45
    assert captured["max_retries"] == 1
