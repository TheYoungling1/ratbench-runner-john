"""The replay LADDER — the only new container logic in this eval. Runs a rendered
setup.sh in ONE fresh mounted container, then climbs
install ▸ env_works ▸ tests_ran ▸ tests_passed, recording how far it got. Reuses
coverage.py's container + classification primitives; adds only the pytest-run
rungs and optional network isolation for the test rung.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src"
for _p in (_REPO_ROOT, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from src.eval.build_script_eval.classify import (  # noqa: E402
    classify_tool_failures, merge_gaps, real_first_failure,
)
from src.eval.build_script_eval.scorecard import LadderResult, classify_pytest_result  # noqa: E402
from src.eval.language_package_eval.coverage import (  # noqa: E402
    _MountedContainer, _write_file, classify_execution_failures,
)

_PYTEST_ENV = "PYTEST_ADDOPTS='-p no:cacheprovider'"


def _disconnect_network_cmd(container_name: str) -> list[str]:
    """Detach the running replay container from the default bridge network before
    the pytest rung (so tests can't silently rely on the network)."""
    return ["docker", "network", "disconnect", "bridge", container_name]


def _fail(rung_reached: str, reason: str, output: str, *, install_ok: bool,
          collect_ok: bool | None = None) -> LadderResult:
    return LadderResult(
        install_ok=install_ok, env_works=False, collect_ok=collect_ok,
        tests_ran=False, tests_passed=False,
        highest_rung=rung_reached, reason=reason,
        first_failure=real_first_failure(output),
        gaps=merge_gaps(classify_execution_failures(output), classify_tool_failures(output)),
    )


def run_replay_ladder(
    repo_dir: str, image: str, setup_script: str, top_import: str | None,
    *, install_timeout: int = 1800, test_timeout: int = 600, isolate_network: bool = True,
) -> LadderResult:
    """Fresh -slim replay ladder. See module docstring for the rung meanings."""
    with _MountedContainer(image, str(Path(repo_dir).resolve())) as box:
        cd = f"cd {box.container_dir}"

        # RUNG 1 — install via setup.sh (from the repo root, mirrors install docs).
        _write_file(box, "/setup.sh", setup_script)
        install = box.run(f"{cd} && bash -x /setup.sh", timeout=install_timeout)
        if not install.ok:
            return _fail("none", "install_failed", install.stdout + install.stderr, install_ok=False)

        # RUNG 2 — env_works: the repo's top-level import is the HARD gate. Test
        # COLLECTION is a separate, more-demanding signal (collect_ok): a
        # pytest-version/config incompatibility (deprecation-as-error under
        # filterwarnings=error, a dropped `_pytest` internal) must NOT sink the
        # headline; only a real, classifier-detectable env gap surfaced during
        # collection does. Import + bootstrap run while the network is still up.
        if top_import:
            imp = box.run(f"{cd} && python3 -c 'import {top_import}'", timeout=120)
            if not imp.ok:
                return _fail("install", "env_broken", imp.stdout + imp.stderr, install_ok=True)

        # Probe-only bootstrap (NOT graph-attributed): pytest is the probe's own
        # tool. A bootstrap FAILURE is probe-infra, never a coverage gap -- its
        # output is never classified.
        bootstrap_ok = box.run("pip install --no-input --quiet pytest", timeout=300).ok

        if bootstrap_ok:
            collected = box.run(f"{cd} && python3 -m pytest --collect-only -q", timeout=600)
            if not collected.ok:
                collect_out = collected.stdout + collected.stderr
                collect_gaps = merge_gaps(
                    classify_execution_failures(collect_out), classify_tool_failures(collect_out),
                )
                if collect_gaps:
                    # a real missing need surfaced during collection -> env gap.
                    return _fail("install", "env_broken", collect_out,
                                 install_ok=True, collect_ok=False)
                # framework/config incompatibility -> env works, suite uncollectable.
                return LadderResult(
                    install_ok=True, env_works=True, collect_ok=False,
                    tests_ran=False, tests_passed=False,
                    highest_rung="env_works", reason="collect_incompatible",
                    first_failure=real_first_failure(collect_out), gaps=(),
                )

        # env_works has now passed (installed clean AND the repo imports, and if
        # pytest bootstrapped, tests COLLECTED clean). If pytest could not be
        # bootstrapped we cannot run the suite -- record a non-gap miss and stop.
        # EXCEPT: with no top_import AND no collect, NOTHING was verified, so
        # env_works=True would be vacuous.
        if not bootstrap_ok:
            if top_import is None:
                return LadderResult(
                    install_ok=True, env_works=False, collect_ok=None,
                    tests_ran=False, tests_passed=False,
                    highest_rung="install", reason="unverified_no_import_no_collect",
                    first_failure=None, gaps=(),
                )
            return LadderResult(
                install_ok=True, env_works=True, collect_ok=None,
                tests_ran=False, tests_passed=False,
                highest_rung="env_works", reason="pytest_unavailable",
                first_failure=None, gaps=(),
            )

        # RUNG 3/4 — actually run the suite (bounded; network optionally cut).
        if isolate_network:
            subprocess.run(_disconnect_network_cmd(box.name), capture_output=True, text=True, timeout=60)
        run = box.run(f"{cd} && {_PYTEST_ENV} python3 -m pytest -q", timeout=test_timeout)
        tests_ran, tests_passed, reason = classify_pytest_result(run.returncode)
        highest = "tests_passed" if tests_passed else ("tests_ran" if tests_ran else "env_works")
        return LadderResult(
            install_ok=True, env_works=True, collect_ok=True,
            tests_ran=tests_ran, tests_passed=tests_passed,
            highest_rung=highest, reason=reason,
            first_failure=None if tests_passed else real_first_failure(run.stdout + run.stderr),
            gaps=() if tests_ran else merge_gaps(
                classify_execution_failures(run.stdout + run.stderr),
                classify_tool_failures(run.stdout + run.stderr),
            ),
        )
