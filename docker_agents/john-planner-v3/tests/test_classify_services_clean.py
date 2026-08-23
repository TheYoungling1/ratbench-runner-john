"""CR9 (Inc 5A): deterministic Service + Config classifier (NO live LLM / NO network).

`classify_services_clean` reads the repo, translates each declared compose service to
a CLEAN setup-shape Service node (via the shipped `translate_service`), repoints config
DSNs into `setup["bind"]`, and emits advisory Config hint nodes — all admitted through
the pure `patch_gate`. It is ADDITIVE/INERT: nothing wires it in yet (CR10 does).

These tests monkeypatch the module's `translate_service` to return canned results (so no
client is ever called) and materialize a fixture repo under `tmp_path`.
"""
import sys
from collections import namedtuple
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
for _p in (str(_ROOT), str(_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from python_deps.depgraph.schema import DepGraph, NodeType, State
from python_deps.depgraph.service_recipes import render_probe_poll

import src.envstate.classify_services_clean as csc
from src.envstate.classify_services_clean import (
    classify_services_clean, make_construction_classifier)

_ARCH = {"dpkg": "arm64", "uname": "aarch64"}

# The canned setup a known-kind redis translation returns (probe already read-only).
_REDIS_SETUP = {
    "install": ["apt-get update",
                "DEBIAN_FRONTEND=noninteractive apt-get install -y redis-server"],
    "start": "redis-server --daemonize yes",
    "probe": "nc -z 127.0.0.1 6379",
    "createdb": None,
    "post": [],
    "bind": [],
}


def _redis_result(**over):
    """A `translate_service` return dict for the redis service (override any key)."""
    setup = over.pop("setup", dict(_REDIS_SETUP))
    res = {"service_name": "cache", "kind": "redis", "route": "known",
           "feasible": True, "setup": setup, "verify": None, "note": "known-kind"}
    res.update(over)
    return res


def _fake_ok(client, model, spec, arch):
    return _redis_result()


def _fake_setup_none(client, model, spec, arch):        # parse-failed sentinel
    return _redis_result(setup=None, feasible=False, note="parse-failed")


def _fake_empty_probe(client, model, spec, arch):       # empty-probe guard target
    return _redis_result(setup={**_REDIS_SETUP, "probe": ""})


def _fake_raises(client, model, spec, arch):
    raise RuntimeError("boom")


def _make_repo(tmp_path):
    """A repo with one redis compose service, a DSN in .env.example, and a .py env read."""
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  cache:\n"
        "    image: redis:7\n"
        "    ports:\n"
        "      - '6379:6379'\n"
    )
    (tmp_path / ".env.example").write_text("CACHE_URL=redis://redis:6379/0\n")
    (tmp_path / "app.py").write_text(
        "import os\n"
        "CACHE_URL = os.environ['CACHE_URL']\n"
    )
    return str(tmp_path)


def test_service_node_admitted(tmp_path, monkeypatch):
    monkeypatch.setattr(csc, "translate_service", _fake_ok)
    out = classify_services_clean(DepGraph(), _make_repo(tmp_path),
                                  client=None, model="m", arch=_ARCH)
    node = out.get("service:cache")
    assert node is not None
    assert node.type is NodeType.SERVICE
    assert node.state is State.MISSING                              # host certify owns SATISFIED
    assert node.data.get("setup") is not None
    assert node.check_command == render_probe_poll("nc -z 127.0.0.1 6379")  # probe-poll check
    # CLEAN shape emits NONE of the legacy keys.
    assert "service_confidence" not in node.data
    assert "binding" not in node.data


def test_repoint_attached_to_setup_bind(tmp_path, monkeypatch):
    monkeypatch.setattr(csc, "translate_service", _fake_ok)
    out = classify_services_clean(DepGraph(), _make_repo(tmp_path),
                                  client=None, model="m", arch=_ARCH)
    node = out.get("service:cache")
    assert node is not None
    assert "export CACHE_URL=redis://127.0.0.1:6379/0" in node.data["setup"]["bind"]


def test_parse_failed_service_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(csc, "translate_service", _fake_setup_none)
    out = classify_services_clean(DepGraph(), _make_repo(tmp_path),
                                  client=None, model="m", arch=_ARCH)
    assert out.get("service:cache") is None                        # setup=None -> not admitted


def test_empty_probe_service_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(csc, "translate_service", _fake_empty_probe)
    out = classify_services_clean(DepGraph(), _make_repo(tmp_path),
                                  client=None, model="m", arch=_ARCH)
    assert out.get("service:cache") is None                        # empty probe -> never admitted


def test_config_node_emitted(tmp_path, monkeypatch):
    monkeypatch.setattr(csc, "translate_service", _fake_ok)
    out = classify_services_clean(DepGraph(), _make_repo(tmp_path),
                                  client=None, model="m", arch=_ARCH)
    cfg = out.get("config:CACHE_URL")
    assert cfg is not None
    assert cfg.type is NodeType.CONFIG
    assert cfg.data.get("promotion") == "hint"                     # advisory hint, never scheduled
    assert cfg.check_command is None


def test_per_service_translate_error_is_isolated_not_batch_fatal(tmp_path, monkeypatch):
    # A translate failure for ONE service must skip only that service, never discard
    # the whole batch. (Before the 2026-07-06 e2e fix, a single translate error
    # unwound classify entirely and returned the input graph — silently dropping the
    # config node and any sibling service that translated fine.) It still never
    # crashes; it now degrades per-service.
    monkeypatch.setattr(csc, "translate_service", _fake_raises)
    g = DepGraph()
    out = classify_services_clean(g, _make_repo(tmp_path),
                                  client=None, model="m", arch=_ARCH)
    assert isinstance(out, DepGraph)                 # best-effort: never raises
    assert out.get("service:cache") is None          # the failing service is skipped
    assert out.get("config:CACHE_URL") is not None   # OTHER nodes survive the per-service error


def test_service_nodes_skips_app_build_services_no_client(tmp_path, monkeypatch):
    # Real-repo shape: app services (web/worker = `build:`-only; frontend = `build:`
    # PLUS an `image:` tag) alongside backing services (postgres/redis = pulled
    # images). The app services must be skipped BEFORE translate_service (proven by
    # the spy) so they never reach the exotic LLM path, while the known-kind backing
    # services still produce Service nodes deterministically with client=None.
    (tmp_path / "compose.yaml").write_text(
        "services:\n"
        "  web:\n"
        "    build: .\n"
        "  worker:\n"
        "    build: .\n"
        "    command: celery worker\n"
        "  frontend:\n"                       # build: + image: tag -> still the app
        "    build: ./frontend\n"
        "    image: 'myapp/frontend:latest'\n"
        "  postgres:\n"
        "    image: 'postgres:16'\n"
        "    environment:\n"
        "      POSTGRES_USER: app\n"
        "      POSTGRES_PASSWORD: secret\n"
        "  redis:\n"
        "    image: 'redis:7'\n"
    )
    translated: list[str] = []
    _real = csc.translate_service

    def _spy(client, model, spec, arch):
        translated.append(spec.service_name)
        return _real(client, model, spec, arch)
    monkeypatch.setattr(csc, "translate_service", _spy)

    Hit = namedtuple("Hit", "evidence_id kind name")
    hit = Hit("ev-svc", "import", "psycopg2")
    nodes = csc._service_nodes(str(tmp_path), _ARCH, None, "", [hit], [])
    names = {n.name for n in nodes}

    assert {"postgres", "redis"} <= names                  # backing services -> nodes (no client)
    assert names.isdisjoint({"web", "worker", "frontend"})  # all app services dropped
    # proven skipped BEFORE translate (not merely caught by the per-service except after):
    assert set(translated) == {"postgres", "redis"}         # incl. the build+image app never translated


def test_never_crashes(tmp_path, monkeypatch):
    # The whole classify body is still wrapped best-effort: a repo-read/collect error
    # (not a per-service translate error) returns the input graph unchanged.
    monkeypatch.setattr(csc, "collect_static_evidence",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    g = DepGraph()
    out = classify_services_clean(g, _make_repo(tmp_path),
                                  client=None, model="m", arch=_ARCH)
    assert out is g                                                 # best-effort: error -> input graph


def test_make_construction_classifier_returns_callable(tmp_path, monkeypatch):
    """The Inc 5B entrypoint (replaces env_classifier.make_construction_classifier):
    returns a classify(graph, repo_path) closure that runs the deterministic classifier.
    A known-kind service needs no live client (translate_service is monkeypatched)."""
    monkeypatch.setattr(csc, "translate_service", _fake_ok)
    classify = make_construction_classifier(client=None, model="m",
                                            arch={"dpkg": "amd64", "uname": "x86_64"})
    assert callable(classify)
    out = classify(DepGraph(), _make_repo(tmp_path))
    assert isinstance(out, DepGraph)
    assert out.get("service:cache") is not None                    # wired through to the classifier
