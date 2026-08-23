"""Held-out human-recipe oracle — the graph-fidelity grader (§2, §3 item 1 of
``docs/superpowers/loops/2026-07-02-graph-fidelity-eval-loop.md``).

This module IS the grader/oracle: it is SUPPOSED to read a repo's Dockerfile,
CI workflows, and docker-compose files — the human-verified recipe held OUT of
construction. The LEAKAGE INVARIANT (§2) binds *construction* (``build_dep_graph``
et al.), which must never read these files in eval mode; it does not bind this
module.

Parses whichever held-out recipe sources exist in a repo directory:
  - ``Dockerfile`` / ``*.Dockerfile`` / ``docker/**``  -> SYSTEM_LIB (apt), RUNTIME
    (``FROM python:X.Y*`` minor), PACKAGE (``pip install`` + pins)
  - ``.github/workflows/*.yml``                         -> SYSTEM_LIB, RUNTIME
    (``actions/setup-python`` ``python-version``, incl. matrix expansion), PACKAGE
  - ``docker-compose*.y*ml``                             -> SERVICE (top-level
    ``services:`` images), CONFIG (their ``environment:`` keys)

Returns a pure, deterministic ``OracleResult`` — the same repo directory always
yields the same result; no network, no LLM. Conservative by design: only what the
recipe text literally declares is reported, never an inferred/hallucinated need.

PACKAGE extraction from a ``pip install`` line keeps only tokens that parse as
a real PEP 508 requirement, dropping flags, local paths, shell variables, and a
small build/CI-tooling denylist (see ``_clean_pip_tokens``); a ``-r``/``-c``
argument is dereferenced into the referenced requirements/constraints file's
own packages instead of being dropped outright. RUNTIME is normalized text
parsed from the recipe ONLY (``_normalize_runtime``) — it never falls back to
this machine's own Python (``sys.version_info`` et al.); an undeclared runtime
is reported as empty, not guessed.
"""
from __future__ import annotations

import re
import shlex
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from packaging.requirements import InvalidRequirement, Requirement

