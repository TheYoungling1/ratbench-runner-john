# tests/bench/test_nodejs_runner_cascade.py
"""Execute NodeLanguage.run_cmd for real, under bash, with npm/npx replaced by shims.

run_cmd is a shell program, so string assertions can only pin its spelling — they cannot show that
detection picks the right runner or that a failing-but-reporting runner stops the cascade. These
tests run the actual command with a scripted `npx` that records every invocation and writes (or
withholds) a JUnit file on demand. `node` is the real one: the detector is a `node -e` program and
the node:test branch is a real `node --test` run.
"""
import json
import os
import pathlib
import shutil
import subprocess

import pytest

from bench.languages import get_language

pytestmark = pytest.mark.skipif(shutil.which("node") is None or shutil.which("bash") is None,
                                reason="needs node + bash to execute run_cmd")

# `npx <flags> <runner> ...` — log the call, then behave as env FAKE_<runner> says:
#   junit       write a 1-pass report, exit 0        (a healthy suite)
#   fail_junit  write a 1-fail report, exit 1        (a legitimately FAILING suite)
#   empty       write an empty <testsuites/>, exit 0 (ran, collected nothing)
#   <unset>     write nothing, exit 1                (runner not installed / crashed)
# The output path is read the way each runner really takes it: mocha from `mochaFile=`, vitest
# from `--outputFile=`, jest from the JEST_JUNIT_OUTPUT_DIR/_NAME env pair.
_NPX_SHIM = r"""#!/bin/bash
echo "npx $*" >> "$SHIM_LOG"
runner=""
out=""
for a in "$@"; do
  case "$a" in
    mochaFile=*) out="${a#mochaFile=}" ;;
    --outputFile=*) out="${a#--outputFile=}" ;;
    -*) ;;
    *) [ -z "$runner" ] && runner="$a" ;;
  esac
done
[ -n "$out" ] || out="$JEST_JUNIT_OUTPUT_DIR/$JEST_JUNIT_OUTPUT_NAME"
eval "mode=\${FAKE_$runner:-}"
case "$mode" in
  junit) printf '%s' '<testsuites><testsuite name="s" tests="1" failures="0"><testcase classname="s" name="a"/></testsuite></testsuites>' > "$out"; exit 0 ;;
  fail_junit) printf '%s' '<testsuites><testsuite name="s" tests="1" failures="1"><testcase classname="s" name="a"><failure message="x">boom</failure></testcase></testsuite></testsuites>' > "$out"; exit 1 ;;
  empty) printf '%s' '<testsuites/>' > "$out"; exit 0 ;;
  *) exit 1 ;;
esac
"""

_NPM_SHIM = '#!/bin/bash\necho "npm $*" >> "$SHIM_LOG"\nexit 0\n'


def _run(tmp_path, pkg: dict | str | None, files: dict | None = None, **fakes):
    """Materialise a repo, run run_cmd under bash, return (invocations, junit_text_or_None)."""
    W = tmp_path / "testbed"
    W.mkdir()
    if pkg is not None:
        (W / "package.json").write_text(pkg if isinstance(pkg, str) else json.dumps(pkg))
    for name, body in (files or {}).items():
        p = W / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)

    binv = tmp_path / "bin"
    binv.mkdir()
    for name, body in (("npx", _NPX_SHIM), ("npm", _NPM_SHIM)):
        f = binv / name
        f.write_text(body)
        f.chmod(0o755)

    log = tmp_path / "shim.log"
    junit = W / "logs" / "junit.xml"
    node_dir = os.path.dirname(shutil.which("node"))
    env = {"PATH": f"{binv}:{node_dir}:/usr/bin:/bin", "SHIM_LOG": str(log),
           "HOME": str(tmp_path), **{f"FAKE_{k}": v for k, v in fakes.items()}}
    cmd = get_language("nodejs").run_cmd(str(W), str(junit))
    subprocess.run(["bash", "-c", cmd], env=env, capture_output=True, timeout=180)
    calls = log.read_text().splitlines() if log.exists() else []
    return calls, (junit.read_text() if junit.exists() else None)


def _runners(calls: list) -> list:
    """The test runners npx was asked to launch, in order (drops the npm reporter installs)."""
    out = []
    for c in calls:
        if not c.startswith("npx "):
            continue
        for tok in c.split()[1:]:
            if not tok.startswith("-"):
                out.append(tok)
                break
    return out


# ---------------------------------------------------------------- detection

def test_scripts_test_selects_vitest_over_a_declared_jest(tmp_path):
    # Migrating repos keep both frameworks in devDependencies; scripts.test is what the project
    # actually runs, so it must win over the dependency map.
    calls, _ = _run(tmp_path, {"scripts": {"test": "vitest run --coverage"},
                               "devDependencies": {"vitest": "^2", "jest": "^29"}})
    assert _runners(calls)[0] == "vitest"


def test_declared_vitest_selects_vitest_when_the_script_is_indirect(tmp_path):
    calls, _ = _run(tmp_path, {"scripts": {"test": "npm run test:unit"},
                               "devDependencies": {"vitest": "^2"}})
    assert _runners(calls)[0] == "vitest"


def test_declared_mocha_selects_mocha(tmp_path):
    calls, _ = _run(tmp_path, {"scripts": {"test": "npm run test:unit"},
                               "devDependencies": {"mocha": "^10"}})
    assert _runners(calls)[0] == "mocha"


