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
    """First dotted identifier used as an exception label. Warnings are NOT errors.

    A Warning token SHADOWS the rest of the line rather than being skipped past. measure.py:21's
    `_COLLECT_ERR` deliberately captures `...Warning:` lines into `collect_errors`, so warning
    text reaches this function on real rows — and a warning whose MESSAGE quotes an exception
    (`<string>:2: UserWarning: RuntimeError: boom`) would otherwise fabricate a counted
    `RuntimeError` event out of a line that reported no error at all. Scanning past the warning
    label is what made that possible; stopping at it is the fix. A genuine error line puts its
    own token first (`E   ImportError: ... see DeprecationWarning`), so it is unaffected.
    """
    for m in _TOKEN.finditer(line):
        tok = m.group(1)
        if tok.endswith("Warning"):
            return None
        if tok.endswith(_TOKEN_SUFFIXES):
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


# OperationalError is deliberately NOT here. sqlite3/sqlalchemy raise it for "no such table" and
# ordinary config errors far more often than for a refused connection, and the token alone cannot
# tell those apart. Including it inflated service_unavailable — the one category an arm most wants
# to move — so it stays `uncategorized` and the raw line is kept for offline re-derivation.
_SERVICE_SUFFIXES = ("ConnectionError", "ConnectionRefusedError", "ConnectionResetError",
                     "DockerException")
_SYNTAX = ("SyntaxError", "IndentationError", "TabError")

# The full taxonomy. Used as the report universe so columns are stable across runs and a category
# hidden by masking in BOTH arms still shows a wide unknown interval rather than vanishing.
CATEGORIES = ("harness_error", "internal_import_failure", "module_not_found", "syslib_missing",
              "partial_import", "import_failed", "service_unavailable", "syntax_error",
              "file_missing", "uncategorized")


def _is_internal(group: str, repo_toplevel: tuple) -> bool:
    return bool(group) and group.split(".")[0] in set(repo_toplevel or ())


def _categorize_python(token: str, group: str, group_kind: str, repo_toplevel: tuple) -> str:
    # harness_error FIRST: _pytest.pathlib.ImportPathMismatchError must not reach ImportError
    if token.startswith(("_pytest.", "Pytest")) or token == "UsageError":
        return "harness_error"
    if token.endswith("ModuleNotFoundError"):
        return "internal_import_failure" if _is_internal(group, repo_toplevel) else "module_not_found"
    if token.endswith("ImportError"):
        if group_kind == "soname":
            return "syslib_missing"
        if group_kind == "name":
            return "partial_import"
        if group_kind == "module":
            return ("internal_import_failure" if _is_internal(group, repo_toplevel)
                    else "module_not_found")
        return "import_failed"
    if token.endswith(_SERVICE_SUFFIXES):
        return "service_unavailable"
    if token in _SYNTAX:
        return "syntax_error"
    if token == "FileNotFoundError":
        return "file_missing"
    return "uncategorized"


# Keyed by language from day one — this repo already has bench/languages/. Only python is
# populated; cargo / go test / mvn / npm need their own tables with the same structure.
_CATEGORIZERS = {"python": _categorize_python}


def categorize(token: str, group: str, group_kind: str,
               repo_toplevel: tuple = (), language: str = "python") -> str:
    fn = _CATEGORIZERS.get(language)
    return fn(token, group, group_kind, repo_toplevel) if fn else "uncategorized"


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
