# tests/bench/test_measure.py
import os

from bench.schema import HarvestedEnv, RepoSpec
from bench.measure import measure

_JUNIT_OK = """<testsuites><testsuite tests="2">
  <testcase classname="t" name="a"/><testcase classname="t" name="b"/></testsuite></testsuites>"""

REPO = RepoSpec("o/r", "https://github.com/o/r")


def _env(**kw):
    base = dict(agent="v3", repo=REPO, dockerfile="FROM x", base_image="python:3.13-slim",
                status="ok", meta={"tokens_in": 5, "tokens_out": 15})
    base.update(kw)
    return HarvestedEnv(**base)


class FakeDocker:
    def __init__(self, build_rc=0, size_mb=250.0, script=None, junit=_JUNIT_OK):
        self.build_rc, self.size_mb, self.script, self.junit = build_rc, size_mb, script or {}, junit
        self.last_ctx = None
        self.calls = []                # every exec cmd, for asserting the short-circuit ladder

    def build(self, tag, ctx, timeout=None):
        self.last_ctx = ctx
        return self.build_rc, "build log"

    def image_size_mb(self, tag):
        return self.size_mb

    def run_detached(self, tag, name, workdir):
        pass

    def exec(self, name, argv, timeout=None):
        cmd = " ".join(argv)
        self.calls.append(cmd)
        if "cat" in cmd and "junit.xml" in cmd:
            return 0, self.junit, False
        # The /testbed contract probe (bench/contract.py). Answer conforming by default so the
        # existing happy-path measure() tests reach the collect/pytest ladder. A test can override
        # via `script={"py_test_files": (0, "STATUS=empty_testbed py_test_files=0", False)}`.
        if "py_test_files" in cmd:
            for needle, resp in self.script.items():
                if needle in cmd:
                    return resp
            return 0, "STATUS=conforming py_test_files=1", False
        for needle, resp in self.script.items():
            if needle in cmd:
                return resp
        return 0, "", False

    def rm(self, name, tag):
        pass


def test_build_failure_non_ebsr_still_a_row():
    row = measure(_env(), docker=FakeDocker(build_rc=1))
    assert row.build_ok is False and row.ebsr is False and row.executed is False
    assert row.env_status == "ok" and row.status == "build_fail"


def test_collect_rc2_does_not_block_test_run():
    # "--disable-warnings" matches only the EBSR-gate collect (Repo2Run: --collect-only -q
    # --disable-warnings), not the second --continue-on-collection-errors node-id collect.
    script = {"--disable-warnings": (2, "tests/x.py::a\n1 error", False)}
    row = measure(_env(), docker=FakeDocker(script=script))
    assert row.collect_clean is False and row.executed is True and row.ebsr is True
    assert row.total == 2 and row.passed == 2 and row.pass_rate == 1.0
    assert row.status == "collect_error"      # conforming build, but collect rc not in {0,5}


def test_env_missing_short_circuits():
    row = measure(_env(dockerfile=None, status="missing"), docker=FakeDocker())
    assert row.env_status == "missing" and row.build_ok is False and row.executed is False
    assert row.status == "missing"


def test_conforming_probe_records_py_test_files_and_executed_status():
    row = measure(_env(), docker=FakeDocker())
    assert row.status == "executed" and row.py_test_files == 1


def test_probe_non_conforming_denies_ebsr_but_records_collect_for_audit():
    # A built image whose /testbed is empty must NOT receive EBSR credit (the false-green fix), but we
    # STILL run the cheap collect gate so EBSR_repo2run_raw faithfully reproduces the old rc-5 credit
    # (that delta is `false_green_removed`). The EXPENSIVE full pytest run is skipped.
    script = {"py_test_files": (0, "STATUS=empty_testbed py_test_files=0", False),
              "--disable-warnings": (5, "no tests ran", False)}   # empty /testbed -> collect rc 5
    d = FakeDocker(script=script)
    row = measure(_env(), docker=d)
    assert row.build_ok is True and row.status == "empty_testbed"
    assert row.ebsr is False and row.executed is False
    # collect DID run and is recorded (rc 5 is Repo2Run-clean -> feeds the raw diagnostic)...
    assert row.collect_rc == 5 and row.collect_clean is True
    assert any("--collect-only" in c for c in d.calls)
    # ...but the expensive full pytest run was skipped for the non-conforming image.
    assert not any("--junit-xml" in c for c in d.calls)


