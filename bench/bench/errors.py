from __future__ import annotations

import re

from bench.schema import ErrorEvent

# MATCH-THEN-FILTER. A single regex cannot both capture the full dotted FQN and require the right
# suffix. Anchor on the trailing colon: pytest writes "<Exception>: <message>", while a test id is
# "<file>::<name> - " and a class-scoped id is "Foo::bar", so neither can impersonate one.
_TOKEN = re.compile(r"\b([A-Za-z_][\w.]*)(?=:(?!:))")
_TOKEN_SUFFIXES = ("Error", "Exception", "ExceptionGroup")
_WARNING = re.compile(r"\b[A-Za-z_][\w.]*Warning\b")


def _first_token(line: str) -> str | None:
    """First dotted identifier used as an exception label. Warnings are NOT errors."""
    for m in _TOKEN.finditer(line):
        tok = m.group(1)
        if tok.endswith(_TOKEN_SUFFIXES) and not tok.endswith("Warning"):
            return tok
    return None


# ANCHORED PATTERNS FIRST, and the soname pattern is bounded on BOTH sides. Unbounded,
# `lib[\w.+-]*\.so` matches inside any dotted name containing "lib" followed by ".so" —
# "matplotlib.something" yields a FAKE soname "lib.so" written into `group`, which spec 5 calls
# the reproducible primary key.
_GROUP_PATTERNS = (
    ("module", re.compile(r"No module named '([\w.]+)'")),
    ("name", re.compile(r"cannot import name '([\w.]+)'")),
    ("soname", re.compile(r"(?<![\w.])(lib[\w+-]*\.so(?:\.\d+)*)(?![\w.])")),
    ("path", re.compile(r"No such file or directory: '([^']+)'")),
)


def is_warning_only(line: str) -> bool:
    # must NOT call _TOKEN.search directly — it matches bare "W:" prefixes and would return the
    # right answer for the wrong reason
    return bool(_WARNING.search(line)) and _first_token(line) is None


def _group_of(line: str) -> tuple[str, str]:
    for kind, pat in _GROUP_PATTERNS:
        m = pat.search(line)
        if m:
            return m.group(1), kind
    return "", ""


def categorize(token: str, group: str, group_kind: str,
               repo_toplevel: tuple = (), language: str = "python") -> str:
    return "uncategorized"      # replaced in Task 3


def extract_events(lines, *, source: str, repo_toplevel: tuple = (),
                   language: str = "python") -> tuple:
    """Deduped events, key = (source, token, group). Insertion order preserved."""
    order: list = []
    acc: dict = {}
    for line in (lines or ()):
        token = _first_token(line)
        if token is None:
            continue
        group, kind = _group_of(line)
        key = (token, group)
        if key in acc:
            n, kind0, raw = acc[key]
            acc[key] = (n + 1, kind0, raw)
        else:
            order.append(key)
            acc[key] = (1, kind, line.strip()[:200])
    return tuple(
        ErrorEvent(token=tok, group=grp, group_kind=acc[(tok, grp)][1],
                   category=categorize(tok, grp, acc[(tok, grp)][1], repo_toplevel, language),
                   source=source, occurrences=acc[(tok, grp)][0], raw=acc[(tok, grp)][2])
        for tok, grp in order)
