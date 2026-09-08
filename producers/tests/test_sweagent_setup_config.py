# The setup-deliverable config: SWE-agent's own default.yaml tools/parser/history, the
# Repo2Run templates rewritten for /g2e/setup.sh (spec 2026-09-08-fs-arm-swe-runner-design).
import os

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(os.path.dirname(HERE), "sweagent_repo2run_setup_config.yaml")


def _cfg():
    with open(CONFIG) as fh:
        return yaml.safe_load(fh)


def test_uses_sweagent_default_tools_parser_and_history():
    cfg = _cfg()
    tools = cfg["agent"]["tools"]
    assert [b["path"] for b in tools["bundles"]] == [
        "tools/registry", "tools/edit_anthropic", "tools/review_on_submit_m"]
    assert tools["parse_function"]["type"] == "function_calling"
    assert tools["enable_bash_tool"] is True
    assert cfg["agent"]["history_processors"] == [{"type": "cache_control", "last_n_messages": 2}]


def test_templates_carry_the_placeholders_and_the_setup_deliverable():
    t = _cfg()["agent"]["templates"]
    assert "{{context_block}}" in t["instance_template"]
    assert "{{base_image}}" in t["instance_template"]
    assert "/g2e/setup.sh" in t["instance_template"]
    assert "/g2e/runtime_handoff.json" in t["instance_template"]
    assert "Dockerfile" not in t["instance_template"]
    assert t["instance_template"].rstrip().endswith("bash-$")
    assert "/g2e/setup.sh" in t["problem_statement_template"]


def test_system_template_dropped_window_and_fenced_response_format():
    s = _cfg()["agent"]["templates"]["system_template"]
    assert "{{WINDOW}}" not in s
    assert "DISCUSSION" not in s
    assert "{{command_docs}}" in s


def test_model_keeps_the_variety_settings():
    m = _cfg()["agent"]["model"]
    assert m["temperature"] == 0.2
    assert m["completion_kwargs"]["extra_body"]["thinking"] == {"type": "disabled"}
