# Error Classification Port — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** add the error-event / error-surface / interval-delta layer to this repo's `bench/`, speaking the `status` vocabulary that already exists here, then delete the stale fork it was built against.

**Architecture:** Three additions to `bench/bench/`, layered by mechanism. `verdict.py` reads **structured row fields only** — it produces the admissibility flag and backfills `status` for legacy rows *using this repo's existing status names*. `errors.py` does **all string matching** — token, capture group, dedup, category. `report/error_report.py` aggregates per arm and computes the arm-vs-arm interval. `metrics.py`, `compute_metrics`, `status`, `_DISQUALIFIED`, and every existing scorer are untouched.

**Tech Stack:** Python 3.10+, stdlib only (`re`, `dataclasses`, `collections`), pytest.

**Spec:** `docs/superpowers/plans/2026-07-28-error-classification-port-spec.md`. Read §3 (one taxonomy), §4 (why surface ≠ collect_error), and §6 (storage design) before starting — those are contracts, not suggestions.

**Provenance:** every code block below is a port of code that already passed 164 tests and a codex review round in `john-v3-multi-lang`, with the taxonomy renamed per spec §3. Where a block says *verbatim*, it means the fork's version was correct and should not be re-derived.

## Work on `node-producer`, not `main`

`/opt/ratbench` — where Task 8 runs — is on **`node-producer`**, which is 18 commits ahead of
`main` and is a strict superset of it. `schema.py`, `metrics.py`, `unified_bench.py` and
`report/case_study.py` are **byte-identical** on both branches, so every file this plan touches
applies cleanly; only `measure.py` and `languages/` diverged, which is why the citations here are
`node-producer` line numbers. Branch from `node-producer` and record its test baseline, not
`main`'s (`main` is 128 passed / 6 skipped / 1 deselected; `node-producer` has additional tests).

## Global Constraints

- **stdlib only.** No new dependencies.
- **Immutable data.** All new types are `@dataclass(frozen=True)`, matching `bench/bench/schema.py`.
- **Never introduce a status name that is not already in `schema.py:40-42`.** The whole point of this port is one vocabulary. `no_env`, `setup_failed`, `zero_pass` and friends do **not** exist here.
- **`compute_metrics` output must stay byte-identical.** Existing consumers read `metrics.json`. Error output goes to a separate `errors.json`.
- **Backward compatibility.** `unified_bench.py:53` rehydrates stored `row.json` via `MeasureRow(agent=agent, **d)`. Every new field MUST have a default, and `load_rows` must filter to known fields.
- **Package layout.** The importable package is `bench/bench/`; tests live in `bench/tests/bench/`. Run tests from the `bench/` directory: `cd bench && python3 -m pytest tests/bench/<file> -q`.
- **`python3`, not `python`.**
- **`bench/report/` is a package**, not a module. The new file is `bench/bench/report/error_report.py`.
- **Test style:** match `bench/tests/bench/test_metrics_gates.py` — plain asserts, `pytest.mark.parametrize` for tables, a local `_row(**kw)` helper.
- **Commit format:** `<type>(bench): <description>`.

## Landing split

| | tasks | pays off |
|---|---|---|
| **Landing A** | 1–6, 8 | immediately — `--aggregate-only` over the 100 stored rows, zero docker |
| **Landing B** | 7 (row.json block) | needs a `measure()` change and a re-measure |
| **After 8 passes** | 9 (delete the fork) | gated on the acceptance run |

**Landing A is not as inert as it looks.** `runner/cli.py:228` shells out to
`python -m bench.unified_bench --harvest …` (no `--aggregate-only`) after every real run. So
**Task 1 alone** starts writing six new keys into every future `row.json`, whether or not
Tasks 2–9 land. That is harmless to `compute_metrics` (all defaulted, and `metrics.py` never
references them), but any *unported* checkout reading the same `out_root` will crash with
`TypeError: unexpected keyword argument 'error_surface'`. Task 6's `fields()` filter protects the
ported checkout from newer rows; it cannot protect an older checkout, because that requires
patching code this plan does not touch. **Do not share an `out_root` between ported and unported
checkouts.**

---

### Task 1: Schema types

**Files:**
- Modify: `bench/bench/schema.py` (append 3 dataclasses; add 6 fields to `MeasureRow`)
- Test: `bench/tests/bench/test_error_schema.py`

**Interfaces:**
- Produces: `ErrorEvent(token, group, group_kind, category, source, occurrences, raw)`, `RepoVerdict(agent, repo, status, bucket, status_derived, status_flags, error_surface, surface_reason, collect_rc, build_ok, pass_rate, turns_used)`, `ArmErrorReport(...)`, and `MeasureRow` fields `error_surface`, `error_surface_reason`, `status_flags`, `repo_toplevel`.

- [ ] **Step 1: Write the failing test**

```python
# tests/bench/test_error_schema.py
import dataclasses
import json
from dataclasses import asdict

import pytest

from bench.schema import ArmErrorReport, ErrorEvent, MeasureRow, RepoVerdict


def test_error_event_defaults_and_frozen():
    e = ErrorEvent(token="ModuleNotFoundError", group="wrapt", group_kind="module",
                   category="module_not_found", source="collect")
    assert e.occurrences == 1 and e.raw == ""
    # FrozenInstanceError's message is "cannot assign to field 'token'" - it does NOT
    # contain the word "frozen". Match on the exception type.
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.token = "x"


def test_repo_verdict_uses_ratbench_status_vocabulary():
    v = RepoVerdict(agent="a", repo="o/r", status="collect_error", bucket="collect_error",
                    error_surface="masked", surface_reason="startup_abort")
    assert v.status_derived is False and v.status_flags == ()


def test_arm_error_report_defaults_are_empty():
    r = ArmErrorReport(agent="a", n_repos=0)
    assert r.token_repos == {} and r.crosstab == {} and r.n_status_derived == 0
    assert r.source == "collect"


def test_new_measure_row_fields_default_empty():
    row = MeasureRow(agent="a", repo="o/r", env_status="ok", build_ok=True)
    assert row.error_surface == "" and row.error_surface_reason == ""
    assert row.status_flags == () and row.repo_toplevel == ()
    assert row.status == "ok"          # existing default, unchanged


def test_old_row_json_without_new_fields_still_rehydrates():
    # unified_bench.py:53 does MeasureRow(agent=..., **d) over stored row.json.
    # The 100 rows in /opt/ratbench/remeasure_50 predate ALL of these fields, and `status` too.
    old = {"repo": "o/r", "env_status": "ok", "build_ok": True, "collect_rc": 2,
           "collect_errors": ["E   ModuleNotFoundError: No module named 'x'"],
           "pass_rate": 0.0, "meta": {}}
    d = {k: (tuple(v) if isinstance(v, list) else v) for k, v in json.loads(json.dumps(old)).items()}
    row = MeasureRow(agent="baseline", **d)
    # asdict() PRESERVES tuple-typed fields as tuples — `() == []` is False, so asserting a
    # list here can never pass regardless of implementation.
    assert row.error_surface == "" and asdict(row)["status_flags"] == ()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd bench && python3 -m pytest tests/bench/test_error_schema.py -q`
Expected: FAIL with `ImportError: cannot import name 'ArmErrorReport' from 'bench.schema'`

- [ ] **Step 3: Write minimal implementation**

Append to `bench/bench/schema.py`:

```python
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
```

And append these fields to `MeasureRow`, after `meta`:

```python
    error_surface: str = ""            # full | masked | unobserved — set by verdict.error_surface
    error_surface_reason: str = ""
    status_flags: tuple = ()           # what first-match-wins hid (spec 6.3)
    repo_toplevel: tuple = ()          # importable roots under /testbed; enables the internal split
    run_failed_lines: tuple = ()       # FAILED/ERROR lines from the run pass; source="run" events
    language: str = "python"           # keys the signature table; RepoSpec has it, MeasureRow did not
```

All six default, so no existing construction site changes and stored rows keep loading.

**Two of these are easy to get wrong.** `language` exists on `RepoSpec` (`schema.py:12`) but
**not** on `MeasureRow` — a grep for `language:` in `schema.py` hits `RepoSpec` and looks like a
false positive for `MeasureRow`. And `run_failed_lines` must exist even though nothing populates
it yet, or the per-source split in Tasks 5–6 has no field to read and its tests cannot construct
a fixture.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd bench && python3 -m pytest tests/bench/test_error_schema.py -q`
Expected: 5 passed

- [ ] **Step 5: Verify nothing broke**

Run: `cd bench && python3 -m pytest tests/bench -q -m "not slow"`
Expected: no regressions (record the baseline count first)

- [ ] **Step 6: Commit**

```bash
git add bench/bench/schema.py bench/tests/bench/test_error_schema.py
git commit -m "feat(bench): ErrorEvent/RepoVerdict/ArmErrorReport + error-surface fields"
```

---

### Task 2: Token and capture-group extraction

**Files:**
- Create: `bench/bench/errors.py`
- Test: `bench/tests/bench/test_error_extraction.py`

**Interfaces:**
- Produces: `extract_events(lines, *, source, repo_toplevel=(), language="python") -> tuple[ErrorEvent, ...]`, `is_warning_only(line) -> bool`, `categorize(...)` (stubbed here, real in Task 3), `CATEGORIES`.

- [ ] **Step 1: Write the failing test**

```python
# tests/bench/test_error_extraction.py
from bench.errors import extract_events, is_warning_only

