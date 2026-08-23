"""Deterministic graph-id grammar shared by host projection and Maintainer."""
from __future__ import annotations

import re

_UNSAFE = re.compile(r"[^a-z0-9]+")


def slug(text: str) -> str:
    return _UNSAFE.sub("-", str(text).lower()).strip("-")


def contract_id(kind: str, subject: str) -> str:
    return f"contract:{kind}:{slug(subject)}"


def goal_contract_id(name: str) -> str:
    return f"contract:goal:{name}"


def foundational_contract_id(name: str) -> str:
    return f"contract:{name}"


def blocker_id(signature: str) -> str:
    return f"blocker:{slug(signature)}"


def attempt_id(key: str) -> str:
    return f"attempt:{slug(key)}"
