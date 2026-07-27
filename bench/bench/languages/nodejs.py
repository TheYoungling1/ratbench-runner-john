# bench/languages/nodejs.py
"""The Node.js Language. EBSR gate = deps install AND a `test` script exists. Tests run via the
project's framework with a JUnit reporter (jest-junit / mocha-junit-reporter). node/npm live on
/usr/local/bin (on PATH even under the login shell), so no PATH prefix is needed."""
from __future__ import annotations

# The grader's own reporters — not the repo's dependencies, so they are installed --no-save and
# must never reach package.json: adding them there without updating package-lock.json desyncs the
# two, and `npm ci` hard-fails on that mismatch, turning a scoring gap into a gate failure.
_REPORTERS = "jest-junit mocha-junit-reporter"


class NodeLanguage:
    name = "nodejs"
    short_circuit_gate = True   # no working deps/test-script -> tests cannot run

    def ensure_cmd(self, W: str) -> str:
        # install common JUnit reporters (harmless if the repo uses neither framework).
        # measure() runs this BEFORE gate_cmd, whose `npm ci` deletes node_modules wholesale — so
        # for any repo with a lockfile this install does not survive to the run step and run_cmd
        # repeats it. Kept anyway: it populates the npm cache, which makes run_cmd's repeat a
        # cache-served no-op instead of a cold network fetch inside the test timer, and it is the
        # only install on the no-lockfile path (`npm ci` fails without touching node_modules, and
        # the `npm install` fallback leaves these extraneous packages in place).
        return f"cd {W} && npm i -D --no-save {_REPORTERS} 2>/dev/null || true"

    def gate_cmd(self, W: str) -> str:
        # deps install AND a `test` script is defined in package.json
        return (f"cd {W} && (npm ci || npm install) >/dev/null 2>&1 && "
                "node -e \"process.exit((require('./package.json').scripts||{}).test?0:1)\"")

    def gate_pass(self, rc: int) -> bool:
        return rc == 0

    def collect_cmd(self, W: str) -> str:
        return ""

    def run_cmd(self, W: str, junit_out: str) -> str:
        # Reinstall the reporters FIRST. gate_cmd ran `npm ci`, which deletes node_modules and
        # rebuilds it strictly from the lockfile; the --no-save reporters ensure_cmd put there are
        # gone by now, and `npx --no-install` will not fetch them. Without this, jest cannot load
        # the jest-junit reporter, no JUnit file is written, and measure() records executed=False
        # / pass_rate 0 for a suite that may be entirely healthy. --prefer-offline serves the
        # tarballs ensure_cmd already cached, so this is normally a sub-second local reify;
        # --no-audit suppresses the registry advisory call that `npm i` otherwise makes.
        #
        # Unlike ensure_cmd's copy this lands on an already-reified tree, so npm reconciles two new
        # top-level adds against it: --no-save leaves package.json and the lockfile untouched on
        # disk, but a peer-dependency conflict could still shuffle node_modules under the suite we
        # are about to measure. Both reporters are dependency-light, and `|| true` degrades any
        # failure back to the pre-fix scoring gap rather than a corrupted tree.
        #
        # --no-install stays on npx deliberately: the reporters are libraries resolved by require()
        # and are handled above, whereas dropping it would let npx silently fetch a test RUNNER the
        # repo never declared (e.g. latest jest against a mocha project) and fabricate a result.
        #
        # jest via jest-junit — modern jest-junit reads JEST_JUNIT_OUTPUT_DIR/_NAME; the older
        # single-path JEST_JUNIT_OUTPUT is silently ignored (writes to cwd instead). jest exits
        # non-zero when a test fails, so guard the mocha fallback on junit ABSENCE — otherwise a
        # legitimately-failing jest run would trigger mocha and overwrite the report with an empty one.
        return (f'cd {W} && mkdir -p "$(dirname {junit_out})" && '
                f'export JEST_JUNIT_OUTPUT_DIR="$(dirname {junit_out})" && '
                f'export JEST_JUNIT_OUTPUT_NAME="$(basename {junit_out})" && '
                f"(npm i -D --no-save --prefer-offline --no-audit --no-fund {_REPORTERS} "
                ">/dev/null 2>&1 || true; "
                "npx --no-install jest --ci --reporters=default --reporters=jest-junit; "
                f"[ -f {junit_out} ] || npx --no-install mocha --reporter mocha-junit-reporter "
                f"--reporter-options mochaFile={junit_out}) 2>/dev/null || true")

    def junit_glob(self, W: str) -> str:
        return f"{W}/logs/junit.xml"

    def pkg_count_cmd(self, W: str) -> str:
        return ""
