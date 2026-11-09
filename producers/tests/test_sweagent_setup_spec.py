# --setup-spec: template substitution, deployment overrides, in-session collect. No sweagent import.
#
# The collect_script tests moved out with the function: the grade step mirrors Graph2Env's
# evaluator, so it is rendered there (Graph2Env/src/ReAct_Version/swe_collect.py, covered by
# Graph2Env/tests/test_swe_collect.py) and arrives here as spec["collect_script"]. What stays
# below is this runner's own half of that contract: the substitution it performs and the
# exit-code marker it parses back.
import copy

from producers.sweagent_repo2run_runner import apply_setup_spec, parse_collect_rc

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