REAL = [
    "E   ModuleNotFoundError: No module named 'gguf'",
    "E   ImportError: cannot import name '_accelerate' from 'qiskit'",
    "E   ImportError: libGL.so.1: cannot open shared object file: No such file or directory",
    "E   ConnectionRefusedError: [Errno 61] Connection refused",
    "E   FileNotFoundError: [Errno 2] No such file or directory: '/opt/missing.cfg'",
    "E   redis.exceptions.ConnectionError: Error 111 connecting to localhost:6379.",
]


def test_token_is_verbatim_fqn_not_leaf():
    tokens = [e.token for e in extract_events(REAL, source="collect")]
    assert "redis.exceptions.ConnectionError" in tokens and "ConnectionError" not in tokens


def test_capture_groups_by_kind():
    ev = extract_events(REAL, source="collect")
    by_token = {e.token: e for e in ev}
    assert (by_token["ModuleNotFoundError"].group,
            by_token["ModuleNotFoundError"].group_kind) == ("gguf", "module")
    assert by_token["FileNotFoundError"].group_kind == "path"
    kinds = {e.group_kind: e.group for e in ev if e.token == "ImportError"}
    assert kinds["soname"] == "libGL.so.1" and kinds["name"] == "_accelerate"


def test_token_is_the_exception_label_not_a_test_name():
    # on a FAILED line the first *Error identifier is the TEST NAME; anchoring on the
    # trailing colon is what stops a node id impersonating an exception
    ev = extract_events(
        ["FAILED tests/test_client.py::test_raises_ValueError - AssertionError: assert 1 == 2"],
        source="run")
    assert ev[0].token == "AssertionError"


def test_class_scoped_node_id_cannot_impersonate_an_exception():
    ev = extract_events(["FAILED t.py::TestFooError::test_x - ValueError: bad"], source="run")
    assert ev[0].token == "ValueError"


def test_anchored_module_pattern_wins_over_the_bare_soname_pattern():
    for line, kind, group in [
        ("E   ModuleNotFoundError: No module named 'libs.something'", "module", "libs.something"),
        ("E   ModuleNotFoundError: No module named 'library.sockets'", "module", "library.sockets"),
        ("E   ImportError: cannot import name 'x' from 'libs.solver'", "name", "x"),
    ]:
        ev = extract_events([line], source="collect")
        assert (ev[0].group_kind, ev[0].group) == (kind, group), line


def test_soname_is_not_fabricated_from_a_dotted_module_name():
    ev = extract_events(
        ["E   AttributeError: module 'matplotlib.something' has no attribute 'widget'"],
        source="collect")
    assert (ev[0].token, ev[0].group, ev[0].group_kind) == ("AttributeError", "", "")


def test_real_soname_still_resolves():
    ev = extract_events(["E   ImportError: libstdc++.so.6: cannot open shared object file"],
                        source="collect")
    assert (ev[0].group_kind, ev[0].group) == ("soname", "libstdc++.so.6")


def test_exception_group_is_not_dropped():
    ev = extract_events(["E   ExceptionGroup: several errors (2 sub-exceptions)"], source="collect")
    assert len(ev) == 1 and ev[0].token == "ExceptionGroup"


def test_cascade_dedups_to_one_event_with_occurrences():
    lines = ["E   ModuleNotFoundError: No module named 'cascade_absent_pkg_qq'"] * 3
    ev = extract_events(lines, source="collect")
    assert len(ev) == 1 and ev[0].occurrences == 3


def test_distinct_groups_are_distinct_events():
    ev = extract_events(["E   ModuleNotFoundError: No module named 'wrapt'",
                         "E   ModuleNotFoundError: No module named 'httpx'"], source="collect")
    assert len(ev) == 2


def test_warnings_are_not_events():
    line = "/usr/lib/_pytest/config/__init__.py:331: PytestDeprecationWarning: unset"
    assert extract_events([line], source="collect") == () and is_warning_only(line) is True


def test_lines_without_a_token_are_skipped():
    assert extract_events(["2 tests collected, 1 error", ""], source="collect") == ()


def test_source_is_recorded_and_raw_truncated_to_200():
    ev = extract_events(["E   ModuleNotFoundError: No module named 'x' " + "y" * 500], source="run")
    assert ev[0].source == "run" and len(ev[0].raw) == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd bench && python3 -m pytest tests/bench/test_error_extraction.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'bench.errors'`

- [ ] **Step 3: Write minimal implementation**

Port verbatim — these three patterns each fixed a confirmed defect and must not be re-derived.

```python
# bench/errors.py
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
```

> **This was a live fabrication bug**, found by codex review against real pytest output and fixed
> in the Task 2 follow-up commit. The original `and not tok.endswith("Warning")` *continued*
> scanning past the warning label instead of stopping, so `<string>:2: UserWarning: RuntimeError:
> boom` — a line reporting no error at all — produced a counted `RuntimeError` event. It is not
> hypothetical: `measure.py:21` puts `Warning` in its own capture alternation on purpose, so every
> warning line in the corpus reaches `extract_events`. Two regression tests cover it
> (`test_a_warning_message_quoting_an_exception_does_not_fabricate_an_event`,
> `test_a_warning_label_does_not_shadow_an_error_that_came_first`). Do not revert to the scan.

```python


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


def categorize(token: str, group: str, group_kind: str,
               repo_toplevel: tuple = (), language: str = "python") -> str:
    return "uncategorized"      # replaced in Task 3


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd bench && python3 -m pytest tests/bench/test_error_extraction.py -q`
Expected: 13 passed

- [ ] **Step 5: Commit**

```bash
git add bench/bench/errors.py bench/tests/bench/test_error_extraction.py
git commit -m "feat(bench): exception-token + capture-group extraction with per-repo dedup"
```

---

### Task 3: Category derivation

**Files:**
- Modify: `bench/bench/errors.py` (replace the `categorize` stub, add `CATEGORIES`)
- Test: `bench/tests/bench/test_error_categories.py`

**Interfaces:**
- Produces: real `categorize(...)` returning one of `CATEGORIES`; `CATEGORIES` tuple exported for the report universe.

- [ ] **Step 1: Write the failing test**

```python
# tests/bench/test_error_categories.py
import pytest

from bench.errors import CATEGORIES, categorize, extract_events

TOPLEVEL = ("tests", "qiskit", "examples")


@pytest.mark.parametrize("token,group,kind,expected", [
    # harness_error FIRST so _pytest.* never reaches the ImportError branch
    ("_pytest.pathlib.ImportPathMismatchError", "", "", "harness_error"),
    ("UsageError", "", "", "harness_error"),
    ("ModuleNotFoundError", "wrapt", "module", "module_not_found"),
    ("ModuleNotFoundError", "tests.conftest", "module", "internal_import_failure"),
    ("ModuleNotFoundError", "examples.mlperf.models.llama", "module", "internal_import_failure"),
    ("ImportError", "libGL.so.1", "soname", "syslib_missing"),
    ("ImportError", "_accelerate", "name", "partial_import"),
    ("ImportError", "httpx", "module", "module_not_found"),
    ("ImportError", "qiskit.circuit", "module", "internal_import_failure"),
    ("ImportError", "", "", "import_failed"),
    ("ConnectionRefusedError", "", "", "service_unavailable"),
    ("redis.exceptions.ConnectionError", "", "", "service_unavailable"),
    ("docker.errors.DockerException", "", "", "service_unavailable"),
    ("SyntaxError", "", "", "syntax_error"),
    ("IndentationError", "", "", "syntax_error"),
    ("FileNotFoundError", "/opt/x.cfg", "path", "file_missing"),
    # the singleton tail stays uncategorized ON PURPOSE
    ("dash.exceptions.PageError", "", "", "uncategorized"),
    ("RuntimeError", "", "", "uncategorized"),
    # OperationalError is "no such table"/config far more often than a refused connection,
    # and the token cannot tell them apart — it must NOT inflate service_unavailable
    ("sqlalchemy.exc.OperationalError", "", "", "uncategorized"),
    ("sqlite3.OperationalError", "", "", "uncategorized"),
])
def test_category_table(token, group, kind, expected):
    assert categorize(token, group, kind, TOPLEVEL) == expected