def test_tokens_propagated_from_meta():
    row = measure(_env(), docker=FakeDocker())
    assert row.tokens_in == 5 and row.tokens_out == 15


def test_image_delta_uses_base_size():
    d = FakeDocker(size_mb=250.0)
    sizes = {"python:3.13-slim": 200.0}
    d.image_size_mb = lambda tag: sizes.get(tag, 250.0)
    row = measure(_env(), docker=d)
    assert row.image_size_mb == 250.0 and row.image_delta_mb == 50.0


def test_build_context_is_cleaned_up():
    d = FakeDocker()
    measure(_env(), docker=d)
    assert d.last_ctx is not None and not os.path.exists(d.last_ctx)


def test_build_context_cleaned_up_on_build_failure():
    d = FakeDocker(build_rc=1)
    measure(_env(), docker=d)
    assert d.last_ctx is not None and not os.path.exists(d.last_ctx)


def test_collect_error_count_and_collected_from_continue_collect():
    out = ("tests/test_a.py::test_ok\n"
           "____ ERROR collecting tests/test_b.py ____\n"
           "____ ERROR collecting tests/test_c.py ____\n"
           "1 tests collected, 2 errors")
    script = {"continue-on-collection-errors": (0, out, False)}
    row = measure(_env(), docker=FakeDocker(script=script))
    assert row.collect_error_count == 2                              # two "ERROR collecting" headers
    assert row.collected_node_ids == ("tests/test_a.py::test_ok",)  # one test collected


def test_passed_node_ids_translated_to_path_form():
    # JUnit reports classname form; the collected --co list is path form. measure() must
    # translate outcomes to path form so they share one unit with gold (bench/gold.py).
    junit = ('<testsuites><testsuite tests="1">'
             '<testcase classname="tests.test_a" name="test_ok"/></testsuite></testsuites>')
    script = {"continue-on-collection-errors":
              (0, "tests/test_a.py::test_ok\ntests/test_a.py::test_skip", False)}
    row = measure(_env(), docker=FakeDocker(script=script, junit=junit))
    assert row.collected_node_ids == ("tests/test_a.py::test_ok", "tests/test_a.py::test_skip")
    assert row.passed_node_ids == ("tests/test_a.py::test_ok",)


def test_empty_junit_report_is_not_a_green_row():
    # A run that wrote a report but collected nothing must land in `no_tests_collected`, not be
    # credited with EBSR at pass_rate 0.0. The empty root <testsuites/> contains the substring
    # "testsuite", which the old predicate accepted; jest-junit writes exactly this shape when
    # jest matches no test files, so on Node it would have been the common case.
    row = measure(_env(), docker=FakeDocker(junit="<testsuites/>"))
    assert row.build_ok is True
    assert row.executed is False and row.ebsr is False
    assert row.status == "no_tests_collected"
    assert row.total == 0 and row.passed == 0 and row.pass_rate == 0.0


def test_a_report_with_testcases_but_no_totals_still_counts_as_executed():
    # The case the substring check existed for: real <testcase> elements whose <testsuite> carries
    # no `tests` attribute, so the attribute-derived total is 0.
    junit = '<testsuites><testsuite name="s"><testcase classname="s" name="a"/></testsuite></testsuites>'
    row = measure(_env(), docker=FakeDocker(junit=junit))
    assert row.executed is True and row.ebsr is True and row.status == "executed"


# ── image_digest (design item 4b: reproducibility of the MEASURED build) ──────────────────────

def test_image_digest_absent_when_the_docker_double_doesnt_implement_it():
    # FakeDocker (above) has no image_digest() — feature-detected, must not raise or gate.
    row = measure(_env(), docker=FakeDocker())
    assert row.image_digest is None


def test_image_digest_threaded_through_when_the_docker_client_provides_it():
    d = FakeDocker()
    d.image_digest = lambda tag: f"sha256:deadbeef-{tag}"
    row = measure(_env(), docker=d)
    assert row.image_digest == "sha256:deadbeef-bench-v3-o-r"


def test_image_digest_failure_degrades_to_none_not_a_raise():
    d = FakeDocker()

    def boom(tag):
        raise RuntimeError("docker inspect failed")

    d.image_digest = boom
    row = measure(_env(), docker=d)
    assert row.image_digest is None
