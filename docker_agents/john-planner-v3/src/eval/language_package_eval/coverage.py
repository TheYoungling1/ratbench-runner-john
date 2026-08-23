"""Phase-1 COVERAGE evaluator — does the graph capture ~everything needed to
set up a repo's env? (`docs/superpowers/loops/2026-07-02-graph-fidelity-eval-loop.md`
§3 item 3, scoped to coverage only — no repair loop, no agent, no full pytest run).

Two independent references grade the graph, per repo:

  Reference A — held-out recipe oracle (``oracle.parse_oracle``, deterministic,
  no container): diff the graph's tiers against the human-verified Dockerfile /
  CI / compose recipe. Per-tier ``matched`` / ``missing`` / ``extra`` + recall.

  Reference B — execution-discovered missing needs (container truth): build the
  graph construction-only (no agent, no repair loop, the TEST node's real
  ``pytest`` run intercepted), render ``setup.sh``, then replay it in a FRESH
  container and EXERCISE the env (`python -c "import <top_import>"` +
  `pytest --collect-only -q`, collection only). Import/collect failures AFTER a
  successful install are classified into typed coverage gaps the graph missed.

Attribution rule: if `setup.sh` itself fails, that is a Phase-2 (render/build)
concern, not a Phase-1 coverage gap — fall back to Reference-A-only for that
repo (see ``build_repo_scorecard``).

No LLM, no agent, no repair loop anywhere in this module (the scorer, not the
thing being scored). The container probe is integration (guarded by
``_docker_available``); everything else is pure and unit-tested.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import sys
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src"
for _p in (_REPO_ROOT, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from src.eval.language_package_eval.oracle import _TIER_KEYS, parse_oracle  # noqa: E402
from python_deps.depgraph.build import _project_name, build_dep_graph  # noqa: E402
from python_deps.depgraph.build_script import render_build_script  # noqa: E402
from python_deps.depgraph.emit import _apt_name  # noqa: E402
from python_deps.depgraph.executor import (  # noqa: E402
    CommandResult, DockerExecutor, Executor, LocalSubprocessExecutor,
)
from python_deps.depgraph.schema import DepGraph, NodeType  # noqa: E402
from src.envstate.runtime_base import (  # noqa: E402
    DEFAULT_MINOR, SUPPORTED_MINORS, resolve_runtime_base,
)

logger = logging.getLogger(__name__)

# The 5 tiers the held-out oracle can declare (SYSTEM_LIB folds in TOOL apt
# packages -- the oracle has no TOOL/SYSTEM_LIB split; see oracle.py + the
# graph-fidelity ledger). Reused from oracle.py's own tier vocabulary (one
# source of truth for the tier-key list).
ORACLE_TIERS: tuple[str, ...] = _TIER_KEYS


# ---------------------------------------------------------------------------
# Base image (deterministic — no LLM)
# ---------------------------------------------------------------------------

_PYTHON_VERSION_RE = re.compile(r"(\d+\.\d+)")


def _python_version_pin(repo_dir: Path) -> str | None:
    """A supported minor pinned by ``.python-version``, or None."""
    path = repo_dir / ".python-version"
    if not path.is_file():
        return None
    match = _PYTHON_VERSION_RE.search(path.read_text(encoding="utf-8", errors="replace"))
    minor = match.group(1) if match else None
    return minor if minor in SUPPORTED_MINORS else None


def base_image_for_repo(repo_dir: "Path | str") -> tuple[str, str, str]:
    """``(image, minor, reason)`` for ``repo_dir`` — no LLM, fully deterministic.

    A ``.python-version`` pin (if present and supported) is offered as the
    preferred minor; ``resolve_runtime_base`` then clamps/keeps it against
    ``requires-python`` (PEP 621 / poetry) exactly as the real conductor does
    (``src.envstate.base_image_selection.choose_base_image`` minus the LLM
    candidate step -- this eval never calls the LLM ImageSelector).
    """
    repo_dir = Path(repo_dir)
    pinned = _python_version_pin(repo_dir)
    candidate = f"python:{pinned}-slim" if pinned else f"python:{DEFAULT_MINOR}-slim"
    decision = resolve_runtime_base(str(repo_dir), candidate)
    return decision.base_image, decision.minor, decision.reason


# ---------------------------------------------------------------------------
# Graph tier extraction (pure)
# ---------------------------------------------------------------------------

def apt_names_in_graph(graph: DepGraph) -> frozenset[str]:
    """Apt package names the graph resolved for SYSTEM_LIB + TOOL nodes
    (merged -- the oracle has no TOOL/SYSTEM_LIB split, see module docstring)."""
    return frozenset(
        apt for n in graph.nodes
        if n.type in (NodeType.SYSTEM_LIB, NodeType.TOOL)
        for apt in (_apt_name(n),) if apt
    )


def runtime_minors_in_graph(graph: DepGraph) -> frozenset[str]:
    return frozenset(n.version for n in graph.nodes if n.type is NodeType.RUNTIME and n.version)


def package_versions_in_graph(graph: DepGraph) -> dict[str, str | None]:
    """Raw pip name -> resolved version, one entry per PACKAGE node."""
    return {n.name: n.version for n in graph.nodes if n.type is NodeType.PACKAGE}


def service_names_in_graph(graph: DepGraph) -> frozenset[str]:
    return frozenset(n.name for n in graph.nodes if n.type is NodeType.SERVICE)


def config_names_in_graph(graph: DepGraph) -> frozenset[str]:
    return frozenset(n.name for n in graph.nodes if n.type is NodeType.CONFIG)


def graph_counts_by_tier(graph: DepGraph) -> dict[str, int]:
    """Node counts per ``NodeType``, zero-filled so every type is present."""
    counts = Counter(n.type.name for n in graph.nodes)
    return {t.name: counts.get(t.name, 0) for t in NodeType}


# ---------------------------------------------------------------------------
# Generic membership diff (pure)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TierDiff:
    matched: tuple[str, ...]
    missing: tuple[str, ...]
    extra: tuple[str, ...]

    @property
    def recall(self) -> float | None:
        """``matched / (matched + missing)``; None when the oracle declared
        nothing for this tier (no signal, not a 0.0 or 1.0 score)."""
        denom = len(self.matched) + len(self.missing)
        return (len(self.matched) / denom) if denom else None


def _canon_lower(name: str) -> str:
    return name.strip().lower()


def diff_membership(
    oracle_ids: Iterable[str],
    graph_ids: Iterable[str],
    *,
    canon=_canon_lower,
) -> TierDiff:
    """Diff two id sets under a canonicalization key; reports ORIGINAL casing."""
    oracle_by_canon = {canon(x): x for x in oracle_ids}
    graph_by_canon = {canon(x): x for x in graph_ids}
    oracle_keys, graph_keys = oracle_by_canon.keys(), graph_by_canon.keys()
    matched = tuple(sorted(oracle_by_canon[k] for k in oracle_keys & graph_keys))
    missing = tuple(sorted(oracle_by_canon[k] for k in oracle_keys - graph_keys))
    extra = tuple(sorted(graph_by_canon[k] for k in graph_keys - oracle_keys))
    return TierDiff(matched, missing, extra)


# ---------------------------------------------------------------------------
# Package-specific diff: name-only membership + separate pin/content check
# ---------------------------------------------------------------------------

_CANON_RUN = re.compile(r"[-_.]+")
_PIP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def canon_pip(name: str) -> str:
    """PEP 503 normalized comparison key (``Flask_Login`` == ``flask-login``)."""
    return _CANON_RUN.sub("-", name).lower()


def split_pip_spec(spec: str) -> tuple[str, str | None]:
    """``'requests==2.31.0'`` -> ``('requests', '==2.31.0')``; ``'flask'`` ->
    ``('flask', None)``."""
    spec = spec.strip()
    match = _PIP_NAME_RE.match(spec)
    if not match:
        return spec, None
    name = match.group(0)
    rest = spec[len(name):].strip()
    return name, (rest or None)


def pin_status(declared_pin: str, graph_version: str | None) -> str:
    """``'match' | 'mismatch' | 'unverifiable' | 'no_graph_version'``."""
    if graph_version is None:
        return "no_graph_version"
    try:
        satisfied = SpecifierSet(declared_pin).contains(Version(graph_version), prereleases=True)
    except (InvalidSpecifier, InvalidVersion):
        return "unverifiable"
    return "match" if satisfied else "mismatch"


def diff_packages(
    oracle_specs: Iterable[str], graph_versions: Mapping[str, str | None]
) -> tuple[TierDiff, tuple[dict, ...]]:
    """Name-only membership diff (version ignored) + a separate pin-mismatch
    report for names present on both sides (§ rules: report content mismatches
    separately from the membership recall)."""
    oracle_by_canon: dict[str, tuple[str, str | None]] = {}
    for spec in oracle_specs:
        name, pin = split_pip_spec(spec)
        oracle_by_canon[canon_pip(name)] = (name, pin)
    graph_by_canon = {canon_pip(name): (name, version) for name, version in graph_versions.items()}

    diff = diff_membership(
        (name for name, _ in oracle_by_canon.values()),
        (name for name, _ in graph_by_canon.values()),
        canon=canon_pip,
    )

    pin_mismatches: list[dict] = []
    for key, (name, pin) in oracle_by_canon.items():
        if pin is None or key not in graph_by_canon:
            continue
        _, graph_version = graph_by_canon[key]
        status = pin_status(pin, graph_version)
        if status != "match":
            pin_mismatches.append(
                {"name": name, "declared": pin, "graph_version": graph_version, "status": status}
            )
    return diff, tuple(pin_mismatches)


# ---------------------------------------------------------------------------
# Reference A: graph vs oracle, all 5 tiers (pure — the core grading function)
# ---------------------------------------------------------------------------

def score_repo_against_oracle(graph: DepGraph, declared_by_tier: Mapping[str, Sequence[str]]) -> dict:
    """Reference-A scorecard fields: recall + matched/missing/extra per tier,
    plus PACKAGE pin mismatches. Pure — no container, no network."""
    diffs: dict[str, TierDiff] = {
        "SYSTEM_LIB": diff_membership(
            declared_by_tier.get("SYSTEM_LIB", ()), apt_names_in_graph(graph), canon=_canon_lower
        ),
        # RUNTIME: declared_by_tier["RUNTIME"] comes ONLY from oracle.py's recipe
        # parse (FROM python:X.Y / actions/setup-python), NEVER a host-python
        # fallback. When the recipe declares no python this is (), so
        # TierDiff.recall is None ("not applicable") and nothing is reported
        # missing -- an undeclared runtime is not a 0% recall.
        "RUNTIME": diff_membership(
            declared_by_tier.get("RUNTIME", ()), runtime_minors_in_graph(graph), canon=_canon_lower
        ),
        "SERVICE": diff_membership(
            declared_by_tier.get("SERVICE", ()), service_names_in_graph(graph), canon=_canon_lower
        ),
        "CONFIG": diff_membership(
            declared_by_tier.get("CONFIG", ()), config_names_in_graph(graph), canon=_canon_lower
        ),
    }
    pkg_diff, pin_mismatches = diff_packages(
        declared_by_tier.get("PACKAGE", ()), package_versions_in_graph(graph)
    )
    diffs["PACKAGE"] = pkg_diff

    return {
        "oracle_recall_by_tier": {tier: diffs[tier].recall for tier in ORACLE_TIERS},
        "matched_vs_oracle_by_tier": {tier: list(diffs[tier].matched) for tier in ORACLE_TIERS},
        "missing_vs_oracle_by_tier": {tier: list(diffs[tier].missing) for tier in ORACLE_TIERS},
        "extra_vs_oracle_by_tier": {tier: list(diffs[tier].extra) for tier in ORACLE_TIERS},
        "package_pin_mismatches": list(pin_mismatches),
    }


# ---------------------------------------------------------------------------
# Reference B: execution-discovered missing needs (failure classification, pure)
# ---------------------------------------------------------------------------

_MODULE_NOT_FOUND_RE = re.compile(r"ModuleNotFoundError: No module named ['\"]([\w.]+)['\"]")
_SO_IMPORT_ERROR_RE = re.compile(r"(lib[\w.+-]*\.so(?:\.[\w]+)*): cannot open shared object file")
_LDD_NOT_FOUND_RE = re.compile(r"^\s*(\S+)\s*=>\s*not found", re.MULTILINE)
_COMMAND_NOT_FOUND_RE = re.compile(r"(?:^|[\s:])([\w.-]+): command not found", re.MULTILINE)
_SERVICE_ERROR_RE = re.compile(
    r"(ConnectionRefusedError|OperationalError|could not connect to server|"
    r"redis\.exceptions\.ConnectionError|pymongo\.errors\.ServerSelectionTimeoutError)"
)


def classify_execution_failures(output: str) -> tuple[dict, ...]:
    """Parse a probe's combined stdout+stderr into typed coverage-gap
    candidates: ``{"tier": ..., "id": ..., "evidence": ...}``, deduped by
    ``(tier, id)``. Best-effort text classification, not a parser."""
    gaps: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _add(tier: str, node_id: str, evidence: str) -> None:
        key = (tier, node_id)
        if key not in seen:
            seen.add(key)
            gaps.append({"tier": tier, "id": node_id, "evidence": evidence.strip()})

    for m in _MODULE_NOT_FOUND_RE.finditer(output):
        _add("PACKAGE", m.group(1).split(".")[0], m.group(0))
    for m in _SO_IMPORT_ERROR_RE.finditer(output):
        _add("SYSTEM_LIB", m.group(1), m.group(0))
    for m in _LDD_NOT_FOUND_RE.finditer(output):
        _add("SYSTEM_LIB", m.group(1), m.group(0))
    for m in _COMMAND_NOT_FOUND_RE.finditer(output):
        _add("TOOL", m.group(1), m.group(0))
    for m in _SERVICE_ERROR_RE.finditer(output):
        _add("SERVICE", "unknown", m.group(0))
    return tuple(gaps)


def first_failure_evidence(output: str, *, tail_lines: int = 40) -> dict:
    """Best-effort ``{"command": ..., "stderr_tail": ...}`` for an install
    failure: the last ``bash -x`` trace line (if traced) or else the last
    non-empty output line, plus a bounded tail for the scorecard."""
    lines = [line for line in output.splitlines() if line.strip()]
    tail = lines[-tail_lines:]
    command = next((line[2:].strip() for line in reversed(tail) if line.startswith("+ ")), None)
    if command is None and tail:
        command = tail[-1].strip()
    return {"command": command, "stderr_tail": "\n".join(tail)}


# ---------------------------------------------------------------------------
# Top-level import name guess (pure, filesystem read only)
# ---------------------------------------------------------------------------

_IGNORED_TOP_DIRS = frozenset({
    "tests", "test", "docs", "doc", "examples", "example", "scripts", "build",
    "dist", "venv", ".venv", "node_modules", "__pycache__", "site-packages",
})


def _find_case_insensitive_package(base: Path, candidate: str) -> str | None:
    if not base.is_dir():
        return None
    for entry in sorted(base.iterdir()):
        if entry.is_dir() and entry.name.lower() == candidate and (entry / "__init__.py").is_file():
            return entry.name
    return None


def _first_package_dir(base: Path) -> str | None:
    if not base.is_dir():
        return None
    for entry in sorted(base.iterdir()):
        if (
            entry.is_dir()
            and not entry.name.startswith(".")
            and entry.name.lower() not in _IGNORED_TOP_DIRS
            and (entry / "__init__.py").is_file()
        ):
            return entry.name
    return None


def top_level_import_name(repo_dir: "Path | str") -> str | None:
    """Best-effort deterministic guess at the repo's own top-level importable
    package name, for the execution probe's ``python -c "import X"`` check.

    Tries the pyproject project name (dashes/dots -> underscores) under the
    repo root and ``src/``, then falls back to the first plausible package
    directory. Returns None when nothing is found -- the caller then SKIPS the
    import check rather than fabricating a wrong module name."""
    repo_dir = Path(repo_dir)
    candidate = re.sub(r"[-.]", "_", _project_name(str(repo_dir))).lower()
    for base in (repo_dir, repo_dir / "src"):
        found = _find_case_insensitive_package(base, candidate)
        if found is not None:
            return found
    for base in (repo_dir, repo_dir / "src"):
        found = _first_package_dir(base)
        if found is not None:
            return found
    return None


# ---------------------------------------------------------------------------
# Aggregation (pure)
# ---------------------------------------------------------------------------

def pooled_recall_by_tier(scorecards: Sequence[Mapping]) -> dict[str, float | None]:
    """Pool matched/missing COUNTS (not per-repo ratios) across ``scorecards``
    so repos contribute proportionally to their oracle-declared set size."""
    totals = {tier: [0, 0] for tier in ORACLE_TIERS}  # [matched, missing]
    for sc in scorecards:
        matched_by_tier = sc.get("matched_vs_oracle_by_tier", {})
        missing_by_tier = sc.get("missing_vs_oracle_by_tier", {})
        for tier in ORACLE_TIERS:
            totals[tier][0] += len(matched_by_tier.get(tier, ()))
            totals[tier][1] += len(missing_by_tier.get(tier, ()))
    return {
        tier: (matched / (matched + missing) if (matched + missing) else None)
        for tier, (matched, missing) in totals.items()
    }


def missing_node_clusters(scorecards: Sequence[Mapping]) -> tuple[dict, ...]:
    """Missing-node clusters ranked by ``(count desc, tier, id)`` — the
    actionable output: where the graph under-covers, and how often, pooled
    across BOTH the oracle diff and the execution-discovered gaps. Infeasible
    repos are excluded (§2: the repo is the problem, not the graph)."""
    counts: dict[tuple[str, str], set[str]] = defaultdict(set)
    for sc in scorecards:
        if not sc.get("feasible"):
            continue
        repo = sc["repo"]
        for tier, ids in sc.get("missing_vs_oracle_by_tier", {}).items():
            for node_id in ids:
                counts[(tier, node_id)].add(repo)
        for gap in sc.get("execution_missing", ()):
            counts[(gap["tier"], gap["id"])].add(repo)
    clusters = [
        {"tier": tier, "id": node_id, "count": len(repos), "repos": sorted(repos)}
        for (tier, node_id), repos in counts.items()
    ]
    clusters.sort(key=lambda c: (-c["count"], c["tier"], c["id"]))
    return tuple(clusters)


def render_report_md(scorecards: Sequence[Mapping], *, deferred: Sequence[str] = ()) -> str:
    """Aggregate report: pooled per-tier recall (feasible repos) + per-repo
    summary + missing-node clusters ranked by ``(tier, count)`` — the top
    cluster is the next construction fix (per the loop doc's protocol)."""
    feasible = [sc for sc in scorecards if sc.get("feasible")]
    pooled = pooled_recall_by_tier(feasible)
    clusters = missing_node_clusters(scorecards)

    lines: list[str] = [
        "# Phase-1 Coverage Report",
        "",
        f"Corpus: {len(scorecards)} repos ({len(feasible)} feasible).",
    ]
    if deferred:
        lines.append(f"Deferred: {', '.join(deferred)}.")
    lines += [
        "",
        "## Pooled per-tier recall (feasible repos, oracle-diff)",
        "",
        "| Tier | Recall | Note |",
        "|---|---|---|",
    ]
    for tier in ORACLE_TIERS:
        recall = pooled.get(tier)
        note = ""
        if tier in ("SERVICE", "CONFIG"):
            note = "not populated by construction-only pass (LLM classifier out of scope)"
        recall_str = f"{recall:.2f}" if recall is not None else "n/a (no oracle signal)"
        lines.append(f"| {tier} | {recall_str} | {note} |")

    lines += [
        "",
        "## Per-repo summary",
        "",
        "| Repo | Feasible | Install OK | SYSTEM_LIB | RUNTIME | PACKAGE | Exec-missing |",
        "|---|---|---|---|---|---|---|",
    ]
    for sc in scorecards:
        recall_by_tier = sc.get("oracle_recall_by_tier", {})

        def _fmt(tier: str) -> str:
            value = recall_by_tier.get(tier)
            return f"{value:.2f}" if value is not None else "n/a"

        lines.append(
            f"| {sc.get('repo')} | {sc.get('feasible')} | {sc.get('install_ok')} | "
            f"{_fmt('SYSTEM_LIB')} | {_fmt('RUNTIME')} | {_fmt('PACKAGE')} | "
            f"{len(sc.get('execution_missing', ()))} |"
        )

    lines += ["", "## Top missing-node clusters (ranked by count, tier, id)", ""]
    if clusters:
        for i, c in enumerate(clusters, start=1):
            repos = ", ".join(c["repos"])
            lines.append(f"{i}. **{c['tier']}** `{c['id']}` — {c['count']} repo(s): {repos}")
    else:
        lines.append("(none)")

    failed_installs = [sc for sc in scorecards if sc.get("install_ok") is False]
    lines += ["", "## Install failures (Reference-A-only fallback)", ""]
    if failed_installs:
        for sc in failed_installs:
            first_failure = sc.get("install_first_failure") or {}
            lines.append(f"- **{sc.get('repo')}**: `{first_failure.get('command')}`")
    else:
        lines.append("(none)")

    all_concerns = sorted({c for sc in scorecards for c in sc.get("concerns", ())})
    lines += ["", "## Concerns", ""]
    for c in all_concerns:
        lines.append(f"- {c}")
    if not all_concerns:
        lines.append("(none)")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Container integration: construction-only build + fresh-replay execution probe
# ---------------------------------------------------------------------------

def _docker_available() -> bool:
    try:
        proc = subprocess.run(["docker", "info"], capture_output=True, timeout=20)
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class _ConstructionOnlyExecutor:
    """Delegates every command to a real container executor EXCEPT the repo's
    real test-suite run (the TEST goal node's ``python -m pytest -q`` check
    command) -- Phase-1 coverage measures graph CONSTRUCTION only; the repair
    loop / agent / a real pytest run are explicitly out of scope here."""

    def __init__(self, inner: Executor) -> None:
        self._inner = inner
        self.skipped = 0

    def run(self, command: str, *, timeout: int = 300) -> CommandResult:
        stripped = command.strip()
        if "-m pytest" in command or stripped.startswith("pytest"):
            self.skipped += 1
            return CommandResult(
                command=command, returncode=1, stdout="",
                stderr="construction-only: pytest run skipped",
            )
        return self._inner.run(command, timeout=timeout)


def build_graph_construction_only(repo_dir: str, base_image: str, target_python: str) -> DepGraph:
    """Build the dep graph in a scratch container -- no agent, no repair loop,
    the real test suite intercepted (see ``_ConstructionOnlyExecutor``)."""
    with DockerExecutor(base_image) as scratch:
        wrapped = _ConstructionOnlyExecutor(scratch)
        return build_dep_graph(
            repo_dir, wrapped, host_executor=LocalSubprocessExecutor(), target_python=target_python,
        )


@dataclass
class _MountedContainer:
    """Minimal fresh-container lifecycle with a read/write bind mount of the
    repo -- the ``-v`` mount option the task allows in place of ``docker cp``.
    Mirrors ``Executor``'s ``run() -> CommandResult`` contract so probe code
    reads the same as the rest of this module; independent of
    ``DockerExecutor`` (that one has no mount support, which this probe needs)."""

    image: str
    host_dir: str
    container_dir: str = "/workspace/repo"
    name: str = field(default_factory=lambda: f"coverage-probe-{uuid.uuid4().hex[:12]}")

    def __enter__(self) -> "_MountedContainer":
        proc = subprocess.run(
            ["docker", "run", "-d", "-v", f"{self.host_dir}:{self.container_dir}",
             "--name", self.name, self.image, "sleep", "infinity"],
            capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"failed to start probe container: {proc.stderr.strip()}")
        return self

    def __exit__(self, *exc: object) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True, text=True, timeout=120)

    def run(self, command: str, *, timeout: int = 300) -> CommandResult:
        proc = subprocess.run(
            ["docker", "exec", self.name, "sh", "-c", command],
            capture_output=True, text=True, timeout=timeout,
        )
        return CommandResult(command=command, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def _write_file(executor: Executor, path: str, content: str) -> None:
    encoded = base64.b64encode(content.encode()).decode()
    result = executor.run(f"echo {encoded} | base64 -d > {path}", timeout=60)
    if not result.ok:
        raise RuntimeError(f"failed writing {path}: {result.stderr}")


def run_execution_probe(
    repo_dir: str, base_image: str, setup_script: str, top_import: str | None,
    *, install_timeout: int = 1800,
) -> dict:
    """Fresh ``-slim`` replay (Reference B): install via the rendered
    ``setup.sh``, then EXERCISE the env (import + ``pytest --collect-only``,
    collection only -- never the real suite). Returns
    ``{install_ok, install_first_failure, execution_missing}``."""
    with _MountedContainer(base_image, os.path.abspath(repo_dir)) as fresh:
        _write_file(fresh, "/setup.sh", setup_script)
        # setup.sh is run FROM the repo root (the standard contract every project's
        # install docs assume; mirrors the held-out oracle Dockerfiles' `WORKDIR
        # /app; RUN pip install -e .`). The final `pip install -e .` resolves `.`
        # against this cwd, so running from `/` would fail to find pyproject.toml.
        install = fresh.run(
            f"cd {fresh.container_dir} && bash -x /setup.sh", timeout=install_timeout
        )
        if not install.ok:
            return {
                "install_ok": False,
                "install_first_failure": first_failure_evidence(install.stdout + install.stderr),
                "execution_missing": (),
            }

        failure_output: list[str] = []
        if top_import:
            imported = fresh.run(
                f"cd {fresh.container_dir} && python3 -c 'import {top_import}'", timeout=120
            )
            if not imported.ok:
                failure_output.append(imported.stdout + imported.stderr)

        # Probe-only bootstrap (NOT graph-attributed): pytest is the probe's own
        # tool, not a runtime need the graph was asked to provide. Guarded: a
        # failed bootstrap must never masquerade as a graph-caused "missing
        # pytest" PACKAGE gap, so collection is skipped (not misclassified) when
        # the bootstrap itself fails.
        bootstrap = fresh.run("pip install --no-input --quiet pytest", timeout=300)
        if bootstrap.ok:
            collected = fresh.run(
                f"cd {fresh.container_dir} && python3 -m pytest --collect-only -q", timeout=600
            )
            if not collected.ok:
                failure_output.append(collected.stdout + collected.stderr)

        # Classified as ONE pass over both probes' combined output so the same
        # underlying gap (e.g. the repo's own package missing from both the
        # import check and collection) is deduped, not double-reported.
        gaps = classify_execution_failures("\n".join(failure_output))
        return {"install_ok": True, "install_first_failure": None, "execution_missing": gaps}


# ---------------------------------------------------------------------------
# Per-repo scorecard (orchestrates construction + both references)
# ---------------------------------------------------------------------------

_PROXY_CONCERN = (
    "Reference-A recall is a PROXY: the held-out Dockerfile/CI/compose recipe "
    "may itself omit real needs (uv/poetry-managed installs record no explicit "
    "pip package list) or use tools this oracle parser doesn't read -- a "
    "'missing' entry can reflect the oracle's blind spot, not the graph's."
)
_NEEDS_TIER_CONCERN = (
    "CONFIG/SERVICE/DataAsset tiers are never populated by this construction-only "
    "pass (the LLM env_classifier that proposes them is out of scope here, per "
    "the no-LLM-in-the-scorer / no-agent rule) -- their recall is not meaningful "
    "signal in this report."
)


def build_repo_scorecard(repo_dir: str, full_name: str, *, feasible: bool | None) -> dict:
    """The full per-repo Phase-1 coverage scorecard (§ per-repo JSON schema)."""
    concerns: list[str] = [_PROXY_CONCERN, _NEEDS_TIER_CONCERN]

    oracle = parse_oracle(repo_dir)
    image, minor, image_reason = base_image_for_repo(repo_dir)
    concerns.append(f"base image: {image_reason}")

    graph = DepGraph()
    construction_error: str | None = None
    try:
        graph = build_graph_construction_only(repo_dir, image, minor)
    except Exception as exc:  # noqa: BLE001 — one repo's crash must not abort the corpus
        construction_error = f"{type(exc).__name__}: {exc}"
        concerns.append(f"construction_failed: {construction_error}")

    counts = graph_counts_by_tier(graph)
    if construction_error is None and counts.get("PACKAGE", 0) == 0:
        concerns.append(
            "0 PACKAGE nodes captured by construction -- check repo_dir points to "
            "an installable project root (e.g. a monorepo dev-root pyproject.toml "
            "declares no runtime dependencies of its own)."
        )

    oracle_score = score_repo_against_oracle(graph, oracle.declared_by_tier)

    install_ok: bool | None = None
    install_first_failure: dict | None = None
    execution_missing: tuple[dict, ...] = ()
    if construction_error is not None:
        concerns.append("construction failed -- execution probe skipped.")
    elif not _docker_available():
        concerns.append("docker unavailable -- execution probe skipped, Reference-A only.")
    else:
        script = render_build_script(graph, ())
        top_import = top_level_import_name(repo_dir)
        try:
            probe = run_execution_probe(repo_dir, image, script, top_import)
            install_ok = probe["install_ok"]
            install_first_failure = probe["install_first_failure"]
            execution_missing = probe["execution_missing"]
            if not install_ok:
                concerns.append(
                    "setup.sh failed on fresh replay -- Reference-A only for this repo "
                    "(a render/build-dep issue is Phase-2 scope, not a Phase-1 coverage gap)."
                )
            elif any(g["tier"] == "PACKAGE" and g["id"] == top_import for g in execution_missing):
                concerns.append(
                    f"'{top_import}' (the repo's OWN package) failed to import: the renderer "
                    "never emits an install step for the PROJECT node itself (only PACKAGE/"
                    "SYSTEM_LIB/TOOL nodes are 'reciped' by build_script._is_reciped), so "
                    "setup.sh never installs the repo under test. A flat-layout repo (the "
                    "package dir sits at repo root) can still import by accident via Python's "
                    "cwd-prepended sys.path; a src-layout or name-mismatched repo cannot. This "
                    "generalizes across the corpus and is a render/construction gap, not a "
                    "one-off."
                )
        except Exception as exc:  # noqa: BLE001 — probe failure must not abort the corpus
            concerns.append(f"execution_probe_failed: {type(exc).__name__}: {exc}")

    return {
        "repo": full_name,
        "base_image": image,
        "target_python": minor,
        "feasible": feasible,
        "graph_counts_by_tier": counts,
        "oracle_source": list(oracle.source),
        **oracle_score,
        "install_ok": install_ok,
        "install_first_failure": install_first_failure,
        "execution_missing": list(execution_missing),
        "concerns": concerns,
    }


# ---------------------------------------------------------------------------
# CLI: run the corpus
# ---------------------------------------------------------------------------

CORPUS: tuple[str, ...] = (
    "fastapi/typer",
    "crytic/slither",
    "mvt-project/mvt",
    "python-semantic-release/python-semantic-release",
    "mckinsey/vizro",
    "crystaldba/postgres-mcp",
)
DEFERRED: tuple[str, ...] = ("unit8co/darts",)  # torch: too heavy/slow for this pass

_SMOKE_DIR = _REPO_ROOT / "outputs" / "graph_fidelity" / "_smoke"
_OUT_DIR = _REPO_ROOT / "outputs" / "graph_fidelity" / "coverage"
_LABELS_PATH = _REPO_ROOT / "outputs" / "graph_fidelity" / "baseline_labels.json"


def _repo_dir_name(full_name: str) -> str:
    return full_name.split("/", 1)[1]


def _repo_id(full_name: str) -> str:
    org, name = full_name.split("/", 1)
    return f"{org}__{name}"


def _load_labels(labels_path: Path = _LABELS_PATH) -> dict:
    if not labels_path.exists():
        return {}
    return json.loads(labels_path.read_text())


def _empty_scorecard(full_name: str, feasible: bool | None, concern: str) -> dict:
    """Fallback scorecard shape for a repo whose scoring crashed outright (no
    graph, no oracle diff) -- same keys as ``build_repo_scorecard``'s return,
    all empty/None, so ``render_report_md`` never has to special-case a repo."""
    return {
        "repo": full_name, "base_image": None, "target_python": None,
        "feasible": feasible, "graph_counts_by_tier": {}, "oracle_source": [],
        "oracle_recall_by_tier": {}, "matched_vs_oracle_by_tier": {},
        "missing_vs_oracle_by_tier": {}, "extra_vs_oracle_by_tier": {},
        "package_pin_mismatches": [], "install_ok": None,
        "install_first_failure": None, "execution_missing": [],
        "concerns": [concern],
    }


def run_corpus(
    corpus: Sequence[str] = CORPUS, *, out_dir: Path = _OUT_DIR, smoke_dir: Path = _SMOKE_DIR,
) -> list[dict]:
    labels = _load_labels()
    out_dir.mkdir(parents=True, exist_ok=True)
    scorecards: list[dict] = []
    for full_name in corpus:
        repo_id = _repo_id(full_name)
        repo_dir = smoke_dir / _repo_dir_name(full_name)
        feasible = labels.get(repo_id, {}).get("feasible")
        logger.info("coverage: scoring %s (repo_dir=%s)", full_name, repo_dir)
        try:
            scorecard = build_repo_scorecard(str(repo_dir), full_name, feasible=feasible)
        except Exception as exc:  # noqa: BLE001 — one repo's crash must not abort the corpus
            scorecard = _empty_scorecard(
                full_name, feasible, f"scorecard_failed: {type(exc).__name__}: {exc}"
            )
        (out_dir / f"{repo_id}.json").write_text(json.dumps(scorecard, indent=2, sort_keys=True) + "\n")
        scorecards.append(scorecard)
    return scorecards


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    scorecards = run_corpus()
    report = render_report_md(scorecards, deferred=DEFERRED)
    report_path = _OUT_DIR / "report.md"
    report_path.write_text(report)
    print(f"Wrote {len(scorecards)} scorecards + {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
