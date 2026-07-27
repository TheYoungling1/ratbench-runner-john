# bench/schema.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class RepoSpec:
    full_name: str            # "owner/repo"
    repo_url: str             # https://github.com/owner/repo
    language: str = "python"
    commit: Optional[str] = None   # dataset-pinned SHA to check out; None => live default-branch HEAD


@dataclass(frozen=True)
class HarvestedEnv:
    agent: str
    repo: RepoSpec
    dockerfile: str | None            # None => no Dockerfile found (status="missing")
    setup_scripts: dict = field(default_factory=dict)   # sibling files the Dockerfile COPYs
    base_image: str | None = None
    # harvest resolution (design §2.5): _meta.status if present, else legacy_ok (Dockerfile found)
    # / legacy_missing (none). Producers may also set produced|unmeasurable|error.
    status: str = "ok"
    meta: dict = field(default_factory=dict)   # from bench_meta.json (cost keys None if absent)


@dataclass(frozen=True)
class MeasureRow:
    agent: str
    repo: str
    env_status: str                   # LEGACY alias ("ok" | "missing"); kept for back-compat
    build_ok: bool
    # Did the initial construction setup.sh run to rc 0? True whenever build_ok (setup.sh is a fatal
    # build layer); on a failed build, derived from a truncated re-build through the setup step so it
    # is NOT conflated with a clone/pytest-install failure. Feeds `setup_compile_rate` in metrics.
    setup_compile_ok: bool = False
    build_log_tail: str = ""
    # Failure taxonomy (design §2.2): exactly one status per measured env. One of
    # {unmeasurable, error, missing, measure_error, build_fail, non_conforming, empty_testbed,
    #  no_tests_collected, collect_error, timed_out, executed}; "legacy_ok"/"ok" for legacy rows.
    status: str = "ok"
    py_test_files: int | None = None  # C3 diagnostic: python test files under /testbed (never gates)
    collect_rc: int | None = None
    collect_clean: bool = False
    collect_errors: tuple = ()
    collect_error_count: int = 0        # # of "ERROR collecting <module>" in the collect output
    collected_node_ids: tuple = ()      # tests collected (len = number of tests collected)
    executed: bool = False
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    passed_node_ids: tuple = ()
    failed_node_ids: tuple = ()
    error_node_ids: tuple = ()
    ebsr: bool = False
    pass_rate: float = 0.0
    timed_out: bool = False
    image_size_mb: float | None = None
    image_delta_mb: float | None = None
    installed_pkg_count: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    llm_calls: int | None = None
    turns_used: int | None = None
    cost_usd: float | None = None   # agent-reported spend (Claude Code total_cost_usd); None
                                    # for producers whose model API reports no cost
    produce_s: float | None = None
    build_s: float | None = None
    test_s: float | None = None
    meta: dict = field(default_factory=dict)
    error_surface: str = ""            # full | masked | unobserved — set by verdict.error_surface
    error_surface_reason: str = ""
    status_flags: tuple = ()           # what first-match-wins hid (spec 6.3)
    repo_toplevel: tuple = ()          # importable roots under /testbed; enables the internal split
    run_failed_lines: tuple = ()       # FAILED/ERROR lines from the run pass; source="run" events
    language: str = "python"           # keys the signature table; RepoSpec has it, MeasureRow did not


@dataclass(frozen=True)
class ErrorEvent:
    token: str                 # verbatim FQN exception label, e.g. "redis.exceptions.ConnectionError"
    group: str                 # captured payload: module name, soname, imported name, path
    group_kind: str            # module | soname | name | path | ""
    category: str              # DERIVED view over (token, group, group_kind, repo_toplevel)
    source: str                # collect | run | install — NEVER pooled (spec section 8)
    occurrences: int = 1       # raw lines collapsed by dedup on (source, token, group)
    raw: str = ""              # first raw line, 200 chars, kept for offline re-derivation


@dataclass(frozen=True)
class RepoVerdict:
    agent: str
    repo: str
    status: str                # THIS REPO'S vocabulary (schema.py:40-42). Never a new name.
    bucket: str                # status, or zero_pass|partial|success when status == "executed"
    error_surface: str         # full | masked | unobserved
    surface_reason: str        # startup_abort | nothing_collected | build_failed | no_env | ""
    status_derived: bool = False   # True => backfilled by legacy_status, not measured
    # ORTHOGONAL FLAGS, not modes (spec 6.3): first-match-wins is what makes buckets sum to n,
    # but it is lossy. Flags carry what the ordering hid.
    status_flags: tuple = ()
    collect_rc: int | None = None
    build_ok: bool = False
    pass_rate: float = 0.0
    turns_used: int | None = None


@dataclass(frozen=True)
class ArmErrorReport:
    agent: str
    n_repos: int
    source: str = "collect"        # collect | run — one report per source, never pooled
    n_admissible: int = 0
    n_masked: int = 0
    n_unobserved: int = 0
    n_status_derived: int = 0      # LOUD backfill counter (spec 3.1)
    buckets: dict = field(default_factory=dict)           # bucket -> #repos
    surfaces: dict = field(default_factory=dict)          # surface -> #repos
    token_repos: dict = field(default_factory=dict)       # token -> #repos  (SUBSTRATE)
    token_events: dict = field(default_factory=dict)      # token -> #deduped events
    category_repos: dict = field(default_factory=dict)    # category -> #repos (DERIVED)
    category_events: dict = field(default_factory=dict)
    crosstab: dict = field(default_factory=dict)          # "bucket|category" -> #repos
    top_groups: dict = field(default_factory=dict)        # category -> [[group, #repos], ...]
    uncategorized_rate: float = 0.0
