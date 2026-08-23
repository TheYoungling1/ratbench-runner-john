"""PEP 725 ``[external]`` reader — declared OS build-deps -> ObservedNeed (host-side).

Source-priority #1 of the system-library-detection design (spec §2): a package MAY
declare the external OS libraries/tools its build needs in ``pyproject.toml``'s
``[external]`` table using DepURL identifiers (PEP 725). This module fetches that
table from the sdist, parses the DepURLs, and maps the confident ones onto capability
``ObservedNeed``s that feed the existing ``os_resolver`` / ``reconcile_predicted``
machinery (predict and observe collapse on ``capability_id``).

Adoption is ~zero today (PEP 725 is draft; few packages ship ``[external]``), so
``pep725_external`` almost always returns ``[]`` — the common, correct path. It is
wired for the future: as adoption grows it becomes the authoritative upstream-declared
source, and PEP 804's registry (``registry.json`` / ``known-ecosystems.json``) will
supply the DepURL->apt name mapping we currently curate by hand.

DepURL grammar (PEP 725): ``dep:type/namespace/name@version?qualifiers#subpath`` with an
optional trailing PEP 508 marker (``; platform_system=='Linux'``); ``type`` is a PURL
type or the literal ``virtual``. We read ``build-requires`` + ``host-requires`` (the
build tier), evaluate markers against a Linux/glibc target (spec §4), map
``generic/<name>`` to a curated capability, and SKIP everything else (virtual compilers
= toolchain, virtual interfaces = flavor-divergent, language PURLs = not OS libs) — high
precision over coverage. Pure + failure-tolerant: any fetch/parse error degrades to ``[]``.

The fetch ``executor`` must share the host filesystem (like ``wheel_preflight``): the
sdist is downloaded to a host tempdir and its ``pyproject.toml`` read in-process.
"""

from __future__ import annotations

import logging
import os
import shlex
import sys
import tarfile
import tempfile
import zipfile
from typing import NamedTuple

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

from python_deps.depgraph.executor import Executor
from python_deps.depgraph.os_resolver import ObservedNeed, capability_id
from python_deps.import_mapping import normalize_package_name

logger = logging.getLogger(__name__)

# DepURL ``generic/<name>`` -> (ObservedNeed.kind, capability name). Confident,
# apt-resolvable subset only: each resolves via os_resolver.PROVIDER_TABLE or apt-file,
# preserving the "every emitted need is apt-resolvable" invariant. Unknown generics are
# SKIPPED (never fabricated). Keyed by the lower-cased canonical DepURL name (these
# become PEP 804 registry names once that lands). apt target shown for review:
#   openssl    -> libssl-dev            (PROVIDER_TABLE header openssl/ssl.h)
#   libffi     -> libffi-dev            (apt-file   header ffi.h)
#   zlib       -> zlib1g-dev            (apt-file   header zlib.h)
#   libjpeg    -> libjpeg-dev           (apt-file   header jpeglib.h)
#   pkg-config -> pkgconf               (PROVIDER_TABLE binary pkg-config)
#   cairo      -> libcairo2-dev         (PROVIDER_TABLE pkgconfig cairo)
#   glib[-2.0] -> libglib2.0-dev        (PROVIDER_TABLE pkgconfig glib-2.0)
#   freetype   -> libfreetype-dev       (apt-file   pkgconfig freetype2)
#   libxml2    -> libxml2-dev           (apt-file   pkgconfig libxml-2.0)
#   libcurl    -> libcurl4-openssl-dev  (PROVIDER_TABLE binary curl-config)
_GENERIC_TO_CAPABILITY: dict[str, tuple[str, str]] = {
    "openssl": ("header", "openssl/ssl.h"),
    "libffi": ("header", "ffi.h"),
    "zlib": ("header", "zlib.h"),
    "libjpeg": ("header", "jpeglib.h"),
    "pkg-config": ("binary", "pkg-config"),
    "cairo": ("pkgconfig", "cairo"),
    "glib": ("pkgconfig", "glib-2.0"),
    "glib-2.0": ("pkgconfig", "glib-2.0"),
    "freetype": ("pkgconfig", "freetype2"),
    "libxml2": ("pkgconfig", "libxml-2.0"),
    "libcurl": ("binary", "curl-config"),
}

