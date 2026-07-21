# tests/bench/test_setup_compile.py
"""`setup_compile_ok` — did the initial construction setup.sh run to rc 0?

The emitted Dockerfile runs `RUN bash <setup.sh>` as a FATAL layer, so build success already
implies rc 0. The interesting case is a FAILED build: was setup.sh the culprit, or a neighbouring
step (clone before / `pip install pytest` after)? measure() isolates it by rebuilding a copy of the
Dockerfile truncated to end right after the setup step (cache-optimal — it shares layers with the
full build).
"""
import os

from bench.measure import _truncate_after_setup, measure
from bench.metrics import compute_metrics
from bench.schema import HarvestedEnv, MeasureRow, RepoSpec

_JUNIT_OK = ('<testsuites><testsuite tests="1">'
             '<testcase classname="t" name="a"/></testsuite></testsuites>')

REPO = RepoSpec("o/r", "https://github.com/o/r")

_DF = (
    "FROM python:3.13-slim\n"
    "WORKDIR /testbed\n"
    "RUN git clone --depth=1 https://github.com/o/r /testbed\n"
    "COPY setup.sh /tmp/v3_setup.sh\n"
    "RUN bash /tmp/v3_setup.sh\n"
    "RUN pip install --no-cache-dir pytest\n"
)


def _env(**kw):
    base = dict(agent="v3", repo=REPO, dockerfile=_DF, base_image="python:3.13-slim",
                status="ok", setup_scripts={"setup.sh": "echo hi\n"}, meta={})
    base.update(kw)
    return HarvestedEnv(**base)


class FakeDocker:
    """build() returns build_rc for the full build and probe_rc for the `-setupprobe` build."""

    def __init__(self, build_rc=0, probe_rc=0, junit=_JUNIT_OK):
        self.build_rc, self.probe_rc, self.junit = build_rc, probe_rc, junit
        self.builds = []

    def build(self, tag, ctx, timeout=None):
        self.builds.append(tag)
        if tag.endswith("-setupprobe"):
            return self.probe_rc, "probe log"
        return self.build_rc, "build log"

    def image_size_mb(self, tag):
        return 250.0

    def run_detached(self, tag, name, workdir):
        pass

    def exec(self, name, argv, timeout=None):
        cmd = " ".join(argv)
        if "cat" in cmd and "junit.xml" in cmd:
            return 0, self.junit, False
        if "py_test_files" in cmd:
            return 0, "STATUS=conforming py_test_files=1", False
        return 0, "", False

    def rm(self, name, tag):
        pass


# ── truncation helper ────────────────────────────────────────────────────────
def test_truncate_keeps_through_setup_run_and_drops_trailing_steps():
    t = _truncate_after_setup(_DF, "setup.sh")
    assert t is not None
    assert "RUN bash /tmp/v3_setup.sh" in t
    assert "pip install" not in t                 # everything after the setup step is dropped
    assert t.strip().splitlines()[-1].strip() == "RUN bash /tmp/v3_setup.sh"


def test_truncate_none_when_no_setup_copy():
    assert _truncate_after_setup("FROM x\nRUN echo hi\n", "setup.sh") is None


def test_truncate_handles_line_continuation_in_setup_run():
    df = (
        "FROM x\n"
        "COPY setup.sh /s.sh\n"
        "RUN bash /s.sh \\\n"
        "    && echo done\n"
        "RUN pip install pytest\n"
    )
    t = _truncate_after_setup(df, "setup.sh")
    assert t is not None
    assert "echo done" in t and "pip install" not in t


# ── measure() integration ────────────────────────────────────────────────────
def test_setup_compile_ok_true_when_build_ok():
    d = FakeDocker(build_rc=0)
    row = measure(_env(), docker=d)
    assert row.setup_compile_ok is True
    assert not any(t.endswith("-setupprobe") for t in d.builds)   # no probe needed on success


def test_setup_compile_ok_true_when_build_fails_after_setup():
    # Full build fails (e.g. the trailing `pip install pytest`), but the truncated probe
    # (through setup.sh) succeeds -> setup.sh itself ran rc 0.
    d = FakeDocker(build_rc=1, probe_rc=0)
    row = measure(_env(), docker=d)
    assert row.build_ok is False and row.status == "build_fail"
    assert row.setup_compile_ok is True
    assert any(t.endswith("-setupprobe") for t in d.builds)       # probe ran


def test_setup_compile_ok_false_when_setup_step_fails():
    d = FakeDocker(build_rc=1, probe_rc=1)
    row = measure(_env(), docker=d)
    assert row.build_ok is False and row.setup_compile_ok is False


def test_setup_compile_ok_false_when_setup_step_unidentifiable_on_failed_build():
    d = FakeDocker(build_rc=1)
    row = measure(_env(dockerfile="FROM x\n", setup_scripts={}), docker=d)
    assert row.setup_compile_ok is False
    assert not any(t.endswith("-setupprobe") for t in d.builds)   # nothing to probe


def test_setup_compile_ok_false_on_unmeasurable_env():
    row = measure(_env(dockerfile=None, status="missing"), docker=FakeDocker())
    assert row.setup_compile_ok is False


# ── metrics aggregation ──────────────────────────────────────────────────────
def _mrow(**kw):
    base = dict(agent="a", repo="o/r", env_status="ok", build_ok=True, status="executed",
                executed=True, ebsr=True, pass_rate=1.0, collect_rc=0, collect_clean=True,
                setup_compile_ok=True)
    base.update(kw)
    return MeasureRow(**base)


def test_setup_compile_rate_aggregates_over_produced_rows():
    rows = [
        _mrow(setup_compile_ok=True),
        _mrow(setup_compile_ok=True),
        _mrow(build_ok=False, status="build_fail", executed=False, ebsr=False,
              collect_clean=False, setup_compile_ok=False),
    ]
    m = compute_metrics(rows)
    assert m["n_setup_compile"] == 2
    assert m["setup_compile_rate"] == round(2 / 3, 4)
