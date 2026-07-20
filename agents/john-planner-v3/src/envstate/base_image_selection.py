"""Base-image selection facade: pick a candidate, then pin its python minor.

Composes two existing pieces into the single decision the conductor needs:

* :class:`src.image_selector.ImageSelector` — LLM picks one image from a
  language handler's candidate list (ported from the pruned agent path).
* :func:`src.envstate.runtime_base.resolve_runtime_base` — reads
  ``requires-python`` and pins the chosen tag's python minor.

Returns ONE :class:`BaseImageChoice` whose ``minor`` is meant to flow into BOTH
the container tag AND ``build_dep_graph(target_python=...)`` (one decision, two
consumers). An explicit override is honored verbatim (no LLM, no rewrite). Any
selection failure degrades to ``python:{DEFAULT_MINOR}-slim`` so a run never
dies on image selection.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from src.image_selector import ImageSelector
from src.envstate.runtime_base import (
    DEFAULT_MINOR,
    _base_image_minor,
    resolve_runtime_base,
)

logger = logging.getLogger(__name__)

_DEFAULT_IMAGE = f"python:{DEFAULT_MINOR}-slim"

# Matches a BARE `python:X.Y` or `python:X.Y.Z` tag with no variant suffix
# (no `-slim`, `-bookworm`, `-alpine`, etc. and no trailing `latest`/garbage).
_BARE_PYTHON_TAG = re.compile(r"^python:\d+\.\d+(?:\.\d+)?$")


def _ensure_slim(image: str) -> str:
    """Append ``-slim`` to a BARE ``python:X.Y[.Z]`` tag; unchanged otherwise.

    The AUTO path (``ImageSelector`` + ``resolve_runtime_base``) only rewrites
    the python minor of whatever base the selector proposed — it never adds a
    variant. Left alone, a bare candidate like ``python:3.10`` resolves to the
    ~1GB buildpack-deps image (gcc + every ``-dev`` header preinstalled), which
    pre-satisfies the graph's System-tier discovery and kills its signal. Any
    image that already has a variant suffix (``-slim``, ``-bookworm``,
    ``-alpine``, ...), isn't a recognizable ``X.Y`` tag (``python:latest``), or
    isn't a ``python:`` base at all is returned unchanged.
    """
    if _BARE_PYTHON_TAG.match(image):
        return f"{image}-slim"
    return image


@dataclass(frozen=True)
class BaseImageChoice:
    """The base-image decision: the tag to boot, the python minor to resolve
    against, an optional docker ``--platform`` override, and provenance."""

    image: str
    minor: str
    platform_override: str | None
    reason: str


def choose_base_image(
    repo_path: str,
    client,
    model: str,
    *,
    explicit: str | None = None,
    log_dir: str | None = None,
) -> BaseImageChoice:
    """Select + pin the base image for ``repo_path``.

    ``explicit`` (any value other than ``None``) is honored verbatim — no LLM,
    no tag rewrite; ``minor`` is read from the tag (or ``DEFAULT_MINOR``). When
    ``explicit`` is ``None`` the LLM ``ImageSelector`` picks a candidate and
    ``resolve_runtime_base`` pins its minor against ``requires-python``. Never
    raises: on any failure returns the default ``python:{DEFAULT_MINOR}-slim``.
    """
    if explicit is not None:
        minor = _base_image_minor(explicit) or DEFAULT_MINOR
        return BaseImageChoice(explicit, minor, None, f"explicit override: {explicit}")

    try:
        selector = ImageSelector(client, model=model)
        selected, _handler, _docs, platform_override = selector.select_base_image(
            repo_path, log_dir=log_dir
        )
        decision = resolve_runtime_base(repo_path, selected)
        image = _ensure_slim(decision.base_image)
        normalized_note = "" if image == decision.base_image else f" -> normalized to {image!r}"
        return BaseImageChoice(
            image=image,
            minor=decision.minor,
            platform_override=platform_override,
            reason=(
                f"auto: selected {selected!r} -> pinned {decision.base_image!r} "
                f"({decision.reason}){normalized_note}"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — selection must never break a run
        logger.warning("base-image selection unavailable, using default: %s", exc)
        return BaseImageChoice(_DEFAULT_IMAGE, DEFAULT_MINOR, None, f"degraded fallback: {exc}")
