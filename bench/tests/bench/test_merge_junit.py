# tests/bench/test_merge_junit.py
from bench.measure import _merge_junit, parse_junit


def test_merge_combines_many_surefire_files_into_one_parseable_doc():
    f1 = ('<?xml version="1.0"?><testsuite name="A" tests="2" failures="0">'
          '<testcase classname="fixture.CalcTest" name="a"/>'
          '<testcase classname="fixture.CalcTest" name="b"/></testsuite>')
    f2 = ('<?xml version="1.0"?><testsuite name="B" tests="1" failures="1">'
          '<testcase classname="fixture.CalcTest" name="c"><failure message="x"/></testcase>'
          '</testsuite>')
    j = parse_junit(_merge_junit(f1 + f2))
    assert j["total"] == 3 and j["passed"] == 2 and j["failed"] == 1


def test_merge_empty_is_empty():
    assert _merge_junit("") == ""
    assert _merge_junit("   \n ") == ""
