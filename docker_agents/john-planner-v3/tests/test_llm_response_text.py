import unittest
from types import SimpleNamespace

from src.envstate.llm_response import response_text, strip_reasoning_markup


def _resp(message):
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class ResponseTextTests(unittest.TestCase):
    def test_returns_content_when_present(self):
        resp = _resp(SimpleNamespace(content="Action: ls", reasoning="ignored"))
        self.assertEqual(response_text(resp), "Action: ls")

    def test_falls_back_to_reasoning_attr_when_content_none(self):
        resp = _resp(SimpleNamespace(content=None, reasoning="Action: pip install x"))
        self.assertEqual(response_text(resp), "Action: pip install x")

    def test_falls_back_to_reasoning_attr_when_content_empty(self):
        resp = _resp(SimpleNamespace(content="", reasoning="Action: pip install x"))
        self.assertEqual(response_text(resp), "Action: pip install x")

    def test_falls_back_to_model_extra_reasoning(self):
        msg = SimpleNamespace(content="", model_extra={"reasoning": "Action: make"})
        self.assertEqual(response_text(_resp(msg)), "Action: make")

    def test_returns_empty_when_all_missing(self):
        msg = SimpleNamespace(content=None)
        self.assertEqual(response_text(_resp(msg)), "")

    def test_returns_empty_on_malformed_response(self):
        self.assertEqual(response_text(SimpleNamespace(choices=[])), "")
        self.assertEqual(response_text(SimpleNamespace()), "")
        self.assertEqual(response_text(None), "")


class StripReasoningMarkupTests(unittest.TestCase):
    def test_clean_text_unchanged(self):
        text = "Thought: X\nAction: ls"
        self.assertEqual(strip_reasoning_markup(text), text)

    def test_empty_string_returns_empty(self):
        self.assertEqual(strip_reasoning_markup(""), "")

    def test_none_returns_empty(self):
        self.assertEqual(strip_reasoning_markup(None), "")

    def test_complete_think_block_removed(self):
        text = "<think>internal reasoning here</think>\n\nThought: X\nAction: ls"
        self.assertEqual(strip_reasoning_markup(text), "Thought: X\nAction: ls")

    def test_complete_think_block_multiline_removed(self):
        text = "<think>\nline1\nline2\n</think>\nThought: done\nAction: pwd"
        self.assertEqual(strip_reasoning_markup(text), "Thought: done\nAction: pwd")

    def test_orphan_closing_tag_stripped(self):
        # leaked tail: opening <think> went to reasoning field, tail ends in </think>
        text = "etry.\n</think>\n\nThought: X\nAction: ls"
        self.assertEqual(strip_reasoning_markup(text), "Thought: X\nAction: ls")

    def test_only_think_block_returns_empty(self):
        self.assertEqual(strip_reasoning_markup("<think>stuff</think>"), "")

    def test_only_orphan_fragment_returns_empty(self):
        self.assertEqual(strip_reasoning_markup("some tail\n</think>"), "")

    def test_idempotent_on_already_clean(self):
        text = "Thought: X\nAction: ls"
        self.assertEqual(strip_reasoning_markup(strip_reasoning_markup(text)), text)

    def test_idempotent_on_think_block(self):
        text = "<think>x</think>\n\nAnswer"
        result = strip_reasoning_markup(text)
        self.assertEqual(strip_reasoning_markup(result), result)

    def test_multiple_complete_think_blocks_removed(self):
        text = "<think>a</think>\n<think>b</think>\nAction: echo hi"
        self.assertEqual(strip_reasoning_markup(text), "Action: echo hi")

    # --- false-positive / content-preservation tests ---

    def test_orphan_close_with_action_before_it_not_stripped(self):
        # Real action appears before a bare </think>; the action must be preserved.
        text = "Action: ls\n</think>\nmore"
        result = strip_reasoning_markup(text)
        self.assertIn("Action: ls", result)

    def test_orphan_close_with_final_answer_before_it_not_stripped(self):
        # Final Answer: directive before a bare </think> must not be eaten.
        text = "Final Answer: Success\n</think>\ntrailing"
        result = strip_reasoning_markup(text)
        self.assertIn("Final Answer: Success", result)

    def test_full_block_then_literal_close_tag_preserved(self):
        # A complete <think>...</think> block followed by a literal </think> in
        # the answer must not silently drop the answer text.
        text = '<think>a</think>\nAnswer with </think> inside'
        result = strip_reasoning_markup(text)
        self.assertIn("Answer with", result)

    def test_content_before_think_block_preserved(self):
        # Text before a complete block should survive (e.g. multi-turn responses).
        text = "Action: pwd\n<think>x</think>"
        self.assertEqual(strip_reasoning_markup(text), "Action: pwd")


class ResponseTextWithMarkupTests(unittest.TestCase):
    def test_leaked_tail_plus_answer_returns_answer(self):
        content = "etry.\n</think>\n\nThought: X\nAction: ls"
        msg = SimpleNamespace(content=content, reasoning="ignored")
        self.assertEqual(response_text(_resp(msg)), "Thought: X\nAction: ls")

    def test_content_only_think_block_falls_back_to_reasoning(self):
        content = "<think>internal</think>"
        msg = SimpleNamespace(content=content, reasoning="Action: pip install x")
        self.assertEqual(response_text(_resp(msg)), "Action: pip install x")

    def test_content_only_orphan_fragment_falls_back_to_reasoning(self):
        content = "some tail\n</think>"
        msg = SimpleNamespace(content=content, reasoning="Action: make")
        self.assertEqual(response_text(_resp(msg)), "Action: make")

    def test_content_with_full_think_block_and_answer(self):
        content = "<think>reason</think>\n\nThought: done\nAction: pwd"
        msg = SimpleNamespace(content=content, reasoning="ignored")
        self.assertEqual(response_text(_resp(msg)), "Thought: done\nAction: pwd")

    def test_both_empty_after_strip_returns_empty(self):
        content = "<think>only this</think>"
        msg = SimpleNamespace(content=content, reasoning="")
        self.assertEqual(response_text(_resp(msg)), "")

    def test_clean_content_unchanged_by_strip(self):
        content = "Action: ls"
        msg = SimpleNamespace(content=content, reasoning="ignored")
        self.assertEqual(response_text(_resp(msg)), "Action: ls")


if __name__ == "__main__":
    unittest.main()
