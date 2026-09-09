# --setup-spec: template substitution, deployment overrides, in-session collect. No sweagent import.
import copy

from producers.sweagent_repo2run_runner import apply_setup_spec, collect_script, parse_collect_rc

_CFG = {
    "agent": {
        "model": {"name": "x", "temperature": 0.2, "per_instance_cost_limit": 2.0},
        "templates": {
            "system_template": "sys",
            "instance_template": "task\n{{context_block}}\n(base {{base_image}})\nbash-$",
            "next_step_template": "{{observation}}",
        },
    },
    "env": {"deployment": {"type": "docker", "image": "python:3.10"}, "repo": {"type": "local", "path": "r"}},
}
_SPEC = {
    "context_block": "\nADDITIONAL CONTEXT. hello\n",
    "base_image": "python:3.11-slim",
    "deploy_image": "g2e-deploy-python-3.11-slim",
    "docker_args": ["-v", "jayint_pip_cache:/root/.cache/pip"],
    "model": {"name": "openai/m", "api_base": "https://b", "api_key": "$K", "temperature": 0.2,
              "per_instance_cost_limit": 0, "total_cost_limit": 0, "per_instance_call_limit": 30},
}


def test_apply_setup_spec_substitutes_placeholders_and_overrides_deployment_and_model():
    before = copy.deepcopy(_CFG)
    cfg = apply_setup_spec(_CFG, _SPEC)
    assert _CFG == before, "must not mutate its input"
    t = cfg["agent"]["templates"]["instance_template"]
    assert "{{context_block}}" not in t and "ADDITIONAL CONTEXT. hello" in t
    assert "(base python:3.11-slim)" in t
    assert cfg["env"]["deployment"]["image"] == "g2e-deploy-python-3.11-slim"
    assert cfg["env"]["deployment"]["docker_args"] == ["-v", "jayint_pip_cache:/root/.cache/pip"]
    assert cfg["agent"]["model"]["name"] == "openai/m"
    assert cfg["agent"]["model"]["per_instance_call_limit"] == 30
    assert cfg["agent"]["model"]["per_instance_cost_limit"] == 0


def test_apply_setup_spec_with_empty_block_leaves_only_a_blank_line():
    cfg = apply_setup_spec(_CFG, {**_SPEC, "context_block": ""})
    assert "task\n\n(base python:3.11-slim)" in cfg["agent"]["templates"]["instance_template"]


def test_collect_script_exports_env_starts_services_and_marks_rc():
    handoff = {"version": 1,
               "services": [{"kind": "redis", "start": "redis-server --daemonize yes", "check": "redis-cli ping"}],
               "environment": {"DJANGO_SETTINGS_MODULE": "app.settings", "PATH": "/repo/.venv/bin:/usr/bin"},
               "capabilities": []}
    script = collect_script(handoff, "pytest --collect-only -q --disable-warnings")
    assert script.startswith("cd /repo; ")
    assert "export DJANGO_SETTINGS_MODULE=app.settings; " in script
    assert "export PATH=/repo/.venv/bin:/usr/bin; " in script
    assert "redis-server --daemonize yes; " in script
    assert "for _i in $(seq 1 30); do (redis-cli ping) && break; sleep 1; done; " in script
    assert script.endswith("pytest --collect-only -q --disable-warnings > /tmp/g2e_collect.txt 2>&1; echo __G2E_RC=$?")


def test_collect_script_bootstraps_pytest_and_the_repo_venv_like_the_evaluator():
    # The prompt promises the agent that the evaluator installs pytest and puts /repo/.venv/bin
    # first on PATH before collecting. Without mirroring that, a setup.sh which correctly relies
    # on the promise is graded 127 (pytest: command not found).
    script = collect_script({"version": 1, "services": [], "environment": {}, "capabilities": []}, "pytest -q")
    assert script.startswith("cd /repo; ")
    assert "/repo/.venv/bin/python -m pip install -q pytest" in script
    assert "uv pip install --python /repo/.venv/bin/python pytest" in script
    assert "python3 -m pip install -q --break-system-packages pytest" in script
    assert "export PATH=/repo/.venv/bin:$PATH" in script
    assert script.endswith("pytest -q > /tmp/g2e_collect.txt 2>&1; echo __G2E_RC=$?")


def test_collect_script_applies_the_handoff_after_the_venv_path_so_the_handoff_wins():
    # Evaluator order: image ENV PATH is baked in, then `docker run --env` per handoff entry.
    handoff = {"version": 1, "services": [], "environment": {"PATH": "/custom/bin"}, "capabilities": []}
    script = collect_script(handoff, "pytest -q")
    assert script.index("export PATH=/repo/.venv/bin:$PATH") < script.index("export PATH=/custom/bin")


def test_collect_script_installs_pytest_before_the_test_runs():
    script = collect_script({"version": 1, "services": [], "environment": {}, "capabilities": []}, "pytest -q")
    assert script.index("pip install") < script.index("> /tmp/g2e_collect.txt")


def test_parse_collect_rc():
    assert parse_collect_rc("blah\n__G2E_RC=0\n") == 0
    assert parse_collect_rc("__G2E_RC=5") == 5
    assert parse_collect_rc("no marker") is None


# --- swe-rex request timeout -------------------------------------------------------------
# aiohttp's default ClientTimeout(total=300) caps every swe-rex run_in_session request.
# RemoteRuntime._request passes no timeout, so a command running past 300 s dies as
# asyncio.TimeoutError and takes the whole session down as exit_error — regardless of
# tools.execution_timeout. Observed on two smoke runs after execution_timeout was raised to 600.
from producers.sweagent_repo2run_runner import (  # noqa: E402
    patch_swerex_request_timeout,
    swerex_request_timeout,
)


def test_swerex_request_timeout_tracks_the_tool_ceiling_with_headroom():
    cfg = {"agent": {"tools": {"execution_timeout": 600}}}
    assert swerex_request_timeout(cfg) == 720.0


def test_swerex_request_timeout_falls_back_to_the_sweagent_default():
    assert swerex_request_timeout({}) == 150.0


def test_patch_raises_the_aiohttp_default_and_is_idempotent():
    import aiohttp

    original = aiohttp.client.DEFAULT_TIMEOUT
    try:
        assert patch_swerex_request_timeout(720.0, log=lambda *a: None) is True
        assert aiohttp.client.DEFAULT_TIMEOUT.total == 720.0
        # sock_connect must survive: dropping it would make a dead container hang for 720 s
        assert aiohttp.client.DEFAULT_TIMEOUT.sock_connect == original.sock_connect
        # already high enough -> no-op, so the 30 s Repo2Run arm is never lowered or re-wrapped
        assert patch_swerex_request_timeout(150.0, log=lambda *a: None) is False
        assert aiohttp.client.DEFAULT_TIMEOUT.total == 720.0
    finally:
        aiohttp.client.DEFAULT_TIMEOUT = original
