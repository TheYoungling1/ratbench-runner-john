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


# ── multi-module report collection ──────────────────────────────────────────────────────────
#
# Measured on Netflix/concurrency-limits (run rj-smoke3): the gate PASSED and the repo still
# scored zero, because it declares 5 subprojects and Gradle writes JUnit to
# concurrency-limits-core/build/test-results/test/ while the old spec globbed only the ROOT.
# 43 of the 50 repos in rat_java50.json are multi-module (19/20 in the large tier), so the
# root-only glob would have false-zeroed most of the dataset.

def test_junit_collect_is_byte_identical_for_single_path_languages():
    # python/go/node/rust all return one fixed path. Their collection command must not change at
    # all — this fix is Java-only, and a shared code path is exactly where that guarantee slips.
    from bench.measure import _junit_collect
    for spec in ("/testbed/logs/junit.xml", "/testbed/target/nextest/ci/junit.xml"):
        assert _junit_collect(spec, "/testbed") == f"cat {spec} 2>/dev/null || true"


def test_junit_collect_recurses_for_a_multi_file_spec():
    # `**` does NOT recurse under `bash -lc` without globstar, and a large multi-module build can
    # name more files than one `cat` argv holds — so this uses find with `-exec ... +`.
    from bench.measure import _junit_collect
    from bench.languages.java import JavaLanguage
    cmd = _junit_collect(JavaLanguage().junit_glob("/testbed"), "/testbed")
    assert cmd.startswith("find /testbed -type f")
    assert "-path '*/target/surefire-reports/*.xml'" in cmd     # Maven, any depth
    assert "-path '*/build/test-results/test/*.xml'" in cmd      # Gradle, any depth
    assert "-exec cat {} +" in cmd                               # batches; no argv overflow
    assert "cat /testbed/target" not in cmd                      # the old root-only glob is gone


def test_java_measure_issues_a_recursive_report_read():
    d = FakeDocker(junit=_SUREFIRE)
    row = measure(lang_env("java"), docker=d)
    assert row.executed is True
    reads = [c for c in d.calls if "surefire" in c]
    assert reads, "no junit read issued"
    # FakeDocker records the whole argv, so these are prefixed with the `bash -lc` wrapper.
    assert all("find /testbed -type f" in c for c in reads), reads
    assert not any("cat /testbed/target" in c for c in reads), reads   # the root-only glob is gone
