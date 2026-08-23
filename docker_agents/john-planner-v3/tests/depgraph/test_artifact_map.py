import json

from python_deps.depgraph.artifact_map import resolve_artifact_map
from python_deps.depgraph.executor import CommandResult


class FakeExec:
    """Substring-keyed fake (longest key wins); records calls."""

    def __init__(self, responses=None, default=None):
        self.responses = dict(responses or {})
        self.default = default
        self.calls = []

    def run(self, command, *, timeout=300):
        self.calls.append(command)
        matches = [k for k in self.responses if k in command]
        if matches:
            return self.responses[max(matches, key=len)]
        if self.default is not None:
            return self.default
        return CommandResult(command=command, returncode=127, stdout="", stderr="no fake")


def _res(stdout="", rc=0, stderr=""):
    return CommandResult(command="", returncode=rc, stdout=stdout, stderr=stderr)


_REPORT = json.dumps(
    {
        "version": "1",
        "install": [
            {
                "metadata": {"name": "requests", "version": "2.31.0"},
                "download_info": {
                    "url": "https://files.pythonhosted.org/x/requests-2.31.0-py3-none-any.whl"
                },
            },
            {
                "metadata": {"name": "psycopg2", "version": "2.9.9"},
                "download_info": {
                    "url": "https://files.pythonhosted.org/y/psycopg2-2.9.9.tar.gz"
                },
            },
        ],
    }
)


def test_primary_classifies_wheel_and_sdist():
    ex = FakeExec({"pip install --dry-run": _res(stdout=_REPORT)})
    out = resolve_artifact_map(["requests==2.31.0", "psycopg2==2.9.9"], ex)
    assert out == {"requests": "wheel", "psycopg2": "sdist"}


def test_empty_reqs_returns_empty():
    ex = FakeExec()
    assert resolve_artifact_map([], ex) == {}
    assert ex.calls == []  # never runs pip for an empty closure


def test_primary_failure_returns_empty_without_target_env():
    ex = FakeExec(default=_res(rc=1, stderr="pip: command not found"))
    assert resolve_artifact_map(["foo==1.0"], ex) == {}


def test_non_pypi_url_entry_is_skipped():
    report = json.dumps(
        {
            "install": [
                {"metadata": {"name": "localpkg"}, "download_info": {"dir_info": {}}},
                {
                    "metadata": {"name": "flask"},
                    "download_info": {
                        "url": "https://files.pythonhosted.org/z/flask-3.0-py3-none-any.whl"
                    },
                },
            ]
        }
    )
    ex = FakeExec({"pip install --dry-run": _res(stdout=report)})
    out = resolve_artifact_map(["localpkg", "flask"], ex)
    assert out == {"flask": "wheel"}  # dir_info/local entry left unclassified


def test_report_name_is_canonicalized():
    report = json.dumps(
        {
            "install": [
                {
                    "metadata": {"name": "Flask_SQLAlchemy"},
                    "download_info": {
                        "url": "https://x/Flask_SQLAlchemy-3.1-py3-none-any.whl"
                    },
                }
            ]
        }
    )
    ex = FakeExec({"pip install --dry-run": _res(stdout=report)})
    assert resolve_artifact_map(["Flask-SQLAlchemy"], ex) == {"flask-sqlalchemy": "wheel"}


def test_garbage_stdout_returns_empty():
    ex = FakeExec({"pip install --dry-run": _res(stdout="Collecting requests\n...noise")})
    assert resolve_artifact_map(["requests"], ex) == {}


def test_primary_command_gates_cat_on_pip_success_and_clears_stale_report():
    ex = FakeExec({"pip install --dry-run": _res(stdout=_REPORT)})
    resolve_artifact_map(["requests==2.31.0"], ex)
    cmd = next(c for c in ex.calls if "pip install --dry-run" in c)
    # cat the report ONLY if pip succeeded (else result.ok reflects a stale cat)
    assert "&& cat" in cmd
    # clear any stale report before the run so a failed pip cannot return old data
    assert "rm -f /tmp/depgraph-artifact-report.json" in cmd


from python_deps.depgraph.target_env import TargetEnv


def _target_env():
    return TargetEnv(
        python_full="3.11.0",
        python_version="3.11",
        platform_machine="x86_64",
        sys_platform="linux",
        os_name="posix",
        platform_system="Linux",
        python_platform_tag="x86_64-manylinux_2_28",
    )


def test_platform_fallback_wheel_when_download_ok():
    # primary unmatched -> {}; target_env present -> platform fallback runs.
    ex = FakeExec({"pip download": _res(rc=0, stdout="Saved foo-1.0-...whl")})
    out = resolve_artifact_map(["foo==1.0"], ex, target_env=_target_env())
    assert out == {"foo": "wheel"}


