# src/python_deps/depgraph/debian_builddeps.py
"""Debian source Build-Depends reader + PyPI->Debian source name-mapper.

Backbone of the sdist build-dep prior (system-library-detection design §2,
source #2). For a source-built Python package, the distro that already knows its
system build-deps is Debian: ``apt-cache showsrc <src>`` prints the source
package's ``Build-Depends:`` — a list of *apt* package names, exactly the
``-dev`` packages that provide the headers / linkable libs / build tools an sdist
compile needs. This module (a) maps a PyPI distribution name onto its Debian
*source* name and (b) reads + filters that source's ``Build-Depends:`` into a
plain list of apt package names.

Reproducibility: the target base image is pinned to snapshot.debian.org, so
``apt-cache showsrc`` is deterministic once ``deb-src`` is enabled.

DECISION (documented): a ``Build-Depends`` token is already an apt package name
(``libpq-dev``) — an apt INSTALL DIRECTIVE, not a capability need. This module
carries the apt name verbatim and returns ``list[str]``. It does NOT wrap tokens
in ``ObservedNeed``, does NOT reverse-engineer a capability kind (unsound for
numeric-suffixed / non-``lib``-prefixed names, which would collapse ~97% name
recall to ~50%), and does NOT route them through ``os_resolver.resolve`` — the
apt name IS the fix. Part 3 owns node representation (apt-keyed ``aptdep:`` TOOL
nodes with ``chosen_fix=apt:<name>`` and a ``dpkg`` check) and dedup against the
capability-resolved apt set. The capability-keyed OBSERVE path (``probe.py``)
is unchanged.
"""

from __future__ import annotations

import json
import logging
import re
import shlex

from python_deps.depgraph.executor import Executor
from python_deps.import_mapping import normalize_package_name

logger = logging.getLogger(__name__)

# deb822 sources file the snapshot-pinned Debian base ships (bookworm+).
_DEBIAN_SOURCES = "/etc/apt/sources.list.d/debian.sources"
_APT_UPDATE_TIMEOUT = 180


def ensure_deb_src(executor: Executor) -> bool:
    """Enable ``deb-src`` on the target container's signed deb822 stanza, once.

    Patches the EXISTING ``Types: deb`` line to ``Types: deb deb-src`` (a bare
    classic ``deb-src`` line triggers a ``Signed-By`` conflict that aborts apt),
    then ``apt-get update``. Idempotent + filesystem-cached: if the stanza already
    reads ``Types: deb deb-src`` it returns immediately without re-patching or
    re-updating. Returns ``False`` on any failure (non-Debian base, missing file,
    no network) so callers degrade to "no prior" rather than crash.
    """
    if executor.run(f"grep -q '^Types: deb deb-src$' {_DEBIAN_SOURCES}").ok:
        return True
    patched = executor.run(
        f"sed -i 's/^Types: deb$/Types: deb deb-src/' {_DEBIAN_SOURCES}"
    )
    if not patched.ok:
        return False
    return executor.run("apt-get update", timeout=_APT_UPDATE_TIMEOUT).ok


# Debian/Python packaging machinery — never the sdist's *system* build-deps.
_MACHINERY: frozenset[str] = frozenset({
    "cdbs", "quilt", "dpkg-dev", "autotools-dev", "autoconf", "automake",
    "libtool", "po-debconf", "gettext", "intltool", "chrpath",
    "python3", "python3-all", "python3-all-dev", "python3-all-dbg",
})
_MACHINERY_PREFIX: tuple[str, ...] = (
    "debhelper", "dh-", "python3-", "python-", "libjs-", "libpython", "cython",
    "librust-",
)

_BUILD_DEPENDS_RE = re.compile(r"^Build-Depends:(.*(?:\n[ \t].*)*)", re.MULTILINE)
_QUALIFIER_RE = re.compile(r"\(.*?\)|\[.*?\]|<.*?>")  # (version) [arch] <profile>
_BINARY_RE = re.compile(r"^Binary:(.*(?:\n[ \t].*)*)", re.MULTILINE)


