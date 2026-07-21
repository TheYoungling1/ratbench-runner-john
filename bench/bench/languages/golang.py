# bench/languages/golang.py
"""The Go Language. EBSR gate = the module compiles (`go build ./...`). Tests run via gotestsum,
which emits JUnit XML (one file, possibly many <testsuite> per package — parse_junit handles that).
gotestsum is installed to /usr/local/bin (already on PATH) so run_cmd needs no PATH juggling."""
from __future__ import annotations


class GoLanguage:
    name = "golang"
    short_circuit_gate = True   # if `go build` fails, gotestsum cannot run — skip it

    def ensure_cmd(self, W: str) -> str:
        # Install the JUnit reporter at measure time (host-owned, untimed, before the gate). GOBIN
        # forces a standard PATH location so run_cmd finds it regardless of GOPATH/GOBIN config.
        return "GOBIN=/usr/local/bin go install gotest.tools/gotestsum@latest 2>/dev/null || true"

    def gate_cmd(self, W: str) -> str:
        return f"cd {W} && go build ./..."

    def gate_pass(self, rc: int) -> bool:
        return rc == 0

    def collect_cmd(self, W: str) -> str:
        return ""   # node-ids come straight from the JUnit <testcase> elements

    def run_cmd(self, W: str, junit_out: str) -> str:
        return f"cd {W} && gotestsum --junitfile {junit_out} --format standard-quiet -- ./..."

    def junit_glob(self, W: str) -> str:
        return f"{W}/logs/junit.xml"

    def pkg_count_cmd(self, W: str) -> str:
        return ""
