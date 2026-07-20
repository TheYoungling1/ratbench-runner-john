"""Provider dispatch: pick the ecosystem whose ``detect`` wins above threshold."""

from __future__ import annotations

from typing import Sequence

from ecosystems.base import EcosystemProvider


def select_provider(
    repo: str,
    providers: Sequence[EcosystemProvider],
    *,
    threshold: float = 0.5,
    default: EcosystemProvider | None = None,
) -> EcosystemProvider:
    """Return the highest-confidence provider whose ``detect(repo) >= threshold``.

    Ties are broken by registration order (first wins). When NO provider clears the
    threshold: return ``default`` if one was supplied, else raise ``LookupError``.
    The ``default`` seam is load-bearing for zero-impact — the pre-seam
    ``build_dep_graph`` refused NO repo, so Task 7 passes the ``PythonProvider``
    instance as ``default`` and a degenerate/manifest-less repo still dispatches to
    Python instead of newly raising ``LookupError``.
    """
    best: EcosystemProvider | None = None
    best_score = -1.0
    for provider in providers:
        score = provider.detect(repo)
        if score >= threshold and score > best_score:
            best = provider
            best_score = score
    if best is None:
        if default is not None:
            return default
        raise LookupError(f"no ecosystem provider detected for {repo!r}")
    return best


# Registered providers, dispatch order = tie-break order. Rust/Node append here in
# Slices 2/3. Imported at module load; safe because ``build.py`` never imports
# ``ecosystems`` at module level (only ``build_dep_graph`` does, lazily).
from ecosystems.python.provider import PythonProvider  # noqa: E402

PROVIDERS: tuple = (PythonProvider(),)
