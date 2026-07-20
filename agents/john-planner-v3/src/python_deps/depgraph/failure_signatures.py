"""Table-independent failure-signature extraction: stderr -> ObservedNeed list.

PURE. No executor, no resolve(), no graph/id concerns — only pattern-matches the
TEXT SHAPE of known failure signatures and returns ObservedNeeds. Resolving a
need to an apt provider stays os_resolver.resolve()'s job, called by the caller
AFTER extraction.

The discrimination rule every pattern satisfies:
  1. an ANCHORED failure signature (the fixed diagnostic phrasing of a real
     failure) — a bare mention in a path / "Using X" line / invocation matches
     none of these; and
  2. the name captured from a FIXED POSITION relative to the anchor, never
     scanned from a table — so an unseen header/binary/module resolves for free.
"""
from __future__ import annotations

import os
import re

from python_deps.depgraph.os_resolver import ObservedNeed, default_context
from python_deps.failure_classifier import SONAME_RES

_HDR_EXTS = (".h", ".hh", ".hpp", ".hxx", ".H", ".tcc", ".ipp")
_HDR = r"[\w./+-]+\.(?:h|hh|hpp|hxx|H|tcc|ipp)"

HEADER_RES = (
    re.compile(rf"(?:fatal error:\s*)?({_HDR})\s*:\s*No such file or directory"),  # gcc/g++/cc
    re.compile(rf"fatal error:\s*'({_HDR})'\s*file not found"),                    # clang, quoted
    re.compile(rf"'({_HDR})'\s*file not found"),                                   # clang driver
)

BINARY_RES = (
    re.compile(r"(?:^|\n)(?:\S*sh: )?([A-Za-z0-9_][\w.+-]*): command not found\b"),          # shell
    re.compile(r"/bin/(?:sh|dash): \d+: ([A-Za-z0-9_][\w.+-]*): not found\b"),               # dash numbered
    re.compile(r"([A-Za-z0-9_][\w.+-]*) executable (?:was )?not found\b"),                   # setuptools
    re.compile(r"[Tt]he ['\"]([A-Za-z0-9_][\w.+-]*)['\"] executable (?:was |is )?not found\b"),  # meson/skbuild
    re.compile(r"configure: error: Cannot find ([A-Za-z0-9_][\w.+-]*)"),                     # autoconf "Cannot find X"
    re.compile(r"configure: error: ([A-Za-z0-9_][\w.+-]*) not found\b"),                     # autoconf "X not found"
    re.compile(r"checking for ([A-Za-z0-9_][\w.+()-]*)\.\.\.\s*(?:not found|no)\b"),         # autoconf probe (…no)
    re.compile(r"Could not find ([A-Za-z0-9_][\w.+-]*)\b(?!\s*[:=])"),                       # cmake find_program
    re.compile(r"Program(?: or command)? ['\"]?([A-Za-z0-9_][\w.+-]*)['\"]? not found or not executable"),  # meson
    re.compile(r"which: no ([A-Za-z0-9_][\w.+-]*) in \("),                                   # which
    re.compile(r"error: command ['\"]([A-Za-z0-9_][\w.+-]*)['\"] failed:\s*No such file or directory\b"),  # distutils errno=2
)

PKGCONFIG_RES = (
    re.compile(r"No package ['\"]([A-Za-z0-9][\w.+-]*)['\"] found"),                          # pkg-config (quotes REQUIRED)
    re.compile(r"Package ([A-Za-z0-9][\w.+-]*) was not found in the pkg-config search path"), # pkg-config (unquoted tail is safe)
    re.compile(r"Package ['\"]([A-Za-z0-9][\w.+-]*)['\"], required by ['\"][\w:.+-]+['\"], not found"),  # transitive
    re.compile(r'Dependency "([A-Za-z0-9][\w.+-]*)" not found, tried (?=[a-z, ]*pkgconfig)[a-z, ]+'),    # meson (pkgconfig-gated)
    re.compile(r"Dependency '([A-Za-z0-9][\w.+-]*)' not found(?!, tried)"),                   # meson simple fallback
    re.compile(r"--\s*No package '([A-Za-z0-9][\w.+-]*)' found"),                             # cmake pkg_check_modules echo
    re.compile(r"None of the required ['\"]([A-Za-z0-9][\w.+;-]*)['\"] found"),               # meson alternatives (split on ';')
)

LINKER_RES = (
    re.compile(r"(?m)^\s*/?(?:usr/bin/)?ld(?:\.(?:bfd|gold|lld))?:\s*cannot find -l([\w.+-]+)"),
)


def _norm_header(name: str) -> str:
    """Keep the name as printed; basename only an absolute or traversal path."""
    if name.startswith("/") or name.startswith("../") or "/../" in name:
        return os.path.basename(name)
    return name


def _line_of(text: str, pos: int, max_chars: int = 500) -> str:
    """The single line of ``text`` containing offset ``pos`` (evidence)."""
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    if end < 0:
        end = len(text)
    return text[start:end].strip()[:max_chars]


def extract_needs(stderr: str, *, context_hint: str = "build") -> list[ObservedNeed]:
    """See module docstring. Dedup by (kind, name), first-occurrence text order."""
    text = stderr or ""
    hits: list[tuple[int, str, str]] = []  # (position, kind, name)

    for rx in HEADER_RES:
        for m in rx.finditer(text):
            hits.append((m.start(1), "header", _norm_header(m.group(1))))
    for rx in BINARY_RES:
        for m in rx.finditer(text):
            name = m.group(1)
            kind = "header" if name.endswith(_HDR_EXTS) else "binary"
            hits.append((m.start(1), kind, name))
    for rx in PKGCONFIG_RES:
        for m in rx.finditer(text):
            for alt in m.group(1).split(";"):
                alt = alt.strip()
                if alt:
                    hits.append((m.start(1), "pkgconfig", alt))
    for rx in SONAME_RES:
        for m in rx.finditer(text):
            hits.append((m.start(1), "soname", m.group(1)))
    for rx in LINKER_RES:
        for m in rx.finditer(text):
            hits.append((m.start(1), "linker_lib", m.group(1)))

    hits.sort(key=lambda h: h[0])
    seen: set[tuple[str, str]] = set()
    needs: list[ObservedNeed] = []
    for pos, kind, name in hits:
        key = (kind, name)
        if key in seen:
            continue
        seen.add(key)
        context = context_hint if kind == "binary" else default_context(kind)
        needs.append(
            ObservedNeed(kind=kind, name=name, context=context, evidence=_line_of(text, pos))
        )
    return needs
