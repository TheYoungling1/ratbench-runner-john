# bench/languages/python.py
"""The Python (pytest) Language — the four pytest-specific commands measure() used to hard-code.
ensure_cmd/run_cmd have since changed (never alter the repo's pytest stack), so rows measured before
that change are not byte-identical for repos where the old `pip install pytest pytest-timeout` hurt."""
from __future__ import annotations

import shlex


class PythonLanguage:
    name = "python"
    short_circuit_gate = False   # the collect gate is diagnostic; the full run happens regardless

    _TIMEOUT_FLAGS = "--timeout=120 --timeout-method=signal"

    def ensure_cmd(self, W: str) -> str:
        # Never change the repo's own test stack. pytest is installed only when absent; pytest-timeout
        # only with --no-deps onto pytest>=7, because it requires 7+ and a plain install upgraded
        # Cloud-CV/EvalAI's pinned pytest 5.4.2 to 9.x, which rejects its setup.cfg. If the repo then
        # cannot load pytest with --timeout (SeleniumBase refuses it: JonBunator/Enterr; Checkmk's
        # conftest also defines --session-timeout), the plugin we added is removed again. The marker
        # file is what lets run_cmd pass --timeout; it also needs the plugin importable, because
        # pytest 5.4.2 exits 0 on `--timeout ... --help` with no plugin at all.
        mark, ours = f"{W}/logs/.bench_timeout_ok", f"{W}/logs/.bench_timeout_installed"
        pip = "python -m pip {0} --break-system-packages {1} || python -m pip {0} {1}"
        return (
            f"mkdir -p {W}/logs; "
            "python -m pytest --version >/dev/null 2>&1 || "
            f"{pip.format('install -q', 'pytest')} || true; "
            "if ! python -c 'import pytest_timeout' >/dev/null 2>&1 && "
            "python -c 'import pytest, sys; sys.exit(int(pytest.__version__.split(\".\")[0]) < 7)' "
            ">/dev/null 2>&1; then "
            f"{{ {pip.format('install -q --no-deps', 'pytest-timeout')}; }} && touch {ours}; fi; "
            "if python -c 'import pytest_timeout' >/dev/null 2>&1 && "
            f"python -m pytest {self._TIMEOUT_FLAGS} --help >/dev/null 2>&1; then touch {mark}; "
            f"elif [ -f {ours} ]; then {pip.format('uninstall -y -q', 'pytest-timeout')}; fi; true")

    # Collection passes NO path: the container's workdir is W, and an explicit path makes pytest
    # ignore the repo's `testpaths`, collecting vendored libs/examples/scripts the repo never runs
    # (fontbakery snippets/, ramble lib/ramble/external, tinygrad examples/). With no testpaths
    # configured pytest collects from cwd, exactly as before. run_cmd has always passed no path.
    # PYTEST_ADDOPTS / PYTEST_PLUGINS (a Dockerfile ENV or the runtime handoff) could narrow the suite
    # the same way an edited pytest.ini would, so every measured pytest invocation clears them.
    _NO_PYTEST_ENV = "unset PYTEST_ADDOPTS PYTEST_PLUGINS; "

    def gate_cmd(self, W: str) -> str:
        return f"{self._NO_PYTEST_ENV}python -m pytest --collect-only -q --disable-warnings; exit ${{PIPESTATUS[0]:-$?}}"

    # Root-level files pytest reads its config from (pytest 9 adds pytest.toml / .pytest.toml).
    _ROOT_CFG = ("pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml", "pyproject.toml",
                 "tox.ini", "setup.cfg", "conftest.py")

    def config_restore_cmd(self, W: str, sha: str) -> str:
        """Put the repo's OWN pytest config back before it is measured, so a system cannot narrow
        its suite (agents wrote a narrow `testpaths` pytest.ini on markqvist/reticulum and
        numba/llvmlite). Against the pinned commit (HEAD when absent from the object store,
        printed as such): root config files and conftest.py at any depth are checked out if the
        commit tracks them, deleted if not. Nested pytest.ini/tox.ini/... are left: with no path
        argument pytest reads config only from W and its ancestors. Exits non-zero, naming the
        path, on any failure; measure() records the log."""
        names = " ".join(self._ROOT_CFG)
        root_re = "|".join(n.replace(".", r"\.") for n in self._ROOT_CFG)
        return (
            f"cd {W} && [ \"$(git rev-parse --show-toplevel 2>/dev/null)\" = \"$(pwd -P)\" ] "
            "|| { echo 'FAILED: not a git worktree root'; exit 2; }\n"
            f"SHA={shlex.quote(sha)}; REF=HEAD; WHY=no_pinned_sha\n"
            'if [ -n "$SHA" ]; then WHY=pinned_sha_absent; '
            'git cat-file -e "$SHA^{commit}" 2>/dev/null && { REF=$SHA; WHY=pinned; }; fi\n'
            'echo "REF=$REF ($WHY)"\n'
            "{ git -c core.quotepath=off ls-tree -r --name-only \"$REF\"; "
            "find . -path ./.git -prune -o \\( -name site-packages -o -name dist-packages \\) -prune "
            "-o \\( -type f -o -type l \\) -print | sed 's|^\\./||'; } "
            f"| grep -E '^({root_re})$|/conftest\\.py$' | sort -u | {{ rc=0\n"
            "while IFS= read -r f; do\n"
            '  if git cat-file -e "$REF:$f" 2>/dev/null; then\n'
            '    git diff --quiet "$REF" -- "$f" 2>/dev/null || { git checkout -q "$REF" -- "$f" '
            '&& echo "restored $f" || { echo "FAILED restore $f"; rc=1; }; }\n'
            '  else rm -f "$f" && echo "removed $f" || { echo "FAILED remove $f"; rc=1; }; fi\n'
            "done\n"
            # ponytail: only W's parent is swept -- for W=/testbed that is `/`, every ancestor.
            f'P=$(dirname {W}); for n in {names}; do [ -e "$P/$n" ] || continue; '
            'rm -f "$P/$n" && echo "removed $P/$n" || { echo "FAILED remove $P/$n"; rc=1; }; done\n'
            "exit $rc; }")

    def gate_pass(self, rc: int) -> bool:
        return rc in (0, 5)

    def collect_cmd(self, W: str) -> str:
        return f"{self._NO_PYTEST_ENV}python -m pytest --co -q --continue-on-collection-errors 2>&1 || true"

    def run_cmd(self, W: str, junit_out: str) -> str:
        return (f'F=""; [ -f {W}/logs/.bench_timeout_ok ] && F="{self._TIMEOUT_FLAGS}"; '
                f"{self._NO_PYTEST_ENV}python -m pytest -q --continue-on-collection-errors --junit-xml={junit_out} $F || true")

    def junit_glob(self, W: str) -> str:
        return f"{W}/logs/junit.xml"

    def pkg_count_cmd(self, W: str) -> str:
        return "python -m pip list --format=freeze 2>/dev/null | wc -l"
