"""Per-task ReAct history reset tests.

TDD: these tests are written BEFORE the fix and MUST fail initially.
Once the fix is applied they should all pass.

Tests:
  1. LlmWorkerPlanner.reset() clears history and new brief is injected.
  2. Worker.run_task() calls planner.reset() at the start of every task.
  3. Worker.run_task() does NOT crash when the planner has no reset() method.
  4. Cross-task brief regression: two sequential run_task calls on the same
     Worker feed the correct distinct brief to the planner for each task.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

# LlmWorkerPlanner / Worker / WorkerReport removed with worker.py (Task 37)
# All classes below are skipped.


# ---------------------------------------------------------------------------
# Fake OpenAI-compatible client helpers
# ---------------------------------------------------------------------------

def _make_fake_response(content: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


class RecordingClient:
    """Fake client that records every create() call and returns scripted responses."""

    def __init__(self, responses):
        # list of content strings, popped in order
        self._responses = list(responses)
        self.calls = []  # list of kwargs dicts passed to create()

        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        content = self._responses.pop(0)
        return _make_fake_response(content)


# ---------------------------------------------------------------------------
# Fake planners for Worker-level tests
# ---------------------------------------------------------------------------

class ResettableFakePlanner:
    """Records calls; supports reset(); scripts next_action responses."""

    def __init__(self, steps):
        self.steps = list(steps)  # list of (action, is_finished)
        self.reset_called = False
        self.next_action_calls = []   # (brief, observations) per call
        self._reset_call_index = None  # index into next_action_calls at which reset happened

    def reset(self):
        self.reset_called = True
        # record WHEN reset was called relative to next_action calls
        self._reset_call_index = len(self.next_action_calls)

    def next_action(self, task_brief, recent_observations):
        self.next_action_calls.append((task_brief, list(recent_observations)))
        return self.steps.pop(0)


class NoResetFakePlanner:
    """Fake planner WITHOUT a reset() method — must not crash Worker."""

    def __init__(self, steps):
        self.steps = list(steps)

    def next_action(self, task_brief, recent_observations):
        return self.steps.pop(0)


class BriefRecordingPlanner:
    """Records the task_brief passed to each next_action call; supports reset()."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.briefs_seen = []

    def reset(self):
        pass  # intentionally does NOT clear briefs_seen so we can observe across tasks

    def next_action(self, task_brief, recent_observations):
        self.briefs_seen.append(task_brief)
        return self.steps.pop(0)


# ---------------------------------------------------------------------------
# Test 1: LlmWorkerPlanner.reset() behaviour
# ---------------------------------------------------------------------------

@unittest.skip("worker.py removed — LlmWorkerPlanner / Worker deleted in Task 37")
class TestLlmWorkerPlannerReset(unittest.TestCase):
    """reset() must clear history so the next task brief is re-injected."""

    def _make_planner_with_recording_client(self, responses):
        client = RecordingClient(responses)
        planner = LlmWorkerPlanner(client, model="test-model")
        return planner, client

    def test_reset_clears_history(self):
        """After reset(), planner.history is empty."""
        planner, _ = self._make_planner_with_recording_client([
            "Thought: first\nAction: ls",
        ])
        planner.next_action("BRIEF-1", [])
        self.assertTrue(len(planner.history) > 0, "history should be non-empty after a call")

        planner.reset()

        self.assertEqual(planner.history, [], "reset() must clear history to []")

    def test_new_brief_injected_after_reset(self):
        """After reset(), the very next next_action() call injects the new brief."""
        planner, client = self._make_planner_with_recording_client([
            "Thought: first\nAction: ls",   # response to BRIEF-1
            "Thought: second\nAction: pwd",  # response to BRIEF-2
        ])

        # First task
        planner.next_action("BRIEF-1", [])
        self.assertEqual(len(client.calls), 1)
        first_user_messages = [m for m in client.calls[0]["messages"] if m["role"] == "user"]
        self.assertTrue(
            any("BRIEF-1" in m["content"] for m in first_user_messages),
            "BRIEF-1 must appear in the first call's user messages",
        )

        # Reset and start second task
        planner.reset()
        planner.next_action("BRIEF-2", [])
        self.assertEqual(len(client.calls), 2)

        second_user_messages = [m for m in client.calls[1]["messages"] if m["role"] == "user"]
        self.assertTrue(
            any("BRIEF-2" in m["content"] for m in second_user_messages),
            "BRIEF-2 must appear in the second call's user messages after reset",
        )
        # BRIEF-1 must NOT pollute the second call's messages
        self.assertFalse(
            any("BRIEF-1" in m["content"] for m in second_user_messages),
            "BRIEF-1 must NOT appear in the second call's messages after reset",
        )


# ---------------------------------------------------------------------------
# Test 2: Worker.run_task() calls planner.reset() at task start
# ---------------------------------------------------------------------------