def test_internal_split_defaults_to_external_without_repo_toplevel():
    # with no roots we must NOT guess internal — that would fabricate the split
    assert categorize("ModuleNotFoundError", "tests.conftest", "module", ()) == "module_not_found"


def test_only_first_dotted_segment_is_matched():
    assert categorize("ModuleNotFoundError", "tests", "module", ("tests",)) == "internal_import_failure"
    assert categorize("ModuleNotFoundError", "testsuite", "module", ("tests",)) == "module_not_found"


@pytest.mark.parametrize("language", ["go", "rust", "java", "nodejs"])
def test_non_python_languages_are_unpopulated_not_wrong(language):
    assert categorize("ModuleNotFoundError", "wrapt", "module", (), language) == "uncategorized"


def test_extract_events_threads_category_through():
    ev = extract_events(["E   ModuleNotFoundError: No module named 'tests.conftest'"],
                        source="collect", repo_toplevel=("tests",))
    assert ev[0].category == "internal_import_failure"


def test_categories_constant_covers_every_table_output():
    outs = {categorize(t, g, k, TOPLEVEL) for t, g, k in
            [("ModuleNotFoundError", "wrapt", "module"), ("ImportError", "libGL.so.1", "soname"),
             ("UsageError", "", ""), ("RuntimeError", "", "")]}
    assert outs <= set(CATEGORIES)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd bench && python3 -m pytest tests/bench/test_error_categories.py -q`
Expected: FAIL — `cannot import name 'CATEGORIES'`

- [ ] **Step 3: Write minimal implementation**

Replace the `categorize` stub in `bench/bench/errors.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd bench && python3 -m pytest tests/bench/test_error_categories.py tests/bench/test_error_extraction.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add bench/bench/errors.py bench/tests/bench/test_error_categories.py
git commit -m "feat(bench): category derivation over (token, group, repo_toplevel), language-keyed"
```

---

### Task 4: `error_surface`, `legacy_status`, `bucket`

**Files:**
- Create: `bench/bench/verdict.py`
- Test: `bench/tests/bench/test_error_verdict.py`

**Interfaces:**
- Produces: `error_surface(row) -> (surface, reason)`, `legacy_status(row) -> str`, `resolve_status(row) -> (status, derived)`, `bucket(status, pass_rate, threshold=0.8) -> str`, `status_flags(row) -> tuple`, `verdict(row, *, turn_cap=None, threshold=0.8) -> RepoVerdict`.

**This is the task where the port could go wrong.** `legacy_status` must emit **only names already in `schema.py:40-42`**. If you find yourself writing `no_env` or `setup_failed`, stop — that is the fork's vocabulary and the whole point of this port is to not have two.

> **It DID go wrong here, in a way no unit test caught.** The code below originally read
> `if row.env_status != "ok"` in both `error_surface` and `legacy_status`. That is the fork's
> predicate, and this repo does not say `"ok"`: `measure.py:19` defines
> `_MEASURABLE = ("ok", "legacy_ok", "produced")`, and `measure.py:245` tests membership in
> **that set** before deciding `missing`. The real 50-row fixture at
> `bench/tests/bench/data/py50_rows.json` is **45 `produced` / 5 `legacy_missing`** — zero `"ok"`.
> So the ported predicate scored **all 50 rows** `("unobserved", "no_env")`, 43 of them with a
> successful build, and made `legacy_status` return `missing` for every single one.
>
> Every unit test in Task 4 still passed, because they all build rows with `env_status="ok"`.
> Only running against real rows exposed it. That is why
> `test_the_real_corpus_is_not_uniformly_unobserved` exists — a canary over the fixture — and why
> `_MEASURABLE` is **imported** from `bench.measure` rather than re-declared. A local copy is
> exactly what lets this drift back in.
>
> The same review round found `status_flags` fabricating flags from dataclass defaults. See the
> note on that function below. Both are fixed in the Task 4 follow-up commit; do not revert
> either.

- [ ] **Step 1: Write the failing test**

```python
# tests/bench/test_error_verdict.py
import pytest

from bench.schema import MeasureRow
from bench.verdict import (bucket, error_surface, legacy_status, resolve_status,
                           status_flags, verdict)


def _row(**kw):
    base = dict(agent="a", repo="o/r", env_status="ok", build_ok=True, collect_rc=0,
                collect_clean=True, collected_node_ids=("tests/a.py::t",), executed=True,
                pass_rate=1.0, timed_out=False, meta={})
    base.update(kw)
    return MeasureRow(**base)


# ---- error_surface -------------------------------------------------------------------
def test_per_module_failure_with_tests_collected_is_FULL_not_masked():
    # THE case that distinguishes surface from status: measure.py sets collect_error whenever
    # collect_clean is false, regardless of how many tests were collected (spec section 4)
    r = _row(collect_rc=2, collect_clean=False,
             collected_node_ids=tuple(f"t{i}.py::x" for i in range(500)))
    assert error_surface(r) == ("full", "")


def test_rc4_with_nothing_collected_is_masked_startup_abort():
    assert error_surface(_row(collect_rc=4, collect_clean=False, collected_node_ids=())) \
        == ("masked", "startup_abort")


def test_rc2_collecting_nothing_is_masked_not_full():
    # 9/50 baseline repos exit rc=2 having collected ZERO tests; an rc-in-{3,4} detector
    # scores these full and under-counts masking by more than half
    assert error_surface(_row(collect_rc=2, collect_clean=False, collected_node_ids=())) \
        == ("masked", "nothing_collected")


def test_rc5_no_tests_collected_is_masked_not_a_clean_full_surface():
    assert error_surface(_row(collect_rc=5, collect_clean=True, collected_node_ids=())) \
        == ("masked", "nothing_collected")


def test_build_failure_is_unobserved_not_clean():
    assert error_surface(_row(build_ok=False)) == ("unobserved", "build_failed")


def test_missing_env_is_unobserved():
    assert error_surface(_row(env_status="missing", build_ok=False)) == ("unobserved", "no_env")


# ---- legacy_status: MUST use this repo's vocabulary ----------------------------------
@pytest.mark.parametrize("kw,expected", [
    (dict(env_status="missing", build_ok=False), "missing"),
    (dict(build_ok=False, meta={"error": "CalledProcessError(125, ['docker','run'])"}),
     "measure_error"),
    (dict(build_ok=False, meta={"error": "OSError(28, 'No space left on device')"}),
     "measure_error"),
    (dict(build_ok=False), "build_fail"),
    (dict(timed_out=True), "timed_out"),
    # NOT "collect_error": measure.py assigns non_conforming/empty_testbed/gate_fail at gates
    # BEFORE the :344 block, from inputs that are never persisted. A collect-unclean legacy row
    # could be any of the four, so naming one would be a silent mislabelling (spec 3.1.1).
    (dict(collect_clean=False), "unknown_conformance"),
    (dict(executed=True), "executed"),
    (dict(executed=False), "no_tests_collected"),
])
def test_legacy_status_emits_only_existing_vocabulary(kw, expected):
    assert legacy_status(_row(**kw)) == expected


def test_legacy_status_never_invents_the_forks_vocabulary():
    from bench.verdict import LEGACY_STATUSES
    forbidden = {"no_env", "setup_failed", "zero_pass", "partial", "success", "infra_error"}
    assert not (set(LEGACY_STATUSES) & forbidden)


def test_unknown_conformance_is_the_only_new_name():
    from bench.verdict import LEGACY_STATUSES
    existing = {"unmeasurable", "error", "missing", "measure_error", "build_fail",
                "non_conforming", "empty_testbed", "no_tests_collected", "collect_error",
                "timed_out", "executed", "gate_fail"}
    assert set(LEGACY_STATUSES) - existing == {"unknown_conformance"}


def test_backfill_never_claims_a_status_it_cannot_know():
    # a non_conforming row still runs collect, so collect_clean is usually False. Labelling it
    # collect_error would be confidently wrong.
    assert legacy_status(_row(collect_clean=False)) != "collect_error"


# ---- resolve_status ------------------------------------------------------------------
def test_measured_status_wins_and_is_not_marked_derived():
    assert resolve_status(_row(status="gate_fail")) == ("gate_fail", False)


def test_absent_or_legacy_status_is_backfilled_and_marked():
    assert resolve_status(_row(status="legacy_ok", build_ok=False)) == ("build_fail", True)
    assert resolve_status(_row(status="ok", timed_out=True)) == ("timed_out", True)


# ---- bucket --------------------------------------------------------------------------
def test_bucket_passes_non_executed_status_through():
    assert bucket("collect_error", 0.0) == "collect_error"
    assert bucket("build_fail", 0.0) == "build_fail"


def test_bucket_splits_executed_on_pass_rate():
    assert bucket("executed", 0.0) == "zero_pass"
    assert bucket("executed", 0.5) == "partial"
    assert bucket("executed", 0.8) == "success"
    assert bucket("executed", 0.79) == "partial"


