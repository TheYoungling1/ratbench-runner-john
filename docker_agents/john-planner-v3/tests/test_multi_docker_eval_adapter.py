"""Offline contract tests for the run_v3 -> RAT-harness adapter.

The three side-effecting steps (_clone, _head_sha, _run_v3) are mocked, so these
run with no Docker, network, or LLM — they pin the harness contract:
``process_single_instance`` returns ``{instance_id: {dockerfile, setup_scripts,
base_image, logs}}`` where ``dockerfile`` is a self-contained image spec that
clones the repo into /testbed and runs the certified setup.sh.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from multi_docker_eval_adapter import MultiDockerEvalAdapter


def _adapter(tmp_path, *, setup_text="cd /app && pip install -e .", base="python:3.12-slim"):
    a = MultiDockerEvalAdapter(output_dir=str(tmp_path))
    a._clone = lambda repo_url: tmp_path / "v3_src"          # type: ignore[assignment]
    a._head_sha = lambda src_dir: "deadbeef"                 # type: ignore[assignment]
    a._run_v3 = lambda src_dir, base_image, model: (setup_text, base)  # type: ignore[assignment]
    return a


def test_dockerfile_contract(tmp_path):
    a = _adapter(tmp_path)
    res = a.process_single_instance(
        {"instance_id": "anthropics__anthropic-sdk-python",
         "repo_url": "https://github.com/anthropics/anthropic-sdk-python"},
        base_image="auto", model="deepseek/deepseek-v4-flash",
    )
    # harness does res.get(instance_id, res) -> the result dict
    r = res["anthropics__anthropic-sdk-python"]
    assert r["base_image"] == "python:3.12-slim"
    df = r["dockerfile"]
    assert df.startswith("FROM python:3.12-slim")
    assert "git clone --depth=1 https://github.com/anthropics/anthropic-sdk-python /testbed" in df
    assert "COPY setup.sh /tmp/v3_setup.sh" in df
    assert "RUN bash /tmp/v3_setup.sh" in df
    # setup.sh is surfaced verbatim (harness writes it into the build context)...
    assert "setup.sh" in r["setup_scripts"]
    # ...with /app normalized to /testbed so the editable install resolves.
    assert r["setup_scripts"]["setup.sh"] == "cd /testbed && pip install -e ."
    assert r["logs"]["head_sha"] == "deadbeef"


def test_missing_repo_url_is_clean_failure(tmp_path):
    a = MultiDockerEvalAdapter(output_dir=str(tmp_path))
    res = a.process_single_instance({"instance_id": "x__y"})  # no repo_url
    r = res["x__y"]
    assert r["dockerfile"] is None            # -> harness reports no_dockerfile, not a crash
    assert "repo_url" in r["logs"]["error"]


def test_run_v3_failure_yields_error_log_not_crash(tmp_path):
    a = MultiDockerEvalAdapter(output_dir=str(tmp_path))
    a._clone = lambda repo_url: tmp_path / "v3_src"          # type: ignore[assignment]
    a._head_sha = lambda src_dir: "abc"                      # type: ignore[assignment]

    def _boom(src_dir, base_image, model):
        raise RuntimeError("run_v3_e2e produced no setup.sh")

    a._run_v3 = _boom                                        # type: ignore[assignment]
    res = a.process_single_instance(
        {"instance_id": "o__r", "repo_url": "https://github.com/o/r"})
    r = res["o__r"]
    assert r["dockerfile"] is None
    assert "no setup.sh" in r["logs"]["error"]


def test_base_image_fallback_when_unparsed(tmp_path):
    # _run_v3 returning an explicit base flows straight to FROM.
    a = _adapter(tmp_path, base="python:3.10-slim")
    r = a.process_single_instance(
        {"instance_id": "o__r", "repo_url": "https://github.com/o/r"})["o__r"]
    assert r["dockerfile"].startswith("FROM python:3.10-slim")


def _capture_run_v3_cmd(tmp_path, monkeypatch):
    """Drive the REAL _run_v3 with subprocess mocked so we can inspect the argv
    it builds (and confirm the LLM model is still passed)."""
    import multi_docker_eval_adapter as M

    class _Proc:
        returncode = 0
        stdout = "[v3] base-image: python:3.11-slim (py 3.11) — auto"
        stderr = ""

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        out = cmd[cmd.index("--out") + 1]  # write the artifact the adapter validates
        Path(out).write_text("echo built")
        return _Proc()

    monkeypatch.setattr(M.subprocess, "run", fake_run)
    a = M.MultiDockerEvalAdapter(output_dir=str(tmp_path))
    setup, base = a._run_v3(tmp_path / "src", "auto", "deepseek/deepseek-v4-flash")
    assert base == "python:3.11-slim"
    return captured["cmd"]


def test_construction_only_env_adds_flag_keeps_model(tmp_path, monkeypatch):
    monkeypatch.setenv("V3_CONSTRUCTION_ONLY", "1")
    cmd = _capture_run_v3_cmd(tmp_path, monkeypatch)
    assert "--construction-only" in cmd, "V3_CONSTRUCTION_ONLY=1 must pass the flag"
    # LLM stays ON in construction-only mode (base-image + service classify use it).
    assert "--model" in cmd and "deepseek/deepseek-v4-flash" in cmd


def test_construction_only_absent_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("V3_CONSTRUCTION_ONLY", raising=False)
    cmd = _capture_run_v3_cmd(tmp_path, monkeypatch)
    assert "--construction-only" not in cmd, "default must run the full repair loop"
