from bench.errors import extract_events, is_warning_only

REAL = [
    "E   ModuleNotFoundError: No module named 'gguf'",
    "E   ImportError: cannot import name '_accelerate' from 'qiskit'",
    "E   ImportError: libGL.so.1: cannot open shared object file: No such file or directory",
    "E   ConnectionRefusedError: [Errno 61] Connection refused",
    "E   FileNotFoundError: [Errno 2] No such file or directory: '/opt/missing.cfg'",
    "E   redis.exceptions.ConnectionError: Error 111 connecting to localhost:6379.",
]


def test_token_is_verbatim_fqn_not_leaf():
    tokens = [e.token for e in extract_events(REAL, source="collect")]
    assert "redis.exceptions.ConnectionError" in tokens and "ConnectionError" not in tokens


def test_capture_groups_by_kind():
    ev = extract_events(REAL, source="collect")
    by_token = {e.token: e for e in ev}
    assert (by_token["ModuleNotFoundError"].group,
            by_token["ModuleNotFoundError"].group_kind) == ("gguf", "module")
    assert by_token["FileNotFoundError"].group_kind == "path"
    kinds = {e.group_kind: e.group for e in ev if e.token == "ImportError"}
    assert kinds["soname"] == "libGL.so.1" and kinds["name"] == "_accelerate"


def test_token_is_the_exception_label_not_a_test_name():
    # on a FAILED line the first *Error identifier is the TEST NAME; anchoring on the
    # trailing colon is what stops a node id impersonating an exception
    ev = extract_events(
        ["FAILED tests/test_client.py::test_raises_ValueError - AssertionError: assert 1 == 2"],
        source="run")
    assert ev[0].token == "AssertionError"


def test_class_scoped_node_id_cannot_impersonate_an_exception():
    ev = extract_events(["FAILED t.py::TestFooError::test_x - ValueError: bad"], source="run")
    assert ev[0].token == "ValueError"


def test_anchored_module_pattern_wins_over_the_bare_soname_pattern():
    for line, kind, group in [
        ("E   ModuleNotFoundError: No module named 'libs.something'", "module", "libs.something"),
        ("E   ModuleNotFoundError: No module named 'library.sockets'", "module", "library.sockets"),
        ("E   ImportError: cannot import name 'x' from 'libs.solver'", "name", "x"),
    ]:
        ev = extract_events([line], source="collect")
        assert (ev[0].group_kind, ev[0].group) == (kind, group), line


def test_soname_is_not_fabricated_from_a_dotted_module_name():
    ev = extract_events(
        ["E   AttributeError: module 'matplotlib.something' has no attribute 'widget'"],
        source="collect")
    assert (ev[0].token, ev[0].group, ev[0].group_kind) == ("AttributeError", "", "")


def test_real_soname_still_resolves():
    ev = extract_events(["E   ImportError: libstdc++.so.6: cannot open shared object file"],
                        source="collect")
    assert (ev[0].group_kind, ev[0].group) == ("soname", "libstdc++.so.6")


def test_exception_group_is_not_dropped():
    ev = extract_events(["E   ExceptionGroup: several errors (2 sub-exceptions)"], source="collect")
    assert len(ev) == 1 and ev[0].token == "ExceptionGroup"


def test_cascade_dedups_to_one_event_with_occurrences():
    lines = ["E   ModuleNotFoundError: No module named 'cascade_absent_pkg_qq'"] * 3
    ev = extract_events(lines, source="collect")
    assert len(ev) == 1 and ev[0].occurrences == 3


def test_distinct_groups_are_distinct_events():
    ev = extract_events(["E   ModuleNotFoundError: No module named 'wrapt'",
                         "E   ModuleNotFoundError: No module named 'httpx'"], source="collect")
    assert len(ev) == 2


def test_warnings_are_not_events():
    line = "/usr/lib/_pytest/config/__init__.py:331: PytestDeprecationWarning: unset"
    assert extract_events([line], source="collect") == () and is_warning_only(line) is True


def test_a_warning_message_quoting_an_exception_does_not_fabricate_an_event():
    # measure.py:21 `_COLLECT_ERR` matches `(Error|Exception|Warning):`, so warning lines are
    # deliberately captured into collect_errors and DO reach this function on real rows. Scanning
    # past the warning label counted this line as a RuntimeError. Real pytest output, verbatim.
    line = "<string>:2: UserWarning: RuntimeError: boom"
    assert extract_events([line], source="collect") == ()
    assert is_warning_only(line) is True


def test_a_warning_label_does_not_shadow_an_error_that_came_first():
    # the shadow is positional, not a blanket warning filter: an error line may legitimately
    # mention a warning class in its message
    ev = extract_events(
        ["E   ImportError: cannot import name 'x' from 'p' (see DeprecationWarning)"],
        source="collect")
    assert len(ev) == 1 and ev[0].token == "ImportError" and ev[0].group == "x"


def test_lines_without_a_token_are_skipped():
    assert extract_events(["2 tests collected, 1 error", ""], source="collect") == ()


def test_source_is_recorded_and_raw_truncated_to_200():
    ev = extract_events(["E   ModuleNotFoundError: No module named 'x' " + "y" * 500], source="run")
    assert ev[0].source == "run" and len(ev[0].raw) == 200
