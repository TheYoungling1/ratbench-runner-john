# bench/verdict.py
from __future__ import annotations

import re

from bench.measure import _MEASURABLE
from bench.schema import RepoVerdict

# `_MEASURABLE` is IMPORTED, never re-declared. It is ("ok", "legacy_ok", "produced") — the
# harvest statuses whose env has a rebuildable artifact (measure.py:19), and the exact set
# measure.py:245 tests before deciding `missing`. The fork this layer was ported from wrote
# `env_status != "ok"`, which on this repo's rows is catastrophically wrong: the real corpus is
# 45/50 `produced`, so every healthy row scored ("unobserved", "no_env") and legacy_status
# returned `missing` for all of them. A duplicated copy of this tuple is what would let that
# drift back in, so it is a single source of truth.

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

# `unified_bench.load_rows` SYNTHESISES a status for rows stored before the taxonomy existed:
# `legacy_ok` if there is a build/execution signal, else `missing`. `legacy_ok` is unambiguous —
# nothing else emits it — but `missing` is ALSO a real status that measure.py:245 assigns, so the
# value alone cannot say which produced it. load_rows therefore stamps this marker on the row's
# meta, and resolve_status treats a marked row as un-measured. Without it, a backfilled `missing`
# reads as measured: on the 100-row acceptance corpus that under-reported n_status_derived as
# 48/44 instead of 50/50 on a corpus with NO stored status at all, and left unit8co/darts labelled
# `missing` when its env harvested fine and the row's own meta says measure_error.
STATUS_BACKFILL_MARKER = "_status_backfilled"


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
    """(status, was_derived). A measured status always wins — but a status that `load_rows`
    invented is not a measured one, however real its name looks (see STATUS_BACKFILL_MARKER)."""
    s = getattr(row, "status", "") or ""
    if s in _ALREADY_MEASURED or (row.meta or {}).get(STATUS_BACKFILL_MARKER):
        return legacy_status(row), True
    return s, False


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
