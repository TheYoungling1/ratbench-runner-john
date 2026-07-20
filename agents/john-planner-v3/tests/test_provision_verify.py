# tests/test_provision_verify.py
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

import urllib.error

from src.envstate.provision_verify import verify_plan


def test_no_urls_all_ok():
    # No download URL anywhere -> nothing to check, no network touched.
    out = verify_plan({"install": ["apt-get install -y redis-server"],
                       "start": "redis-server"})
    assert out["n"] == 0
    assert out["all_ok"] is True
    assert out["urls"] == []


def test_loopback_skipped():
    # A loopback/runtime URL is filtered out before any check runs.
    out = verify_plan({"install": ["curl http://127.0.0.1:9000/x"]})
    assert out["n"] == 0
    assert out["all_ok"] is True


def test_good_url_ok(monkeypatch):
    class _Resp:
        status = 200
        def getcode(self):
            return 200
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    out = verify_plan({"install": ["wget https://example.com/app.tar.gz"]})
    assert out["n"] == 1
    assert out["all_ok"] is True
    assert out["urls"][0]["state"] == "ok"
    assert out["urls"][0]["status"] == 200


def test_bad_url_fail(monkeypatch):
    def _fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            "https://example.com/nope.tar.gz", 404, "NF", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    out = verify_plan({"install": ["wget https://example.com/nope.tar.gz"]})
    assert out["n"] == 1
    assert out["all_ok"] is False
    assert out["urls"][0]["state"] == "bad"


def test_never_raises(monkeypatch):
    def _fake_urlopen(req, timeout=None):
        raise ConnectionError("boom")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    out = verify_plan({"install": ["wget https://example.com/app.tar.gz"]})
    assert isinstance(out, dict)
    assert out["n"] == 1
    assert out["all_ok"] is False
    assert out["urls"][0]["state"] == "error"