# ---- flags ---------------------------------------------------------------------------
def test_status_flags_carry_what_the_ordering_hid():
    r = _row(timed_out=True, build_ok=False, collect_clean=False)
    assert legacy_status(r) == "build_fail"          # build_ok checked before timed_out
    assert "timed_out" in status_flags(r)            # ...but the flag preserves it


# ---- unconverged: threaded through 9 call sites, so it needs its own coverage ----------
def test_unconverged_flag_requires_an_explicit_turn_cap():
    # num_turn is never written into bench_meta, so a meta fallback would silently read as
    # "everything converged"
    r = _row(turns_used=30, pass_rate=0.0)
    assert "unconverged" not in verdict(r).status_flags
    assert "unconverged" in verdict(r, turn_cap=30).status_flags


def test_unconverged_is_false_when_the_run_succeeded_at_the_cap():
    assert "unconverged" not in verdict(_row(turns_used=30, pass_rate=1.0), turn_cap=30).status_flags


def test_unconverged_tolerates_missing_turns_used():
    assert "unconverged" not in verdict(_row(turns_used=None, pass_rate=0.0), turn_cap=30).status_flags


def test_unconverged_is_a_flag_never_a_bucket():
    v = verdict(_row(turns_used=30, pass_rate=0.5, status="executed"), turn_cap=30)
    assert v.bucket == "partial" and "unconverged" in v.status_flags


def test_verdict_combines_everything():
    v = verdict(_row(collect_rc=4, collect_clean=False, collected_node_ids=(),
                     executed=False, pass_rate=0.0))
    # collect_clean False on a legacy row is AMBIGUOUS between collect_error / non_conforming /
    # empty_testbed / gate_fail, so the backfill names the ambiguity
    assert v.status == "unknown_conformance" and v.bucket == "unknown_conformance"
    assert (v.error_surface, v.surface_reason) == ("masked", "startup_abort")
    assert v.status_derived is True and v.repo == "o/r"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd bench && python3 -m pytest tests/bench/test_error_verdict.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'bench.verdict'`

- [ ] **Step 3: Write minimal implementation**

```python
# bench/verdict.py
from __future__ import annotations

import re

from bench.measure import _MEASURABLE   # ("ok", "legacy_ok", "produced") — measure.py:19.
from bench.schema import RepoVerdict    # IMPORTED, never re-declared. See the warning above.

# Infra is identified from the stored exception repr (unified_bench.py:32). This is a string match
# on the ONE branch that empties a denominator, so it is provisional: the count must be reported,
# never silently excluded. A structured infra_returncode on the row would retire it.
_INFRA = re.compile(r"CalledProcessError\(125|DockerException|No space left|Errno 28")

DEFAULT_THRESHOLD = 0.8

# Every value legacy_status can emit. All but one already exist in schema.py:40-42.
# `unknown_conformance` is the SINGLE deliberate addition (spec 3.1.1): measure.py assigns
# non_conforming / empty_testbed / gate_fail at gates before the :344 block, from inputs that are
# never persisted (contract.py's `reason` is dropped; py_test_files is 0 on every failing
# branch). A collect-unclean legacy row could be any of those four, so we name the ambiguity
# instead of picking one. It is NOT added to metrics._DISQUALIFIED — it changes no denominator.
LEGACY_STATUSES = ("missing", "measure_error", "build_fail", "timed_out", "unknown_conformance",
                   "executed", "no_tests_collected")
_ALREADY_MEASURED = ("", "ok", "legacy_ok")


def error_surface(row) -> tuple:
    """(surface, reason). POSITIVE structural test: did collection produce tests? A per-module
    failure still collects the other modules; a startup abort collects nothing. This is NOT
    `collect_error` — measure.py:346 sets that whether or not tests were collected."""
    if row.env_status not in _MEASURABLE:
        return "unobserved", "no_env"
    if not row.build_ok:
        return "unobserved", "build_failed"
    if row.collect_rc != 0 and not (row.collected_node_ids or ()):
        return "masked", ("startup_abort" if row.collect_rc in (3, 4) else "nothing_collected")
    return "full", ""


def legacy_status(row) -> str:
    """Backfill for rows measured before `status` existed. Covers 7 of 12 statuses: non_conforming/
    empty_testbed/gate_fail are assigned at gates BEFORE measure.py:344 from inputs that are never
    persisted, so they are unreachable here (spec 3.1.1)."""
    if row.env_status not in _MEASURABLE:
        return "missing"
    if _INFRA.search(str((row.meta or {}).get("error") or "")):
        return "measure_error"
    if not row.build_ok:
        return "build_fail"
    if row.timed_out:
        return "timed_out"
    if not row.collect_clean:
        return "unknown_conformance"   # NOT collect_error — see LEGACY_STATUSES above
    return "executed" if row.executed else "no_tests_collected"


def resolve_status(row) -> tuple:
    """(status, was_derived). A measured status always wins."""
    s = getattr(row, "status", "") or ""
    if s not in _ALREADY_MEASURED:
        return s, False
    return legacy_status(row), True


def bucket(status: str, pass_rate: float, threshold: float = DEFAULT_THRESHOLD) -> str:
    """`status` lumps everything that ran into `executed`, which collapses real signal. Sub-divide
    ONLY that value — never introduce a name that competes with the status vocabulary."""
    if status != "executed":
        return status
    if pass_rate <= 0:
        return "zero_pass"
    return "partial" if pass_rate < threshold else "success"


def status_flags(row) -> tuple:
    """First-match-wins is what makes buckets sum to n, but it is lossy. Flags carry the rest.

    EVERY FLAG NEEDS EVIDENCE THAT ITS STAGE ACTUALLY RAN. `build_ok` is False and
    `collect_clean` is False on a row that never reached those stages — the first because
    measure.py:245 short-circuits before docker.build, the second because it is simply the
    dataclass default. Flagging off the raw booleans invents conditions: on the real 50-row
    fixture, all 7 rows with `collect_rc is None` (collect never ran) were flagged
    `collect_error`. A fabricated flag is worse than a missing one — it inflates the count of
    repos said to have hit a condition, and flags exist precisely to be read as evidence.
    """
    flags = []
    if row.timed_out:                       # explicit bool, only ever set True by an observation
        flags.append("timed_out")
    if not row.build_ok and row.env_status in _MEASURABLE:
        flags.append("build_fail")          # ...the env was buildable, so the build really failed
    if not row.collect_clean and row.collect_rc is not None:
        flags.append("collect_error")       # collect_rc is None until collect actually runs
    chosen, _ = resolve_status(row)
    return tuple(f for f in flags if f != chosen)


def verdict(row, *, turn_cap=None, threshold: float = DEFAULT_THRESHOLD) -> RepoVerdict:
    surface, reason = error_surface(row)
    status, derived = resolve_status(row)
    flags = list(status_flags(row))
    if turn_cap and (row.turns_used or 0) >= turn_cap and row.pass_rate < threshold:
        flags.append("unconverged")
    return RepoVerdict(
        agent=row.agent, repo=row.repo, status=status,
        bucket=bucket(status, row.pass_rate, threshold),
        error_surface=surface, surface_reason=reason, status_derived=derived,
        status_flags=tuple(flags), collect_rc=row.collect_rc, build_ok=row.build_ok,
        pass_rate=row.pass_rate, turns_used=row.turns_used)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd bench && python3 -m pytest tests/bench/test_error_verdict.py -q`
Expected: all pass (the parametrized cases make an exact count brittle — do not chase a number)

- [ ] **Step 5: Commit**

```bash
git add bench/bench/verdict.py bench/tests/bench/test_error_verdict.py
git commit -m "feat(bench): error_surface + legacy_status backfill in the existing vocabulary"
```

---

### Task 5: Per-arm report

**Files:**
- Create: `bench/bench/report/error_report.py`
- Test: `bench/tests/bench/test_error_report.py`

**Interfaces:**
- Produces: `row_events(row) -> tuple[ErrorEvent, ...]`, `arm_report(rows, *, source="collect", turn_cap=None, threshold=0.8) -> ArmErrorReport`.

- [ ] **Step 1: Write the failing test**

```python
# tests/bench/test_error_report.py
import pytest

from bench.report.error_report import arm_report, row_events
from bench.schema import MeasureRow

MNF_W = "E   ModuleNotFoundError: No module named 'wrapt'"
MNF_H = "E   ModuleNotFoundError: No module named 'httpx'"
MNF_T = "E   ModuleNotFoundError: No module named 'tests.conftest'"
RUN_BOTO = "FAILED tests/a.py::t - ModuleNotFoundError: No module named 'boto3'"
RUN_ASSERT = "FAILED tests/b.py::t - AssertionError: expected 3 got 4"