@unittest.skip("worker.py removed — Worker deleted in Task 37")
class TestWorkerCallsReset(unittest.TestCase):
    """Worker.run_task() must call planner.reset() before the first next_action()."""

    def _simple_executor(self, action):
        return (True, "ok")

    def test_reset_called_before_next_action(self):
        planner = ResettableFakePlanner([
            ("echo hi", False),
            ("", True),
        ])
        worker = Worker(planner=planner, max_actions=6)
        report = worker.run_task(
            {"task_id": "t1", "goal": "do something", "max_actions": 6},
            self._simple_executor,
        )
        self.assertTrue(planner.reset_called, "Worker must call planner.reset() at task start")
        # reset must have happened BEFORE any next_action calls
        self.assertEqual(
            planner._reset_call_index, 0,
            "reset() must be called before the first next_action() (index 0)",
        )
        self.assertEqual(report.status, "complete")

    def test_worker_does_not_crash_without_reset_method(self):
        """A planner that lacks reset() must not crash run_task (regression safety)."""
        planner = NoResetFakePlanner([
            ("ls", False),
            ("", True),
        ])
        worker = Worker(planner=planner, max_actions=6)
        # Must not raise
        report = worker.run_task(
            {"task_id": "t-noreset", "goal": "safe", "max_actions": 6},
            self._simple_executor,
        )
        self.assertEqual(report.status, "complete")


# ---------------------------------------------------------------------------
# Test 3: Cross-task brief correctness
# ---------------------------------------------------------------------------

@unittest.skip("worker.py removed — Worker / LlmWorkerPlanner deleted in Task 37")
class TestCrossTaskBriefReset(unittest.TestCase):
    """Sequential run_task calls on the same Worker deliver the right brief each time."""

    def _executor(self, action):
        return (True, "ok")

    def test_second_task_brief_delivered_to_planner(self):
        """After two run_task calls, the planner must have seen each task's distinct brief."""
        planner = BriefRecordingPlanner([
            # task 1 steps
            ("ls", False),
            ("", True),
            # task 2 steps
            ("pwd", False),
            ("", True),
        ])
        worker = Worker(planner=planner, max_actions=6)

        task1 = {
            "task_id": "t1",
            "goal": "install numpy",
            "relevant_state": [],
            "constraints": [],
            "allowed_actions": [],
            "success_criteria": [],
            "stop_conditions": [],
            "max_actions": 6,
        }
        task2 = {
            "task_id": "t2",
            "goal": "run tests",
            "relevant_state": [],
            "constraints": [],
            "allowed_actions": [],
            "success_criteria": [],
            "stop_conditions": [],
            "max_actions": 6,
        }

        report1 = worker.run_task(task1, self._executor)
        report2 = worker.run_task(task2, self._executor)

        self.assertEqual(report1.status, "complete")
        self.assertEqual(report2.status, "complete")

        # At least two distinct briefs were presented across the two tasks
        self.assertGreaterEqual(len(planner.briefs_seen), 2)

        # The first brief must contain task1's goal
        self.assertIn("install numpy", planner.briefs_seen[0],
                      "First task's goal must appear in the first brief passed to next_action")

        # The second brief (start of task2) must contain task2's goal
        # Find the index where the second task's brief appears
        task2_briefs = [b for b in planner.briefs_seen if "run tests" in b]
        self.assertTrue(
            len(task2_briefs) >= 1,
            "Second task's goal ('run tests') must appear in at least one brief seen by the planner",
        )

    def test_llm_planner_second_task_brief_via_recording_client(self):
        """Using LlmWorkerPlanner directly: second task's brief appears in 2nd task's API call."""
        client = RecordingClient([
            "Thought: first action\nAction: ls",   # task 1 step 1
            "Thought: done\nFinal Answer: Success", # task 1 finish
            "Thought: second action\nAction: pwd",  # task 2 step 1
            "Thought: done\nFinal Answer: Success", # task 2 finish
        ])
        planner = LlmWorkerPlanner(client, model="test-model")
        worker = Worker(planner=planner, max_actions=6)

        task1 = {
            "task_id": "t1",
            "goal": "UNIQUE_GOAL_ALPHA",
            "relevant_state": [],
            "constraints": [],
            "allowed_actions": [],
            "success_criteria": [],
            "stop_conditions": [],
            "max_actions": 6,
        }
        task2 = {
            "task_id": "t2",
            "goal": "UNIQUE_GOAL_BETA",
            "relevant_state": [],
            "constraints": [],
            "allowed_actions": [],
            "success_criteria": [],
            "stop_conditions": [],
            "max_actions": 6,
        }

        def _executor(action):
            return (True, "ok")

        worker.run_task(task1, _executor)
        worker.run_task(task2, _executor)

        # Find the API calls that belong to task 2 (contain UNIQUE_GOAL_BETA)
        task2_calls = [
            c for c in client.calls
            if any("UNIQUE_GOAL_BETA" in m.get("content", "") for m in c["messages"])
        ]
        self.assertTrue(
            len(task2_calls) >= 1,
            "UNIQUE_GOAL_BETA must appear in the messages of at least one API call for task 2",
        )

        # UNIQUE_GOAL_ALPHA must NOT appear in any task-2 API call
        for call in task2_calls:
            contents = [m.get("content", "") for m in call["messages"]]
            self.assertFalse(
                any("UNIQUE_GOAL_ALPHA" in c for c in contents),
                "Task-1 goal (UNIQUE_GOAL_ALPHA) must NOT pollute task-2 API messages",
            )


if __name__ == "__main__":
    unittest.main()
