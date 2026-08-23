"""Per-container-generation memo for the VERIFY_TEST_CMD execution.

run_v3 runs the SAME ``sandbox_execute(VERIFY_TEST_CMD)`` twice in a discover
cycle — once as the scheduler's test probe (``_run_tests_verified``) and once as
the discover gate (``_run_discover_gate``) — against a container that nothing
mutated between the two calls (see orchestrator design doc §0.1). This memo
returns the first raw ``(ok, out)`` to the second caller instead of paying a
second full pytest run.

It is keyed on a container-generation token that run_v3 bumps on EVERY container
mutation (reset_to_base / run_install_script). A stale pass/fail can therefore
never be served: any mutation makes the cached token stale and forces a fresh
execution. The cache is deliberately the one mutable object in this seam — its
whole purpose is memoization — but its state is fully private and only ever a
function of the raw executor + the generation token.
"""
from __future__ import annotations

from collections.abc import Callable


class VerifyTestCache:
    """Memoize ``exec_test()`` per container generation.

    exec_test: () -> (ok: bool, out: str)   raw ``sandbox_execute(VERIFY_TEST_CMD)``.
    gen:       () -> int                     current container generation (monotonic).
    """

    def __init__(
        self,
        exec_test: Callable[[], tuple[bool, str]],
        gen: Callable[[], int],
    ) -> None:
        self._exec_test = exec_test
        self._gen = gen
        self._token: int | None = None
        self._result: tuple[bool, str] | None = None

    def run(self) -> tuple[bool, str]:
        g = self._gen()
        if self._result is not None and self._token == g:
            return self._result
        result = self._exec_test()
        self._token = g
        self._result = result
        return result
