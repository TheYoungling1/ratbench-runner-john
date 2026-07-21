# bench/languages/java.py
"""The Java Language. Build tool detected at run time (Maven if pom.xml, else Gradle), preferring the
repo's ./mvnw / ./gradlew wrapper. EBSR gate = main + test sources compile (test-compile /
testClasses). Tests run (NOT skipped) via `mvn test` / `gradle test`, which write JUnit XML natively
(Surefire -> target/surefire-reports/*.xml; Gradle -> build/test-results/test/*.xml) — measure()
merges the many files. Java lives under $JAVA_HOME; the login shell strips it from PATH, so commands
prepend $JAVA_HOME/bin (mvn/gradle are on the base image's /usr/bin)."""
from __future__ import annotations

_JAVA_PATH = 'export PATH="${JAVA_HOME:-/opt/java/openjdk}/bin:$PATH"'


class JavaLanguage:
    name = "java"
    short_circuit_gate = True

    def ensure_cmd(self, W: str) -> str:
        return ""   # Surefire/Gradle emit JUnit natively; nothing to install

    def gate_cmd(self, W: str) -> str:
        # `if wrapper; then wrapper; else system; fi` — NOT `wrapper && ... || system`: the &&/||
        # idiom would run the system tool whenever the WRAPPER command merely exits non-zero (a real
        # compile failure), conflating "wrapper missing" with "compile failed" (double-compile + a
        # version-skewed system tool could false-green the gate).
        return (f"{_JAVA_PATH} && cd {W} && if [ -f pom.xml ]; then "
                "if [ -x ./mvnw ]; then ./mvnw -q -B test-compile; else mvn -q -B test-compile; fi; "
                "else if [ -x ./gradlew ]; then ./gradlew -q testClasses; else gradle -q testClasses; fi; fi")

    def gate_pass(self, rc: int) -> bool:
        return rc == 0

    def collect_cmd(self, W: str) -> str:
        return ""

    def run_cmd(self, W: str, junit_out: str) -> str:
        # `if wrapper; then wrapper; else system; fi` (see gate_cmd) — the &&/|| idiom would re-run
        # the whole suite via the system tool on ANY failing test (the common measured case),
        # doubling test time AND overwriting the Surefire/Gradle reports the pass_rate is read from.
        return (f"{_JAVA_PATH} && cd {W} && if [ -f pom.xml ]; then "
                "if [ -x ./mvnw ]; then ./mvnw -B test; else mvn -B test; fi; "
                "else if [ -x ./gradlew ]; then ./gradlew test; else gradle test; fi; fi")

    def junit_glob(self, W: str) -> str:
        # two report locations (Maven Surefire, Gradle) — measure() cats both globs and merges.
        return f"{W}/target/surefire-reports/*.xml {W}/build/test-results/test/*.xml"

    def pkg_count_cmd(self, W: str) -> str:
        return ""
