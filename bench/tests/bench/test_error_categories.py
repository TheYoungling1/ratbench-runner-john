import pytest

from bench.errors import CATEGORIES, categorize, extract_events

TOPLEVEL = ("tests", "qiskit", "examples")


@pytest.mark.parametrize("token,group,kind,expected", [
    # harness_error FIRST so _pytest.* never reaches the ImportError branch
    ("_pytest.pathlib.ImportPathMismatchError", "", "", "harness_error"),
    ("UsageError", "", "", "harness_error"),
    # ...and this row is the one that actually OBSERVES that ordering. The row above does not:
    # "ImportPathMismatchError" does not end in "ImportError", so it falls through to the harness
    # check wherever that check sits, and moving harness_error below the ImportError branch still
    # passes. A token that matches BOTH predicates is the only way to pin the precedence.
    ("_pytest.outcomes.ImportError", "libGL.so.1", "soname", "harness_error"),
    ("ModuleNotFoundError", "wrapt", "module", "module_not_found"),
    ("ModuleNotFoundError", "tests.conftest", "module", "internal_import_failure"),
    ("ModuleNotFoundError", "examples.mlperf.models.llama", "module", "internal_import_failure"),
    ("ImportError", "libGL.so.1", "soname", "syslib_missing"),
    ("ImportError", "_accelerate", "name", "partial_import"),
    ("ImportError", "httpx", "module", "module_not_found"),
    ("ImportError", "qiskit.circuit", "module", "internal_import_failure"),
    ("ImportError", "", "", "import_failed"),
    ("ConnectionRefusedError", "", "", "service_unavailable"),
    ("redis.exceptions.ConnectionError", "", "", "service_unavailable"),
    ("docker.errors.DockerException", "", "", "service_unavailable"),
    ("SyntaxError", "", "", "syntax_error"),
    ("IndentationError", "", "", "syntax_error"),
    ("FileNotFoundError", "/opt/x.cfg", "path", "file_missing"),
    # the singleton tail stays uncategorized ON PURPOSE
    ("dash.exceptions.PageError", "", "", "uncategorized"),
    ("RuntimeError", "", "", "uncategorized"),
    # OperationalError is "no such table"/config far more often than a refused connection,
    # and the token cannot tell them apart — it must NOT inflate service_unavailable
    ("sqlalchemy.exc.OperationalError", "", "", "uncategorized"),
    ("sqlite3.OperationalError", "", "", "uncategorized"),
])
def test_category_table(token, group, kind, expected):
    assert categorize(token, group, kind, TOPLEVEL) == expected


def test_internal_split_defaults_to_external_without_repo_toplevel():
    # with no roots we must NOT guess internal — that would fabricate the split
    assert categorize("ModuleNotFoundError", "tests.conftest", "module", ()) == "module_not_found"


def test_only_first_dotted_segment_is_matched():
    assert categorize("ModuleNotFoundError", "tests", "module", ("tests",)) == "internal_import_failure"
    assert categorize("ModuleNotFoundError", "testsuite", "module", ("tests",)) == "module_not_found"


@pytest.mark.parametrize("language", ["go", "rust", "java", "nodejs"])
def test_non_python_languages_are_unpopulated_not_wrong(language):
    assert categorize("ModuleNotFoundError", "wrapt", "module", (), language) == "uncategorized"


def test_extract_events_threads_category_through():
    ev = extract_events(["E   ModuleNotFoundError: No module named 'tests.conftest'"],
                        source="collect", repo_toplevel=("tests",))
    assert ev[0].category == "internal_import_failure"


def test_categories_constant_covers_every_table_output():
    # EQUALITY, not `<=`. A subset assertion over four inputs is satisfied by a CATEGORIES that
    # omits `partial_import` entirely — and CATEGORIES is the report universe (`_universe` in
    # report/error_report.py), so a category missing from it never gets an interval column and
    # silently reads as "this arm has no such errors" instead of "we never looked".
    # One input per branch, both directions pinned.
    per_branch = [
        ("UsageError", "", ""),                                # harness_error
        ("ModuleNotFoundError", "tests.conftest", "module"),   # internal_import_failure
        ("ModuleNotFoundError", "wrapt", "module"),            # module_not_found
        ("ImportError", "libGL.so.1", "soname"),               # syslib_missing
        ("ImportError", "_accelerate", "name"),                # partial_import
        ("ImportError", "", ""),                               # import_failed
        ("ConnectionRefusedError", "", ""),                    # service_unavailable
        ("SyntaxError", "", ""),                               # syntax_error
        ("FileNotFoundError", "/opt/x.cfg", "path"),           # file_missing
        ("RuntimeError", "", ""),                              # uncategorized
    ]
    outs = {categorize(t, g, k, TOPLEVEL) for t, g, k in per_branch}
    assert outs == set(CATEGORIES)
