import os, tempfile
from src.envstate import base_image_selection as bis
from src.envstate.base_image_selection import BaseImageChoice, choose_base_image


def _repo(requires_python: str | None) -> str:
    d = tempfile.mkdtemp()
    body = "[project]\nname='x'\n"
    if requires_python is not None:
        body += f"requires-python='{requires_python}'\n"
    with open(os.path.join(d, "pyproject.toml"), "w") as fh:
        fh.write(body)
    return d


def test_explicit_override_is_verbatim_no_llm(monkeypatch):
    # If the selector is ever constructed here, fail loudly — explicit must skip it.
    monkeypatch.setattr(bis, "ImageSelector",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM used")))
    choice = choose_base_image(_repo(">=3.10"), client=object(), model="m",
                               explicit="python:3.12-slim")
    assert choice.image == "python:3.12-slim"
    assert choice.platform_override is None
    assert "explicit" in choice.reason.lower()


def test_auto_selects_then_pins_minor(monkeypatch):
    class _FakeSelector:
        def __init__(self, *a, **k): pass
        def select_base_image(self, repo_path, **k):
            # selector picks 3.13; requires-python says <3.11, so the pin must clamp.
            return "python:3.13-slim", object(), "docs", None
    monkeypatch.setattr(bis, "ImageSelector", _FakeSelector)
    choice = choose_base_image(_repo(">=3.10,<3.11"), client=object(), model="m", explicit=None)
    assert choice.minor == "3.10"                 # clamped into requires-python
    assert choice.image == "python:3.10-slim"     # tag rewritten to the pinned minor
    assert isinstance(choice, BaseImageChoice)


def test_auto_threads_platform_override(monkeypatch):
    class _FakeSelector:
        def __init__(self, *a, **k): pass
        def select_base_image(self, repo_path, **k):
            return "python:3.11-slim", object(), "docs", "linux/amd64"
    monkeypatch.setattr(bis, "ImageSelector", _FakeSelector)
    choice = choose_base_image(_repo(">=3.11"), client=object(), model="m")
    assert choice.platform_override == "linux/amd64"


def test_selection_failure_degrades_to_default(monkeypatch):
    class _BoomSelector:
        def __init__(self, *a, **k): pass
        def select_base_image(self, repo_path, **k): raise RuntimeError("LLM down")
    monkeypatch.setattr(bis, "ImageSelector", _BoomSelector)
    choice = choose_base_image(_repo(">=3.10"), client=object(), model="m")
    assert choice.image == "python:3.11-slim"     # DEFAULT fallback
    assert "degraded" in choice.reason.lower() or "fallback" in choice.reason.lower()


def test_auto_bare_tag_is_normalized_to_slim(monkeypatch):
    class _FakeSelector:
        def __init__(self, *a, **k): pass
        def select_base_image(self, repo_path, **k):
            # bare tag, no variant — must not reach the caller un-slimmed.
            return "python:3.10", object(), "docs", None
    monkeypatch.setattr(bis, "ImageSelector", _FakeSelector)
    choice = choose_base_image(_repo(">=3.10"), client=object(), model="m", explicit=None)
    assert choice.image == "python:3.10-slim"


def test_auto_already_slim_tag_is_not_double_suffixed(monkeypatch):
    class _FakeSelector:
        def __init__(self, *a, **k): pass
        def select_base_image(self, repo_path, **k):
            return "python:3.11-slim", object(), "docs", None
    monkeypatch.setattr(bis, "ImageSelector", _FakeSelector)
    choice = choose_base_image(_repo(">=3.11"), client=object(), model="m", explicit=None)
    assert choice.image == "python:3.11-slim"     # NOT "python:3.11-slim-slim"


def test_explicit_bare_tag_is_honored_verbatim_not_normalized(monkeypatch):
    # If the selector is ever constructed here, fail loudly — explicit must skip it.
    monkeypatch.setattr(bis, "ImageSelector",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM used")))
    choice = choose_base_image(_repo(">=3.10"), client=object(), model="m",
                               explicit="python:3.12")
    assert choice.image == "python:3.12"          # NOT normalized to "-slim"
