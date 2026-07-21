# bench/languages/__init__.py
"""Per-language measure strategies. Each Language supplies shell-command strings only; measure()
owns Docker and the status taxonomy. get_language() defaults to Python so existing rows are unchanged."""
from __future__ import annotations

from bench.languages.base import Language
from bench.languages.python import PythonLanguage

_REGISTRY = {
    "python": PythonLanguage(),
}


def get_language(name):
    """Return the Language for `name`, defaulting to Python for unknown/empty names."""
    return _REGISTRY.get((name or "python").lower(), _REGISTRY["python"])


__all__ = ["Language", "get_language", "PythonLanguage"]
