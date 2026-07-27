# tests/bench/test_measure_nodejs.py
from bench.measure import measure
from langfake import FakeDocker, lang_env

_JEST_JUNIT = ('<testsuites><testsuite name="sum" tests="3" failures="1">'
               '<testcase classname="sum" name="a"/>'
               '<testcase classname="sum" name="b"/>'
               '<testcase classname="sum" name="c"><failure message="fail">x</failure></testcase>'
               '</testsuite></testsuites>')


def test_nodejs_happy_path_known_answer():
    d = FakeDocker(junit=_JEST_JUNIT)
    row = measure(lang_env("nodejs"), docker=d)
    assert row.build_ok is True and row.ebsr is True and row.executed is True
    assert row.total == 3 and row.passed == 2 and row.failed == 1
    assert row.pass_rate == 0.6667 and row.status == "executed"
    assert any("npm ci" in c or "npm install" in c for c in d.calls)   # gate
    # ensure_cmd (always run) also mentions "jest"/"mocha" (installing jest-junit /
    # mocha-junit-reporter), so anchor on "npx" — unique to the actual test-runner invocation
    # in run_cmd — to assert the RUN step specifically happened.
    assert any("npx --no-install jest" in c or "npx --no-install mocha" in c for c in d.calls)


def test_nodejs_gate_fail_short_circuits():
    # the `test` script / install gate fails -> gate_fail, run skipped.
    d = FakeDocker(script={"process.exit": (1, "", False)}, junit=_JEST_JUNIT)
    row = measure(lang_env("nodejs"), docker=d)
    assert row.status == "gate_fail" and row.ebsr is False and row.executed is False
    # "npx" only appears in run_cmd (ensure_cmd's jest-junit/mocha-junit-reporter install
    # literally contains "jest"/"mocha" too, so those substrings can't distinguish gate-fail).
    assert not any("npx" in c for c in d.calls)


def _node_lang():
    from bench.languages import get_language
    return get_language("nodejs")


def test_reporters_are_reinstalled_after_the_gate_wipes_node_modules():
    """measure() runs ensure_cmd BEFORE gate_cmd, and the gate's `npm ci` deletes node_modules
    wholesale and rebuilds it from the lockfile alone — evicting the --no-save reporters that
    ensure_cmd installed. `npx --no-install` then refuses to fetch them, so jest cannot load
    jest-junit, no JUnit is written, and a healthy suite scores executed=False / pass_rate 0.

    Pin the ordering: some reporter install must occur AFTER the last `npm ci`. Keying on
    --no-save is what makes this discriminating — run_cmd has always contained the string
    "jest-junit" (as --reporters=jest-junit) without ever installing it."""
    d = FakeDocker(junit=_JEST_JUNIT)
    measure(lang_env("nodejs"), docker=d)
    wipes = [i for i, c in enumerate(d.calls) if "npm ci" in c]
    installs = [i for i, c in enumerate(d.calls) if "--no-save" in c and "jest-junit" in c]
    assert wipes, "expected the EBSR gate to run `npm ci`"
    assert any(i >= wipes[-1] for i in installs), (
        "no reporter install after `npm ci` — the wiped jest-junit is never restored, so the "
        "run step writes no JUnit and every lockfile-bearing Node repo scores zero tests")


def test_run_cmd_installs_reporters_before_invoking_the_runner():
    # ensure_cmd and run_cmd reach the container as two separate `bash -lc` strings, but the
    # install and the jest invocation live inside the SAME run_cmd string — so the order that
    # actually matters is textual. Reporter install must precede the runner that loads it.
    cmd = _node_lang().run_cmd("/testbed", "/testbed/logs/junit.xml")
    assert "--no-save" in cmd, "run_cmd must (re)install the reporters itself"
    assert cmd.index("--no-save") < cmd.index("npx --no-install jest")


def test_run_cmd_keeps_no_install_and_the_junit_absence_guard():
    # Two properties the reinstall must not trade away: (1) --no-install stays, so npx never
    # silently fetches a test RUNNER the repo did not declare; (2) the mocha fallback stays
    # guarded on junit ABSENCE, so a legitimately-failing jest run cannot trigger mocha and
    # overwrite the report with an empty one.
    cmd = _node_lang().run_cmd("/testbed", "/testbed/logs/junit.xml")
    assert "npx --no-install jest" in cmd and "npx --no-install mocha" in cmd
    assert "[ -f /testbed/logs/junit.xml ] || npx --no-install mocha" in cmd


def test_ensure_cmd_still_prewarms_the_reporters():
    # run_cmd repairing the wipe does not make ensure_cmd dead. It populates the npm cache so the
    # repair is a cache-served reify rather than a cold fetch inside the test timer, and on the
    # no-lockfile path (`npm ci` fails without touching node_modules, `npm install` leaves
    # extraneous packages alone) it is the install that actually survives.
    cmd = _node_lang().ensure_cmd("/testbed")
    assert "jest-junit" in cmd and "mocha-junit-reporter" in cmd and "--no-save" in cmd


def test_get_language_node_aliases():
    from bench.languages import get_language
    from bench.languages.nodejs import NodeLanguage
    for alias in ("nodejs", "node", "javascript", "typescript", "JavaScript"):
        assert isinstance(get_language(alias), NodeLanguage)
