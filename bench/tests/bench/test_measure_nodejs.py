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


def test_get_language_node_aliases():
    from bench.languages import get_language
    from bench.languages.nodejs import NodeLanguage
    for alias in ("nodejs", "node", "javascript", "typescript", "JavaScript"):
        assert isinstance(get_language(alias), NodeLanguage)
