"""Stable node-id constructors for the concrete dependency graph.

Ids are deterministic strings of the form ``<kind>:<name>`` so the same
entity discovered by different stages collapses onto a single node.
"""

from __future__ import annotations

import re

# Characters that are safe to keep verbatim inside an id segment.  Package
# names are case-sensitive (``Pillow`` != ``pillow``) and may legitimately
# contain ``.``, ``-``, ``_`` and ``==`` (version pin), so we preserve those.
_UNSAFE = re.compile(r"[^A-Za-z0-9._=+-]+")

TEST_NODE_ID = "test:repo_tests_pass"


def slug(text: str) -> str:
    """Sanitize free text into an id-safe segment (whitespace/odd chars -> '-')."""
    cleaned = _UNSAFE.sub("-", text.strip())
    return cleaned.strip("-")


def import_id(name: str) -> str:
    return f"import:{name}"


def package_id(name: str, version: str | None) -> str:
    if version:
        return f"pkg:{name}=={version}"
    return f"pkg:{name}"


def syslib_id(soname: str) -> str:
    return f"syslib:{soname}"


def tool_id(tool: str) -> str:
    return f"tool:{tool}"


def header_id(name: str) -> str:
    return f"header:{name}"


def binary_id(name: str) -> str:
    return f"binary:{name}"


def pkgconfig_id(name: str) -> str:
    return f"pkgconfig:{name}"


def linker_id(name: str) -> str:
    return f"linker:{name}"


def apt_build_id(name: str) -> str:
    """Node id for an apt-keyed Debian build directive.

    Separate id space (``aptdep:``) from capability ids (``binary:``/``header:``/
    ``pkgconfig:``/``syslib:``/``linker:``) — a Debian Build-Depends token is an
    apt install directive (the apt name IS the fix), not a capability need, and it
    pre-satisfies the build so it never collapses with a capability observation.
    """
    return f"aptdep:{name}"


def capability_id(kind: str, name: str) -> str:
    """Capability node id for a resolver need; the single reconciliation key."""
    builders = {
        "soname": syslib_id,
        "header": header_id,
        "binary": binary_id,
        "pkgconfig": pkgconfig_id,
        "linker_lib": linker_id,
    }
    return builders[kind](name)


def project_id(name: str) -> str:
    return f"project:{slug(name)}"


def config_id(name: str) -> str:
    return f"config:{name}"


def service_id(name: str) -> str:
    return f"service:{name}"


def runtime_id(minor: str) -> str:
    return f"runtime:python-{minor}"
