# bench/languages/nodejs.py
"""The Node.js Language. EBSR gate = deps install AND a `test` script exists. The test RUNNER is
detected from the repo's own package.json (scripts.test first, then declared deps) and invoked with
a JUnit reporter: jest/jest-junit, mocha/mocha-junit-reporter, vitest (built-in junit reporter) or
`node --test` (built-in junit reporter). node/npm live on /usr/local/bin (on PATH even under the
login shell), so no PATH prefix is needed."""
from __future__ import annotations

# The grader's own reporters — not the repo's dependencies, so they are installed --no-save and
# must never reach package.json: adding them there without updating package-lock.json desyncs the
# two, and `npm ci` hard-fails on that mismatch, turning a scoring gap into a gate failure.
# Only jest and mocha need them; vitest and node:test emit JUnit themselves.
_REPORTERS = "jest-junit mocha-junit-reporter"

# Runner detection, evaluated INSIDE the container against the repo's package.json. Prints exactly
# one of: vitest | nodetest | mocha | jest. Everything is wrapped in try/catch and every failure
# path (file absent, malformed JSON, node missing) degrades to the historical `jest` default, whose
# cascade is the pre-detection behaviour verbatim.
#
# scripts.test is read BEFORE the dependency map: it is what the project actually runs, so it
# settles repos that declare two frameworks (a jest->vitest migration keeps both in devDependencies).
# Word-boundary matching is by explicit character class rather than \w so the program survives the
# single-quoted shell context without backslash escaping.
_DETECT_JS = (
    'var p={};try{p=require("./package.json")}catch(e){};'
    'var d=Object.assign({},p.dependencies,p.devDependencies);'
    'var s=String((p.scripts||{}).test||"");'
    'var B="[^A-Za-z0-9_]";'
    'var w=function(n){return new RegExp("(^|"+B+")"+n+"("+B+"|$)").test(s)};'
    'var has=function(n){return Object.prototype.hasOwnProperty.call(d,n)};'
    'var r="jest";'
    'if(w("vitest"))r="vitest";'
    # `node ... --test` (allowing --test-reporter, rejecting --testdir); [^&|;] keeps the match
    # inside one command of a chained script.
    'else if(new RegExp("(^|"+B+")node[^&|;]*--test("+B+"|$)").test(s))r="nodetest";'
    'else if(w("mocha"))r="mocha";'
    'else if(w("jest"))r="jest";'
    'else if(has("vitest"))r="vitest";'
    'else if(has("mocha")&&!has("jest"))r="mocha";'
    'console.log(r)'
)


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
        # Detect the runner, then run an absence-guarded cascade: the detected runner first, then
        # the historical jest -> mocha chain as a residual.
        #
        # THE ABSENCE GUARD (unchanged in meaning). Every step after the first is gated on the
        # JUnit file NOT existing (`[ -f OUT ] && break`), never on a runner's exit code. Test
        # runners exit non-zero when tests FAIL, so an exit-code guard would let a legitimately
        # failing suite fall through to the next runner, which would overwrite the real report with
        # an empty one. Because the loop breaks as soon as the file exists, a runner that wrote a
        # report — passing or failing — is always the last one to touch it. `rm -f OUT` up front
        # makes that guard sound rather than dependent on the file being absent by luck.
        #
        # --no-install stays on every npx invocation: reporters are libraries resolved by require()
        # and are installed explicitly below, whereas dropping it would let npx silently fetch a
        # test RUNNER the repo never declared (e.g. latest jest against a mocha project) and
        # fabricate a result.
        #
        # _reps reinstalls the reporters that gate_cmd's `npm ci` wiped (it deletes node_modules and
        # rebuilds it strictly from the lockfile, evicting ensure_cmd's --no-save copies; without
        # this, jest cannot load jest-junit, no JUnit is written, and a healthy suite records
        # executed=False / pass_rate 0). --prefer-offline serves the tarballs ensure_cmd already
        # cached, so it is normally a sub-second local reify; --no-audit suppresses the registry
        # advisory call. It is called only from the jest and mocha branches — vitest and node:test
        # emit JUnit natively, so the detected-vitest / detected-node:test paths pay no install at
        # all. `|| true` degrades any failure back to the pre-fix scoring gap rather than a
        # corrupted tree (this lands on an already-reified tree, so a peer conflict could otherwise
        # shuffle node_modules under the suite being measured; both reporters are dependency-light).
        #
        # jest via jest-junit — modern jest-junit reads JEST_JUNIT_OUTPUT_DIR/_NAME; the older
        # single-path JEST_JUNIT_OUTPUT is silently ignored (writes to cwd instead).
        #
        # node:test's junit reporter needs a recent node (>= 20.10 / 21); on an older base image the
        # invocation fails without writing a file and the cascade continues, i.e. no regression.
        d = f'"$(dirname {junit_out})"'
        return (
            f'cd {W} && mkdir -p {d} && rm -f {junit_out} && '
            f'export JEST_JUNIT_OUTPUT_DIR={d} && '
            f'export JEST_JUNIT_OUTPUT_NAME="$(basename {junit_out})" && ('
            f"_reps() {{ npm i -D --no-save --prefer-offline --no-audit --no-fund {_REPORTERS} "
            ">/dev/null 2>&1 || true; }; "
            "_jest() { _reps; npx --no-install jest --ci --reporters=default --reporters=jest-junit; }; "
            # Bare `mocha`'s default spec is ./test/*.{js,cjs,mjs} — NOT recursive — so repos that
            # keep tests in tests/ or test/unit/** collect nothing. Widening is a RETRY, gated on
            # the first report containing no <testcase>: a narrow run that found real tests is
            # never re-run, so no currently-working repo changes behaviour (and no repo mocharc
            # `spec` is overridden unless it already yielded nothing). Only existing directories are
            # passed, since mocha errors on a missing spec path.
            f"_mocha() {{ _reps; npx --no-install mocha --reporter mocha-junit-reporter "
            f"--reporter-options mochaFile={junit_out}; "
            f'grep -q "<testcase" {junit_out} 2>/dev/null || {{ S=""; '
            'for p in test tests spec specs; do [ -d "$p" ] && S="$S $p"; done; '
            "[ -n \"$S\" ] && npx --no-install mocha --recursive $S "
            f"--reporter mocha-junit-reporter --reporter-options mochaFile={junit_out}; }}; }}; "
            f"_vitest() {{ npx --no-install vitest run --reporter=junit --outputFile={junit_out}; }}; "
            f"_nodetest() {{ node --test --test-reporter=junit "
            f"--test-reporter-destination={junit_out}; }}; "
            f"R=$(node -e '{_DETECT_JS}' 2>/dev/null || echo jest); "
            'case "$R" in jest) C="jest mocha";; mocha) C="mocha jest";; '
            '*) C="$R jest mocha";; esac; '
            f'for r in $C; do [ -f {junit_out} ] && break; "_$r"; done'
            ") 2>/dev/null || true"
        )

    def junit_glob(self, W: str) -> str:
        return f"{W}/logs/junit.xml"

    def pkg_count_cmd(self, W: str) -> str:
        return ""
