"""Tests for complete_with_retry in src/envstate/llm_response.py.

All fakes use SimpleNamespace and call-counter patterns (no pytest fixtures/markers).
"""
import unittest
from types import SimpleNamespace


def _make_response(content, prompt_tokens=10, completion_tokens=5, total_tokens=15):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        ),
    )


def _make_sequential_client(responses):
    """Return a fake client whose create() returns responses in sequence.

    *responses* is a list of (content, prompt_tokens, completion_tokens, total_tokens)
    tuples (or just content strings — in that case default token counts are used).
    Raises IndexError if called more times than responses are provided.
    """
    call_log = []
    resp_iter = iter(responses)

    def _create(*, model, messages, **kwargs):
        call_log.append(list(messages))  # capture a copy
        item = next(resp_iter)
        if isinstance(item, str):
            return _make_response(item)
        content, pt, ct, tt = item
        return _make_response(content, pt, ct, tt)

    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=_create)
        )
    )
    return client, call_log


class TestCompleteWithRetryEmptyThenValid(unittest.TestCase):
    """Empty content on attempt 1 -> valid on attempt 2 -> returns valid, 2 calls."""

    def test_returns_valid_text_on_second_attempt(self):
        from src.envstate.llm_response import complete_with_retry

        valid_text = "Hello, world!"
        client, call_log = _make_sequential_client(["", valid_text])

        text, usage, response = complete_with_retry(client, "m", [{"role": "user", "content": "go"}])

        self.assertEqual(text, valid_text)
        self.assertEqual(len(call_log), 2)

    def test_accumulated_usage_across_attempts(self):
        from src.envstate.llm_response import complete_with_retry

        client, _ = _make_sequential_client([
            ("", 10, 5, 15),
            ("valid", 20, 8, 28),
        ])

        _, usage, _r = complete_with_retry(client, "m", [{"role": "user", "content": "go"}])

        self.assertEqual(usage["input_tokens"], 30)
        self.assertEqual(usage["output_tokens"], 13)
        self.assertEqual(usage["total_tokens"], 43)


class TestCompleteWithRetryAcceptPredicate(unittest.TestCase):
    """Text is non-empty but accept() returns False on attempt 1, True on attempt 2."""

    def test_retries_when_accept_returns_false(self):
        from src.envstate.llm_response import complete_with_retry

        client, call_log = _make_sequential_client(["bad text", "good text"])

        text, usage, response = complete_with_retry(
            client, "m", [{"role": "user", "content": "go"}],
            accept=lambda t: t == "good text",
        )

        self.assertEqual(text, "good text")
        self.assertEqual(len(call_log), 2)


class TestCompleteWithRetryUsageAccumulation(unittest.TestCase):
    """Usage is accumulated across all attempts regardless of accept outcome."""

    def test_three_attempts_usage_summed(self):
        from src.envstate.llm_response import complete_with_retry

        client, call_log = _make_sequential_client([
            ("", 5, 2, 7),
            ("", 6, 3, 9),
            ("valid", 7, 4, 11),
        ])

        text, usage, response = complete_with_retry(
            client, "m", [{"role": "user", "content": "go"}], max_attempts=3
        )

        self.assertEqual(text, "valid")
        self.assertEqual(len(call_log), 3)
        self.assertEqual(usage["input_tokens"], 18)
        self.assertEqual(usage["output_tokens"], 9)
        self.assertEqual(usage["total_tokens"], 27)


class TestCompleteWithRetryMaxAttemptsCap(unittest.TestCase):
    """Returns last response after max_attempts even when never good; exactly max_attempts calls."""

    def test_returns_last_text_when_never_good(self):
        from src.envstate.llm_response import complete_with_retry

        # Always empty — should give up after max_attempts=3
        client, call_log = _make_sequential_client(["", "", "still empty"])

        text, usage, response = complete_with_retry(
            client, "m", [{"role": "user", "content": "go"}], max_attempts=3
        )

        self.assertEqual(text, "still empty")
        self.assertEqual(len(call_log), 3)

    def test_default_max_attempts_is_three(self):
        from src.envstate.llm_response import complete_with_retry

        client, call_log = _make_sequential_client(["", "", "last"])

        complete_with_retry(client, "m", [{"role": "user", "content": "go"}])

        self.assertEqual(len(call_log), 3)


class TestCompleteWithRetryFirstResponseGood(unittest.TestCase):
    """If the first response is already good, exactly ONE create call is made (parity)."""

    def test_single_call_when_first_response_valid(self):
        from src.envstate.llm_response import complete_with_retry

        client, call_log = _make_sequential_client([("good text", 10, 5, 15)])

        text, usage, response = complete_with_retry(client, "m", [{"role": "user", "content": "go"}])

        self.assertEqual(text, "good text")
        self.assertEqual(len(call_log), 1)
        self.assertEqual(usage["input_tokens"], 10)
        self.assertEqual(usage["output_tokens"], 5)
        self.assertEqual(usage["total_tokens"], 15)
        self.assertIsNotNone(response)

    def test_single_call_when_accept_passes_first(self):
        from src.envstate.llm_response import complete_with_retry

        client, call_log = _make_sequential_client(["accepted"])

        text, usage, response = complete_with_retry(
            client, "m", [{"role": "user", "content": "go"}],
            accept=lambda t: True,
        )

        self.assertEqual(text, "accepted")
        self.assertEqual(len(call_log), 1)


