# bench/languages/golang.py
"""The Go Language. EBSR gate = the module compiles (`go build ./...`). Tests run via gotestsum,
which emits JUnit XML (one file, possibly many <testsuite> per package — parse_junit handles that).
gotestsum installs to /usr/local/bin (on PATH); each command prepends /usr/local/go/bin because
measure()'s login shell (`bash -lc`) sources /etc/profile, which resets PATH and would otherwise
drop the go toolchain the base image put there."""
from __future__ import annotations

_GO_PATH = "export PATH=$PATH:/usr/local/go/bin"


class GoLanguage:
    name = "golang"
    short_circuit_gate = True   # if `go build` fails, gotestsum cannot run — skip it

    def ensure_cmd(self, W: str) -> str:
        return f"{_GO_PATH} && GOBIN=/usr/local/bin go install gotest.tools/gotestsum@latest 2>/dev/null || true"

    def gate_cmd(self, W: str) -> str:
        return f"{_GO_PATH} && cd {W} && go build ./..."

    def gate_pass(self, rc: int) -> bool:
        return rc == 0

    def collect_cmd(self, W: str) -> str:
        return ""   # node-ids come straight from the JUnit <testcase> elements

    def run_cmd(self, W: str, junit_out: str) -> str:
        return f"{_GO_PATH} && cd {W} && gotestsum --junitfile {junit_out} --format standard-quiet -- ./..."

    def junit_glob(self, W: str) -> str:
        return f"{W}/logs/junit.xml"

    def pkg_count_cmd(self, W: str) -> str:
        return ""