def _row(repo, **kw):
    # collected_node_ids MUST be non-empty for a `full` surface
    base = dict(agent="a", repo=repo, env_status="ok", build_ok=True, status="executed",
                collect_rc=0, collect_clean=True, collected_node_ids=("tests/a.py::t",),
                executed=True, collect_errors=(), repo_toplevel=(), pass_rate=0.5,
                timed_out=False, meta={})
    base.update(kw)
    return MeasureRow(**base)


def test_counting_unit_is_repo_presence_not_event_volume():
    rows = [_row("o/big", collect_errors=tuple([MNF_W] * 40)),
            _row("o/a", collect_errors=(MNF_H,)), _row("o/b", collect_errors=(MNF_H,))]
    r = arm_report(rows)
    assert r.token_repos["ModuleNotFoundError"] == 3
    assert r.token_events["ModuleNotFoundError"] == 3     # deduped per repo, not 42


def test_masked_repos_are_counted_but_contribute_no_events():
    rows = [_row("o/full", collect_errors=(MNF_W,)),
            _row("o/masked", collect_rc=4, collect_clean=False, collected_node_ids=(),
                 collect_errors=(MNF_H,))]
    r = arm_report(rows)
    assert r.n_masked == 1 and r.n_admissible == 1
    assert r.token_repos["ModuleNotFoundError"] == 1 and "httpx" not in str(r.top_groups)


def test_collect_and_run_are_never_pooled():
    rows = [_row("o/x", collect_errors=(MNF_W,), run_failed_lines=(RUN_BOTO, RUN_ASSERT))]
    c, n = arm_report(rows, source="collect"), arm_report(rows, source="run")
    assert c.source == "collect" and n.source == "run"
    assert c.token_events == {"ModuleNotFoundError": 1}
    assert c.uncategorized_rate == 0.0        # NOT 0.5 — the AssertionError is a CODE failure
    assert n.token_events == {"ModuleNotFoundError": 1, "AssertionError": 1}


def test_crosstab_is_keyed_on_bucket_not_status():
    rows = [_row("o/x", status="executed", pass_rate=0.5, collect_errors=(MNF_W,))]
    assert arm_report(rows).crosstab["partial|module_not_found"] == 1


def test_derived_status_is_counted_loudly():
    rows = [_row("o/x", status="", collect_errors=(MNF_W,)),
            _row("o/y", status="executed", collect_errors=(MNF_W,))]
    assert arm_report(rows).n_status_derived == 1


def test_internal_split_uses_repo_toplevel_from_the_row():
    rows = [_row("o/x", collect_errors=(MNF_T, MNF_W), repo_toplevel=("tests", "src"))]
    r = arm_report(rows)
    assert r.category_repos["internal_import_failure"] == 1
    assert r.category_repos["module_not_found"] == 1


def test_top_groups_counts_repos_not_occurrences():
    rows = [_row("o/x", collect_errors=(MNF_W, MNF_W, MNF_W)), _row("o/y", collect_errors=(MNF_W,))]
    assert dict(arm_report(rows).top_groups["module_not_found"])["wrapt"] == 2


def test_unobserved_rows_counted_and_excluded():
    rows = [_row("o/dead", build_ok=False, status="build_fail"), _row("o/live", collect_errors=(MNF_W,))]
    r = arm_report(rows)
    assert r.n_unobserved == 1 and r.n_repos == 2 and r.n_admissible == 1
    assert r.buckets["build_fail"] == 1


def test_duplicate_repo_rows_are_rejected_not_silently_miscounted():
    with pytest.raises(ValueError, match="duplicate repo"):
        arm_report([_row("o/x"), _row("o/x")])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd bench && python3 -m pytest tests/bench/test_error_report.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'bench.report.error_report'`

- [ ] **Step 3: Write minimal implementation**

```python
# bench/report/error_report.py
from __future__ import annotations

from collections import Counter

from bench.errors import CATEGORIES, extract_events
from bench.schema import ArmErrorReport
from bench.verdict import DEFAULT_THRESHOLD, verdict


def row_events(row) -> tuple:
    """Collect-pass and run-pass events, tagged by source and never pooled (spec section 8)."""
    tl = row.repo_toplevel or ()
    lang = row.language or "python"
    return (extract_events(row.collect_errors, source="collect", repo_toplevel=tl, language=lang)
            + extract_events(row.run_failed_lines, source="run", repo_toplevel=tl, language=lang))


def arm_report(rows, *, source: str = "collect", turn_cap=None,
               threshold: float = DEFAULT_THRESHOLD) -> ArmErrorReport:
    """One report PER SOURCE (spec section 8)."""
    dupes = sorted({r.repo for r in rows if sum(1 for x in rows if x.repo == r.repo) > 1})
    if dupes:
        raise ValueError(f"arm_report: duplicate repo rows would corrupt repo-level counts: {dupes}")

    verdicts = {r.repo: verdict(r, turn_cap=turn_cap, threshold=threshold) for r in rows}
    buckets = Counter(v.bucket for v in verdicts.values())
    surfaces = Counter(v.error_surface for v in verdicts.values())

    token_repos, token_events = Counter(), Counter()
    cat_repos, cat_events, crosstab = Counter(), Counter(), Counter()
    groups: dict = {}
    n_events = n_uncat = 0

    for r in rows:
        v = verdicts[r.repo]
        if v.error_surface != "full":          # only admissible repos contribute events
            continue
        evs = tuple(e for e in row_events(r) if e.source == source)
        for e in evs:
            token_events[e.token] += 1
            cat_events[e.category] += 1
            n_events += 1
            n_uncat += (e.category == "uncategorized")
        for tok in {e.token for e in evs}:
            token_repos[tok] += 1
        for cat in {e.category for e in evs}:
            cat_repos[cat] += 1
            crosstab[f"{v.bucket}|{cat}"] += 1
        for cat, grp in {(e.category, e.group) for e in evs if e.group}:
            groups.setdefault(cat, Counter())[grp] += 1

    return ArmErrorReport(
        agent=(rows[0].agent if rows else ""), n_repos=len(rows), source=source,
        n_admissible=surfaces["full"], n_masked=surfaces["masked"],
        n_unobserved=surfaces["unobserved"],
        n_status_derived=sum(1 for v in verdicts.values() if v.status_derived),
        buckets=dict(buckets), surfaces=dict(surfaces),
        token_repos=dict(token_repos), token_events=dict(token_events),
        category_repos=dict(cat_repos), category_events=dict(cat_events),
        crosstab=dict(crosstab),
        top_groups={c: [list(x) for x in g.most_common(10)] for c, g in groups.items()},
        uncategorized_rate=round(n_uncat / n_events, 4) if n_events else 0.0)