class TestCompleteWithRetryMessagesNotMutated(unittest.TestCase):
    """The caller's messages list must not be mutated across retries."""

    def test_caller_list_unchanged_after_retry(self):
        from src.envstate.llm_response import complete_with_retry

        original_messages = [{"role": "user", "content": "go"}]
        original_len = len(original_messages)
        original_copy = [dict(m) for m in original_messages]

        client, _ = _make_sequential_client(["", "valid"])

        text, usage, response = complete_with_retry(client, "m", original_messages)

        self.assertEqual(len(original_messages), original_len)
        self.assertEqual(original_messages, original_copy)

    def test_caller_list_unchanged_with_max_attempts_exhausted(self):
        from src.envstate.llm_response import complete_with_retry

        original_messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
        original_len = len(original_messages)

        client, _ = _make_sequential_client(["", "", ""])

        text, usage, response = complete_with_retry(client, "m", original_messages, max_attempts=3)

        self.assertEqual(len(original_messages), original_len)


class TestCompleteWithRetryCustomNudge(unittest.TestCase):
    """The retry_nudge content appears in the messages sent on subsequent attempts."""

    def test_custom_nudge_appended_in_retry_messages(self):
        from src.envstate.llm_response import complete_with_retry

        client, call_log = _make_sequential_client(["", "ok"])

        text, usage, response = complete_with_retry(
            client, "m", [{"role": "user", "content": "go"}],
            retry_nudge="CUSTOM NUDGE HERE",
        )

        # Second call must include the custom nudge
        self.assertEqual(len(call_log), 2)
        second_call_messages = call_log[1]
        nudge_contents = [m["content"] for m in second_call_messages if m.get("role") == "user"]
        self.assertTrue(
            any("CUSTOM NUDGE HERE" in c for c in nudge_contents),
            f"Expected nudge in retry messages, got: {second_call_messages}",
        )


class TestCompleteWithRetryDefaultNudge(unittest.TestCase):
    """When retry_nudge is None, a default nudge message is still appended."""

    def test_default_nudge_appended_on_retry(self):
        from src.envstate.llm_response import complete_with_retry

        client, call_log = _make_sequential_client(["", "ok"])

        text, usage, response = complete_with_retry(client, "m", [{"role": "user", "content": "go"}])

        self.assertEqual(len(call_log), 2)
        second_call_messages = call_log[1]
        # There should be more messages in the retry than the original
        self.assertGreater(len(second_call_messages), 1)
        # The last message should be a user-role nudge
        last_msg = second_call_messages[-1]
        self.assertEqual(last_msg["role"], "user")
        self.assertTrue(len(last_msg["content"]) > 0)


class TestCompleteWithRetryKwargsForwarded(unittest.TestCase):
    """Extra kwargs (e.g. temperature) are forwarded to every create() call."""

    def test_kwargs_forwarded_to_create(self):
        from src.envstate.llm_response import complete_with_retry

        received_kwargs = []

        def _create(*, model, messages, **kwargs):
            received_kwargs.append(kwargs)
            if len(received_kwargs) == 1:
                return _make_response("")
            return _make_response("ok")

        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
        )

        text, usage, response = complete_with_retry(
            client, "m", [{"role": "user", "content": "go"}], temperature=0
        )

        self.assertEqual(len(received_kwargs), 2)
        for kw in received_kwargs:
            self.assertEqual(kw.get("temperature"), 0)


class TestCompleteWithRetryResponseReturned(unittest.TestCase):
    """The third return value is the final raw response object."""

    def test_response_object_returned_on_first_good(self):
        """When the first response is good, the returned response is that object."""
        from src.envstate.llm_response import complete_with_retry

        sentinel = _make_response("ok", 1, 1, 2)
        responses_iter = iter([sentinel])

        def _create(*, model, messages, **kwargs):
            return next(responses_iter)

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))
        text, usage, response = complete_with_retry(client, "m", [{"role": "user", "content": "go"}])

        self.assertIs(response, sentinel)

    def test_response_object_returned_on_exhaustion(self):
        """When all attempts fail, the last response object is still returned."""
        from src.envstate.llm_response import complete_with_retry

        resp1 = _make_response("", 1, 1, 2)
        resp2 = _make_response("", 1, 1, 2)
        resp3 = _make_response("still empty", 1, 1, 2)
        responses_iter = iter([resp1, resp2, resp3])

        def _create(*, model, messages, **kwargs):
            return next(responses_iter)

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))
        text, usage, response = complete_with_retry(
            client, "m", [{"role": "user", "content": "go"}], max_attempts=3
        )

        self.assertIs(response, resp3)


class TestCompleteWithRetryAcceptRaises(unittest.TestCase):
    """If accept() raises, that attempt is treated as not-good (no exception propagates)."""

    def test_accept_raising_treated_as_not_good_then_retried(self):
        from src.envstate.llm_response import complete_with_retry

        call_count = {"n": 0}

        def _create(*, model, messages, **kwargs):
            call_count["n"] += 1
            content = "bad" if call_count["n"] == 1 else "good text"
            return _make_response(content)

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))

        def exploding_accept(text):
            if text == "bad":
                raise ValueError("parse error")
            return True

        text, usage, response = complete_with_retry(
            client, "m", [{"role": "user", "content": "go"}],
            accept=exploding_accept,
        )

        self.assertEqual(text, "good text")
        self.assertEqual(call_count["n"], 2)

    def test_accept_always_raises_returns_last_text_without_propagating(self):
        from src.envstate.llm_response import complete_with_retry

        client, call_log = _make_sequential_client(["attempt1", "attempt2", "attempt3"])

        def always_explodes(text):
            raise RuntimeError("always broken")

        # Must not propagate; returns last text after max_attempts
        text, usage, response = complete_with_retry(
            client, "m", [{"role": "user", "content": "go"}],
            accept=always_explodes,
            max_attempts=3,
        )

        self.assertEqual(text, "attempt3")
        self.assertEqual(len(call_log), 3)


if __name__ == "__main__":
    unittest.main()
