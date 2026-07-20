# src/envstate/runtime_base.py
"""Runtime-tier base-image selection (pure).

Two responsibilities, both side-effect-free:

* :func:`choose_python_minor` — turn a project's declared python constraint
  (PEP 621 ``requires-python`` or poetry ``^``/``~`` form) into ONE concrete
  minor (e.g. ``"3.10"``). Policy v2: defer to the rich ImageSelector's chosen
  python (``prefer``); only clamp to satisfy ``requires-python``. Without a
  ``prefer``, keep ``default`` if it satisfies, else the nearest supported minor
  to ``default`` within the constraint.
* :func:`pin_base_python` — rewrite the python component of a ``python:X`` base
  tag to a chosen minor, preserving the OS/variant suffix the Synthesizer chose.

Undeclared / unparseable / out-of-range constraints fall back to ``prefer`` (if
given) or ``default`` so the feature is byte-identical to today when it can't
improve on the guess.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from src.envstate.manifest import read_requires_python

# Ascending order — policy searches within this set.
SUPPORTED_MINORS: tuple[str, ...] = ("3.9", "3.10", "3.11", "3.12", "3.13")
DEFAULT_MINOR = "3.11"

_VERSION_TOKEN = re.compile(r"^\d+(?:\.\d+)*$")


def _normalize_constraint(raw: str) -> str:
    """Normalize poetry ``^``/``~`` shorthand to a PEP 440 specifier string.

    ``^3.10`` -> ``">=3.10,<4.0"``;  ``~3.10`` -> ``">=3.10,<3.11"``. Anything
    else (already a PEP 440 specifier) is returned unchanged for SpecifierSet.
    """
    raw = raw.strip()
    if raw.startswith("^"):
        ver = raw[1:].strip()
        major = ver.split(".")[0]
        return f">={ver},<{int(major) + 1}.0"
    if raw.startswith("~"):
        ver = raw[1:].strip()
        parts = ver.split(".")
        if len(parts) >= 2:
            return f">={ver},<{parts[0]}.{int(parts[1]) + 1}"
        return f">={ver},<{int(parts[0]) + 1}"
    return raw


def _base_image_minor(base_image: str) -> str | None:
    """The python minor already in a ``python:X.Y...`` tag (the ImageSelector's chosen
    base), or None when the base is not a recognizable supported ``python:X.Y`` tag."""
    name, sep, tag = base_image.rpartition(":")
    if not sep or "/" in tag or name.split("/")[-1] != "python":
        return None
    m = re.match(r"^(\d+\.\d+)", tag.split("-")[0])
    minor = m.group(1) if m else None
    return minor if minor in SUPPORTED_MINORS else None


def _nearest_supported(satisfying: list[str], anchor: str) -> str:
    """The satisfying minor closest to ``anchor`` by position in SUPPORTED_MINORS
    (ties -> the higher minor)."""
    idx = {m: i for i, m in enumerate(SUPPORTED_MINORS)}
    a = idx.get(anchor, idx[DEFAULT_MINOR])
    return min(satisfying, key=lambda m: (abs(idx[m] - a), -idx[m]))


def choose_python_minor(
    requires_python: str | None,
    default: str = DEFAULT_MINOR,
    *,
    prefer: str | None = None,
) -> tuple[str, str]:
    """Return ``(minor, reason)``. The pin sits ON TOP of the rich ImageSelector;
    ``prefer`` is the minor the selector already chose. Policy:
      * keep ``prefer`` when it satisfies ``requires_python`` (common case — never
        override a good rich choice);
      * clamp ``prefer`` to the nearest supported satisfying minor when it violates
        the constraint (LLM out-of-bounds guard);
      * with no ``prefer``: ``default`` if it satisfies, else the nearest supported
        minor to ``default`` within the constraint;
      * ``default`` when nothing is declared or nothing supported satisfies.
    Never raises.
    """
    if not requires_python or not requires_python.strip():
        if prefer:
            return prefer, f"no requires-python; kept selector base {prefer}"
        return default, "no requires-python declared; default"
    raw = requires_python.strip()
    try:
        spec = SpecifierSet(_normalize_constraint(raw))
    except Exception:
        if prefer:
            return prefer, f"unparseable requires-python {raw!r}; kept selector base {prefer}"
        return default, f"unparseable requires-python {raw!r}; default"
    satisfying = [m for m in SUPPORTED_MINORS
                  if spec.contains(Version(f"{m}.0"), prereleases=False)]
    if not satisfying:
        return default, f"no supported minor satisfies {raw!r}; default"
    if prefer and prefer in satisfying:
        return prefer, f"kept selector base {prefer} (satisfies {raw!r})"
    if prefer:
        chosen = _nearest_supported(satisfying, prefer)
        return chosen, f"clamped selector base {prefer} into {raw!r} -> {chosen}"
    if default in satisfying:
        return default, f"default {default} satisfies {raw!r}"
    chosen = _nearest_supported(satisfying, default)
    return chosen, f"nearest supported to default within {raw!r} -> {chosen}"


def pin_base_python(base_image: str, minor: str) -> str:
    """Rewrite a ``python:X`` base tag's version to ``minor``, keeping the variant.

    ``python:3.11-slim`` + ``"3.10"`` -> ``python:3.10-slim``. Non-python bases
    (``ubuntu:22.04``) and python tags without a recognizable version component
    (``python:latest``) are returned unchanged — conservative: never make a base
    worse than the Synthesizer's choice.
    """
    name, sep, tag = base_image.rpartition(":")
    if not sep or "/" in tag:
        # no tag (bare name, or the ":" belonged to a registry:port)
        return base_image
    if name.split("/")[-1] != "python":
        return base_image
    tokens = tag.split("-")
    if not _VERSION_TOKEN.match(tokens[0]):
        return base_image
    tokens[0] = minor
    return f"{name}:{'-'.join(tokens)}"


@dataclass(frozen=True)
class RuntimeBaseDecision:
    """The Runtime-tier decision for one repo: the concrete python minor to
    target, the (possibly pinned) base image, and provenance for advise/logs.
    """

    minor: str            # concrete python minor, e.g. "3.10" — the resolve target
    base_image: str       # base image after pinning (unchanged when not pinnable)
    reason: str           # why this minor (which choose_python_minor branch fired)
    requires_python: str | None  # the raw declared constraint, or None


def resolve_runtime_base(
    repo_path: str, base_image: str, *, default: str = DEFAULT_MINOR
) -> RuntimeBaseDecision:
    """Derive the Runtime-tier base decision for ``repo_path`` (the seam entry).

    Reads the declared python constraint, picks a concrete minor, and pins the
    base tag to it. The same ``minor`` is meant to flow into both the ``FROM``
    line (``base_image``) and ``build_dep_graph(target_python=...)``. Pure apart
    from the manifest read; never raises.
    """
    requires_python = read_requires_python(repo_path)
    prefer = _base_image_minor(base_image)
    minor, reason = choose_python_minor(requires_python, default=default, prefer=prefer)
    return RuntimeBaseDecision(
        minor=minor,
        base_image=pin_base_python(base_image, minor),
        reason=reason,
        requires_python=requires_python,
    )


def screen_runtime_pin(repo_path: str, default: str = DEFAULT_MINOR) -> dict:
    """Pure pre-screen: what would the pin do for this repo, no container needed.

    ``base_would_change`` approximates "the live base would differ from the
    selector's usual default" as ``declared AND chosen != default``. It is a
    go/no-go signal for the A/B, not ground truth (the real selector may pick a
    non-default minor); e2e confirms.
    """
    requires_python = read_requires_python(repo_path)
    minor, reason = choose_python_minor(requires_python, default=default)
    return {
        "requires_python": requires_python,
        "would_pin_to": minor,
        "reason": reason,
        "base_would_change": requires_python is not None and minor != default,
    }
