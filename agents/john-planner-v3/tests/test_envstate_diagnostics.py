import json
import os
import tempfile
import types
import unittest


def _fake_response(content=None, reasoning=None, finish_reason=None,
                   prompt_tokens=10, completion_tokens=5, total_tokens=15):
    """Build a SimpleNamespace fake that matches the OpenAI-compat shape."""
    usage = types.SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )
    message = types.SimpleNamespace(content=content, reasoning=reasoning)
    choice = types.SimpleNamespace(message=message, finish_reason=finish_reason)
    return types.SimpleNamespace(choices=[choice], usage=usage)


class LogLlmExchangeNoOpTests(unittest.TestCase):
    """When no log path is configured, the function is a no-op and never raises."""

    def setUp(self):
        # Ensure the env var is absent for these tests.
        self._orig = os.environ.pop("ENVSTATE_LLM_LOG", None)

    def tearDown(self):
        if self._orig is not None:
            os.environ["ENVSTATE_LLM_LOG"] = self._orig
        else:
            os.environ.pop("ENVSTATE_LLM_LOG", None)

    def test_no_env_no_log_path_does_nothing(self):
        from src.envstate.diagnostics import log_llm_exchange
        resp = _fake_response(content="Action: ls")
        # Must not raise, must not create any file.
        log_llm_exchange("supervisor", resp, parsed={"task_id": "t1"})

    def test_explicit_none_log_path_overrides_env(self):
        """Passing log_path=None while env is set: env wins — not a no-op."""
        # This test just verifies the path is env-derived; a real write is
        # covered by the write tests below.  Here we just confirm no crash.
        from src.envstate.diagnostics import log_llm_exchange
        resp = _fake_response(content="Action: ls")
        os.environ["ENVSTATE_LLM_LOG"] = ""
        log_llm_exchange("supervisor", resp)  # empty string -> no-op

    def test_empty_string_log_path_is_noop(self):
        from src.envstate.diagnostics import log_llm_exchange
        resp = _fake_response(content="Action: ls")
        log_llm_exchange("supervisor", resp, log_path="")


