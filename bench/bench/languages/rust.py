# bench/languages/rust.py
"""The Rust Language. EBSR gate = the crate + its test target compile (`cargo test --no-run`). Tests
run via cargo-nextest, which emits JUnit when a `ci` profile with a junit path is configured. cargo
lives at /usr/local/cargo/bin on the rust image; measure()'s login shell strips that from PATH, so
each command prepends it (same fix as Go)."""
from __future__ import annotations

_CARGO_PATH = "export PATH=/usr/local/cargo/bin:$PATH"


class RustLanguage:
    name = "rust"
    short_circuit_gate = True   # if the test target doesn't compile, nextest cannot run

    def ensure_cmd(self, W: str) -> str:
        # PREBUILT cargo-nextest binary (decoupled from the crate's toolchain) + a ci profile that
        # writes JUnit to target/nextest/ci/junit.xml. curl ships on the rust base image.
        return (
            'URL=https://get.nexte.st/latest/linux; '
            '[ "$(uname -m)" = aarch64 ] && URL=https://get.nexte.st/latest/linux-arm; '
            'curl -LsSf "$URL" | tar zxf - -C /usr/local/bin 2>/dev/null || true; '
            f'mkdir -p {W}/.config && '
            f'printf "[profile.ci.junit]\\npath = \\"junit.xml\\"\\n" > {W}/.config/nextest.toml'
        )

    def gate_cmd(self, W: str) -> str:
        return f"{_CARGO_PATH} && cd {W} && cargo test --no-run"

    def gate_pass(self, rc: int) -> bool:
        return rc == 0

    def collect_cmd(self, W: str) -> str:
        return ""

    def run_cmd(self, W: str, junit_out: str) -> str:
        return f"{_CARGO_PATH} && cd {W} && cargo nextest run --profile ci"

    def junit_glob(self, W: str) -> str:
        return f"{W}/target/nextest/ci/junit.xml"

    def pkg_count_cmd(self, W: str) -> str:
        return ""
