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


def test_collect_script_with_bare_handoff_is_just_the_test():
    script = collect_script({"version": 1, "services": [], "environment": {}, "capabilities": []}, "pytest -q")
    assert script == "cd /repo; pytest -q > /tmp/g2e_collect.txt 2>&1; echo __G2E_RC=$?"


def test_parse_collect_rc():
    assert parse_collect_rc("blah\n__G2E_RC=0\n") == 0
    assert parse_collect_rc("__G2E_RC=5") == 5
    assert parse_collect_rc("no marker") is None
