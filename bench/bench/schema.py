# bench/schema.py
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RepoSpec:
    full_name: str            # "owner/repo"
    repo_url: str             # https://github.com/owner/repo
    language: str = "python"


@dataclass(frozen=True)
class HarvestedEnv:
    agent: str
    repo: RepoSpec
    dockerfile: str | None            # None => no Dockerfile found (status="missing")
    setup_scripts: dict = field(default_factory=dict)   # sibling files the Dockerfile COPYs
    base_image: str | None = None
    status: str = "ok"                # "ok" | "missing"
    meta: dict = field(default_factory=dict)   # from bench_meta.json (cost keys None if absent)


@dataclass(frozen=True)
class MeasureRow:
    agent: str
    repo: str
    env_status: str                   # "ok" | "missing"
    build_ok: bool
    build_log_tail: str = ""
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
    produce_s: float | None = None
    build_s: float | None = None
    test_s: float | None = None
    meta: dict = field(default_factory=dict)
