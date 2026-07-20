"""Tests for workdir / repo_structure propagation through types, serde,
render_planning_view, and Worker.run_task.

TDD: these tests MUST fail before the implementation is in place.
"""
import unittest
from types import SimpleNamespace

# BaseFacts/EnvStateSnapshot/Requirement/Source/Status removed with types.py (Task 39)
# snapshot_to_dict/snapshot_from_dict removed with serde.py (Task 40)
# TypesDefaultsTests and SerdeRoundTripTests skipped below
# render_planning_view removed with supervisor.py (Task 36)
# Worker / build_task_brief removed with worker.py (Task 37) — WorkerWorkdirTests skipped below
from src.envstate.ledger import ActionLedger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_facts(**kw):
    return BaseFacts(image="python:3.11-slim", **kw)


def _snapshot(workdir=None, repo_structure=""):
    base = BaseFacts(image="python:3.11-slim", workdir=workdir)
    return EnvStateSnapshot(
        revision=3,
        container_id="ctr123",
        base=base,
        repo_structure=repo_structure,
    )


class FakeWorkerPlanner:
    """Records every (task_brief, recent_observations) call; always finishes immediately."""

    def __init__(self):
        self.calls = []  # list of task_brief strings received

    def next_action(self, task_brief, recent_observations):
        self.calls.append(task_brief)
        return ("", True)  # immediately signal finished


class FakeExecutor:
    def __call__(self, action):
        return (True, "ok")


# ---------------------------------------------------------------------------
# 1. types.py defaults
# ---------------------------------------------------------------------------

@unittest.skip("v0 types removed — BaseFacts/EnvStateSnapshot deleted with types.py (Task 39)")
class TypesDefaultsTests(unittest.TestCase):
    """BaseFacts.workdir defaults to None; EnvStateSnapshot.repo_structure defaults to ''."""

    def test_base_facts_workdir_defaults_none(self):
        bf = BaseFacts(image="ubuntu:22.04")
        self.assertIsNone(bf.workdir)

    def test_base_facts_workdir_can_be_set(self):
        bf = BaseFacts(image="ubuntu:22.04", workdir="/app")
        self.assertEqual(bf.workdir, "/app")

    def test_snapshot_repo_structure_defaults_empty_string(self):
        snap = EnvStateSnapshot(revision=0, container_id="c", base=BaseFacts(image="x"))
        self.assertEqual(snap.repo_structure, "")

    def test_snapshot_repo_structure_can_be_set(self):
        snap = EnvStateSnapshot(
            revision=0, container_id="c",
            base=BaseFacts(image="x"),
            repo_structure="src/\n  main.py\n",
        )
        self.assertEqual(snap.repo_structure, "src/\n  main.py\n")

    def test_existing_base_facts_construction_still_works(self):
        """All pre-existing construction patterns (no workdir kwarg) remain valid."""
        bf = BaseFacts(image="python:3.11-slim", distro="debian", arch="amd64", python="3.11.9")
        self.assertEqual(bf.image, "python:3.11-slim")
        self.assertIsNone(bf.workdir)

    def test_existing_snapshot_construction_still_works(self):
        snap = EnvStateSnapshot(
            revision=7, container_id="abc",
            base=BaseFacts(image="img"),
            requirements=(
                Requirement(
                    id="lang:foo", name="foo", kind="LanguagePackage",
                    status=Status.REQUIRED, source=Source.STATIC_SCAN,
                ),
            ),
        )
        self.assertEqual(snap.revision, 7)
        self.assertEqual(snap.repo_structure, "")


# ---------------------------------------------------------------------------
# 2. serde round-trip
# ---------------------------------------------------------------------------

@unittest.skip("v0 serde removed — snapshot_to_dict/snapshot_from_dict deleted with serde.py (Task 40)")
class SerdeRoundTripTests(unittest.TestCase):
    def test_round_trip_with_workdir_and_repo_structure(self):
        original = EnvStateSnapshot(
            revision=5,
            container_id="ctr99",
            base=BaseFacts(image="ubuntu:22.04", workdir="/app"),
            repo_structure="src/\n  main.py\n  utils.py\n",
        )
        restored = snapshot_from_dict(snapshot_to_dict(original))
        self.assertEqual(restored.base.workdir, "/app")
        self.assertEqual(restored.repo_structure, "src/\n  main.py\n  utils.py\n")
        self.assertEqual(restored, original)

    def test_round_trip_workdir_none(self):
        original = EnvStateSnapshot(
            revision=1, container_id="c",
            base=BaseFacts(image="x", workdir=None),
            repo_structure="",
        )
        restored = snapshot_from_dict(snapshot_to_dict(original))
        self.assertIsNone(restored.base.workdir)
        self.assertEqual(restored.repo_structure, "")

    def test_legacy_dict_without_workdir_field_still_deserialises(self):
        """A dict produced by OLD code (no workdir key) must deserialise cleanly
        with workdir=None and repo_structure=''.  This guards backwards compat
        with stored snapshots that predate the new fields."""
        legacy_dict = {
            "revision": 2,
            "container_id": "old",
            "base": {"image": "img"},   # no workdir key
            "requirements": [],
            "provider_facts": [],
            "open_failures": [],
            "stale_evidence": [],
            "plan_notes": [],
            # no repo_structure key
        }
        snap = snapshot_from_dict(legacy_dict)
        self.assertIsNone(snap.base.workdir)
        self.assertEqual(snap.repo_structure, "")


