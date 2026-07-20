"""Phase-A coverage oracle — which top-level import names the RESOLVED closure
provides, read from wheel RECORD metadata (NOT a post-install snapshot).

The repair fixpoint's "is this import satisfied?" test unions the top-level
module names that the RESOLVED package nodes' wheels ship, obtained through an
INJECTED ``RecordProvider`` (:mod:`python_deps.depgraph.repair`). Reading RECORD
metadata rather than ``packages_distributions()`` is the whole point of
Correction 3: a package that RESOLVED but FAILED TO BUILD is still counted
PROVIDED here (its wheel RECORDs the module), so a build failure is a Phase-B
gap and is never misrouted to Phase-A under-declaration repair.

``resolved_record_coverage`` is pure (no Executor, no network); it is the piece
every fixpoint test exercises with an injected FAKE provider.
:func:`default_record_provider` is the cheap post-install seam;
:func:`pypi_record_provider` is the PRE-install PyPI wheel reader (P1.5) that
grounds not-yet-installed repair candidates; :func:`composite_record_provider`
layers them (installed short-circuits the candidate read) and is the production
default wired in ``build.py`` — so real builds actually repair under-declarations.
The single network path (``_default_wheel_top_levels``) sits behind an INJECTED
``fetch`` seam (mirroring ``pins._default_fetch``); unit tests always pass a fake.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import urllib.request
import zipfile

from python_deps.depgraph.executor import Executor
from python_deps.depgraph.relink import PACKAGES_DIST_CMD, parse_packages_distributions
from python_deps.depgraph.repair import RecordProvider
from python_deps.depgraph.schema import Node, NodeType, State
from python_deps.import_mapping import normalize_package_name


def resolved_record_coverage(
    pkg_nodes: list[Node], record_provider: RecordProvider
) -> set[str]:
    """The lowercased UNION of top-level module names the RESOLVED packages ship.

    Iterates the ``Package`` nodes (skipping resolver diagnostic ``MISSING``
    placeholders, which have no wheel to read), asks the injected
    ``record_provider`` for each dist's top-level modules, and unions everything
    non-``None`` (a ``None`` return — no wheel / sdist-only / unknown — contributes
    nothing and falls through to the install/import backstop). Pure: no Executor,
    no network; the provider is the sole source of truth.
    """
    provided: set[str] = set()
    for node in pkg_nodes:
        if node.type is not NodeType.PACKAGE or node.state is State.MISSING:
            continue
        modules = record_provider(node.name)
        if modules:
            provided |= {module.lower() for module in modules}
    return provided


def default_record_provider(container_executor: Executor) -> RecordProvider:
    """Production ``RecordProvider`` — INTERIM post-install container dist-info.

    Builds ``dist -> {top-level modules}`` by inverting the container's
    ``importlib.metadata.packages_distributions()`` (run once, memoized, no
    network). A dist absent from the installed environment returns ``None``.

    KNOWN LIMITATION (why it is not used ALONE): this reads POST-INSTALL state, so
    (a) a resolved-but-failed-to-build dist reports ``None`` here rather than its
    RECORD modules, and (b) a not-yet-installed repair CANDIDATE also reports
    ``None`` — meaning ``choose_provider`` can never ``confirm`` a candidate from
    THIS provider alone, so repair driven by it in isolation is inert. P1.5 closes
    that gap: :func:`composite_record_provider` layers this cheap installed reader
    UNDER :func:`pypi_record_provider` (the PRE-install PyPI wheel read), and the
    composite — not this provider bare — is the production default in ``build.py``.
    This provider stays the fast path for already-installed closure members (no
    PyPI call); the composite falls through to the pre-install reader only when it
    is blind (candidates, failed builds).
    """
    cache: dict[str, set[str]] = {}
    built = {"done": False}

    def provider(dist: str) -> "set[str] | None":
        if not built["done"]:
            built["done"] = True
            result = container_executor.run(PACKAGES_DIST_CMD)
            if result.ok:
                for module, dists in parse_packages_distributions(result.stdout).items():
                    for owner in dists:
                        cache.setdefault(normalize_package_name(owner), set()).add(module)
        return cache.get(normalize_package_name(dist))

    return provider


# --------------------------------------------------------------------------- #
# P1.5 — PRE-install PyPI wheel-metadata provider (makes production repair work)
# --------------------------------------------------------------------------- #
# The single network path in this module: read a candidate dist's wheel top-level
# modules straight from PyPI, BEFORE anything is installed, so ``choose_provider``
# can confirm a not-yet-installed repair candidate. Isolated behind an INJECTED
# ``fetch`` seam (exactly like ``pins._default_fetch``) so unit tests never reach
# the socket — they pass a fake ``fetch``. Ports the validated spike chain in
# ``src/eval/graph_fidelity/underdeclaration_repair_poc.py``.
_PYPI_JSON = "https://pypi.org/pypi/{dist}/json"
_HTTP_TIMEOUT = 20
_MAX_WHEEL_BYTES = 20 * 1024 * 1024
_UA = {"User-Agent": "depgraph-record-provider/0.1 (+local)"}


def _http_json(url: str) -> "dict | None":
    """GET ``url`` and parse JSON, or ``None`` on any failure (best-effort)."""
    try:
        with urllib.request.urlopen(  # noqa: S310 — fixed https PyPI host
            urllib.request.Request(url, headers=_UA), timeout=_HTTP_TIMEOUT
        ) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — absence/error -> None, never raise
        return None


def _find_wheel(data: dict) -> "tuple[str | None, int]":
    """Pick a ``bdist_wheel`` ``(url, size)`` from a PyPI JSON payload.

    Prefers the latest release's ``urls``; falls back to scanning ``releases``
    newest-first. Returns ``(None, 0)`` when the dist ships no wheel (sdist-only).
    """
    for f in data.get("urls", []):
        if f.get("packagetype") == "bdist_wheel":
            return f.get("url"), f.get("size") or 0
    for _ver, files in reversed(list(data.get("releases", {}).items())):
        for f in files:
            if f.get("packagetype") == "bdist_wheel":
                return f.get("url"), f.get("size") or 0
    return None, 0


def _wheel_top_levels(path: str) -> set[str]:
    """Lowercased top-level module names a downloaded wheel ships.

    Reads ``*.dist-info/top_level.txt`` when present, else infers from the first
    path segment of each real (non-metadata) member (RECORD inference), matching
    the spike's ``_wheel_top_levels``.
    """
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        top_level = [n for n in names if n.endswith(".dist-info/top_level.txt")]
        if top_level:
            return {
                token.strip().lower()
                for token in archive.read(top_level[0]).decode("utf-8", "replace").split()
                if token.strip()
            }
        tops: set[str] = set()
        for name in names:  # RECORD-inference: first path segment of real files
            if ".dist-info/" in name or ".data/" in name:
                continue
            segment = name.split("/")[0].split(".")[0]  # strip .py/.cpython-*.so
            if segment and not segment.startswith("__"):
                tops.add(segment.lower())
        return tops


def _default_wheel_top_levels(dist: str) -> "set[str] | None":
    """Real PyPI wheel read — the ONLY network code here; tests inject a fake.

    ``/{dist}/json`` -> pick a ``bdist_wheel`` -> download (skipping wheels over
    ``_MAX_WHEEL_BYTES``) -> return :func:`_wheel_top_levels`. ``None`` on any
    absence/failure (not on PyPI, sdist-only, too big, network error), which the
    grounding layer reads as ``blind``.
    """
    data = _http_json(_PYPI_JSON.format(dist=dist))
    if data is None:
        return None
    url, size = _find_wheel(data)
    if not url:
        return None
    if size and size > _MAX_WHEEL_BYTES:
        return None
    tmp = tempfile.mkdtemp(prefix="rec-whl-")
    try:
        path = os.path.join(tmp, "w.whl")
        with urllib.request.urlopen(  # noqa: S310 — url is a PyPI-hosted wheel
            urllib.request.Request(url, headers=_UA), timeout=_HTTP_TIMEOUT
        ) as response, open(path, "wb") as handle:
            shutil.copyfileobj(response, handle)
        return _wheel_top_levels(path)
    except Exception:  # noqa: BLE001 — failure -> None (blind), never raise
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def pypi_record_provider(*, fetch=_default_wheel_top_levels) -> RecordProvider:
    """PRE-install ``RecordProvider`` reading a candidate dist's wheel top-levels.

    Given a dist NAME, the injected ``fetch`` returns the lowercased set of
    top-level modules its (latest compatible) wheel ships, or ``None`` when
    unavailable (not on PyPI / no wheel / sdist-only). Keyed by NAME only, by
    design: a repair CANDIDATE has no resolved version yet, and a dist's top-level
    module names are stable across versions — so ``fetch`` queries ``/{name}/json``
    (latest). This is the piece the post-install :func:`default_record_provider`
    cannot supply, so ``choose_provider`` can now ``confirm`` a not-yet-installed
    candidate. ``fetch`` is the INJECTED network seam (mirrors
    ``pins._default_fetch``); unit tests pass a fake, so no test reaches PyPI.
    Cached per dist (canon-keyed): each dist's ``fetch`` runs at most once and a
    ``None`` answer is cached too (a blind dist is never re-queried).
    """
    cache: dict[str, "set[str] | None"] = {}

    def provider(dist: str) -> "set[str] | None":
        key = normalize_package_name(dist)
        if key not in cache:
            cache[key] = fetch(dist)
        return cache[key]

    return provider


def composite_record_provider(
    installed_provider: RecordProvider, candidate_provider: RecordProvider
) -> RecordProvider:
    """Cheap post-install coverage first; PyPI candidate read only when blind.

    For a dist, consult ``installed_provider`` (the memoized post-install
    container dist-info — free, network-less). If it returns a non-``None`` set,
    use it and NEVER call ``candidate_provider``. Only when the installed provider
    is blind (``None`` — a not-yet-installed repair candidate, or a
    resolved-but-failed-to-build dist) does it fall through to
    ``candidate_provider`` (the PRE-install PyPI wheel read). So candidates and
    failed builds get a real pre-install answer while already-installed closure
    members stay network-free (the short-circuit). Cached per dist (canon-keyed)
    so a dist is neither re-probed nor re-fetched.
    """
    cache: dict[str, "set[str] | None"] = {}

    def provider(dist: str) -> "set[str] | None":
        key = normalize_package_name(dist)
        if key in cache:
            return cache[key]
        provided = installed_provider(dist)
        if provided is None:
            provided = candidate_provider(dist)
        cache[key] = provided
        return provided

    return provider