def test_platform_fallback_sdist_on_no_matching_distribution():
    ex = FakeExec(
        {"pip download": _res(rc=1, stderr="ERROR: No matching distribution found for foo")}
    )
    out = resolve_artifact_map(["foo==1.0"], ex, target_env=_target_env())
    assert out == {"foo": "sdist"}


def test_platform_fallback_skips_on_unrelated_error():
    ex = FakeExec({"pip download": _res(rc=1, stderr="Connection timed out")})
    out = resolve_artifact_map(["foo==1.0"], ex, target_env=_target_env())
    assert out == {}  # network error is NOT an sdist signal


def test_primary_nonempty_wins_over_fallback():
    ex = FakeExec(
        {
            "pip install --dry-run": _res(stdout=_REPORT),  # requests->wheel, psycopg2->sdist
            "pip download": _res(rc=0, stdout="whatever"),
        }
    )
    out = resolve_artifact_map(
        ["requests==2.31.0", "psycopg2==2.9.9"], ex, target_env=_target_env()
    )
    assert out == {"requests": "wheel", "psycopg2": "sdist"}
    assert not any("pip download" in c for c in ex.calls)  # fallback never consulted


def test_platform_fallback_passes_full_expanded_tag_set():
    ex = FakeExec({"pip download": _res(rc=0, stdout="ok")})
    resolve_artifact_map(["foo==1.0"], ex, target_env=_target_env())
    cmd = next(c for c in ex.calls if "pip download" in c)
    assert "--only-binary=:all:" in cmd
    assert "--platform manylinux_2_28_x86_64" in cmd
    assert "--platform manylinux2014_x86_64" in cmd  # legacy alias present
    assert "--platform linux_x86_64" in cmd
    assert "--abi cp311" in cmd
    assert "--abi abi3" in cmd
    assert "--python-version 3.11" in cmd
    assert "--implementation cp" in cmd


def test_pypi_fallback_wheel_when_compatible_wheel_listed():
    # primary + platform fallback both yield {} (127 default), pypi lists a
    # matching manylinux wheel.
    files = "foo-1.0-cp311-cp311-manylinux_2_17_x86_64.whl\nfoo-1.0.tar.gz"
    ex = FakeExec({"pypi.org": _res(stdout=files)})
    out = resolve_artifact_map(["foo==1.0"], ex, target_env=_target_env())
    assert out == {"foo": "wheel"}


def test_pypi_fallback_sdist_when_only_sdist_listed():
    ex = FakeExec({"pypi.org": _res(stdout="foo-1.0.tar.gz")})
    out = resolve_artifact_map(["foo==1.0"], ex, target_env=_target_env())
    assert out == {"foo": "sdist"}


def test_pypi_fallback_non_linux_wheel_is_sdist_for_target():
    files = "foo-1.0-cp311-cp311-macosx_11_0_arm64.whl\nfoo-1.0.zip"
    ex = FakeExec({"pypi.org": _res(stdout=files)})
    out = resolve_artifact_map(["foo==1.0"], ex, target_env=_target_env())
    assert out == {"foo": "sdist"}  # macos wheel does not match a linux target


def test_pypi_fallback_targets_version_endpoint():
    ex = FakeExec({"pypi.org": _res(stdout="foo-1.0.tar.gz")})
    resolve_artifact_map(["foo==1.0"], ex, target_env=_target_env())
    cmd = next(c for c in ex.calls if "pypi.org" in c)
    assert "foo/1.0/json" in cmd


def test_platform_fallback_still_wins_over_pypi():
    ex = FakeExec(
        {
            "pip download": _res(rc=0, stdout="ok"),
            "pypi.org": _res(stdout="foo-1.0.tar.gz"),
        }
    )
    out = resolve_artifact_map(["foo==1.0"], ex, target_env=_target_env())
    assert out == {"foo": "wheel"}  # platform fallback resolved -> pypi not reached
    assert not any("pypi.org" in c for c in ex.calls)


import logging


def test_primary_logs_tier_and_classification_counts(caplog):
    ex = FakeExec({"pip install --dry-run": _res(stdout=_REPORT)})
    with caplog.at_level(logging.INFO, logger="python_deps.depgraph.artifact_map"):
        resolve_artifact_map(["requests==2.31.0", "psycopg2==2.9.9", "extra==1.0"], ex)
    line = next(r.getMessage() for r in caplog.records if "artifact_map: tier=" in r.getMessage())
    assert "tier=primary" in line
    assert "wheel=1" in line and "sdist=1" in line and "unclassified=1" in line
