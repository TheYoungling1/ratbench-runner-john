# tests/bench/test_junit_parser.py
from bench.measure import parse_junit

_XML = """<?xml version="1.0"?>
<testsuites><testsuite name="pytest" tests="4" failures="1" errors="1" skipped="1">
  <testcase classname="tests.test_a" name="test_ok"/>
  <testcase classname="tests.test_a" name="test_bad"><failure message="x">boom</failure></testcase>
  <testcase classname="tests.test_b" name="test_err"><error message="y">nope</error></testcase>
  <testcase classname="tests.test_b" name="test_skip"><skipped/></testcase>
</testsuite></testsuites>"""


def test_counts_and_outcomes():
    r = parse_junit(_XML)
    assert (r["total"], r["passed"], r["failed"], r["errors"], r["skipped"]) == (4, 1, 1, 1, 1)


def test_node_ids_by_outcome():
    r = parse_junit(_XML)
    assert r["passed_node_ids"] == ("tests.test_a::test_ok",)
    assert r["failed_node_ids"] == ("tests.test_a::test_bad",)
    assert r["error_node_ids"] == ("tests.test_b::test_err",)


def test_total_from_testsuite_attribute_rat_parity():
    # RAT parity: total/skipped come from the <testsuite> ATTRIBUTES, not the <testcase> element
    # count. Here the attribute (tests=10) exceeds the 3 elements (subtest-report inflation).
    xml = ('<testsuites><testsuite tests="10" failures="0" errors="0" skipped="2">'
           '<testcase classname="t" name="a"/><testcase classname="t" name="b"/>'
           '<testcase classname="t" name="c"><skipped/></testcase></testsuite></testsuites>')
    r = parse_junit(xml)
    assert r["total"] == 10 and r["skipped"] == 2   # from <testsuite> attributes
    assert r["passed"] == 2                          # passed counted from <testcase> elements (a, b)


def test_orphan_testcases_outside_any_testsuite_are_counted_from_elements():
    # node --test's junit reporter wraps describe()-grouped tests in <testsuite tests=N> but writes
    # top-level test() calls as bare <testcase> children of <testsuites>. Those carry no attribute
    # counters, so attribute-only totals omit them: an all-top-level suite would report total 0
    # (pass_rate 0.0 on a healthy repo) and this mixed one would divide 2 passes by 2 instead of 3.
    xml = ('<testsuites><testsuite name="grp" tests="2" failures="1" errors="0" skipped="0">'
           '<testcase classname="t" name="ok"/>'
           '<testcase classname="t" name="bad"><failure message="1 == 2">x</failure></testcase>'
           '</testsuite>'
           '<testcase classname="t" name="flat"/></testsuites>')
    r = parse_junit(xml)
    assert (r["total"], r["passed"], r["failed"]) == (3, 2, 1)
    assert r["passed_node_ids"] == ("t::ok", "t::flat")


def test_orphan_outcomes_are_counted_once_each():
    xml = ('<testsuites>'
           '<testcase classname="t" name="p"/>'
           '<testcase classname="t" name="f"><failure message="m">x</failure></testcase>'
           '<testcase classname="t" name="e"><error message="m">x</error></testcase>'
           '<testcase classname="t" name="s"><skipped/></testcase>'
           '</testsuites>')
    r = parse_junit(xml)
    assert (r["total"], r["passed"], r["failed"], r["errors"], r["skipped"]) == (4, 1, 1, 1, 1)


def test_nested_testcases_are_never_double_counted():
    # The orphan rule must stay inert for every reporter that nests its testcases (pytest,
    # jest-junit, mocha-junit-reporter, vitest, Surefire/Gradle, gotestsum, nextest): the
    # attribute totals stand alone, even when they disagree with the element count.
    r = parse_junit(_XML)
    assert r["total"] == 4                       # the <testsuite tests="4"> attribute, not 4 + 4
    nested_deep = ('<testsuites><testsuite tests="1"><testsuite tests="1">'
                   '<testcase classname="t" name="a"/></testsuite></testsuite></testsuites>')
    assert parse_junit(nested_deep)["total"] == 2   # both attributes; the testcase adds nothing


def test_empty_or_garbage_returns_zeroed():
    assert parse_junit("")["total"] == 0 and parse_junit("")["passed_node_ids"] == ()
    assert parse_junit("<not-xml")["total"] == 0