```

`run_failed_lines` is a Task-1 field that nothing populates yet, so on every stored row it is
empty and the `run` report is empty. That is correct and is asserted in the Task 8 gate: a
**non-empty** run report on the current corpus would mean the sources got pooled.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd bench && python3 -m pytest tests/bench/test_error_report.py -q`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add bench/bench/report/error_report.py bench/tests/bench/test_error_report.py
git commit -m "feat(bench): per-arm error report — repo-level counting, bucket crosstab"
```

---

### Task 6: Interval delta, masking 2×2, and the `errors.json` wiring

**Files:**
- Modify: `bench/bench/report/error_report.py`, `bench/bench/unified_bench.py`
- Test: `bench/tests/bench/test_error_delta.py`, `bench/tests/bench/test_error_aggregation.py`

**Interfaces:**
- Produces: `category_status`, `interval_delta`, `masking_2x2`, `paired_repos`, `delta` in `error_report.py`; `load_rows`, `aggregate_errors`, CLI flags `--turn-cap` / `--delta` in `unified_bench.py`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/bench/test_error_delta.py
from bench.report.error_report import (category_status, delta, interval_delta,
                                       masking_2x2, paired_repos)
from bench.schema import MeasureRow

MNF_W = "E   ModuleNotFoundError: No module named 'wrapt'"
SVC = "E   redis.exceptions.ConnectionError: Error 111 connecting to localhost:6379."
UNI = ("module_not_found", "service_unavailable")


def _row(agent, repo, **kw):
    base = dict(agent=agent, repo=repo, env_status="ok", build_ok=True, status="executed",
                collect_rc=0, collect_clean=True, collected_node_ids=("tests/a.py::t",),
                executed=True, collect_errors=(), repo_toplevel=(), pass_rate=0.5,
                timed_out=False, meta={})
    base.update(kw)
    return MeasureRow(**base)


def _masked(agent, repo, **kw):
    return _row(agent, repo, collect_rc=4, collect_clean=False, collected_node_ids=(), **kw)


def test_masked_repo_contributes_its_known_cause_to_present_rest_unknown():
    s = category_status([_masked("A", "o/1", collect_errors=(MNF_W,))], UNI)
    assert s["module_not_found"] == {"present": 1, "absent": 0, "unknown": 0}
    assert s["service_unavailable"] == {"present": 0, "absent": 0, "unknown": 1}


def test_full_surface_without_the_category_is_absent_not_unknown():
    s = category_status([_row("A", "o/1", collect_errors=(MNF_W,))], UNI)
    assert s["service_unavailable"] == {"present": 0, "absent": 1, "unknown": 0}


def test_unobserved_repo_is_all_unknown():
    s = category_status([_row("A", "o/1", build_ok=False)], UNI)
    assert s["module_not_found"] == {"present": 0, "absent": 0, "unknown": 1}


def test_interval_is_identified_when_it_excludes_zero():
    a = [_row("A", f"o/{i}", collect_errors=(MNF_W,)) for i in (1, 2, 3)]
    b = [_row("B", f"o/{i}", collect_errors=(SVC,)) for i in (1, 2, 3)]
    iv = interval_delta(a, b)["module_not_found"]
    assert (iv["lower"], iv["upper"]) == (-3, -3) and iv["identified"] is True


def test_interval_spans_zero_when_masking_could_explain_the_move():
    a = [_row("A", "o/1", collect_errors=(MNF_W,)), _row("A", "o/2", collect_errors=(SVC,))]
    b = [_row("B", "o/1", collect_errors=(SVC,)), _masked("B", "o/2")]
    iv = interval_delta(a, b)["module_not_found"]
    assert iv["lower"] == -1 and iv["upper"] == 0 and iv["identified"] is False


def test_masking_2x2_exposes_a_regression_the_marginals_hide():
    a = [_masked("A", "o/1"), _masked("A", "o/2"), _row("A", "o/3")]
    b = [_row("B", "o/1"), _row("B", "o/2"), _masked("B", "o/3")]
    assert masking_2x2(a, b) == {"neither_masked": 0, "a_only_masked": 2, "b_only_masked": 1,
                                 "both_masked": 0, "only_in_a": 0, "only_in_b": 0}


def test_masking_2x2_does_not_fabricate_cells_for_a_repo_missing_from_an_arm():
    m = masking_2x2([_row("A", "o/a")], [_row("B", "o/b")])
    assert m["neither_masked"] == 0 and m["only_in_a"] == 1 and m["only_in_b"] == 1


def test_paired_set_is_a_sensitivity_row_not_the_headline():
    a = [_row("A", "o/1", collect_errors=(MNF_W,)), _masked("A", "o/2")]
    b = [_row("B", "o/1"), _row("B", "o/2", collect_errors=(MNF_W,))]
    d = delta(a, b)
    assert paired_repos(a, b) == frozenset({"o/1"})
    assert d["sensitivity_paired"]["paired_repos"] == 1
    assert "category_interval" in d and "category_repos_delta" not in d


def test_category_absent_from_both_arms_still_appears_with_zero_width_interval():
    iv = delta([_row("A", "o/1")], [_row("B", "o/1")])["category_interval"]["syslib_missing"]
    assert iv["lower"] == 0 and iv["upper"] == 0 and iv["identified"] is False
```

```python
# tests/bench/test_error_aggregation.py
import json
import os
from dataclasses import asdict

from bench.schema import MeasureRow
from bench.unified_bench import aggregate, aggregate_errors, load_rows, main

MNF = "E   ModuleNotFoundError: No module named 'wrapt'"


def _write(root, agent, repo, **kw):
    base = dict(agent=agent, repo=repo, env_status="ok", build_ok=True, status="executed",
                collect_rc=0, collect_clean=True, collected_node_ids=("tests/a.py::t",),
                executed=True, collect_errors=(MNF,), pass_rate=0.5, meta={})
    base.update(kw)
    p = os.path.join(root, agent, *repo.split("/"), "row.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(asdict(MeasureRow(**base)), f, default=list)


def test_load_rows_groups_by_agent(tmp_path):
    _write(str(tmp_path), "baseline", "o/1")
    _write(str(tmp_path), "repaired", "o/1")
    assert set(load_rows(str(tmp_path))) == {"baseline", "repaired"}


def test_load_rows_ignores_unknown_fields_from_a_newer_checkout(tmp_path):
    _write(str(tmp_path), "baseline", "o/1")
    p = os.path.join(str(tmp_path), "baseline", "o", "1", "row.json")
    d = json.load(open(p)); d["a_field_from_the_future"] = 1
    json.dump(d, open(p, "w"))
    assert load_rows(str(tmp_path))["baseline"][0].repo == "o/1"


def test_aggregate_errors_is_keyed_by_agent_then_source(tmp_path):
    _write(str(tmp_path), "baseline", "o/1")
    _write(str(tmp_path), "baseline", "o/2", collect_rc=4, collect_clean=False,
           collected_node_ids=(), status="collect_error")
    out = aggregate_errors(str(tmp_path))
    assert set(out["baseline"]) == {"collect", "run"}
    assert out["baseline"]["collect"]["n_masked"] == 1
    assert out["baseline"]["run"]["token_repos"] == {}


def test_metrics_output_shape_is_unchanged(tmp_path):
    _write(str(tmp_path), "baseline", "o/1")
    m = aggregate(str(tmp_path))
    assert set(m) == {"baseline"} and "errors" not in m["baseline"]


def test_main_writes_errors_json_next_to_metrics_json(tmp_path):
    _write(str(tmp_path), "baseline", "o/1")
    assert main(["--out", str(tmp_path), "--aggregate-only"]) == 0
    assert os.path.isfile(os.path.join(str(tmp_path), "metrics.json"))
    assert "baseline" in json.load(open(os.path.join(str(tmp_path), "errors.json")))


def test_unknown_agent_in_delta_fails_before_writing_anything(tmp_path):
    _write(str(tmp_path), "baseline", "o/1")
    assert main(["--out", str(tmp_path), "--aggregate-only", "--delta", "baseline:nope"]) == 2
    assert not os.path.exists(os.path.join(str(tmp_path), "errors.json"))
    assert not os.path.exists(os.path.join(str(tmp_path), "metrics.json"))


def test_metrics_json_is_written_even_if_classification_raises(tmp_path, monkeypatch):
    _write(str(tmp_path), "baseline", "o/1")
    import bench.unified_bench as ub
    monkeypatch.setattr(ub, "aggregate_errors",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert main(["--out", str(tmp_path), "--aggregate-only"]) == 0
    assert os.path.isfile(os.path.join(str(tmp_path), "metrics.json"))
    assert not os.path.exists(os.path.join(str(tmp_path), "errors.json"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd bench && python3 -m pytest tests/bench/test_error_delta.py tests/bench/test_error_aggregation.py -q`
Expected: FAIL — `cannot import name 'category_status'` and `cannot import name 'aggregate_errors'`

- [ ] **Step 3a: Append to `bench/bench/report/error_report.py`**