def test_jest_remains_the_default_when_nothing_is_recognised(tmp_path):
    calls, _ = _run(tmp_path, {"scripts": {"test": "nx run-many -t test"}})
    assert _runners(calls)[0] == "jest"


@pytest.mark.parametrize("pkg", [None, "{ this is not json", "", {"scripts": {}}])
def test_unreadable_package_json_degrades_to_the_historical_jest_then_mocha(tmp_path, pkg):
    # Detection must never crash the run step: absent, malformed or empty package.json all fall
    # back to exactly the pre-detection cascade.
    calls, _ = _run(tmp_path, pkg)
    assert _runners(calls) == ["jest", "mocha"]


def test_no_install_survives_on_every_npx_invocation(tmp_path):
    # Dropping --no-install would let npx silently fetch a test RUNNER the repo never declared and
    # fabricate a result.
    calls, _ = _run(tmp_path, {"devDependencies": {"vitest": "^2"}})
    npx = [c for c in calls if c.startswith("npx ")]
    assert npx and all("--no-install" in c for c in npx)


# ------------------------------------------------------- the absence guard

def test_a_failing_jest_run_does_not_fall_through_to_mocha(tmp_path):
    # THE property the cascade must keep: jest exits non-zero on a failing suite but still writes
    # its report. Guarding on exit code (rather than on the JUnit file's absence) would run mocha
    # next and overwrite a real result with an empty one.
    calls, junit = _run(tmp_path, {"devDependencies": {"jest": "^29"}}, jest="fail_junit")
    assert _runners(calls) == ["jest"]
    assert "<failure" in junit and 'tests="1"' in junit


def test_a_failing_vitest_run_does_not_fall_through_either(tmp_path):
    calls, junit = _run(tmp_path, {"devDependencies": {"vitest": "^2"}}, vitest="fail_junit")
    assert _runners(calls) == ["vitest"]
    assert "<failure" in junit


def test_a_runner_that_writes_nothing_falls_through_to_the_next(tmp_path):
    calls, junit = _run(tmp_path, {"devDependencies": {"vitest": "^2"}}, mocha="junit")
    assert _runners(calls) == ["vitest", "jest", "mocha"]   # detected first, then the residual chain
    assert "<testcase" in junit


def test_the_detected_runner_is_not_retried_by_the_residual_chain(tmp_path):
    calls, _ = _run(tmp_path, {"devDependencies": {"mocha": "^10"}})
    assert _runners(calls) == ["mocha", "jest"]             # mocha first, and only once


def test_the_vitest_path_pays_for_no_reporter_install(tmp_path):
    # vitest and node:test emit JUnit natively; paying npm for jest-junit there is pure latency.
    calls, _ = _run(tmp_path, {"devDependencies": {"vitest": "^2"}}, vitest="junit")
    assert not [c for c in calls if c.startswith("npm ")]


def test_the_jest_path_still_reinstalls_the_reporters_the_gate_wiped(tmp_path):
    # gate_cmd's `npm ci` deletes node_modules and rebuilds it from the lockfile alone, evicting
    # ensure_cmd's --no-save reporters; without this reinstall jest cannot load jest-junit and a
    # healthy suite scores executed=False / pass_rate 0.
    calls, _ = _run(tmp_path, {"devDependencies": {"jest": "^29"}}, jest="junit")
    assert any(c.startswith("npm ") and "jest-junit" in c and "--no-save" in c for c in calls)


# ------------------------------------------------------------- node --test

def test_node_test_is_detected_and_produces_a_real_junit_report(tmp_path):
    # Not a shim: real `node --test --test-reporter=junit`. Also pins that no npx runner is
    # launched, i.e. the built-in runner short-circuits the whole cascade.
    calls, junit = _run(
        tmp_path,
        {"scripts": {"test": "node --test"}},
        {"a.test.js": 'const t=require("node:test");const a=require("node:assert");\n'
                      't.test("ok",()=>a.equal(1,1));\n'},
    )
    assert _runners(calls) == []
    assert junit is not None and "<testcase" in junit


def test_node_test_flag_lookalikes_do_not_trigger_the_built_in_runner(tmp_path):
    calls, _ = _run(tmp_path, {"scripts": {"test": "node scripts/run.js --testdir spec"}})
    assert _runners(calls)[0] == "jest"


# ------------------------------------------------- mocha's non-recursive default spec

def test_mocha_retries_recursively_when_the_default_spec_collects_nothing(tmp_path):
    # Bare mocha's default spec is ./test/*.{js,cjs,mjs} — NOT recursive — so a repo with tests in
    # tests/ or test/unit/** reports zero. Retry with --recursive over the directories that exist.
    calls, _ = _run(tmp_path, {"devDependencies": {"mocha": "^10"}},
                    {"tests/unit/a.spec.js": "// test\n"}, mocha="empty")
    mocha_calls = [c for c in calls if c.startswith("npx ") and " mocha" in c]
    assert len(mocha_calls) == 2
    assert "--recursive" in mocha_calls[1]
    assert " tests " in mocha_calls[1] and " test " not in mocha_calls[1]   # only existing dirs


def test_mocha_is_not_retried_when_the_default_spec_found_tests(tmp_path):
    # The widening must not touch a repo that already works: a report with real <testcase>
    # elements ends the mocha step, so a repo-supplied .mocharc `spec` is never overridden.
    calls, _ = _run(tmp_path, {"devDependencies": {"mocha": "^10"}},
                    {"test/a.js": "// test\n"}, mocha="junit")
    assert len([c for c in calls if c.startswith("npx ") and " mocha" in c]) == 1