def _builds_python3_binary(showsrc_stdout: str) -> bool:
    """True iff the source's ``Binary:`` field lists a ``python3-*`` package —
    evidence it is the Python package's Debian source, not an unrelated same-named
    one (Debian's Lisp ``cffi``, the ``cups`` daemon source). Reads the PRODUCED
    binaries (``Binary:``), never ``Build-Depends:`` (which may build-depend on
    python3 machinery)."""
    match = _BINARY_RE.search(showsrc_stdout or "")
    if not match:
        return False
    names = [tok.strip() for tok in match.group(1).replace("\n", " ").split(",")]
    return any(n.startswith("python3-") for n in names)


def _is_python_source_stanza(stdout: str) -> bool:
    """A showsrc stanza worth learning from: has ``Build-Depends`` AND produces a
    ``python3-*`` binary."""
    return "Build-Depends:" in stdout and _builds_python3_binary(stdout)


def is_machinery(token: str) -> bool:
    """True for Debian/Python packaging cruft (not a system build-dep)."""
    return token in _MACHINERY or token.startswith(_MACHINERY_PREFIX)


def is_system_lib(token: str) -> bool:
    """Keep any non-machinery build-dep: system libs (``*-dev``, ``lib*``) AND real
    build tools (``swig``, ``cargo``, ``proj-bin``). Only Debian/Python packaging
    machinery — including ``librust-*`` vendored-crate shadows — is dropped."""
    return not is_machinery(token)


def parse_build_depends(showsrc_stdout: str) -> list[str]:
    """First ``Build-Depends:`` field -> bare apt package names, in order.

    Comma-splits the field (honoring RFC822 continuation lines); per entry takes
    the first ``|`` alternative and strips ``(version)`` / ``[arch]`` /
    ``<profile>`` qualifiers, yielding the bare package token. ``[]`` when the
    stanza has no ``Build-Depends`` (``Build-Depends-Indep`` is intentionally not
    read here — build-arch system libs live in ``Build-Depends``).
    """
    match = _BUILD_DEPENDS_RE.search(showsrc_stdout or "")
    if not match:
        return []
    names: list[str] = []
    for entry in match.group(1).split(","):
        entry = entry.strip()
        if not entry:
            continue
        first_alt = entry.split("|", 1)[0].strip()
        bare = _QUALIFIER_RE.sub("", first_alt).strip()
        parts = bare.split()
        if parts:
            names.append(parts[0])
    return names


# PyPI -> Debian source overrides where neither the normalized name nor the
# ``python-<name>`` variant is the Debian source. Authoritative; checked first.
# Small + extensible; the long tail is Repology's job (Task 5).
_SOURCE_ALIASES: dict[str, str] = {
    "mysqlclient": "python-mysqldb",
}


def _strip_py_prefix(normalized: str) -> str:
    """Bare library stem: drop a leading ``python-`` or ``py`` prefix."""
    if normalized.startswith("python-"):
        return normalized[len("python-"):]
    if normalized.startswith("py") and len(normalized) > 2:
        return normalized[2:]
    return normalized


def source_candidates(pypi_name: str) -> list[str]:
    """Ordered, de-duplicated Debian *source*-name candidates for a PyPI name.

    Order (first ``Build-Depends`` hit wins downstream): curated alias
    (authoritative); the normalized name; ``python-<normalized>`` (Debian's
    python-source convention: pyaudio->python-pyaudio, cffi->python-cffi,
    shapely->python-shapely); then the ``python-``/``py``-stripped bare stem
    LAST — a generic stem (``curl``, ``magic``) can false-match an unrelated
    Debian source, so it is the final resort.
    """
    normalized = normalize_package_name(pypi_name)
    ordered = [
        _SOURCE_ALIASES.get(normalized),
        normalized,
        f"python-{normalized}",
        _strip_py_prefix(normalized),
    ]
    seen: set[str] = set()
    out: list[str] = []
    for cand in ordered:
        if cand and cand not in seen:
            seen.add(cand)
            out.append(cand)
    return out


def _showsrc(executor: Executor, src: str):
    return executor.run(f"apt-cache showsrc {shlex.quote(src)}")


_REPOLOGY_URL = "https://repology.org/api/v1/project/{name}"
_CURL_TIMEOUT = 30