```python
def category_status(rows, universe, *, source: str = "collect", turn_cap=None,
                    threshold: float = DEFAULT_THRESHOLD) -> dict:
    """category -> {present, absent, unknown} over ALL rows (spec section 7). A masked repo is
    PARTIALLY observed: its startup cause is `present`, every other category `unknown`."""
    out = {c: {"present": 0, "absent": 0, "unknown": 0} for c in universe}
    for r in rows:
        v = verdict(r, turn_cap=turn_cap, threshold=threshold)
        known = {e.category for e in row_events(r) if e.source == source}
        for c in universe:
            if c in known:
                out[c]["present"] += 1
            elif v.error_surface == "full":
                out[c]["absent"] += 1
            else:
                out[c]["unknown"] += 1
    return out


def _universe(rows_a, rows_b, *, source: str) -> tuple:
    cats = set(CATEGORIES)
    for rows in (rows_a, rows_b):
        for r in rows:
            cats |= {e.category for e in row_events(r) if e.source == source}
    return tuple(sorted(cats))


def interval_delta(rows_a, rows_b, *, source: str = "collect", turn_cap=None,
                   threshold: float = DEFAULT_THRESHOLD) -> dict:
    """Manski-style bounds. `identified` means the interval excludes zero, so the direction holds
    no matter what the masked repos were hiding."""
    kw = dict(source=source, turn_cap=turn_cap, threshold=threshold)
    uni = _universe(rows_a, rows_b, source=source)
    sa, sb = category_status(rows_a, uni, **kw), category_status(rows_b, uni, **kw)
    out = {}
    for c in uni:
        a, b = sa[c], sb[c]
        lower = b["present"] - (a["present"] + a["unknown"])
        upper = (b["present"] + b["unknown"]) - a["present"]
        out[c] = {"a": a, "b": b, "lower": lower, "upper": upper,
                  "identified": lower > 0 or upper < 0}
    return out


def masking_2x2(rows_a, rows_b, *, turn_cap=None, threshold: float = DEFAULT_THRESHOLD) -> dict:
    """Marginals are compatible with repos REGRESSING; report cells. Only repos present in BOTH
    arms can be cross-tabulated — a repo missing from one arm is reported separately rather than
    silently counted as 'not masked'."""
    def surf(rows):
        return {r.repo: verdict(r, turn_cap=turn_cap, threshold=threshold).error_surface
                for r in rows}
    sa, sb = surf(rows_a), surf(rows_b)
    both = set(sa) & set(sb)
    cells = Counter((sa[repo] == "masked", sb[repo] == "masked") for repo in both)
    return {"neither_masked": cells[(False, False)], "a_only_masked": cells[(True, False)],
            "b_only_masked": cells[(False, True)], "both_masked": cells[(True, True)],
            "only_in_a": len(set(sa) - set(sb)), "only_in_b": len(set(sb) - set(sa))}


def paired_repos(rows_a, rows_b, *, turn_cap=None, threshold: float = DEFAULT_THRESHOLD):
    """SENSITIVITY ONLY — conditioning on error_surface is post-treatment (spec section 7)."""
    def full(rows):
        return {r.repo for r in rows
                if verdict(r, turn_cap=turn_cap, threshold=threshold).error_surface == "full"}
    return frozenset(full(rows_a) & full(rows_b))


def _diff(da: dict, db: dict) -> dict:
    return {k: db.get(k, 0) - da.get(k, 0) for k in sorted(set(da) | set(db))}


def delta(rows_a, rows_b, *, source: str = "collect", turn_cap=None,
          threshold: float = DEFAULT_THRESHOLD) -> dict:
    kw = dict(turn_cap=turn_cap, threshold=threshold)
    fa = arm_report(rows_a, source=source, **kw)
    fb = arm_report(rows_b, source=source, **kw)
    paired = paired_repos(rows_a, rows_b, **kw)
    pa = arm_report([r for r in rows_a if r.repo in paired], source=source, **kw)
    pb = arm_report([r for r in rows_b if r.repo in paired], source=source, **kw)
    return {
        "source": source,
        "n_repos": {"a": fa.n_repos, "b": fb.n_repos},
        "category_interval": interval_delta(rows_a, rows_b, source=source, **kw),   # HEADLINE
        "masking_2x2": masking_2x2(rows_a, rows_b, **kw),
        "surfaces": {"a": fa.surfaces, "b": fb.surfaces},
        "buckets": {"a": fa.buckets, "b": fb.buckets},
        "status_derived": {"a": fa.n_status_derived, "b": fb.n_status_derived},
        "uncategorized_rate": {"a": fa.uncategorized_rate, "b": fb.uncategorized_rate},
        "sensitivity_paired": {
            "paired_repos": len(paired),
            "category_repos_delta": _diff(pa.category_repos, pb.category_repos),
            "token_repos_delta": _diff(pa.token_repos, pb.token_repos),
        },
    }
```

- [ ] **Step 3b: Modify `bench/bench/unified_bench.py`**

Add `import sys`, `from dataclasses import asdict, fields`, and
`from bench.report.error_report import arm_report, delta as arm_delta`. Extract row loading out of
`aggregate` — **keep its existing legacy-`status` backfill at lines 51-52 exactly as it is**:

```python
_ROW_FIELDS = {f.name for f in fields(MeasureRow)}
SOURCES = ("collect", "run")


def load_rows(out_root: str) -> dict:
    by_agent: dict = {}
    for p in glob(os.path.join(out_root, "*", "**", "row.json"), recursive=True):
        with open(p) as f:
            d = json.load(f)
        agent = os.path.relpath(p, out_root).split(os.sep)[0]
        d.pop("agent", None)
        if "status" not in d:
            d["status"] = "legacy_ok" if (d.get("build_ok") or d.get("executed")) else "missing"
        # Filter to KNOWN fields: a row.json written by a newer checkout otherwise crashes an
        # older one with TypeError. Costs one line, prevents a cross-branch collision.
        row = MeasureRow(agent=agent, **{k: (tuple(v) if isinstance(v, list) else v)
                                         for k, v in d.items() if k in _ROW_FIELDS})
        by_agent.setdefault(agent, []).append(row)
    return by_agent


def aggregate(out_root: str, gold: dict | None = None) -> dict:
    return {a: compute_metrics(rows, gold=gold) for a, rows in load_rows(out_root).items()}


def aggregate_errors(out_root: str, *, turn_cap=None) -> dict:
    """{agent: {source: report}} — never pooled across sources (spec section 8)."""
    return {a: {s: asdict(arm_report(rows, source=s, turn_cap=turn_cap)) for s in SOURCES}
            for a, rows in load_rows(out_root).items()}
```

Add the CLI flags and rewrite the tail of `main()` (currently `unified_bench.py:89-93`) so
`metrics.json` is written **first** and the error block cannot abort it:

```python
    ap.add_argument("--turn-cap", type=int, default=None,
                    help="repair-loop turn cap; required for the unconverged flag")
    ap.add_argument("--delta", help="AGENT_A:AGENT_B — arm-vs-arm interval delta")
```

> **Two review findings landed on top of this block.** (a) The `--delta` check below sits *after*
> `run_one()` in the non-`--aggregate-only` path, so a typo cost a full docker measure pass before
> returning 2. The agent names come from `--harvest` and are knowable up front, so a format +
> membership check now runs **before** `discover()`; the check below stays, because it is the one
> that covers `--aggregate-only` and catches an agent that harvested but produced no rows.
> (b) On a classification failure the `except` must **delete** a pre-existing `errors.json` —
> `metrics.json` has just been rewritten, so a surviving errors.json from an earlier run reads as
> part of the same report, at exit code 0, with nothing signalling the mismatch. Both are
> mutation-checked. Note `--delta ""` is falsy and deliberately means "not requested".

```python
    # runs BEFORE discover()/run_one() — see the note above
    if a.delta:
        _name_a, _sep, _name_b = a.delta.partition(":")
        if not (_sep and _name_a and _name_b):
            print(f"--delta must be AGENT_A:AGENT_B, got {a.delta!r}", file=sys.stderr)
            return 2
        if not a.aggregate_only:
            _known = set(_parse_harvest(a.harvest))
            if _name_a not in _known or _name_b not in _known:
                print(f"unknown agent in --delta: {a.delta} (--harvest has {sorted(_known)})",
                      file=sys.stderr)
                return 2

    # ...and this one still validates before any WRITE
    pair = None
    if a.delta:
        name_a, _, name_b = a.delta.partition(":")
        by_agent = load_rows(a.out)
        if name_a not in by_agent or name_b not in by_agent:
            print(f"unknown agent in --delta: {a.delta} (have {sorted(by_agent)})", file=sys.stderr)
            return 2
        pair = (by_agent[name_a], by_agent[name_b])

    out = aggregate(a.out, gold=gold)
    with open(os.path.join(a.out, "metrics.json"), "w") as f:   # EXISTING deliverable, first
        json.dump(out, f, indent=2)
    print(json.dumps(out, indent=2))

    try:
        errs = aggregate_errors(a.out, turn_cap=a.turn_cap)
        if pair:
            errs["_delta"] = {s: arm_delta(pair[0], pair[1], source=s, turn_cap=a.turn_cap)
                              for s in SOURCES}
        with open(errors_path, "w") as f:      # errors_path bound before the try
            json.dump(errs, f, indent=2)
    except Exception as e:                       # classification is additive — never fatal
        # ...but a STALE errors.json is worse than none (see the note above).
        if os.path.exists(errors_path):
            os.remove(errors_path)
            print(f"removed stale {errors_path} — it predates this run", file=sys.stderr)
        print(f"error classification failed: {e!r}", file=sys.stderr)
    return 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd bench && python3 -m pytest tests/bench -q -m "not slow"`
Expected: all new tests pass, no regressions

- [ ] **Step 5: Commit**

```bash
git add bench/bench/report/error_report.py bench/bench/unified_bench.py \
        bench/tests/bench/test_error_delta.py bench/tests/bench/test_error_aggregation.py
git commit -m "feat(bench): interval delta + masking 2x2 + errors.json beside metrics.json"
```

---

### Task 7: per-repo `errors` block on `MeasureRow` — **LANDING B, DEFERRED**

> **This task replaces a cut one.** An earlier draft put the per-repo block in
> `case_study.json`. That is not implementable: `case_study.py:200` binds a plain **dict** from
> `inline_score.score_agent()`, not a `MeasureRow`, and pipeline B has none of the fields this
> layer reads (spec §0). Wiring it there raises `AttributeError` on the first task — and because
> `runner/benchmark.py:863-876` wraps consolidation in a best-effort `try/except`, it would
> silently emit **zero** `case_study.json` files for the entire run instead of failing loudly.
> Do not reinstate it without designing the cross-pipeline join first.

The per-repo block belongs in `row.json`, produced by the same pipeline that has the data.

