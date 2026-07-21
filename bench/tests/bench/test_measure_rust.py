# tests/bench/test_measure_rust.py
from bench.measure import measure
from langfake import FakeDocker, lang_env

_NEXTEST_JUNIT = ('<testsuites><testsuite name="fixture" tests="3" failures="1">'
                  '<testcase classname="fixture" name="a"/>'
                  '<testcase classname="fixture" name="b"/>'
                  '<testcase classname="fixture" name="c"><failure message="fail">x</failure></testcase>'
                  '</testsuite></testsuites>')


def test_rust_happy_path_known_answer():
    d = FakeDocker(junit=_NEXTEST_JUNIT)
    row = measure(lang_env("rust"), docker=d)
    assert row.build_ok is True and row.ebsr is True and row.executed is True
    assert row.total == 3 and row.passed == 2 and row.failed == 1
    assert row.pass_rate == 0.6667 and row.status == "executed"
    assert any("cargo test --no-run" in c for c in d.calls)   # gate
    assert any("nextest run" in c for c in d.calls)           # run


def test_rust_build_break_short_circuits():
    d = FakeDocker(script={"cargo test --no-run": (1, "error[E0433]", False)}, junit=_NEXTEST_JUNIT)
    row = measure(lang_env("rust"), docker=d)
    assert row.status == "gate_fail" and row.ebsr is False and row.executed is False
    assert not any("nextest run" in c for c in d.calls)


def test_get_language_rust_alias():
    from bench.languages import get_language
    from bench.languages.rust import RustLanguage
    assert isinstance(get_language("rust"), RustLanguage)
    assert isinstance(get_language("Rust"), RustLanguage)
