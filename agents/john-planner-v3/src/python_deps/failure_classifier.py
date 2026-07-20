from __future__ import annotations

import re

from .models import DependencyFailure


MODULE_NOT_FOUND_RE = re.compile(
    r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)['\"]"
)
IMPORT_NAME_ERROR_RE = re.compile(
    r"ImportError:\s+cannot import name ['\"]?([^'\"\s]+)['\"]?.*?\sfrom\s['\"]?([^'\"\s]+)",
    re.DOTALL,
)
NO_MATCHING_DISTRIBUTION_RE = re.compile(
    r"No matching distribution found for\s+([A-Za-z0-9_.-]+(?:[<>=!~]=?[^,\s]*)?)",
    re.IGNORECASE,
)
PIP_REQUIRES_BUT_HAVE_RE = re.compile(
    r"^\s*(?P<required_by>[A-Za-z0-9_.-]+)(?:\s+[^\n]*?)?\s+requires\s+"
    r"(?P<requirement>.+?),\s+"
    r"(?:but\s+you\s+have|but\s+you'll\s+have)\s+"
    r"(?P<installed>[A-Za-z0-9_.-]+)\s+(?P<version>[^\s,;]+)",
    re.IGNORECASE | re.MULTILINE,
)
# Missing run-time shared library. Each source is anchored to its OWN terminal
# phrase (``cannot open shared object file``) — never to a bare ``No such file or
# directory`` (an ordinary missing-file OSError), and never with DOTALL (which
# cross-matched a lib*.so mention on one line with a not-found on another). The
# ``lib`` prefix is NOT required: ctypes/dlopen sonames need not start with it.
SONAME_RES = (
    re.compile(
        r"error while loading shared libraries:\s+"
        r"([\w.+-]+\.so(?:\.\d+)*):\s+cannot open shared object file"
    ),  # dynamic loader (most specific)
    re.compile(
        r"ImportError:\s+([\w.+-]+\.so(?:\.\d+)*):\s+cannot open shared object file"
    ),  # python import
    re.compile(
        r"OSError:\s+([\w.+-]+(?:\.so(?:\.\d+)*)?):\s+cannot open shared object file"
    ),  # ctypes/dlopen (.so optional)
    re.compile(
        r"(?m)^([\w.+-]+\.so(?:\.\d+)*):\s+cannot open shared object file.*$"
    ),  # bare loader line
)


# GLIBC / symbol-version mismatch: an ABI/base-image problem, NOT an absent
# package (installing the same package no-ops). Routed to its own failure_type so
# the diagnosis layer never treats it as a missing shared library.
_GLIBC_MISMATCH_RE = re.compile(
    r"version [`'\"]?(GLIBC_[0-9.]+|GLIBCXX_[0-9.]+|CXXABI_[0-9.]+)[`'\"]? not found"
)


def _first_soname_match(text: str) -> re.Match | None:
    """First SONAME_RES match anywhere in ``text`` (pattern order = specificity)."""
    for rx in SONAME_RES:
        m = rx.search(text or "")
        if m:
            return m
    return None


def first_soname(text: str) -> str | None:
    """The missing soname reported by ``text``, or None. Table-independent."""
    m = _first_soname_match(text)
    return m.group(1) if m else None


