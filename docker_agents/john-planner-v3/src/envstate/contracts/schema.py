"""Enums, the closed edge-validity table, ownership sets, and secret redaction."""
from __future__ import annotations

import enum
import re


class NodeType(enum.Enum):
    CONTRACT = "Contract"
    BLOCKER = "Blocker"
    ATTEMPT = "Attempt"


class EdgeType(enum.Enum):
    VIOLATES = "violates"        # Blocker  -> Contract
    ADDRESSES = "addresses"      # Attempt  -> Contract
    DEPENDS_ON = "depends_on"    # Contract -> Contract


class ContractStatus(enum.Enum):           # projected, never stored
    SATISFIED = "satisfied"
    VIOLATED = "violated"
    UNKNOWN = "unknown"


class ContractLevel(enum.Enum):
    GOAL = "goal"
    ATOMIC = "atomic"


class BlockerKind(enum.Enum):
    MODULE_NOT_FOUND = "module_not_found"
    MISSING_BINARY = "missing_binary"
    MISSING_SYSTEM_LIBRARY = "missing_system_library"
    VERSION_CONFLICT = "version_conflict"
    BUILD_FAILURE = "build_failure"
    SERVICE_UNREACHABLE = "service_unreachable"
    ENV_VAR_MISSING = "env_var_missing"
    TEST_COLLECTION_FAILURE = "test_collection_failure"
    UNKNOWN = "unknown"


class AttemptKind(enum.Enum):
    PYTHON_INSTALL = "python_install"
    SYSTEM_INSTALL = "system_install"
    ENV_CONFIG = "env_config"
    SERVICE_START = "service_start"
    BUILD_FIX = "build_fix"
    VALIDATION = "validation"
    TEST_RETRY = "test_retry"
    INSPECT = "inspect"
    OTHER = "other"


class AttemptOutcome(enum.Enum):
    PENDING = "pending"
    OK = "ok"
    FAILED = "failed"
    OK_BUT_STILL_BLOCKED = "ok_but_still_blocked"


LAYERS: frozenset[str] = frozenset({"deps", "system", "runtime", "build", "tests", "config"})

EDGE_RULES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    # edge value -> (allowed source types, allowed target types)
    "violates":   (frozenset({"Blocker"}),  frozenset({"Contract"})),
    "addresses":  (frozenset({"Attempt"}),  frozenset({"Contract"})),
    "depends_on": (frozenset({"Contract"}), frozenset({"Contract"})),
}

# Field-level ownership (replaces the old binary node partition):
HOST_CREATABLE_NODE_TYPES: frozenset[str] = frozenset({"Contract", "Attempt"})
MAINTAINER_CREATABLE_NODE_TYPES: frozenset[str] = frozenset({"Contract", "Blocker"})
MAINTAINER_FORBIDDEN_FIELDS: frozenset[str] = frozenset({"status", "outcome", "active"})
VALID_NODE_TYPES: frozenset[str] = frozenset(nt.value for nt in NodeType)
VALID_EDGE_TYPES: frozenset[str] = frozenset(et.value for et in EdgeType)
VALID_STATUSES: frozenset[str] = frozenset(s.value for s in ContractStatus)

_SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9]{8,}\b"),
    re.compile(r"\bgh[ps]_[A-Za-z0-9]{8,}\b"),
    re.compile(r"\b[A-Za-z0-9_]*(?:API_?KEY|TOKEN|SECRET|PASSWORD)[A-Za-z0-9_]*\s*[=:]\s*\S+", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]


def redact_secrets(text: str | None) -> str:
    """Mask common secret shapes before any text enters the graph (spec §15)."""
    if not text:
        return ""
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out
