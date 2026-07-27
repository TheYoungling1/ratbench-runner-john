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