# The build tier we seed (PEP 725 key names, verbatim). ``dependencies`` (runtime) and
# every ``optional-*`` table are out of scope for V1.
_BUILD_TIER_KEYS = ("build-requires", "host-requires")

# Linux/glibc target for marker evaluation (spec §4 scope). Start from the host's
# default_environment() so python_version/etc. are populated, then force the platform axes.
_TARGET_MARKER_OVERRIDES = {
    "os_name": "posix",
    "sys_platform": "linux",
    "platform_system": "Linux",
    "platform_machine": "x86_64",
}


class DepURLParts(NamedTuple):
    type: str
    namespace: str
    name: str


def parse_depurl(depurl: str) -> DepURLParts | None:
    """Parse a DepURL body into (type, namespace, name); None if not a DepURL.

    Accepts the ``dep:`` scheme (PEP 725) and the ``pkg:`` PURL alias. Strips the
    optional ``@version`` / ``?qualifiers`` / ``#subpath`` tail. Any trailing PEP 508
    marker must already be removed by the caller (see ``_split_marker``).
    """
    s = depurl.strip()
    for scheme in ("dep:", "pkg:"):
        if s.startswith(scheme):
            s = s[len(scheme):]
            break
    else:
        return None
    for sep in ("@", "?", "#"):
        cut = s.find(sep)
        if cut != -1:
            s = s[:cut]
    segments = [seg for seg in s.strip().strip("/").split("/") if seg]
    if len(segments) < 2:
        return None
    return DepURLParts(segments[0], "/".join(segments[1:-1]), segments[-1])


def _split_marker(spec: str) -> tuple[str, str]:
    """Split ``"dep:...; marker"`` into (depurl, marker); marker is "" when absent."""
    depurl, _, marker = spec.partition(";")
    return depurl.strip(), marker.strip()


def _marker_selects_target(marker: str) -> bool:
    """True if ``marker`` applies to the Linux target (or is absent/unparseable).

    Unparseable marker / packaging absent -> True (include): never silently drop a
    declared build dep over a marker we could not evaluate.
    """
    if not marker:
        return True
    try:
        from packaging.markers import Marker, default_environment

        env = dict(default_environment())
        env.update(_TARGET_MARKER_OVERRIDES)
        return bool(Marker(marker).evaluate(env))
    except Exception:
        return True


def _need_for(parts: DepURLParts, source: str) -> ObservedNeed | None:
    """Map one DepURL to a build ``ObservedNeed``; None to SKIP (precise-only).

    - ``virtual/...``  -> skip: compilers are the closure-level toolchain
      (build-essential/cargo); ``interface/blas|lapack`` are flavor-divergent
      (handled by the flavor-override table), not a single apt ``-dev``.
    - non-``generic`` PURL types (pypi/cargo/golang/cran/github/...) -> skip:
      language-ecosystem packages or VCS refs, not OS ``-dev`` libraries.
    - ``generic/<name>`` -> curated capability, or skip when unknown.
    """
    if parts.type != "generic":
        return None
    cap = _GENERIC_TO_CAPABILITY.get(parts.name.lower())
    if cap is None:
        return None
    kind, name = cap
    return ObservedNeed(
        kind,
        name,
        context="build",
        strength="curated",
        evidence=f"pep725:{source}" if source else "pep725",
    )


def parse_external_table(pyproject_text: str) -> list[str]:
    """Raw DepURL strings from ``[external] build-requires`` + ``host-requires``.

    ``[]`` when there is no ``[external]`` table (the common case today) or the TOML is
    malformed — degrade silently.
    """
    try:
        doc = tomllib.loads(pyproject_text)
    except Exception:
        return []
    external = doc.get("external")
    if not isinstance(external, dict):
        return []
    specs: list[str] = []
    for key in _BUILD_TIER_KEYS:
        values = external.get(key)
        if isinstance(values, list):
            specs.extend(v for v in values if isinstance(v, str))
    return specs


