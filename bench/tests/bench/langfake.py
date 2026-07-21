# tests/bench/langfake.py — reusable FakeDocker + env builder for per-language measure() tests.
from bench.schema import HarvestedEnv, RepoSpec


class FakeDocker:
    """Records every exec cmd in .calls; returns scripted (rc, stdout, timed_out) by substring
    match. Any `cat …` on a `.xml` path returns the canned junit. The /testbed probe answers
    conforming by default (override via script={"py_test_files": (...)})."""

    def __init__(self, build_rc=0, size_mb=250.0, script=None, junit=""):
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
        if "cat" in cmd and ".xml" in cmd:
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


def lang_env(language, **kw):
    base = dict(agent="v3", repo=RepoSpec("o/r", "https://github.com/o/r", language=language),
                dockerfile="FROM base", base_image="base:latest", status="ok", meta={})
    base.update(kw)
    return HarvestedEnv(**base)
