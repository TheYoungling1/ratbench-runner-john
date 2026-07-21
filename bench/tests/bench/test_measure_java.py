# tests/bench/test_measure_java.py
from bench.measure import measure
from langfake import FakeDocker, lang_env

# TWO Surefire files (one per class), concatenated as the container `cat` of the glob would return.
_SUREFIRE = ('<?xml version="1.0"?><testsuite name="A" tests="2" failures="0">'
             '<testcase classname="fixture.CalcTest" name="a"/>'
             '<testcase classname="fixture.CalcTest" name="b"/></testsuite>'
             '<?xml version="1.0"?><testsuite name="B" tests="1" failures="1">'
             '<testcase classname="fixture.CalcTest" name="c"><failure message="x"/></testcase>'
             '</testsuite>')


def test_java_happy_path_merges_surefire_and_scores():
    d = FakeDocker(junit=_SUREFIRE)
    row = measure(lang_env("java"), docker=d)
    assert row.build_ok is True and row.ebsr is True and row.executed is True
    assert row.total == 3 and row.passed == 2 and row.failed == 1
    assert row.pass_rate == 0.6667 and row.status == "executed"
    assert any("test-compile" in c or "testClasses" in c for c in d.calls)   # gate
    assert any("surefire" in c for c in d.calls)                             # junit read (post-run)


def test_java_compile_fail_short_circuits():
    d = FakeDocker(script={"test-compile": (1, "BUILD FAILURE", False)}, junit=_SUREFIRE)
    row = measure(lang_env("java"), docker=d)
    assert row.status == "gate_fail" and row.ebsr is False and row.executed is False
    assert not any("surefire" in c for c in d.calls)   # never reached the junit read / run


def test_get_language_java_alias():
    from bench.languages import get_language
    from bench.languages.java import JavaLanguage
    assert isinstance(get_language("java"), JavaLanguage)
    assert isinstance(get_language("Java"), JavaLanguage)