def classify_dependency_failure(command: str, observation: str) -> DependencyFailure:
    text = observation or ""

    module_match = MODULE_NOT_FOUND_RE.search(text)
    if module_match:
        import_name = module_match.group(1).strip()
        return DependencyFailure(
            failure_type="module_not_found",
            command=command,
            import_name=import_name,
            message=_excerpt(text, module_match.start()),
        )

    import_match = IMPORT_NAME_ERROR_RE.search(text)
    if import_match:
        imported_name = import_match.group(1).strip()
        module_name = import_match.group(2).strip()
        return DependencyFailure(
            failure_type="import_name_error",
            command=command,
            import_name=module_name.split(".", 1)[0],
            message=_excerpt(text, import_match.start()),
            details={
                "imported_name": imported_name,
                "module_name": module_name,
            },
        )

    no_matching_match = NO_MATCHING_DISTRIBUTION_RE.search(text)
    if no_matching_match:
        package_spec = no_matching_match.group(1).strip()
        package_name = re.split(r"[<>=!~]", package_spec, maxsplit=1)[0]
        return DependencyFailure(
            failure_type="no_matching_distribution",
            command=command,
            package_name=package_name,
            message=_excerpt(text, no_matching_match.start()),
            details={"package_spec": package_spec},
        )

    requires_match = PIP_REQUIRES_BUT_HAVE_RE.search(text)
    if requires_match:
        return DependencyFailure(
            failure_type="dependency_conflict",
            command=command,
            package_name=requires_match.group("required_by"),
            message=_excerpt(text, requires_match.start()),
            details={
                "required_by": requires_match.group("required_by"),
                "requirement": requires_match.group("requirement").strip(),
                "installed_package": requires_match.group("installed"),
                "installed_version": requires_match.group("version"),
            },
        )

    if "ResolutionImpossible" in text or "resolutionimpossible" in text.lower():
        return DependencyFailure(
            failure_type="dependency_conflict",
            command=command,
            message=_first_nonempty_line(text),
            details={"pattern": "ResolutionImpossible"},
        )

    glibc_match = _GLIBC_MISMATCH_RE.search(text)
    if glibc_match:
        return DependencyFailure(
            failure_type="glibc_version_mismatch",
            command=command,
            message=_excerpt(text, glibc_match.start()),
            details={"symbol": glibc_match.group(1)},
        )

    native_match = _first_soname_match(text)
    if native_match:
        return DependencyFailure(
            failure_type="native_library_missing",
            command=command,
            message=_excerpt(text, native_match.start()),
            details={"library": native_match.group(1)},
        )

    if _looks_like_python_syntax_version_failure(text):
        syntax_feature, python_specifier = _infer_python_version_constraint(text)
        return DependencyFailure(
            failure_type="syntax_requires_newer_python",
            command=command,
            message=_first_nonempty_line(text),
            details={
                "syntax_feature": syntax_feature,
                "python_specifier": python_specifier,
            },
        )

    return DependencyFailure(
        failure_type="not_dependency_related",
        command=command,
        message=_first_nonempty_line(text),
    )


def _looks_like_python_syntax_version_failure(text: str) -> bool:
    lower = text.lower()
    if "syntaxerror" not in lower:
        return False
    version_markers = (
        "invalid syntax",
        "match ",
        "f-string",
        "variable annotation",
        "positional-only",
        "parenthesized context managers",
    )
    return any(marker in lower for marker in version_markers)


def _infer_python_version_constraint(text: str) -> tuple[str, str]:
    lower = text.lower()
    if "match " in lower or "case " in lower:
        return "structural pattern matching", ">=3.10"
    if "parenthesized context managers" in lower:
        return "parenthesized context managers", ">=3.10"
    if "positional-only" in lower:
        return "positional-only parameters", ">=3.8"
    if "f-string" in lower or "formatted string literal" in lower:
        return "f-string syntax", ">=3.6"
    if "variable annotation" in lower:
        return "variable annotations", ">=3.6"
    return "unknown syntax feature", ""


def _first_nonempty_line(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:500]
    return ""


def _excerpt(text: str, start: int, max_chars: int = 500) -> str:
    if start < 0:
        start = 0
    excerpt = text[start:start + max_chars].strip()
    return re.sub(r"\s+", " ", excerpt)


# ---------------------------------------------------------------------------
# Runtime sub-parsers (Task 1 & 2 — runtime_classify.py consumers)
# ---------------------------------------------------------------------------

# Env-var names are ALL_CAPS or UPPER_with_underscores (at least one uppercase
# letter; no purely lowercase keys to avoid false-positive dict lookups).
_CONFIG_KEYERROR_RE = re.compile(
    r"""KeyError:\s*['"]([A-Z][A-Z0-9_]*)['"]"""
)

# pydantic v1: field name on its own line before "field required"
# pydantic v2: field name on its own line before "Field required [type=missing"
_CONFIG_PYDANTIC_RE = re.compile(
    r"^([A-Z][A-Z0-9_]+)\n\s+(?:field required|Field required)",
    re.MULTILINE,
)


def classify_config_error(command: str, output: str) -> str | None:
    """Return the env-var name if ``output`` looks like a missing config error, else None."""
    text = output or ""
    m = _CONFIG_KEYERROR_RE.search(text)
    if m:
        return m.group(1)
    m = _CONFIG_PYDANTIC_RE.search(text)
    if m:
        return m.group(1)
    return None


_TOOL_COMMAND_NOT_FOUND_RE = re.compile(
    r"(?:^|[\s/])([A-Za-z0-9_.-]+):\s+(?:command not found|not found)",
    re.MULTILINE,
)
_TOOL_FILENOTFOUNDERROR_RE = re.compile(
    r"FileNotFoundError:.*?['\"]([A-Za-z0-9_.-]+)['\"]"
)


def classify_tool_error(command: str, output: str) -> str | None:
    """Return the tool name if ``output`` looks like a missing executable, else None."""
    text = output or ""
    m = _TOOL_COMMAND_NOT_FOUND_RE.search(text)
    if m:
        return m.group(1)
    m = _TOOL_FILENOTFOUNDERROR_RE.search(text)
    if m:
        return m.group(1)
    return None