**Why deferred:** `row.json` is written by `unified_bench.run_one` from what `measure()` returns,
so populating the block means changing `measure()` — the measured hot path — and it only appears
on rows written *after* that lands. Everything in Landing A already works without it: the
per-repo view for a stored row is `errors_block(row)` called directly, and the aggregate is
`errors.json`.

**Files (when it lands):**
- Modify: `bench/bench/report/error_report.py` (add `errors_block`), `bench/bench/measure.py`
- Test: `bench/tests/bench/test_row_errors_block.py`

**Shape** — spec §6.4 form (b), the audit artifact, `raw` included so a category rule can be
re-derived offline without a re-measure:

```python
def errors_block(row) -> dict:
    surface, reason = error_surface(row)
    _, derived = resolve_status(row)
    evs = row_events(row)
    return {"surface": surface, "surface_reason": reason, "status_derived": derived,
            "events": [asdict(e) for e in evs],
            "by_category": dict(Counter(e.category for e in evs))}
```

`measure()` then sets `error_surface` / `error_surface_reason` on the returned `MeasureRow`, and
`run_one` serialises `errors_block(row)` under an `"errors"` key beside it.

**Two capture obligations that must be settled when `run_failed_lines` gets a producer.** Both
were found during Task 2 and are latent only because nothing populates the field yet — the moment
it does, they become live:

1. **The token filter is currently narrower than arbitrary Python.** `_first_token` accepts only
   tokens ending `Error` / `Exception` / `ExceptionGroup`, so a custom exception like
   `E   BrokenThing: boom`, or pytest's own `E   Failed: DID NOT RAISE`, yields **no event**. On
   the collect path this is harmless and invisible: `measure.py:21`'s `_COLLECT_ERR` requires
   `(Error|Exception|Warning):` in the line, so those lines never reach `collect_errors` in the
   first place — the two filters are aligned. A `run_failed_lines` producer with a *different*
   capture rule breaks that alignment and starts silently dropping real failures. Do not widen
   `_first_token` by loosening the suffix test — that is what lets `foo.py:12` and
   `localhost:6379` become tokens. Widen it, if at all, with a **positional** anchor on pytest's
   own shapes (`^\s*E\s+(<tok>):` and `^(?:FAILED|ERROR)\s+\S+\s+-\s+(<tok>):`), and decide it
   together with the capture rule, not separately.
2. **pytest truncates short-summary lines to terminal width.** Real `FAILED …` lines arrive as
   `- FileNotFoundErr…` / `- ImportError: libG…`, so groups are systematically lost. Extraction
   degrades honestly (`group=''`, no fabrication), but since the dedup key is `(token, group)`,
   one underlying error splits into a truncated and an untruncated event. Capture with a wide
   `COLUMNS`, or read the `FAILURES` block rather than the short summary.

**One correctness note for whoever writes the test.** A row whose only error is
`ModuleNotFoundError: No module named 'pytest_check'` categorises as **`module_not_found`**, not
`harness_error` — `categorize` routes on the *token* (`_pytest.*` / `Pytest*` / `UsageError`),
and `pytest_check` is a *group*. An earlier draft asserted `harness_error` here and was wrong.
Recognising harness *packages* by group would need a new rule the spec's §5 table does not
define; do not add one silently.

---
### Task 8: Acceptance against the real 100-row corpus

**Files:** none modified — this is a verification gate.

- [ ] **Step 1: Copy the corpus, never write into it**

```bash
ssh -o StrictHostKeyChecking=no root@167.233.64.96 bash -s <<'SH'
set -e
rm -rf /opt/ratbench/_errclass && cp -r /opt/ratbench/remeasure_50 /opt/ratbench/_errclass
find /opt/ratbench/_errclass -name row.json | wc -l    # must be 100
SH
```

- [ ] **Step 2: Run the aggregation with this repo's code**

The VM's `/opt/ratbench` is this repo on branch `node-producer`. Verify the checkout carries the
new files before trusting the numbers:

The VM is on `node-producer`. If you followed the header and branched from it, this is a fetch +
checkout; if you accidentally worked from `main`, **stop and rebase first** — there is no
scripted path from `main` onto the VM.

```bash
ssh -o StrictHostKeyChecking=no root@167.233.64.96 bash -s <<'SH'
set -e
cd /opt/ratbench
git fetch --all -q
git checkout -q node-producer && git pull -q --ff-only && git log -1 --oneline
test -f bench/bench/report/error_report.py || { echo "checkout lacks the port"; exit 1; }
cd /opt/ratbench/bench
/opt/rat_venv/bin/python -m bench.unified_bench \
  --out /opt/ratbench/_errclass --aggregate-only --turn-cap 30 --delta baseline:repaired
SH
```

- [ ] **Step 3: Check the measured gate**

These are **measured**, not estimated. Any deviation means the port changed behaviour:

```
baseline.collect   n_admissible=32  n_masked=16  n_unobserved=2   n_status_derived=50
repaired.collect   n_admissible=41  n_masked=3   n_unobserved=6   n_status_derived=50
_delta.collect.masking_2x2 == {neither 34, a_only 13, b_only 0, both 3, only_in_a 0, only_in_b 0}
_delta.collect.sensitivity_paired.paired_repos == 30
category_interval["module_not_found"] == {lower: -20, upper: -5, identified: true}
syslib_missing absent from category_repos in both arms
*.run.token_repos == {}
```

`n_status_derived == 50` per arm is the point of the loud counter: the whole corpus predates
`status`. If it reads 0, `resolve_status` is wrongly treating `legacy_ok` as measured.

- [ ] **Step 4: Record the result**

Write the observed numbers into this plan under the gate above. If any differ, **stop** — the two
`measure.py` forks disagree about the stored rows, and that must be understood before Task 9.

---

### Task 9: Delete the stale fork — **only after Task 8 passes**

**Files (in `john-v3-multi-lang`, a different repo):**
- Delete: `bench/`, `tests/bench/`, `tests/test_metrics.py`
- Fix or delete: `tests/bench_emit/test_wire_contract.py` — **it imports `bench.harvest`**

- [ ] **Step 1: Prove there are no consumers**

The obvious grep is **broken on this machine**: `grep -rn ... .` does not prefix matches with
`./`, so `grep -v "^\./bench/"` filters nothing and returns ~62 lines of noise. Anchor without
the dot:

```bash
cd /Users/john/john-v3-multi-lang
grep -rn --include="*.py" -E "(from|import) bench" . \
  | sed 's|^\./||' | grep -v '^bench/' | grep -v '^tests/bench/'
```

Expected: exactly **two** consumers.

```
tests/test_metrics.py                  from bench.schema / bench.metrics
tests/bench_emit/test_wire_contract.py from bench.harvest import discover
```

That second file is real and was missed by an earlier draft. It is not slow-marked, so it runs
in the default suite, and deleting `bench/` turns it into a collection error. Decide before
Step 2 whether to delete it or port it — it exercises
`test_emit_output_is_harvestable_by_bench`, which is a genuine contract between `src/bench_emit`
and the harvester.

Also confirm the RAT entrypoints are clean:

```bash
grep -n "bench\." run_rat_benchmark.py multi_docker_eval_adapter.py || echo "clean"
```

- [ ] **Step 2: Delete and verify**

```bash
cd /Users/john/john-v3-multi-lang
git rm -r -q bench tests/bench tests/test_metrics.py
# plus whatever you decided for tests/bench_emit/test_wire_contract.py
python3 -m pytest tests -q -m "not slow" 2>&1 | tail -25
```

`tail -25`, **not** `tail -3` — the summary line alone hides a new collection error among the
expected failures. Compare the failure *list*, not just the count.

Expected: no new failures beyond the pre-existing docker-daemon ones
(`test_ldd_probe_docker`, `test_emit_drain_docker` ×2, `test_ctypes_scan_docker`,
`test_wheel_preflight_integration`).

- [ ] **Step 3: Commit**

```bash
git commit -m "chore(bench): delete the stale bench/ fork — ported to ratbench-runner-john

Had no consumers here (run_rat_benchmark.py and multi_docker_eval_adapter.py never
imported bench.*), and had drifted 400+ lines behind the real package."
```

---

## Out of scope

- **Install-log classifier.** `syslib_missing`, `compile_error`, `toolchain_missing`,
  `pip_resolution_conflict` need setup.sh-log machinery. Measured zero times at collection.
- **Per-turn trajectory events** in `react_trace.jsonl`. Designed in spec §8 but not built —
  and when built, must never be summed into `errors.json`.
- **Populating `repo_toplevel`.** Until `measure.py` derives importable roots, the
  internal-vs-external split cannot fire and `internal_import_failure` reads 0 in both arms.
  Note this is `ls -1 /testbed` in neither repo — it must be `__init__.py`-based, or a src-layout
  repo scores its own modules as external.
- **Re-measuring the corpus** so rows carry `status` natively. That retires `legacy_status`.
- **Non-Python signature tables.**
