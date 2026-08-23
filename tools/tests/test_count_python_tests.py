# tools/tests/test_count_python_tests.py — the static test_count counter.
#
# This number is written into a curated dataset and read by humans as "this repo has tests", so the
# risk is a count that is confidently wrong: catching non-test defs, missing the unittest/async
# forms, or walking into vendored trees that inflate every row.
from tools.count_python_tests import count_tree


def _write(root, rel, body):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def test_counts_plain_async_and_unittest_methods_in_both_filename_forms(tmp_path):
    _write(tmp_path, "tests/test_a.py", "def test_one():\n    pass\n\nasync def test_two():\n    pass\n")
    _write(tmp_path, "pkg/b_test.py", "class TestX:\n    def test_method(self):\n        pass\n")
    assert count_tree(str(tmp_path)) == 3


def test_ignores_non_test_files_helpers_and_vendored_trees(tmp_path):
    _write(tmp_path, "tests/test_a.py", "def test_real():\n    pass\ndef helper_test():\n    pass\n")
    _write(tmp_path, "tests/conftest.py", "def test_not_collected_here():\n    pass\n")
    _write(tmp_path, "app.py", "def test_in_source_file():\n    pass\n")
    _write(tmp_path, ".venv/lib/test_vendored.py", "def test_v():\n    pass\n")
    assert count_tree(str(tmp_path)) == 1
