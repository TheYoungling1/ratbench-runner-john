import unittest

from src.envstate.jsonutil import extract_json_object


class JsonUtilTests(unittest.TestCase):
    def test_extracts_from_fence_with_trailing_prose(self):
        text = 'Sure:\n```json\n{"a": {"b": 1}}\n```\nHope that helps!'
        self.assertEqual(extract_json_object(text), {"a": {"b": 1}})

    def test_handles_nested_objects_not_truncated_at_first_brace(self):
        self.assertEqual(extract_json_object('{"x": {"y": 2}, "z": 3}'), {"x": {"y": 2}, "z": 3})

    def test_handles_braces_inside_strings(self):
        self.assertEqual(extract_json_object('{"cmd": "echo ${PATH}"}'), {"cmd": "echo ${PATH}"})

    def test_returns_none_on_no_object(self):
        self.assertIsNone(extract_json_object("no json here"))
        self.assertIsNone(extract_json_object(None))

    def test_unterminated_object_returns_none(self):
        self.assertIsNone(extract_json_object('{"a": 1'))  # no closing brace

    def test_escaped_backslash_before_close_quote_is_handled(self):
        # raw string -> JSON {"k": "a\\b"} ; the \\ is one escaped backslash, the
        # following 'b"}' is NOT swallowed as an escaped quote.
        self.assertEqual(extract_json_object(r'{"k": "a\\b"}'), {"k": "a\\b"})

    def test_pathological_unbalanced_input_is_bounded(self):
        # Thousands of unbalanced "{" must return None quickly (bounded), not hang.
        self.assertIsNone(extract_json_object("{" * 50000))