def needs_from_pyproject(pyproject_text: str, *, source: str = "") -> list[ObservedNeed]:
    """Confident build ``ObservedNeed``s from a pyproject's ``[external]`` table.

    Marker-filters to the Linux target, maps ``generic/<name>`` DepURLs to curated
    capabilities, dedups by ``capability_id``, preserves order. ``[]`` when there is no
    ``[external]`` table.
    """
    needs: list[ObservedNeed] = []
    seen: set[str] = set()
    for spec in parse_external_table(pyproject_text):
        depurl, marker = _split_marker(spec)
        if not _marker_selects_target(marker):
            continue
        parts = parse_depurl(depurl)
        if parts is None:
            continue
        need = _need_for(parts, source)
        if need is None:
            continue
        cid = capability_id(need)
        if cid in seen:
            continue
        seen.add(cid)
        needs.append(need)
    return needs


def _shallowest_pyproject(names: list[str]) -> str | None:
    """The top-level ``<root>/pyproject.toml`` archive member, if any (shallowest wins)."""
    candidates = [n for n in names if n.rsplit("/", 1)[-1] == "pyproject.toml"]
    if not candidates:
        return None
    return min(candidates, key=lambda n: (n.count("/"), len(n)))


def read_sdist_archive(path: str) -> str | None:
    """``pyproject.toml`` text from an sdist ``.tar.gz``/``.tgz``/``.zip``; None on miss.

    Reads the member in-process (no extraction to disk). None on any error or an sdist
    that ships no ``pyproject.toml`` (legacy ``setup.py``-only source).
    """
    try:
        if path.endswith(".zip"):
            with zipfile.ZipFile(path) as zf:
                member = _shallowest_pyproject(zf.namelist())
                return zf.read(member).decode("utf-8", "replace") if member else None
        with tarfile.open(path) as tf:  # .tar.gz / .tgz
            member = _shallowest_pyproject(tf.getnames())
            if member is None:
                return None
            handle = tf.extractfile(member)
            return handle.read().decode("utf-8", "replace") if handle else None
    except Exception:
        return None


def fetch_sdist_pyproject(
    pypi_name: str, version: str | None, executor: Executor
) -> str | None:
    """Download the sdist (no deps, no wheel) and return its ``pyproject.toml`` text.

    ``pip download --no-deps --no-binary :all:`` forces the sdist — the only artifact
    that carries ``[external]`` (a wheel ships no ``pyproject.toml``; PyPI JSON exposes
    only core metadata, which omits ``[external]``). Bounded (300 s) and
    failure-tolerant: any download / listing / read failure returns None. ``executor``
    must share the host filesystem (host executor), like ``wheel_preflight``.
    """
    spec = f"{pypi_name}=={version}" if version else pypi_name
    try:
        with tempfile.TemporaryDirectory() as dest:
            cmd = (
                f"{shlex.quote(sys.executable)} -m pip download "
                "--no-deps --no-binary :all: "
                f"--dest {shlex.quote(dest)} {shlex.quote(spec)}"
            )
            try:
                result = executor.run(cmd, timeout=300)
            except Exception:
                return None
            if not result.ok:
                return None
            try:
                archives = sorted(
                    f
                    for f in os.listdir(dest)
                    if f.endswith((".tar.gz", ".tgz", ".zip"))
                )
            except OSError:
                return None
            if not archives:
                return None
            return read_sdist_archive(os.path.join(dest, archives[0]))
    except Exception:
        return None


def pep725_external(
    pypi_name: str, version: str | None, executor: Executor
) -> list[ObservedNeed]:
    """PEP 725 ``[external]`` build/host requirements of ``pypi_name`` as build needs.

    Fetches the sdist's ``pyproject.toml``, reads ``[external]``, maps confident DepURLs
    to capability ``ObservedNeed``s. Returns ``[]`` when the package has no ``[external]``
    table (the near-universal case today) or on any failure — MUST degrade silently.
    Part 3 consumes this (source-priority #1) and wires the edges by ``capability_id``.
    """
    text = fetch_sdist_pyproject(pypi_name, version, executor)
    canonical = normalize_package_name(pypi_name)
    if text is None:
        logger.debug("pep725: %s external=absent needs=0", canonical)
        return []
    needs = needs_from_pyproject(text, source=canonical)
    if parse_external_table(text):
        logger.info("pep725: %s external=present needs=%d", canonical, len(needs))
    else:
        logger.debug("pep725: %s external=absent needs=0", canonical)
    return needs
