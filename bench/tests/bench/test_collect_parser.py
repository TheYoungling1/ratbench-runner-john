# tests/bench/test_collect_parser.py
import pytest
from bench.measure import parse_collect, parse_collected_node_ids

_OUT = """tests/test_a.py::test_ok
tests/test_a.py::test_two
_______ ERROR collecting tests/test_missing.py _______
E   ModuleNotFoundError: No module named 'foo'
2 tests collected, 1 error
"""


@pytest.mark.parametrize("rc,clean", [(0, True), (5, True), (2, False), (4, False), (3, False)])
def test_collect_clean_matches_repo2run_exit_0_or_5(rc, clean):
    # EBSR gate EXACTLY per Repo2Run (runtest.py:64/72): exit 0 or 5 = success, else fail.
    assert parse_collect(rc, "")["collect_clean"] is clean


def test_collect_errors_scraped():
    assert any("ModuleNotFoundError" in e for e in parse_collect(2, _OUT)["collect_errors"])


def test_collected_node_ids_are_double_colon_lines():
    assert parse_collected_node_ids(_OUT) == ("tests/test_a.py::test_ok", "tests/test_a.py::test_two")