# ---------------------------------------------------------------------------
# 3. render_planning_view
# ---------------------------------------------------------------------------

@unittest.skip("supervisor removed — render_planning_view deleted with supervisor.py")
class RenderPlanningViewWorkdirTests(unittest.TestCase):
    def test_workdir_appears_on_base_line(self):
        snap = _snapshot(workdir="/app")
        view = render_planning_view(snap, ActionLedger(), budget={"steps_remaining": 10})
        self.assertIn("workdir=/app", view)

    def test_workdir_none_renders_none(self):
        snap = _snapshot(workdir=None)
        view = render_planning_view(snap, ActionLedger(), budget={"steps_remaining": 10})
        self.assertIn("workdir=None", view)

    def test_repo_structure_section_present_when_non_empty(self):
        snap = _snapshot(workdir="/app", repo_structure="src/\n  main.py\n")
        view = render_planning_view(snap, ActionLedger(), budget={"steps_remaining": 10})
        self.assertIn("Repository Layout", view)
        self.assertIn("main.py", view)

    def test_repo_structure_section_absent_when_empty(self):
        snap = _snapshot(workdir="/app", repo_structure="")
        view = render_planning_view(snap, ActionLedger(), budget={"steps_remaining": 10})
        self.assertNotIn("Repository Layout", view)

    def test_repo_structure_truncated_at_120_lines(self):
        lines = [f"line{i}" for i in range(200)]
        repo_structure = "\n".join(lines)
        snap = _snapshot(workdir="/app", repo_structure=repo_structure)
        view = render_planning_view(snap, ActionLedger(), budget={"steps_remaining": 10})
        # The 121st line should NOT appear
        self.assertNotIn("line120", view)
        # The 119th line should appear (0-indexed)
        self.assertIn("line119", view)

    def test_repo_structure_truncation_noted_in_view(self):
        lines = [f"L{i}" for i in range(200)]
        snap = _snapshot(workdir="/app", repo_structure="\n".join(lines))
        view = render_planning_view(snap, ActionLedger(), budget={"steps_remaining": 10})
        # Some note about truncation must be present
        self.assertIn("truncated", view.lower())

    def test_repo_structure_no_truncation_note_when_under_cap(self):
        lines = [f"L{i}" for i in range(50)]
        snap = _snapshot(workdir="/app", repo_structure="\n".join(lines))
        view = render_planning_view(snap, ActionLedger(), budget={"steps_remaining": 10})
        self.assertNotIn("truncated", view.lower())

    def test_workdir_in_layout_section_header(self):
        snap = _snapshot(workdir="/app", repo_structure="src/\n")
        view = render_planning_view(snap, ActionLedger(), budget={"steps_remaining": 10})
        self.assertIn("workdir=/app", view)
        # The layout section header should reference the workdir
        self.assertIn("Repository Layout", view)


# ---------------------------------------------------------------------------
# 4. Worker workdir propagation
# ---------------------------------------------------------------------------

@unittest.skip("worker.py removed — WorkerWorkdirTests superseded by build_agent tests")
class WorkerWorkdirTests(unittest.TestCase):
    def test_workdir_prepended_to_brief(self):
        planner = FakeWorkerPlanner()
        worker = Worker(planner=planner, max_actions=6, workdir="/app")
        task_spec = {"task_id": "t1", "goal": "install deps", "relevant_state": [],
                     "constraints": [], "allowed_actions": [], "success_criteria": [],
                     "stop_conditions": []}
        worker.run_task(task_spec, FakeExecutor())
        self.assertTrue(len(planner.calls) >= 1)
        brief = planner.calls[0]
        self.assertIn("/app", brief)
        self.assertIn("Working directory", brief)

    def test_workdir_none_brief_unchanged(self):
        """workdir=None -> brief is identical to build_task_brief(task_spec) output."""
        task_spec = {"task_id": "t2", "goal": "run tests", "relevant_state": ["foo"],
                     "constraints": [], "allowed_actions": [], "success_criteria": [],
                     "stop_conditions": []}
        planner = FakeWorkerPlanner()
        worker = Worker(planner=planner, max_actions=6, workdir=None)
        worker.run_task(task_spec, FakeExecutor())
        self.assertTrue(len(planner.calls) >= 1)
        brief_received = planner.calls[0]
        expected_brief = build_task_brief(task_spec)
        self.assertEqual(brief_received, expected_brief)

    def test_workdir_default_is_none(self):
        worker = Worker.__new__(Worker)
        Worker.__init__(worker, planner=FakeWorkerPlanner(), max_actions=3)
        self.assertIsNone(worker.workdir)

    def test_workdir_stored_on_worker(self):
        worker = Worker(planner=FakeWorkerPlanner(), max_actions=3, workdir="/workspace")
        self.assertEqual(worker.workdir, "/workspace")


if __name__ == "__main__":
    unittest.main()
