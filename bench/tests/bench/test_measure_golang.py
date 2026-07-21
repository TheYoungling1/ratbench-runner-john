# tests/bench/test_measure_golang.py
from bench.schema import HarvestedEnv, RepoSpec
from bench.measure import measure

# gotestsum-shaped JUnit: 3 tests, 2 pass, 1 fail.
_GO_JUNIT = ('<testsuites><testsuite name="fixture" tests="3" failures="1" errors="0">'
             '<testcase classname="fixture" name="TestAddA"/>'
             '<testcase classname="fixture" name="TestAddB"/>'
             '<testcase classname="fixture" name="TestAddC"><failure message="fail">x</failure></testcase>'
             '</testsuite></testsuites>')

GO_REPO = RepoSpec("o/gorepo", "https://github.com/o/gorepo", language="golang")


class FakeDocker:
    def __init__(self, build_rc=0, size_mb=250.0, script=None, junit=_GO_JUNIT):
        self.build_rc, self.size_mb, self.script, self.junit = build_rc, size_mb, script or {}, junit
        self.last_ctx = None
        self.calls = []

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
        if "py_test_files" in cmd:
            for needle, resp in self.script.items():
                if needle in cmd:
                    return resp
            return 0, "STATUS=conforming py_test_files=0", False
        for needle, resp in self.script.items():
            if needle in cmd:
                return resp
        return 0, "", False

    def rm(self, name, tag):
        pass


def _env(**kw):
    base = dict(agent="v3", repo=GO_REPO, dockerfile="FROM golang:1.22",
                base_image="golang:1.22", status="ok", meta={})
    base.update(kw)
    return HarvestedEnv(**base)


def test_golang_happy_path_known_answer():
    d = FakeDocker()
    row = measure(_env(), docker=d)
    assert row.build_ok is True and row.ebsr is True and row.executed is True
    assert row.total == 3 and row.passed == 2 and row.failed == 1
    assert row.pass_rate == 0.6667 and row.status == "executed"
    # go build is the gate; gotestsum is the run.
    assert any("go build" in c for c in d.calls)
    assert any("--junitfile" in c for c in d.calls)


def test_golang_build_break_short_circuits():
    d = FakeDocker(script={"go build": (1, "./calc.go:3: syntax error", False)})
    row = measure(_env(), docker=d)
    assert row.status == "gate_fail" and row.ebsr is False and row.executed is False
    assert not any("--junitfile" in c for c in d.calls)   # expensive run skipped
