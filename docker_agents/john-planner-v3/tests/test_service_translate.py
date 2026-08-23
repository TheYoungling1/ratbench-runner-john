# tests/test_service_translate.py
"""Isolated unit tests for the CR5 translate router (NO live LLM / NO network).

Router tests monkeypatch ``service_translate.full_translate`` to return canned
plan dicts (so no client is ever called) and use URL-free plans (so the real
``verify_plan`` touches no network), or monkeypatch ``verify_plan`` directly.
The ``full_translate`` test monkeypatches ``service_translate.complete_with_retry``.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest

from python_deps.depgraph.patch_gate import is_read_only
from python_deps.depgraph.provisioning_spec import ProvisioningSpec
from src.envstate import service_translate
from src.envstate.service_translate import (
    _image_basename,
    full_translate,
    translate_service,
)

_ARCH = {"dpkg": "arm64", "uname": "aarch64"}


# --------------------------------------------------------------------------- #
# _image_basename
# --------------------------------------------------------------------------- #
def test_image_basename_strips_registry_and_tag():
    assert _image_basename("qdrant/qdrant:v1.9.0") == "qdrant"
    assert _image_basename("acme/thing:1") == "thing"
    assert _image_basename("redis:7") == "redis"
    assert _image_basename("") == ""


def test_image_basename_registry_port():
    # A registry host with a port must not be mistaken for the tag: strip the
    # path segment first, then the tag.
    assert _image_basename("localhost:5000/foo:v1") == "foo"
    assert _image_basename("qdrant/qdrant:v1.9.0") == "qdrant"


# --------------------------------------------------------------------------- #
# translate_service — known kind (deterministic recipe, no LLM)
# --------------------------------------------------------------------------- #
def test_known_kind_no_llm(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("full_translate must NOT be called on the known path")

    monkeypatch.setattr(service_translate, "full_translate", _boom)
    spec = ProvisioningSpec("cache", "redis", "redis:7", port=6379)

    out = translate_service(None, "m", spec, _ARCH)

    assert out["route"] == "known"
    assert out["feasible"] is True
    assert out["kind"] == "redis"
    assert out["service_name"] == "cache"
    assert out["setup"]["probe"] == "redis-cli ping"
    assert out["verify"] is None
    assert out["note"] == "known-kind"


# --------------------------------------------------------------------------- #
# translate_service — exotic (LLM) paths
# --------------------------------------------------------------------------- #
def test_exotic_feasible_normalizes_curl_probe(monkeypatch):
    plan = {
        "install": ["apt-get install -y x"],
        "start": "x &",
        "probe": "curl -f http://localhost:8080/health",
        "feasible": True,
        "note": "",
    }
    monkeypatch.setattr(service_translate, "full_translate", lambda *a, **k: dict(plan))
    spec = ProvisioningSpec("svc", None, "acme/thing:1", port=8080)

    out = translate_service(object(), "m", spec, _ARCH)

    assert out["route"] == "exotic"
    assert out["kind"] == "thing"
    assert out["feasible"] is True
    assert out["note"] == "ok"
    # The probe firewall MUST hold: a curl probe becomes an admissible nc check.
    assert out["setup"]["probe"] == "nc -z 127.0.0.1 8080"
    assert is_read_only(out["setup"]["probe"]) is True
    assert out["verify"]["all_ok"] is True


def test_exotic_infeasible_llm_false(monkeypatch):
    plan = {
        "install": ["apt-get install -y x"],
        "start": "x &",
        "probe": "nc -z 127.0.0.1 9000",
        "feasible": False,
        "note": "docker-only",
    }
    monkeypatch.setattr(service_translate, "full_translate", lambda *a, **k: dict(plan))
    spec = ProvisioningSpec("svc", None, "acme/thing:1", port=9000)

    out = translate_service(object(), "m", spec, _ARCH)

    assert out["route"] == "exotic"
    assert out["feasible"] is False
    assert out["note"] == "could-not-provision"
    # Setup is STILL returned when infeasible — the node is created, demotes at certify.
    assert isinstance(out["setup"], dict)


def test_exotic_verify_fail(monkeypatch):
    plan = {
        "install": ["wget https://example.com/nope.tar.gz"],
        "start": "x &",
        "probe": "nc -z 127.0.0.1 8080",
        "feasible": True,
        "note": "",
    }
    monkeypatch.setattr(service_translate, "full_translate", lambda *a, **k: dict(plan))
    monkeypatch.setattr(
        service_translate,
        "verify_plan",
        lambda p: {"all_ok": False, "n": 1, "urls": [{"url": "x", "state": "bad"}]},
    )
    spec = ProvisioningSpec("svc", None, "acme/thing:1", port=8080)

    out = translate_service(object(), "m", spec, _ARCH)

    assert out["feasible"] is False
    assert out["note"] == "could-not-provision"
    assert out["verify"]["all_ok"] is False
    assert isinstance(out["setup"], dict)


def test_parse_failed(monkeypatch):
    monkeypatch.setattr(
        service_translate,
        "full_translate",
        lambda *a, **k: {"feasible": None, "note": "parse-failed"},
    )
    spec = ProvisioningSpec("svc", None, "acme/thing:1", port=8080)

    out = translate_service(object(), "m", spec, _ARCH)

    assert out["route"] == "exotic"
    assert out["feasible"] is False
    assert out["setup"] is None
    assert out["verify"] is None
    assert out["note"] == "parse-failed"


# --------------------------------------------------------------------------- #
# full_translate — parses client JSON, does NOT sanitize internally
# --------------------------------------------------------------------------- #
def test_full_translate_parses_client_json(monkeypatch):
    monkeypatch.setattr(
        service_translate,
        "complete_with_retry",
        lambda *a, **k: (
            '{"install":[],"start":"s","probe":"p","feasible":true,"note":""}',
            None,
            None,
        ),
    )
    spec = ProvisioningSpec("svc", None, "acme/thing:1", port=8080)

    plan = full_translate(object(), "m", spec, _ARCH)

    assert plan["feasible"] is True
    # NOT sanitized inside full_translate — the raw parsed probe survives verbatim.
    assert plan["probe"] == "p"
    assert plan["start"] == "s"


def test_full_translate_parse_failure_returns_sentinel(monkeypatch):
    monkeypatch.setattr(
        service_translate,
        "complete_with_retry",
        lambda *a, **k: ("not json at all", None, None),
    )
    spec = ProvisioningSpec("svc", None, "acme/thing:1", port=8080)

    plan = full_translate(object(), "m", spec, _ARCH)

    assert plan == {"feasible": None, "note": "parse-failed"}