def repology_debian_sources(pypi_name: str, executor: Executor) -> list[str]:
    """Debian source names Repology maps this project to, or ``[]``.

    Oracle/fallback for the name-mapper (design §2, source #4) when the
    PyPI->Debian heuristic misses. Queries the Repology API and collects
    ``srcname`` for entries whose repo is a Debian family (``debian_*``),
    de-duplicated in first-seen order. Defensive: any curl/JSON failure -> ``[]``
    (never crash the observe path).
    """
    name = normalize_package_name(pypi_name)  # -> [a-z0-9-] only, shell-safe
    url = _REPOLOGY_URL.format(name=name)
    result = executor.run(f"curl -sS -f {shlex.quote(url)}", timeout=_CURL_TIMEOUT)
    if not result.ok:
        return []
    try:
        entries = json.loads(result.stdout or "")
    except (ValueError, TypeError):
        return []
    if not isinstance(entries, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        repo = entry.get("repo", "")
        src = entry.get("srcname", "")
        if isinstance(repo, str) and repo.startswith("debian") and src and src not in seen:
            seen.add(src)
            out.append(src)
    return out


def _resolve_source(pypi_name: str, executor: Executor) -> tuple[str, str] | None:
    """The winning Debian source name AND its ``apt-cache showsrc`` stdout, or None.

    Ensures ``deb-src``, tries the heuristic ``source_candidates`` then the Repology
    fallback, returning the first candidate whose showsrc output is a Python source
    stanza — has a ``Build-Depends:`` field AND produces a ``python3-*`` binary
    (``_is_python_source_stanza``), so a same-named but unrelated Debian source
    (Lisp ``cffi``, the ``cups`` daemon) is never mistaken for the PyPI package's
    source — as ``(src, stdout)`` so callers reuse the fetched stanza instead of
    re-running showsrc. ``None`` when deb-src can't enable or every candidate
    misses.
    """
    if not ensure_deb_src(executor):
        return None
    for cand in source_candidates(pypi_name):
        result = _showsrc(executor, cand)
        if result.ok and _is_python_source_stanza(result.stdout or ""):
            return cand, result.stdout or ""
    # Repology fallback: heuristic candidates all missed; ask the name oracle,
    # then verify each proposed source is a Python source (same gate as above).
    for cand in repology_debian_sources(pypi_name, executor):
        result = _showsrc(executor, cand)
        if result.ok and _is_python_source_stanza(result.stdout or ""):
            return cand, result.stdout or ""
    return None


def pypi_to_debian_source(pypi_name: str, executor: Executor) -> str | None:
    """Debian *source* name for a PyPI distribution, or ``None``.

    Ensures ``deb-src`` is enabled, then tries ``source_candidates`` in order,
    returning the first whose ``apt-cache showsrc`` output carries a
    ``Build-Depends:`` field AND produces a ``python3-*`` binary (evidence it is
    the Python package's source, not an unrelated same-named one — Debian's Lisp
    ``cffi``, the ``cups`` daemon source). When every heuristic candidate misses,
    falls back to Repology (``repology_debian_sources``) as a name oracle,
    verifying each proposed source through the same gate. ``None`` when deb-src
    can't be enabled or every candidate (heuristic + Repology) misses.
    """
    resolved = _resolve_source(pypi_name, executor)
    return resolved[0] if resolved else None


def debian_build_deps(pypi_name: str, executor: Executor) -> list[str]:
    """Machinery-filtered apt package names from Debian ``Build-Depends``.

    Resolves the Debian source name, reads its first ``Build-Depends:``, drops
    packaging machinery (see ``is_system_lib``), keeps everything else — system
    libs and real build tools alike — and returns their apt package names
    verbatim, order-preserving and de-duplicated. These
    are apt INSTALL DIRECTIVES (the apt name IS the fix), consumed by Part 3 which
    dedups them against the capability-resolved apt set before seeding apt-keyed
    ``aptdep:`` nodes. Returns ``[]`` on any miss
    (no source, showsrc error, nothing kept). See the module docstring for why the
    apt name is carried directly rather than wrapped in an ``ObservedNeed``.
    """
    resolved = _resolve_source(pypi_name, executor)
    if resolved is None:
        logger.info("debian: %s -> MISS", pypi_name)
        return []
    src, stdout = resolved
    seen: set[str] = set()
    kept: list[str] = []
    for token in parse_build_depends(stdout):
        if is_system_lib(token) and token not in seen:
            seen.add(token)
            kept.append(token)
    if kept:
        logger.info("debian: %s -> source=%s deps=%s", pypi_name, src, kept)
    else:
        logger.info("debian: %s -> MISS", pypi_name)
    return kept
