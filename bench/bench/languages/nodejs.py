# bench/languages/nodejs.py
"""The Node.js Language. EBSR gate = deps install AND a `test` script exists. Tests run via the
project's framework with a JUnit reporter (jest-junit / mocha-junit-reporter). node/npm live on
/usr/local/bin (on PATH even under the login shell), so no PATH prefix is needed."""
from __future__ import annotations


class NodeLanguage:
    name = "nodejs"
    short_circuit_gate = True   # no working deps/test-script -> tests cannot run

    def ensure_cmd(self, W: str) -> str:
        # install common JUnit reporters (harmless if the repo uses neither framework)
        return f"cd {W} && npm i -D --no-save jest-junit mocha-junit-reporter 2>/dev/null || true"

    def gate_cmd(self, W: str) -> str:
        # deps install AND a `test` script is defined in package.json
        return (f"cd {W} && (npm ci || npm install) >/dev/null 2>&1 && "
                "node -e \"process.exit((require('./package.json').scripts||{}).test?0:1)\"")

    def gate_pass(self, rc: int) -> bool:
        return rc == 0

    def collect_cmd(self, W: str) -> str:
        return ""

    def run_cmd(self, W: str, junit_out: str) -> str:
        # jest via jest-junit (env-driven output path); fall through to mocha's junit reporter
        return (f"cd {W} && export JEST_JUNIT_OUTPUT={junit_out} && "
                "(npx --no-install jest --ci --reporters=default --reporters=jest-junit "
                f"|| npx --no-install mocha --reporter mocha-junit-reporter "
                f"--reporter-options mochaFile={junit_out}) 2>/dev/null || true")

    def junit_glob(self, W: str) -> str:
        return f"{W}/logs/junit.xml"

    def pkg_count_cmd(self, W: str) -> str:
        return ""
