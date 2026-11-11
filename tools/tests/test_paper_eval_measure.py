"""tools/paper_eval_measure.py: one build, two shells, image kept only between the passes."""
import json
from dataclasses import dataclass
from types import SimpleNamespace

from tools.paper_eval_measure import make_docker, measure_both


@dataclass
class Row:
    shell: str


class FakeBase:
    def __init__(self):
        self.calls = []

    def exec(self, name, argv, timeout=None):
        self.calls.append(("exec", timeout))
        return 0, "", False

    def rm(self, name, tag):
        self.calls.append(("rmi", tag))


def _env():
    return SimpleNamespace(agent="paperev", repo=SimpleNamespace(full_name="o/r"), meta={})


def _measure_mod(log):
    mod = SimpleNamespace(_sh=None)

    def measure(env, docker):
        shell = mod._sh("x")[1]
        log.append((shell, docker.keep_image))
        docker.rm("ctr", "tag")
        return Row(shell=shell)
    mod.measure = measure
    return mod


def test_both_shells_one_build_then_image_removed(tmp_path):
    log = []
    docker = make_docker(FakeBase, 1800)()
    done = measure_both(_env(), str(tmp_path), measure_mod=_measure_mod(log), docker=docker,
                        error_row=None)
    assert done == ["lc", "c"]
    assert log == [("-lc", True), ("-c", False)]          # image kept after -lc, removed after -c
    assert [c for c in docker.calls if c[0] == "rmi"] == [("rmi", "tag")]
    for shell in ("lc", "c"):
        row = json.load(open(tmp_path / shell / "paperev" / "o" / "r" / "row.json"))
        assert row["shell"] == f"-{shell}"


def test_resume_skips_written_shell_and_still_removes_image(tmp_path):
    log = []
    p = tmp_path / "lc" / "paperev" / "o" / "r"
    p.mkdir(parents=True)
    (p / "row.json").write_text("{}")
    docker = make_docker(FakeBase, 1800)()
    assert measure_both(_env(), str(tmp_path), measure_mod=_measure_mod(log), docker=docker,
                        error_row=None) == ["c"]
    assert log == [("-c", False)]


def test_untimed_exec_gets_the_cap_and_explicit_timeouts_win():
    d = make_docker(FakeBase, 1800)()
    d.exec("n", ["true"])
    d.exec("n", ["true"], timeout=60)
    assert d.calls == [("exec", 1800), ("exec", 60)]


JUNIT = '<testsuites><testsuite><testcase classname="t" name="x"><failure message="OperationalError: no db"/></testcase></testsuite></testsuites>'


class JunitBase(FakeBase):
    def exec(self, name, argv, timeout=None):
        super().exec(name, argv, timeout)
        if argv[-1].startswith("cat /testbed/logs/junit.xml"):
            return 0, JUNIT, False
        return 0, "", False


def _junit_measure_mod(log):
    mod = SimpleNamespace(_sh=None)

    def measure(env, docker):
        shell = mod._sh("x")[1]
        docker.exec("ctr", mod._sh("cat /testbed/logs/junit.xml 2>/dev/null || true"))
        log.append(shell)
        return Row(shell=shell)
    mod.measure = measure
    return mod


def test_junit_saved_next_to_each_shells_row(tmp_path):
    log = []
    docker = make_docker(JunitBase, 1800)()
    measure_both(_env(), str(tmp_path), measure_mod=_junit_measure_mod(log), docker=docker,
                 error_row=None)
    for shell in ("lc", "c"):
        assert (tmp_path / shell / "paperev" / "o" / "r" / "junit.xml").read_text() == JUNIT


def test_single_shell_option_measures_only_that_shell(tmp_path):
    log = []
    docker = make_docker(JunitBase, 1800)()
    done = measure_both(_env(), str(tmp_path), measure_mod=_junit_measure_mod(log), docker=docker,
                        error_row=None, shells=("c",))
    assert done == ["c"] and log == ["-c"]
    assert not (tmp_path / "lc").exists()