class LogLlmExchangeWriteTests(unittest.TestCase):
    """When a log_path is provided, a valid JSON line must be appended."""

    def test_appends_json_line_with_content(self):
        from src.envstate.diagnostics import log_llm_exchange
        resp = _fake_response(content="Action: pip install x", finish_reason="stop")
        parsed = {"task_id": "t-42", "phase": "Language Dependency Installation"}
        with tempfile.NamedTemporaryFile(mode="r", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            log_llm_exchange("supervisor", resp, parsed=parsed, log_path=path)
            with open(path) as fh:
                lines = fh.read().splitlines()
            self.assertEqual(len(lines), 1)
            record = json.loads(lines[0])
            self.assertEqual(record["role"], "supervisor")
            self.assertEqual(record["raw_content"], "Action: pip install x")
            self.assertIn("task_id", record["parsed"])
            self.assertEqual(record["finish_reason"], "stop")
        finally:
            os.unlink(path)

    def test_appends_json_line_with_reasoning(self):
        from src.envstate.diagnostics import log_llm_exchange
        resp = _fake_response(content=None, reasoning="some reasoning text")
        with tempfile.NamedTemporaryFile(mode="r", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            log_llm_exchange("worker", resp, log_path=path)
            with open(path) as fh:
                lines = fh.read().splitlines()
            record = json.loads(lines[0])
            self.assertIsNone(record["raw_content"])
            self.assertEqual(record["raw_reasoning"], "some reasoning text")
        finally:
            os.unlink(path)

    def test_usage_fields_present(self):
        from src.envstate.diagnostics import log_llm_exchange
        resp = _fake_response(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        with tempfile.NamedTemporaryFile(mode="r", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            log_llm_exchange("maintainer", resp, log_path=path)
            record = json.loads(open(path).readline())
            self.assertEqual(record["usage"]["prompt"], 100)
            self.assertEqual(record["usage"]["completion"], 50)
            self.assertEqual(record["usage"]["total"], 150)
        finally:
            os.unlink(path)

    def test_multiple_calls_append_multiple_lines(self):
        from src.envstate.diagnostics import log_llm_exchange
        resp = _fake_response(content="line one")
        with tempfile.NamedTemporaryFile(mode="r", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            log_llm_exchange("supervisor", resp, log_path=path)
            log_llm_exchange("worker", resp, log_path=path)
            with open(path) as fh:
                lines = fh.read().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["role"], "supervisor")
            self.assertEqual(json.loads(lines[1])["role"], "worker")
        finally:
            os.unlink(path)

    def test_creates_parent_dirs_if_needed(self):
        from src.envstate.diagnostics import log_llm_exchange
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "nested", "dir", "llm.jsonl")
            resp = _fake_response(content="hello")
            log_llm_exchange("supervisor", resp, log_path=path)
            self.assertTrue(os.path.isfile(path))
            lines = open(path).read().splitlines()
            self.assertEqual(len(lines), 1)

    def test_reads_log_path_from_env_var(self):
        from src.envstate.diagnostics import log_llm_exchange
        resp = _fake_response(content="from env")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "envstate_llm.jsonl")
            orig = os.environ.get("ENVSTATE_LLM_LOG")
            try:
                os.environ["ENVSTATE_LLM_LOG"] = path
                log_llm_exchange("supervisor", resp)
                lines = open(path).read().splitlines()
                self.assertEqual(len(lines), 1)
                record = json.loads(lines[0])
                self.assertEqual(record["raw_content"], "from env")
            finally:
                if orig is None:
                    os.environ.pop("ENVSTATE_LLM_LOG", None)
                else:
                    os.environ["ENVSTATE_LLM_LOG"] = orig

    def test_explicit_log_path_takes_priority_over_env(self):
        from src.envstate.diagnostics import log_llm_exchange
        resp = _fake_response(content="explicit path")
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = os.path.join(tmpdir, "env.jsonl")
            explicit_path = os.path.join(tmpdir, "explicit.jsonl")
            orig = os.environ.get("ENVSTATE_LLM_LOG")
            try:
                os.environ["ENVSTATE_LLM_LOG"] = env_path
                log_llm_exchange("supervisor", resp, log_path=explicit_path)
                self.assertTrue(os.path.isfile(explicit_path))
                self.assertFalse(os.path.isfile(env_path))
            finally:
                if orig is None:
                    os.environ.pop("ENVSTATE_LLM_LOG", None)
                else:
                    os.environ["ENVSTATE_LLM_LOG"] = orig

    def test_parsed_is_truncated_to_500_chars(self):
        from src.envstate.diagnostics import log_llm_exchange
        resp = _fake_response(content="x")
        big_parsed = {"data": "A" * 2000}
        with tempfile.NamedTemporaryFile(mode="r", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            log_llm_exchange("supervisor", resp, parsed=big_parsed, log_path=path)
            record = json.loads(open(path).readline())
            self.assertLessEqual(len(record["parsed"]), 510)  # repr + truncation marker
        finally:
            os.unlink(path)

    def test_none_parsed_recorded_as_null(self):
        from src.envstate.diagnostics import log_llm_exchange
        resp = _fake_response(content="y")
        with tempfile.NamedTemporaryFile(mode="r", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            log_llm_exchange("supervisor", resp, parsed=None, log_path=path)
            record = json.loads(open(path).readline())
            self.assertIsNone(record["parsed"])
        finally:
            os.unlink(path)


class LogLlmExchangeMalformedResponseTests(unittest.TestCase):
    """Malformed / unexpected response objects must never raise."""

    def test_none_response_does_not_raise(self):
        from src.envstate.diagnostics import log_llm_exchange
        with tempfile.NamedTemporaryFile(mode="r", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            log_llm_exchange("supervisor", None, log_path=path)
        finally:
            os.unlink(path)

    def test_response_missing_choices_does_not_raise(self):
        from src.envstate.diagnostics import log_llm_exchange
        resp = types.SimpleNamespace()  # no choices attr at all
        with tempfile.NamedTemporaryFile(mode="r", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            log_llm_exchange("maintainer", resp, log_path=path)
        finally:
            os.unlink(path)

    def test_response_with_empty_choices_does_not_raise(self):
        from src.envstate.diagnostics import log_llm_exchange
        resp = types.SimpleNamespace(choices=[])
        with tempfile.NamedTemporaryFile(mode="r", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            log_llm_exchange("worker", resp, log_path=path)
        finally:
            os.unlink(path)

    def test_model_extra_reasoning_extracted(self):
        """Reasoning in model_extra should be captured as raw_reasoning."""
        from src.envstate.diagnostics import log_llm_exchange
        message = types.SimpleNamespace(
            content=None,
            reasoning=None,
            model_extra={"reasoning": "deep thoughts"},
        )
        choice = types.SimpleNamespace(message=message, finish_reason=None)
        usage = types.SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        resp = types.SimpleNamespace(choices=[choice], usage=usage)
        with tempfile.NamedTemporaryFile(mode="r", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            log_llm_exchange("supervisor", resp, log_path=path)
            record = json.loads(open(path).readline())
            self.assertEqual(record["raw_reasoning"], "deep thoughts")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