try:  # PyYAML is available repo-wide; degrade gracefully if ever absent.
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src"
for _p in (_REPO_ROOT, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from python_deps.depgraph.schema import NodeType  # noqa: E402
from python_deps.depgraph.service_scan import scan_compose_services  # noqa: E402
from python_deps.evidence import _read_requirement_lines  # noqa: E402

_TIER_KEYS: tuple[str, ...] = (
    NodeType.SYSTEM_LIB.name,
    NodeType.RUNTIME.name,
    NodeType.PACKAGE.name,
    NodeType.SERVICE.name,
    NodeType.CONFIG.name,
)


@dataclass(frozen=True)
class OracleResult:
    """The held-out recipe's declared node set per tier (§6 scorecard schema)."""

    declared_by_tier: Mapping[str, tuple[str, ...]]
    source: tuple[str, ...] = ()
    held_out: bool = True

    def __post_init__(self) -> None:
        # frozen=True only blocks attribute rebinding; wrap the dict in a
        # read-only view so a caller's plain dict can't mutate this result later
        # (mirrors schema.Node/Edge's data-field convention).
        if not isinstance(self.declared_by_tier, types.MappingProxyType):
            object.__setattr__(
                self, "declared_by_tier", types.MappingProxyType(dict(self.declared_by_tier))
            )

    def to_dict(self) -> dict:
        return {
            "declared_by_tier": {k: list(v) for k, v in self.declared_by_tier.items()},
            "source": list(self.source),
            "held_out": self.held_out,
        }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_oracle(repo_dir: "Path | str") -> OracleResult:
    """Parse whichever held-out recipe sources exist under ``repo_dir``."""
    repo_dir = Path(repo_dir)
    apt: set[str] = set()
    pip_pkgs: set[str] = set()
    runtimes: set[str] = set()
    services: set[str] = set()
    config: set[str] = set()
    sources: list[str] = []

    dockerfiles = _find_dockerfiles(repo_dir)
    if dockerfiles:
        sources.append("Dockerfile")
        for path in dockerfiles:
            file_apt, file_runtimes, file_pip = _parse_dockerfile(_read_text(path), repo_dir)
            apt |= file_apt
            runtimes |= file_runtimes
            pip_pkgs |= file_pip

    workflows = _find_workflows(repo_dir)
    if workflows:
        sources.append("ci")
        for path in workflows:
            file_apt, file_runtimes, file_pip = _parse_workflow(_read_text(path), repo_dir)
            apt |= file_apt
            runtimes |= file_runtimes
            pip_pkgs |= file_pip

    compose_files = _find_compose_files(repo_dir)
    if compose_files:
        sources.append("compose")
        services |= set(scan_compose_services(str(repo_dir)).keys())
        for path in compose_files:
            config |= _compose_env_vars(path)

    declared_by_tier = {
        NodeType.SYSTEM_LIB.name: tuple(sorted(apt)),
        NodeType.RUNTIME.name: tuple(sorted(runtimes)),
        NodeType.PACKAGE.name: tuple(sorted(pip_pkgs)),
        NodeType.SERVICE.name: tuple(sorted(services)),
        NodeType.CONFIG.name: tuple(sorted(config)),
    }
    return OracleResult(declared_by_tier=declared_by_tier, source=tuple(sources))


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def _find_dockerfiles(repo_dir: Path) -> list[Path]:
    found: list[Path] = []
    root_dockerfile = repo_dir / "Dockerfile"
    if root_dockerfile.is_file():
        found.append(root_dockerfile)
    if repo_dir.is_dir():
        found.extend(sorted(p for p in repo_dir.rglob("*.Dockerfile") if p.is_file()))
    docker_dir = repo_dir / "docker"
    if docker_dir.is_dir():
        for path in sorted(docker_dir.rglob("*")):
            if path.is_file() and (path.name == "Dockerfile" or path.name.endswith(".Dockerfile")):
                found.append(path)
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in found:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _find_workflows(repo_dir: Path) -> list[Path]:
    wf_dir = repo_dir / ".github" / "workflows"
    if not wf_dir.is_dir():
        return []
    return sorted(p for p in wf_dir.iterdir() if p.is_file() and p.suffix in (".yml", ".yaml"))


def _find_compose_files(repo_dir: Path) -> list[Path]:
    if not repo_dir.is_dir():
        return []
    out: list[Path] = []
    for entry in sorted(repo_dir.iterdir()):
        if not entry.is_file():
            continue
        low = entry.name.lower()
        if low.startswith("docker-compose") and low.endswith((".yml", ".yaml")):
            out.append(entry)
    return out


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Runtime (python minor) normalization
# ---------------------------------------------------------------------------

_MINOR_RE = re.compile(r"^(\d+)\.(\d+)")


def _normalize_runtime(value: object) -> "str | None":
    """``'3.11.4-slim'`` / ``3.11`` / ``'3.11'`` -> ``'3.11'``; ``None`` if no
    major.minor is present (e.g. a bare major like ``'3'``).

    NEVER falls back to ``sys.version_info`` / ``platform.python_version()`` /
    any other host interpreter default: ``value`` always comes from recipe text
    (a ``FROM python:X.Y`` tag or a ``python-version:`` field) parsed by the
    caller, so "no match" means the recipe didn't declare one -- the correct
    oracle answer is "unknown" (``None``), never this machine's own Python."""
    text = str(value).strip().strip("'\"")
    match = _MINOR_RE.match(text)
    if not match:
        return None
    return f"{match.group(1)}.{match.group(2)}"


# ---------------------------------------------------------------------------
# Shell-command helpers shared by Dockerfile RUN bodies and CI `run:` steps
# ---------------------------------------------------------------------------

def _join_continuations(text: str) -> list[str]:
    """Join backslash-continued lines into single logical lines."""
    logical: list[str] = []
    buffer: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            buffer.append(line[:-1].strip())
        else:
            buffer.append(line.strip())
            logical.append(" ".join(part for part in buffer if part))
            buffer = []
    if buffer:
        logical.append(" ".join(part for part in buffer if part))
    return logical


def _split_commands(shell_text: str) -> list[str]:
    """Split a shell command/RUN body on top-level ``&&`` chains."""
    return [segment.strip() for segment in shell_text.split("&&") if segment.strip()]


_APT_CMDS = ("apt-get", "apt")


def _apt_packages_from_tokens(tokens: list[str]) -> set[str]:
    for i, tok in enumerate(tokens):
        if tok not in _APT_CMDS:
            continue
        rest = tokens[i + 1 :]
        if "install" not in rest[:3]:
            continue
        install_at = rest.index("install")
        return _clean_apt_tokens(rest[install_at + 1 :])
    return set()


def _clean_apt_tokens(tokens: list[str]) -> set[str]:
    """Drop flags (``-y``, ``--no-install-recommends``, ...) and version pins
    (``curl=7.88.1-10`` -> ``curl``); keep the real apt package name."""
    packages: set[str] = set()
    for tok in tokens:
        if tok.startswith("-"):
            continue
        name = tok.split("=", 1)[0].strip()
        if name:
            packages.add(name)
    return packages


_PIP_TOKEN_RE = re.compile(r"^pip[23]?(\.\d+)?$")

# Flags whose argument is a file/URL/local-path, never an external PACKAGE need.
# ``-r``/``-c``/``--group`` additionally get skipped as a NAME here (their
# argument is a requirements/constraints file or a PEP 735 dependency-group
# name, not a package spec).
_PIP_FLAGS_WITH_ARG = frozenset(
    {
        "-r", "--requirement", "-c", "--constraint", "-e", "--editable",
        "-i", "--index-url", "--extra-index-url", "-t", "--target",
        "-f", "--find-links", "--group",
    }
)
# ``-r``/``-c`` name a requirements/constraints FILE -- dereference it so the
# packages it actually declares are captured (the flag+filename token itself
# is never a package).
_PIP_DEREF_FLAGS = frozenset({"-r", "--requirement", "-c", "--constraint"})

# Build-backend / packaging / CI-meta tooling: these routinely appear on a
# `pip install` line (bootstrapping the build, running coverage, ...) but are
# not the repo's runtime env the graph is asked to resolve.
_PIP_BUILD_CI_DENYLIST = frozenset(
    {
        "build", "wheel", "setuptools", "pip", "hatch", "hatchling",
        "poetry-core", "flit-core", "twine", "tox", "nox", "coverage",
    }
)


def _pip_requirement_name(token: str) -> "str | None":
    """The canonical requirement name if ``token`` parses as a valid PEP 508
    requirement (name [+ extras] [+ specifier]); ``None`` otherwise. This is
    the single source of truth for "is this really a package token" -- it
    naturally rejects local paths (``.``/``../pkg``) and shell variables
    (``$WHEEL``) too, since neither is valid PEP 508 syntax."""
    try:
        return Requirement(token).name.strip().lower()
    except InvalidRequirement:
        return None


def _is_local_path(token: str) -> bool:
    """A ``.`` / ``../vizro-core`` / ``dist/x.whl`` style local path -- installs
    the repo itself or a sibling/artifact on disk, never an external PyPI
    package the graph is meant to resolve."""
    return token in (".", "..") or token.startswith(("./", "../")) or "/" in token


def _is_shell_variable(token: str) -> bool:
    """A shell-expanded value (``$psrWheelFile``) filled in at CI run time --
    static text parsing cannot know what package it names, so it is not a
    declared name."""
    return "$" in token


def _packages_from_requirements_file(repo_dir: "Path | None", ref: str) -> set[str]:
    """Dereference a ``-r``/``-c`` argument: the file's own requirement lines
    are where the real PACKAGE declarations live, not the flag/filename token.
    Best-effort: a missing file, or a ``ref`` that escapes ``repo_dir``, yields
    nothing rather than guessing or reading outside the repo."""
    if repo_dir is None:
        return set()
    repo_root = Path(repo_dir).resolve()
    candidate = (repo_root / ref).resolve()
    try:
        candidate.relative_to(repo_root)
    except ValueError:
        return set()
    if not candidate.is_file():
        return set()
    packages: set[str] = set()
    for line in _read_requirement_lines(candidate):
        name = _pip_requirement_name(line)
        if name is None or name in _PIP_BUILD_CI_DENYLIST:
            continue
        packages.add(line)
    return packages


def _pip_packages_from_tokens(tokens: list[str], repo_dir: "Path | None") -> set[str]:
    packages: set[str] = set()
    for i, tok in enumerate(tokens):
        is_pip = bool(_PIP_TOKEN_RE.match(tok))
        is_module_pip = tok in ("python", "python3") and tokens[i + 1 : i + 3] == ["-m", "pip"]
        if not (is_pip or is_module_pip):
            continue
        rest = tokens[i + (1 if is_pip else 3) :]
        if not rest or rest[0] != "install":
            continue
        packages |= _clean_pip_tokens(rest[1:], repo_dir)
    return packages


def _clean_pip_tokens(tokens: list[str], repo_dir: "Path | None") -> set[str]:
    """Keep only tokens that are a real external PACKAGE need: drop flags (and
    their file/path/group-name argument, dereferencing ``-r``/``-c`` into the
    file's own packages), local paths, shell variables, and build/CI tooling
    (see the WHY comment on each check below)."""
    packages: set[str] = set()
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        if tok in _PIP_FLAGS_WITH_ARG:
            arg = tokens[i + 1] if i + 1 < n else None
            if tok in _PIP_DEREF_FLAGS and arg:
                packages |= _packages_from_requirements_file(repo_dir, arg)
            i += 2
            continue
        if tok.startswith("-"):
            # WHY: a bare flag (``-y``, ``--upgrade``, ``--no-cache-dir``, ...)
            # configures pip itself; it never names a package.
            i += 1
            continue
        if _is_local_path(tok) or _is_shell_variable(tok):
            i += 1
            continue
        name = _pip_requirement_name(tok)
        if name is None or name in _PIP_BUILD_CI_DENYLIST:
            i += 1
            continue
        packages.add(tok)
        i += 1
    return packages


def _packages_from_segment(segment: str, repo_dir: "Path | None") -> tuple[set[str], set[str]]:
    """``(apt_packages, pip_packages)`` declared in one ``&&``-separated segment."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        tokens = segment.split()
    return _apt_packages_from_tokens(tokens), _pip_packages_from_tokens(tokens, repo_dir)


# ---------------------------------------------------------------------------
# Dockerfile
# ---------------------------------------------------------------------------

_FROM_PYTHON_RE = re.compile(r"(?im)^FROM\s+(?:\S+/)?python:(\S+)")
_RUN_RE = re.compile(r"(?i)^RUN\s+")


def _parse_dockerfile(text: str, repo_dir: "Path | None" = None) -> tuple[set[str], set[str], set[str]]:
    """``(apt_packages, runtime_minors, pip_packages)`` from one Dockerfile."""
    apt: set[str] = set()
    pip_pkgs: set[str] = set()
    runtimes: set[str] = set()

    for match in _FROM_PYTHON_RE.finditer(text):
        normalized = _normalize_runtime(match.group(1))
        if normalized:
            runtimes.add(normalized)

    for logical in _join_continuations(text):
        if not _RUN_RE.match(logical):
            continue
        body = _RUN_RE.sub("", logical, count=1)
        for segment in _split_commands(body):
            seg_apt, seg_pip = _packages_from_segment(segment, repo_dir)
            apt |= seg_apt
            pip_pkgs |= seg_pip

    return apt, runtimes, pip_pkgs


# ---------------------------------------------------------------------------
# CI workflow
# ---------------------------------------------------------------------------

_MATRIX_REF_RE = re.compile(r"^\$\{\{\s*matrix\.([\w-]+)\s*\}\}$")


def _job_matrix(job: dict) -> dict:
    strategy = job.get("strategy")
    if isinstance(strategy, dict) and isinstance(strategy.get("matrix"), dict):
        return strategy["matrix"]
    return {}


def _runtimes_from_setup_python(step: dict, matrix: dict) -> set[str]:
    with_block = step.get("with")
    if not isinstance(with_block, dict):
        return set()
    value = with_block.get("python-version")
    if value is None:
        return set()
    values = value if isinstance(value, list) else [value]
    out: set[str] = set()
    for item in values:
        text = str(item).strip()
        matrix_ref = _MATRIX_REF_RE.match(text)
        if matrix_ref:
            matrix_values = matrix.get(matrix_ref.group(1))
            if isinstance(matrix_values, list):
                for matrix_value in matrix_values:
                    normalized = _normalize_runtime(matrix_value)
                    if normalized:
                        out.add(normalized)
            continue
        for token in text.split():
            normalized = _normalize_runtime(token)
            if normalized:
                out.add(normalized)
    return out


def _parse_workflow(text: str, repo_dir: "Path | None" = None) -> tuple[set[str], set[str], set[str]]:
    """``(apt_packages, runtime_minors, pip_packages)`` from one workflow file."""
    apt: set[str] = set()
    pip_pkgs: set[str] = set()
    runtimes: set[str] = set()

    if yaml is None:
        return apt, runtimes, pip_pkgs
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return apt, runtimes, pip_pkgs
    if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
        return apt, runtimes, pip_pkgs

    for job in doc["jobs"].values():
        if not isinstance(job, dict) or not isinstance(job.get("steps"), list):
            continue
        matrix = _job_matrix(job)
        for step in job["steps"]:
            if not isinstance(step, dict):
                continue
            uses = step.get("uses")
            if isinstance(uses, str) and "setup-python" in uses:
                runtimes |= _runtimes_from_setup_python(step, matrix)
            run = step.get("run")
            if isinstance(run, str):
                for logical in _join_continuations(run):
                    for segment in _split_commands(logical):
                        seg_apt, seg_pip = _packages_from_segment(segment, repo_dir)
                        apt |= seg_apt
                        pip_pkgs |= seg_pip

    return apt, runtimes, pip_pkgs


# ---------------------------------------------------------------------------
# docker-compose (SERVICE reuses service_scan; CONFIG is oracle-specific)
# ---------------------------------------------------------------------------

def _compose_env_vars(path: Path) -> set[str]:
    """Env var KEYS declared under any compose service's ``environment:`` block."""
    if yaml is None:
        return set()
    try:
        doc = yaml.safe_load(_read_text(path))
    except yaml.YAMLError:
        return set()
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict):
        return set()

    keys: set[str] = set()
    for entry in doc["services"].values():
        if not isinstance(entry, dict):
            continue
        env = entry.get("environment")
        if isinstance(env, dict):
            keys.update(str(k) for k in env.keys())
        elif isinstance(env, list):
            for item in env:
                key = str(item).split("=", 1)[0].strip()
                if key:
                    keys.add(key)
    return keys
