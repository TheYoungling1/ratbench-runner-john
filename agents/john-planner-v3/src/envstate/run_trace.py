"""Task 8a: pure run-trace instrumentation.

``RunTracer`` is the ONE mutable, append-only collector for a `run_v3` call —
the same mutability exception granted to `ActionLedger` (`src/envstate/ledger.py`).
It records facts as the loop runs (patchgate outcomes, discover-gate diagnoses,
per-cycle fresh replays, and the legacy/ablation "did this path execute" marks)
and, on exit, freezes them into an immutable `RunTrace` snapshot via
`snapshot()`. Nothing in this module reads or writes graph/world-model state —
it observes and records only.

This module intentionally has zero orchestrator wiring (that is a later,
separate task) — it is pure data plumbing, unit-testable standalone.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PatchGateRecord:
    cycle: int
    failed_block_id: str | None
    evidence_ref: str | None
    accepted: bool
    accepted_node_ids: tuple[str, ...]
    accepted_block_ids: tuple[str, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True)
class DiscoverRecord:
    cycle: int
    command: str
    used_llm_mutation: bool
    new_node_ids: tuple[str, ...]
    diagnosis_modes: tuple[str, ...]


@dataclass(frozen=True)
class FreshReplayRecord:
    ran: bool
    setup_rc: int | None
    failing_command: str | None
    certified_node_ids: tuple[str, ...]
    unsatisfied_node_ids: tuple[str, ...]
    test_rc: int | None
    test_summary: str


@dataclass(frozen=True)
class RunTrace:
    repo: str = ""
    loop_mode: str = "v3_graph_typed_repair"
    used_emit_drain: bool = False
    used_repair_failed_nodes: bool = False
    used_build_agent_run: bool = False
    used_block_emit: bool = False          # block_emit lives only in the ablation; MUST be False in the method
    patchgate: tuple[PatchGateRecord, ...] = ()
    discover: tuple[DiscoverRecord, ...] = ()
    replays: tuple[FreshReplayRecord, ...] = ()   # one per cycle that actually replayed (Model B)
    manual_block_ids: tuple[str, ...] = ()
    stop_reason: str = ""
    gates: dict = field(default_factory=dict)

    @property
    def last_replay(self) -> "FreshReplayRecord | None":
        return self.replays[-1] if self.replays else None

    def to_dict(self) -> dict[str, Any]:
        """Plain, JSON-serializable dict.

        ``dataclasses.asdict`` recurses into dataclass fields and into the
        elements of list/tuple fields, converting each nested dataclass
        instance to a dict in turn — so `patchgate`/`discover`/`replays`
        (each a tuple of frozen records) come back as tuples of plain dicts
        with no dataclass instances left anywhere in the tree. ``last_replay``
        is a derived `@property`, not a dataclass field, so `asdict` does not
        touch it; it is deliberately left out of the dict (recomputable from
        `replays[-1]` by any consumer that has the dict form).
        """
        return dataclasses.asdict(self)


class RunTracer:
    """Append-only host-owned recorder (same mutability exception as ActionLedger)."""

    def __init__(self, repo: str = "") -> None:
        self._repo = repo
        self._used_emit_drain = False
        self._used_repair_failed_nodes = False
        self._used_build_agent_run = False
        self._used_block_emit = False
        self._patchgate: list[PatchGateRecord] = []
        self._discover: list[DiscoverRecord] = []
        self._replays: list[FreshReplayRecord] = []
        self._manual_block_ids: tuple[str, ...] = ()

    def mark_emit_drain(self) -> None:
        self._used_emit_drain = True

    def mark_repair_failed_nodes(self) -> None:
        self._used_repair_failed_nodes = True

    def mark_build_agent_run(self) -> None:
        self._used_build_agent_run = True

    def mark_block_emit(self) -> None:
        self._used_block_emit = True

    def record_patchgate(self, r: PatchGateRecord) -> None:
        self._patchgate.append(r)

    def record_discover(self, r: DiscoverRecord) -> None:
        self._discover.append(r)

    def record_replay(self, r: FreshReplayRecord) -> None:
        self._replays.append(r)

    def set_last_replay_tests(self, test_rc: int, test_summary: str) -> None:
        """Back-fill the LAST recorded replay's test-gate result in place.

        The test gate (``_run_tests_verified`` / ``_run_discover_gate``) runs
        as a SEPARATE call from the fresh-replay executor that produces
        ``FreshReplayRecord``s, so ``record_replay`` always records
        ``test_rc=None``/``test_summary=""`` — the install result only. Once
        the test gate result is known, this replaces the last list entry with
        a copy carrying the test fields (``dataclasses.replace`` — the
        record itself stays frozen; only the list entry, which this recorder
        owns, is swapped for a new one). Under Model B every cycle replays
        BEFORE the scheduler calls the test gate, so there is always a last
        replay to back-fill by the time this is called; if called more than
        once for the same replay, the most recent test run wins. No-op if no
        replay has been recorded yet.
        """
        if not self._replays:
            return
        self._replays[-1] = dataclasses.replace(
            self._replays[-1], test_rc=test_rc, test_summary=test_summary
        )

    def set_manual_blocks(self, ids: tuple[str, ...]) -> None:
        self._manual_block_ids = tuple(ids)

    def snapshot(self, *, stop_reason: str, gates: dict) -> RunTrace:
        return RunTrace(
            repo=self._repo,
            used_emit_drain=self._used_emit_drain,
            used_repair_failed_nodes=self._used_repair_failed_nodes,
            used_build_agent_run=self._used_build_agent_run,
            used_block_emit=self._used_block_emit,
            patchgate=tuple(self._patchgate),
            discover=tuple(self._discover),
            replays=tuple(self._replays),
            manual_block_ids=self._manual_block_ids,
            stop_reason=stop_reason,
            # Defensive copy (Part-1 review Minor): RunTrace is meant to be an
            # immutable, frozen snapshot, but `dict` is itself mutable — storing
            # the caller's live `gates` object by reference would let a
            # post-snapshot mutation of THAT dict retroactively change an
            # already-returned RunTrace. Copy it in.
            gates=dict(gates),
        )
