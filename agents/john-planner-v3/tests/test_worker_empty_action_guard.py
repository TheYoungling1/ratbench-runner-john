import unittest

# Worker removed with worker.py (Task 37) — EmptyActionGuardTests skipped below


class RecordingExecutor:
    """Records every call so we can assert it was (or was not) invoked."""
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []

    def __call__(self, action):
        self.calls.append(action)
        if self.results:
            return self.results.pop(0)
        return (True, "ok")


class QueuedPlanner:
    """Returns a queued list of (action, is_finished) per step."""
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def next_action(self, task_brief, recent_observations):
        self.calls.append((task_brief, list(recent_observations)))
        return self.steps.pop(0)


class AlwaysEmptyPlanner:
    def __init__(self):
        self.calls = 0

    def next_action(self, task_brief, recent_observations):
        self.calls += 1
        return ("", False)


@unittest.skip("worker.py removed — EmptyActionGuardTests covered by build_agent tests")
class EmptyActionGuardTests(unittest.TestCase):
    def test_blocks_on_repeated_empty_action_without_executing(self):
        planner = AlwaysEmptyPlanner()
        executor = RecordingExecutor()
        worker = Worker(planner=planner, max_actions=6)
        report = worker.run_task({"task_id": "t-empty", "goal": "x", "max_actions": 6}, executor)
        self.assertEqual(report.status, "blocked")
        self.assertIn("no parseable action", report.summary)
        # The executor must never run an empty command.
        self.assertEqual(executor.calls, [])
        self.assertEqual(report.commands_attempted, ())

    def test_real_action_then_finished_completes_normally(self):
        planner = QueuedPlanner([
            ("pip install x", False),
            ("", True),
        ])
        executor = RecordingExecutor([(True, "Successfully installed x")])
        worker = Worker(planner=planner, max_actions=6)
        report = worker.run_task({"task_id": "t-ok", "goal": "x", "max_actions": 6}, executor)
        self.assertEqual(report.status, "complete")
        self.assertEqual(executor.calls, ["pip install x"])
        self.assertEqual(report.commands_attempted, ("pip install x",))

    def test_single_empty_then_recovers_and_counter_resets(self):
        planner = QueuedPlanner([
            ("", False),
            ("ls", False),
            ("", True),
        ])
        executor = RecordingExecutor([(True, "files...")])
        worker = Worker(planner=planner, max_actions=6)
        report = worker.run_task({"task_id": "t-retry", "goal": "x", "max_actions": 6}, executor)
        self.assertEqual(report.status, "complete")
        # Only the real action executed; the single empty did not.
        self.assertEqual(executor.calls, ["ls"])
        self.assertEqual(report.commands_attempted, ("ls",))


if __name__ == "__main__":
    unittest.main()
